// Sync endpoints. Service-token authed; no session, no user. Every
// route is idempotent — desktop may retry any request. Requests that
// carry `is_private = true` for a photo are rejected with 400 (the
// desktop must never send private rows). The web side never trusts a
// synced value into a fact column — the desktop is authoritative for
// EXIF, capture-date confirmations, face embeddings, etc. Contributor
// suggestions and moderator/admin decisions taken on the web are
// pulled back by /sync/pull/confirmed.
//
// Structure:
//   POST /sync/photos                  batch (≤200) upsert
//   PUT  /sync/photos/:id/file         working copy bytes + auto-thumb + records synced_file_version
//   PUT  /sync/photo_backs/:id/file    back-image bytes
//   PUT  /sync/faces/:id/crop          face crop bytes
//
//   POST /sync/photo_masters           batch metadata only
//   POST /sync/people                  batch upsert
//   POST /sync/person_name_variants    batch upsert
//   POST /sync/relationships           batch upsert
//   POST /sync/places                  batch upsert
//   POST /sync/photo_places            batch upsert
//   POST /sync/albums                  batch upsert
//   POST /sync/album_photos            batch upsert
//   POST /sync/faces                   batch upsert (embeddings only if included)
//   POST /sync/photo_backs             batch upsert (metadata + transcription)
//   POST /sync/suggestions             batch upsert (AI + import)
//
//   POST /sync/photo_groups            batch upsert (soft-delete rows sync as-is)
//   GET  /sync/pull/groups             groups + memberships + photo_groups since cursor
//   GET  /sync/pull/confirmed          accepted-suggestion facts + updates since cursor
//   GET  /sync/pull/contributions      status=approved, pulled=false
//   GET  /sync/pull/contributions/:id/files/:file_id
//   POST /sync/pull/contributions/:id/pulled
//   GET  /sync/status
//
// Batch cap: 500 rows per metadata batch (200 for /sync/photos to keep
// the "which need files" hop responsive).

const express = require('express');
const multer = require('multer');
const { requireService } = require('../middleware/require-service');
const { audit } = require('../services/audit');
const storage = require('../services/photo-storage');

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 200 * 1024 * 1024 }, // 200 MB per file
});

const PHOTO_BATCH = 200;
const META_BATCH  = 500;

function toArr(x) { return Array.isArray(x) ? x : []; }
function bad(res, msg, code = 400, extra) { return res.status(code).json({ error: msg, ...(extra || {}) }); }

async function auditDesktop(client, action, entityType, entityId, newValue) {
  await audit(client, { actor: 'desktop', action, entityType, entityId, userId: null, newValue });
}

module.exports = function syncRoutes({ pool }) {
  const router = express.Router();
  router.use(requireService);
  router.use(express.json({ limit: '20mb' }));

  // ------------------------------------------------------------------
  // POST /sync/photos — batch photo metadata upsert.
  // Body: { photos: [ { id, sha256, ..., is_private, ... } ] }
  // Rejects with 400 on any is_private = true.
  // Returns { need_files: [ids] } — ids whose file_version > stored
  // synced_file_version or whose file is missing on disk.
  // ------------------------------------------------------------------
  router.post('/photos', async (req, res, next) => {
    const rows = toArr(req.body && req.body.photos);
    if (rows.length === 0) return res.json({ upserted: 0, need_files: [] });
    if (rows.length > PHOTO_BATCH) return bad(res, `too many photos (max ${PHOTO_BATCH})`);
    for (const r of rows) {
      if (r.is_private === true || r.is_private === 'true') {
        return bad(res, 'private photo rejected', 400, { id: r.id });
      }
    }
    const client = await pool.connect();
    try {
      await client.query('begin');
      const need = [];
      for (const r of rows) {
        const id = Number(r.id);
        if (!id) continue;
        // Upsert by id (desktop is authoritative for ids).
        await client.query(
          `insert into photos
             (id, sha256, phash, dhash, width, height, mime, file_size,
              is_scan, has_no_people,
              capture_date, capture_date_precision, capture_date_confirmed,
              exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon,
              source_root, source_folder, source_filename, scan_batch, scan_sequence,
              physical_ref_note, rescan_wanted, triage_status, is_private, is_deleted, deleted_at,
              orientation, description_ai, completeness_score, file_version,
              working_path, synced_at, updated_at, created_at)
           values
             ($1,$2,$3,$4,$5,$6,$7,$8,
              $9,$10,
              $11,$12,$13,
              $14,$15,$16,$17,
              $18,$19,$20,$21,$22,
              $23,$24,$25,$26,$27,$28,
              $29,$30,$31,$32,
              $33, now(), now(), coalesce($34::timestamptz, now()))
           on conflict (id) do update set
              sha256 = excluded.sha256,
              phash = excluded.phash,
              dhash = excluded.dhash,
              width = excluded.width,
              height = excluded.height,
              mime = excluded.mime,
              file_size = excluded.file_size,
              is_scan = excluded.is_scan,
              has_no_people = excluded.has_no_people,
              capture_date = excluded.capture_date,
              capture_date_precision = excluded.capture_date_precision,
              capture_date_confirmed = excluded.capture_date_confirmed,
              exif_taken_at = excluded.exif_taken_at,
              exif_camera = excluded.exif_camera,
              exif_gps_lat = excluded.exif_gps_lat,
              exif_gps_lon = excluded.exif_gps_lon,
              source_root = excluded.source_root,
              source_folder = excluded.source_folder,
              source_filename = excluded.source_filename,
              scan_batch = excluded.scan_batch,
              scan_sequence = excluded.scan_sequence,
              physical_ref_note = excluded.physical_ref_note,
              rescan_wanted = excluded.rescan_wanted,
              triage_status = excluded.triage_status,
              is_private = excluded.is_private,
              is_deleted = excluded.is_deleted,
              deleted_at = excluded.deleted_at,
              orientation = excluded.orientation,
              description_ai = excluded.description_ai,
              completeness_score = excluded.completeness_score,
              file_version = excluded.file_version,
              working_path = excluded.working_path,
              synced_at = now(),
              updated_at = now()
          `,
          [
            id, r.sha256, r.phash, r.dhash, r.width, r.height, r.mime, r.file_size,
            !!r.is_scan, !!r.has_no_people,
            r.capture_date, r.capture_date_precision || 'unknown', !!r.capture_date_confirmed,
            r.exif_taken_at, r.exif_camera, r.exif_gps_lat, r.exif_gps_lon,
            r.source_root, r.source_folder, r.source_filename, r.scan_batch, r.scan_sequence,
            r.physical_ref_note, !!r.rescan_wanted, r.triage_status || 'untriaged', false, !!r.is_deleted, r.deleted_at,
            r.orientation, r.description_ai, r.completeness_score || 0, r.file_version || 1,
            r.working_path || null, r.updated_at,
          ],
        );
        // Determine whether the file needs uploading. Missing on disk OR
        // synced_file_version is older than file_version.
        const check = (await client.query(
          `select synced_file_version, file_version, working_path from photos where id = $1`, [id],
        )).rows[0];
        if (!check) continue;
        const base = check.working_path ? require('path').basename(check.working_path) : storage.workingBasename(id, r.sha256, r.mime);
        const abs = require('path').join(storage.root(), 'working', base);
        const versionOk = check.synced_file_version != null
          && Number(check.synced_file_version) >= Number(check.file_version);
        if (!versionOk || !storage.fileExists(abs)) need.push(id);
      }
      await auditDesktop(client, 'sync.photos.upsert', 'photos', null, { count: rows.length });
      await client.query('commit');
      res.json({ upserted: rows.length, need_files: need });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // ------------------------------------------------------------------
  // PUT /sync/photos/:id/file — multipart, single field named "file".
  // Writes working/<basename>, generates thumb, records synced_file_version.
  // ------------------------------------------------------------------
  router.put('/photos/:id(\\d+)/file', upload.single('file'), async (req, res, next) => {
    const id = Number(req.params.id);
    if (!req.file) return bad(res, 'need file field (multipart)');
    const client = await pool.connect();
    try {
      const photo = (await client.query(
        `select id, sha256, mime, file_version, is_private, is_deleted from photos where id = $1`, [id],
      )).rows[0];
      if (!photo) return bad(res, 'photo not found', 404);
      if (photo.is_private) return bad(res, 'private photo cannot receive a file');
      await storage.ensureDirs();
      const { basename } = await storage.writeWorking(id, photo.sha256, photo.mime, req.file.buffer);
      await storage.generateThumbFromWorking(id, req.file.buffer);
      await client.query('begin');
      await client.query(
        `update photos
            set working_path = $1,
                synced_file_version = $2,
                synced_at = now()
          where id = $3`,
        [basename, photo.file_version, id],
      );
      await auditDesktop(client, 'sync.file.upload', 'photo', id, {
        basename, file_version: photo.file_version,
      });
      await client.query('commit');
      res.json({ ok: true, id, basename, file_version: photo.file_version });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // PUT /sync/photo_backs/:id/file
  router.put('/photo_backs/:id(\\d+)/file', upload.single('file'), async (req, res, next) => {
    const id = Number(req.params.id);
    if (!req.file) return bad(res, 'need file field (multipart)');
    const client = await pool.connect();
    try {
      const row = (await client.query(
        `select pb.id, pb.sha256, pb.photo_id, p.is_private
           from photo_backs pb
           left join photos p on p.id = pb.photo_id
          where pb.id = $1`, [id],
      )).rows[0];
      if (!row) return bad(res, 'not found', 404);
      if (row.is_private) return bad(res, 'private photo back cannot receive a file');
      await storage.ensureDirs();
      const { basename } = await storage.writeBack(id, row.sha256, 'image/jpeg', req.file.buffer);
      await client.query('begin');
      await client.query(`update photo_backs set working_path = $1 where id = $2`, [basename, id]);
      await auditDesktop(client, 'sync.back.upload', 'photo_back', id, { basename });
      await client.query('commit');
      res.json({ ok: true, id, basename });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.put('/faces/:id(\\d+)/crop', upload.single('file'), async (req, res, next) => {
    const id = Number(req.params.id);
    if (!req.file) return bad(res, 'need file field (multipart)');
    const client = await pool.connect();
    try {
      const row = (await client.query(
        `select f.id, p.is_private
           from faces f join photos p on p.id = f.photo_id
          where f.id = $1 and f.is_deleted = false`, [id],
      )).rows[0];
      if (!row) return bad(res, 'not found', 404);
      if (row.is_private) return bad(res, 'private photo face cannot receive a crop');
      await storage.ensureDirs();
      await storage.writeFaceCrop(id, req.file.buffer);
      await client.query('begin');
      await auditDesktop(client, 'sync.face.crop.upload', 'face', id, {});
      await client.query('commit');
      res.json({ ok: true, id });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // ------------------------------------------------------------------
  // Metadata batch upserts. Each accepts { items: [...] }, batch cap 500.
  // ------------------------------------------------------------------

  function batchUpsert(name, sql, marshal) {
    return async (req, res, next) => {
      const rows = toArr(req.body && req.body.items);
      if (rows.length > META_BATCH) return bad(res, `too many items (max ${META_BATCH})`);
      if (rows.length === 0) return res.json({ upserted: 0 });
      const client = await pool.connect();
      try {
        await client.query('begin');
        let count = 0;
        for (const r of rows) {
          const params = marshal(r);
          if (!params) continue;
          await client.query(sql, params);
          count += 1;
        }
        await auditDesktop(client, `sync.${name}.upsert`, name, null, { count });
        await client.query('commit');
        res.json({ upserted: count });
      } catch (err) {
        await client.query('rollback').catch(() => {});
        next(err);
      } finally {
        client.release();
      }
    };
  }

  router.post('/photo_masters', batchUpsert('photo_masters',
    `insert into photo_masters
       (id, photo_id, master_path, sha256, width, height, dpi, mime, file_size, is_preferred, ingested_at, updated_at)
     values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,coalesce($11::timestamptz, now()), now())
     on conflict (id) do update set
       photo_id = excluded.photo_id,
       master_path = excluded.master_path,
       sha256 = excluded.sha256,
       width = excluded.width,
       height = excluded.height,
       dpi = excluded.dpi,
       mime = excluded.mime,
       file_size = excluded.file_size,
       is_preferred = excluded.is_preferred,
       updated_at = now()`,
    (r) => {
      const id = Number(r.id);
      if (!id) return null;
      return [id, r.photo_id, r.master_path, r.sha256, r.width, r.height, r.dpi, r.mime, r.file_size, !!r.is_preferred, r.ingested_at];
    },
  ));

  router.post('/people', batchUpsert('people',
    `insert into people (id, given_name, middle_name, surname, maiden_name, nickname, suffix,
                         birth_year, death_year, notes, is_deleted, updated_at, created_at)
     values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),coalesce($12::timestamptz, now()))
     on conflict (id) do update set
       given_name = excluded.given_name,
       middle_name = excluded.middle_name,
       surname = excluded.surname,
       maiden_name = excluded.maiden_name,
       nickname = excluded.nickname,
       suffix = excluded.suffix,
       birth_year = excluded.birth_year,
       death_year = excluded.death_year,
       notes = excluded.notes,
       is_deleted = excluded.is_deleted,
       updated_at = now()`,
    (r) => {
      const id = Number(r.id);
      if (!id) return null;
      return [id, r.given_name, r.middle_name, r.surname, r.maiden_name, r.nickname, r.suffix,
              r.birth_year, r.death_year, r.notes, !!r.is_deleted, r.created_at];
    },
  ));

  router.post('/person_name_variants', batchUpsert('person_name_variants',
    `insert into person_name_variants (id, person_id, variant, kind, created_at)
     values ($1,$2,$3,$4,coalesce($5::timestamptz, now()))
     on conflict (id) do update set
       variant = excluded.variant, kind = excluded.kind`,
    (r) => [Number(r.id), r.person_id, r.variant, r.kind, r.created_at],
  ));

  router.post('/relationships', batchUpsert('relationships',
    `insert into relationships (id, person_a_id, person_b_id, type, confirmed, created_by, created_at, updated_at)
     values ($1,$2,$3,$4,$5,$6,coalesce($7::timestamptz, now()), now())
     on conflict (id) do update set
       person_a_id = excluded.person_a_id,
       person_b_id = excluded.person_b_id,
       type = excluded.type,
       confirmed = excluded.confirmed,
       updated_at = now()`,
    (r) => [Number(r.id), r.person_a_id, r.person_b_id, r.type, !!r.confirmed, r.created_by, r.created_at],
  ));

  router.post('/places', batchUpsert('places',
    `insert into places (id, name, latitude, longitude, notes, is_deleted, updated_at, created_at)
     values ($1,$2,$3,$4,$5,$6, now(), coalesce($7::timestamptz, now()))
     on conflict (id) do update set
       name = excluded.name,
       latitude = excluded.latitude,
       longitude = excluded.longitude,
       notes = excluded.notes,
       is_deleted = excluded.is_deleted,
       updated_at = now()`,
    (r) => [Number(r.id), r.name, r.latitude, r.longitude, r.notes, !!r.is_deleted, r.created_at],
  ));

  router.post('/photo_places', batchUpsert('photo_places',
    `insert into photo_places (photo_id, place_id, confirmed)
     values ($1, $2, $3)
     on conflict (photo_id, place_id) do update set confirmed = excluded.confirmed`,
    (r) => [Number(r.photo_id), Number(r.place_id), !!r.confirmed],
  ));

  router.post('/albums', batchUpsert('albums',
    `insert into albums (id, name, description, source, created_by, is_deleted, updated_at, created_at)
     values ($1,$2,$3,$4,$5,$6,now(),coalesce($7::timestamptz, now()))
     on conflict (id) do update set
       name = excluded.name,
       description = excluded.description,
       source = excluded.source,
       is_deleted = excluded.is_deleted,
       updated_at = now()`,
    (r) => [Number(r.id), r.name, r.description, r.source || 'import', r.created_by, !!r.is_deleted, r.created_at],
  ));

  router.post('/album_photos', batchUpsert('album_photos',
    `insert into album_photos (album_id, photo_id, position)
     values ($1, $2, $3)
     on conflict (album_id, photo_id) do update set position = excluded.position`,
    (r) => [Number(r.album_id), Number(r.photo_id), r.position],
  ));

  // /sync/faces — the desktop is authoritative for the face row itself
  // (bbox / person_id / disputed / review_status / is_deleted / embedding).
  // Embeddings are optional; the desktop only sends them when
  // SYNC_FACE_EMBEDDINGS=true. Faces on private photos are excluded on
  // the desktop side; we skip them here too as a belt-and-braces check.
  router.post('/faces', async (req, res, next) => {
    const rows = toArr(req.body && req.body.items);
    if (rows.length > META_BATCH) return bad(res, `too many items (max ${META_BATCH})`);
    if (rows.length === 0) return res.json({ upserted: 0 });
    const client = await pool.connect();
    try {
      await client.query('begin');
      let count = 0;
      for (const r of rows) {
        const id = Number(r.id);
        if (!id) continue;
        // Refuse if the parent photo is is_private on the web.
        const parent = (await client.query(
          `select is_private, is_deleted from photos where id = $1`, [r.photo_id],
        )).rows[0];
        if (!parent || parent.is_private) continue;
        // Optional embedding.
        const embedding = Array.isArray(r.embedding) ? r.embedding : null;
        await client.query(
          `insert into faces
             (id, photo_id, person_id, bbox, embedding, embedding_model, embedding_stale,
              source, is_disputed, disputed_by, dispute_note, created_by,
              review_status, review_note, reviewed_at,
              is_deleted, deleted_at, delete_reason,
              updated_at, created_at)
           values
             ($1,$2,$3,$4::jsonb,$5,$6,$7,
              $8,$9,$10,$11,$12,
              $13,$14,$15,
              $16,$17,$18,
              now(), coalesce($19::timestamptz, now()))
           on conflict (id) do update set
              photo_id = excluded.photo_id,
              person_id = excluded.person_id,
              bbox = excluded.bbox,
              embedding = coalesce(excluded.embedding, faces.embedding),
              embedding_model = coalesce(excluded.embedding_model, faces.embedding_model),
              embedding_stale = excluded.embedding_stale,
              source = excluded.source,
              is_disputed = excluded.is_disputed,
              disputed_by = excluded.disputed_by,
              dispute_note = excluded.dispute_note,
              created_by = excluded.created_by,
              review_status = excluded.review_status,
              review_note = excluded.review_note,
              reviewed_at = excluded.reviewed_at,
              is_deleted = excluded.is_deleted,
              deleted_at = excluded.deleted_at,
              delete_reason = excluded.delete_reason,
              updated_at = now()`,
          [
            id, r.photo_id, r.person_id, r.bbox, embedding, r.embedding_model, !!r.embedding_stale,
            r.source || 'ai', !!r.is_disputed, r.disputed_by, r.dispute_note, r.created_by,
            r.review_status || 'pending', r.review_note, r.reviewed_at,
            !!r.is_deleted, r.deleted_at, r.delete_reason,
            r.created_at,
          ],
        );
        count += 1;
      }
      await auditDesktop(client, 'sync.faces.upsert', 'faces', null, { count });
      await client.query('commit');
      res.json({ upserted: count });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/photo_backs', batchUpsert('photo_backs',
    `insert into photo_backs
       (id, photo_id, master_path, sha256, working_path, source_folder, source_filename, scan_sequence,
        transcribed_text, transcription_confidence, transcription_confirmed,
        updated_at, created_at)
     values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),coalesce($12::timestamptz, now()))
     on conflict (id) do update set
       photo_id = excluded.photo_id,
       master_path = excluded.master_path,
       sha256 = excluded.sha256,
       working_path = excluded.working_path,
       source_folder = excluded.source_folder,
       source_filename = excluded.source_filename,
       scan_sequence = excluded.scan_sequence,
       transcribed_text = excluded.transcribed_text,
       transcription_confidence = excluded.transcription_confidence,
       transcription_confirmed = excluded.transcription_confirmed,
       updated_at = now()`,
    (r) => [Number(r.id), r.photo_id, r.master_path, r.sha256, r.working_path,
            r.source_folder, r.source_filename, r.scan_sequence,
            r.transcribed_text, r.transcription_confidence, !!r.transcription_confirmed, r.created_at],
  ));

  router.post('/suggestions', batchUpsert('suggestions',
    `insert into suggestions
       (id, photo_id, user_id, kind, payload, confidence, status, source, model,
        resolved_by, resolved_at, resolution_note, updated_at, created_at)
     values ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12, now(), coalesce($13::timestamptz, now()))
     on conflict (id) do update set
       photo_id = excluded.photo_id,
       user_id = excluded.user_id,
       kind = excluded.kind,
       payload = excluded.payload,
       confidence = excluded.confidence,
       status = excluded.status,
       source = excluded.source,
       model = excluded.model,
       resolved_by = excluded.resolved_by,
       resolved_at = excluded.resolved_at,
       resolution_note = excluded.resolution_note,
       updated_at = now()`,
    (r) => [Number(r.id), r.photo_id, r.user_id, r.kind, r.payload,
            r.confidence, r.status || 'pending', r.source || 'ai',
            r.model, r.resolved_by, r.resolved_at, r.resolution_note, r.created_at],
  ));

  // ------------------------------------------------------------------
  // /sync/photo_groups — both directions. is_deleted rows sync as-is.
  // ------------------------------------------------------------------
  router.post('/photo_groups', async (req, res, next) => {
    const rows = toArr(req.body && req.body.items);
    if (rows.length > META_BATCH) return bad(res, `too many items (max ${META_BATCH})`);
    if (rows.length === 0) return res.json({ upserted: 0 });
    const client = await pool.connect();
    try {
      await client.query('begin');
      let count = 0;
      for (const r of rows) {
        const p = Number(r.photo_id);
        const g = Number(r.group_id);
        if (!p || !g) continue;
        // Skip if the group or the photo doesn't exist on the web side.
        const ok = (await client.query(
          `select 1 from photos where id = $1 and is_private = false
           union all select 1 from groups where id = $2`, [p, g],
        )).rows.length >= 2;
        if (!ok) continue;
        // Last-writer-wins by updated_at: only accept the row if the
        // incoming updated_at is >= the stored updated_at.
        const stored = (await client.query(
          `select updated_at from photo_groups where photo_id = $1 and group_id = $2`, [p, g],
        )).rows[0];
        if (stored && r.updated_at && new Date(r.updated_at) < new Date(stored.updated_at)) continue;
        await client.query(
          `insert into photo_groups (photo_id, group_id, added_by, is_deleted, deleted_at, deleted_by, added_at, updated_at)
             values ($1,$2,$3,$4,$5,$6, coalesce($7::timestamptz, now()), now())
           on conflict (photo_id, group_id) do update set
             added_by = coalesce(excluded.added_by, photo_groups.added_by),
             is_deleted = excluded.is_deleted,
             deleted_at = excluded.deleted_at,
             deleted_by = excluded.deleted_by,
             updated_at = now()`,
          [p, g, r.added_by, !!r.is_deleted, r.deleted_at, r.deleted_by, r.added_at],
        );
        count += 1;
      }
      await auditDesktop(client, 'sync.photo_groups.upsert', 'photo_groups', null, { count });
      await client.query('commit');
      res.json({ upserted: count });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // /sync/pull/groups?since=<ts>  — groups + memberships + photo_groups
  // changed since `since`. `since` is a full ISO timestamp. Empty = all.
  router.get('/pull/groups', async (req, res, next) => {
    try {
      const since = req.query.since ? new Date(req.query.since) : new Date(0);
      if (isNaN(since.getTime())) return bad(res, 'bad since');
      const groups = (await pool.query(
        `select id, name, description, created_by, is_deleted, deleted_at, deleted_by,
                created_at, updated_at
           from groups where updated_at > $1 order by updated_at asc`, [since],
      )).rows;
      const members = (await pool.query(
        `select group_id, user_id, role, added_by, added_at, is_deleted, deleted_at, deleted_by, updated_at
           from group_members where updated_at > $1 order by updated_at asc`, [since],
      )).rows;
      const photoGroups = (await pool.query(
        `select photo_id, group_id, added_by, added_at, is_deleted, deleted_at, deleted_by, updated_at
           from photo_groups where updated_at > $1 order by updated_at asc`, [since],
      )).rows;
      const users = (await pool.query(
        `select id, email, display_name, role, status
           from users where updated_at > $1 order by updated_at asc`, [since],
      )).rows;
      res.json({
        cursor: new Date().toISOString(),
        groups, members, photo_groups: photoGroups, users,
      });
    } catch (err) { next(err); }
  });

  // /sync/pull/confirmed?since=<ts>
  //   accepted-suggestions + fact writes + comment counts + likes counts.
  router.get('/pull/confirmed', async (req, res, next) => {
    try {
      const since = req.query.since ? new Date(req.query.since) : new Date(0);
      if (isNaN(since.getTime())) return bad(res, 'bad since');

      const acceptedSug = (await pool.query(
        `select id, photo_id, kind, payload, resolved_by, resolved_at, resolution_note
           from suggestions
          where status = 'accepted' and resolved_at > $1
          order by resolved_at asc`, [since],
      )).rows;
      // Every fact-set audit row since `since` (photo.*, face.*, relationship.*, person.create).
      const factAudits = (await pool.query(
        `select id, action, entity_type, entity_id, user_id, actor, previous_value, new_value, created_at
           from audit_log
          where created_at > $1
            and action in (
              'photo.capture_date.set',
              'photo.description.set',
              'photo.place.set',
              'photo.has_no_people.set',
              'face.assign',
              'face.dispute.resolve',
              'relationship.confirm',
              'person.create',
              'suggestion.accept',
              'suggestion.reject'
            )
          order by id asc`, [since],
      )).rows;
      // Comments summary: {photo_id, comment_count, has_hidden}. Bodies
      // stay web-authoritative (answer #8).
      const commentSummary = (await pool.query(
        `select photo_id,
                count(*) filter (where not is_hidden)::int as comment_count,
                bool_or(is_hidden) as has_hidden,
                max(updated_at)::timestamptz as latest_updated
           from comments
          where updated_at > $1
          group by photo_id`, [since],
      )).rows;
      const likeCounts = (await pool.query(
        `select photo_id, count(*)::int as like_count, max(created_at)::timestamptz as latest
           from likes
          where created_at > $1
          group by photo_id`, [since],
      )).rows;

      res.json({
        cursor: new Date().toISOString(),
        accepted_suggestions: acceptedSug,
        fact_audits: factAudits,
        comments_summary: commentSummary,
        likes_counts: likeCounts,
      });
    } catch (err) { next(err); }
  });

  // /sync/pull/contributions?status=approved&pulled=false
  router.get('/pull/contributions', async (req, res, next) => {
    try {
      const status = String(req.query.status || 'approved');
      const pulled = String(req.query.pulled || 'false') === 'true';
      const { rows } = await pool.query(
        `select c.id, c.user_id, c.note, c.status, c.group_ids, c.decided_by, c.decided_at,
                c.pulled_at, c.created_at,
                u.email as user_email, u.display_name as user_display_name
           from contributions c
      left join users u on u.id = c.user_id
          where c.status in ($1, 'partial')
            and ((c.pulled_at is null) = $2::bool)
          order by c.id asc`,
        [status, !pulled],
      );
      const items = [];
      for (const c of rows) {
        const files = (await pool.query(
          `select id, original_filename, stored_path, sha256, size, mime, width, height,
                  exif_taken_at, phash, status, decided_at, approved_group_ids
             from contribution_files
            where contribution_id = $1
              and status = 'approved'
            order by id asc`,
          [c.id],
        )).rows;
        if (files.length === 0) continue; // partial with 0 approved yet
        items.push({
          id: Number(c.id),
          user_id: c.user_id != null ? Number(c.user_id) : null,
          user_email: c.user_email,
          user_display_name: c.user_display_name,
          note: c.note,
          status: c.status,
          group_ids: (c.group_ids || []).map(Number),
          decided_at: c.decided_at,
          created_at: c.created_at,
          files: files.map((f) => ({
            id: Number(f.id),
            original_filename: f.original_filename,
            sha256: f.sha256,
            size: f.size != null ? Number(f.size) : null,
            mime: f.mime,
            width: f.width, height: f.height,
            exif_taken_at: f.exif_taken_at,
            phash: f.phash,
            approved_group_ids: (f.approved_group_ids || []).map(Number),
          })),
        });
      }
      res.json({ items });
    } catch (err) { next(err); }
  });

  router.get('/pull/contributions/:id(\\d+)/files/:file_id(\\d+)', async (req, res, next) => {
    try {
      const cid = Number(req.params.id);
      const fid = Number(req.params.file_id);
      const { rows } = await pool.query(
        `select stored_path, mime, status
           from contribution_files
          where id = $1 and contribution_id = $2`, [fid, cid],
      );
      const f = rows[0];
      if (!f) return res.status(404).json({ error: 'not found' });
      if (f.status !== 'approved') return res.status(403).json({ error: 'not approved' });
      const abs = require('path').join(storage.root(), f.stored_path);
      if (!storage.fileExists(abs)) return res.status(410).json({ error: 'gone' });
      res.setHeader('Content-Type', f.mime || 'application/octet-stream');
      res.sendFile(abs);
    } catch (err) { next(err); }
  });

  router.post('/pull/contributions/:id(\\d+)/pulled', async (req, res, next) => {
    const id = Number(req.params.id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const r = (await client.query(
        `select status, pulled_at from contributions where id = $1 for update`, [id],
      )).rows[0];
      if (!r) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      if (r.pulled_at) { await client.query('rollback'); return res.json({ ok: true, already_pulled: true }); }
      await client.query(`update contributions set pulled_at = now() where id = $1`, [id]);
      await auditDesktop(client, 'contribution.pulled', 'contribution', id, {});
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // /sync/status — light snapshot. Also reports which database this
  // server is writing to (name + cluster identifier) so the desktop's
  // push pre-flight can refuse when the web shares the desktop's own DB
  // (fix-up 11: a local verification push once rewrote every desktop
  // working_path to a bare basename that way).
  router.get('/status', async (req, res, next) => {
    try {
      let db = null;
      try {
        const r = await pool.query(
          `select current_database() as name, system_identifier::text as system_identifier
             from pg_control_system()`,
        );
        db = r.rows[0] || null;
      } catch (e) {
        // pg_control_system() may be restricted on some roles; the desktop
        // treats a missing identity as "cannot verify" and warns.
        db = { name: null, system_identifier: null, error: String(e.message || e) };
      }
      const rows = (await pool.query(`
        select 'photos' as name,       count(*)::int as n, max(updated_at) as last_touch from photos
        union all select 'faces',      count(*)::int, max(updated_at) from faces
        union all select 'photo_backs',count(*)::int, max(updated_at) from photo_backs
        union all select 'people',     count(*)::int, max(updated_at) from people
        union all select 'places',     count(*)::int, max(updated_at) from places
        union all select 'albums',     count(*)::int, max(updated_at) from albums
        union all select 'groups',     count(*)::int, max(updated_at) from groups
        union all select 'group_members', count(*)::int, max(updated_at) from group_members
        union all select 'photo_groups',  count(*)::int, max(updated_at) from photo_groups
        union all select 'contributions', count(*)::int, max(updated_at) from contributions
        union all select 'contribution_files', count(*)::int, max(updated_at) from contribution_files
      `)).rows;
      res.json({ tables: rows, db });
    } catch (err) { next(err); }
  });

  return router;
};
