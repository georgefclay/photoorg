// Contributions API.
//
//   POST   /api/contributions                      create a pending contribution
//   HEAD   /api/contributions/:id/files?sha256=    204 if server already holds
//   POST   /api/contributions/:id/files            one file, multipart, ≤100MB
//   POST   /api/contributions/:id/finish           mark complete, email admin
//   GET    /api/contributions/mine                 uploader sees own (with files)
//
//   GET    /api/admin/contributions?status=pending
//   POST   /api/admin/contributions/:id/files/:file_id/approve
//   POST   /api/admin/contributions/:id/files/:file_id/reject
//   POST   /api/admin/contributions/:id/approve-all
//   POST   /api/admin/contributions/:id/reject-all
//
// Duplicate policy (answer #11): every upload gets a duplicate badge
// (sha256 exact OR pHash ≤ 10 against `photos`). Approving a
// sha256-exact duplicate does not create a new photo — the file
// stays on the VM and is marked approved but flagged so desktop pull
// skips it. pHash-near duplicates become normal contribution files;
// laptop-side dedupe / rescan decides on the eventual outcome.
//
// Rate limit: 600 uploads / hour per user (answer #12), admins exempt.
// Global cap: 100 MB per file, JPEG/PNG/HEIC/TIFF/webp/video (video
// stored with is_video=true and no thumb / no phash).

const path = require('path');
const crypto = require('crypto');
const express = require('express');
const multer = require('multer');
const sharp = require('sharp');
const exifParser = require('exif-parser');
const fs = require('fs/promises');

const { requireUser, requireAdmin } = require('../middleware/require-user');
const { isModerator } = require('../middleware/visibility');
const { audit } = require('../services/audit');
const { send } = require('../services/email');
const { dhash64Hex, hamming64Hex } = require('../services/phash');
const storage = require('../services/photo-storage');

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 100 * 1024 * 1024 }, // 100 MB
});

const IMAGE_MIMES = new Set(['image/jpeg', 'image/png', 'image/tiff', 'image/heic', 'image/webp']);
const VIDEO_MIMES = new Set(['video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm']);

function toInt(v) { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; }
function toIntArr(v) {
  if (!Array.isArray(v)) return [];
  return v.map(toInt).filter((n) => Number.isInteger(n) && n > 0);
}

function baseUrl(req) {
  return process.env.BASE_URL || `${req.protocol}://${req.get('host')}`;
}

function sha256Hex(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

// Extract EXIF minimally — takes JPEG buffer, returns { width, height,
// takenAt } or partial. Silent on failure.
function extractExif(buffer, mime) {
  try {
    if (mime !== 'image/jpeg') return {};
    const parser = exifParser.create(buffer);
    const r = parser.parse();
    const takenAtSec = r.tags && (r.tags.DateTimeOriginal || r.tags.CreateDate);
    return {
      width: r.imageSize && r.imageSize.width,
      height: r.imageSize && r.imageSize.height,
      takenAt: takenAtSec ? new Date(takenAtSec * 1000) : null,
    };
  } catch { return {}; }
}

// Simple in-process 600/hour user counter (matches contrib-rate-limit's shape).
const UPLOAD_WINDOW_MS = 60 * 60 * 1000;
const UPLOAD_LIMIT = 600;
const uploadSlots = new Map();
function uploadHit(userId) {
  const now = Date.now();
  const cutoff = now - UPLOAD_WINDOW_MS;
  const arr = uploadSlots.get(userId) || [];
  while (arr.length && arr[0] < cutoff) arr.shift();
  if (arr.length >= UPLOAD_LIMIT) return { ok: false, retryMs: arr[0] + UPLOAD_WINDOW_MS - now };
  arr.push(now);
  uploadSlots.set(userId, arr);
  return { ok: true };
}

// Determine whether this user may target these groups.
async function canUploaderTargetGroups(pool, user, groupIds) {
  if (!groupIds || groupIds.length === 0) return true; // empty targets are fine
  if (user.role === 'admin') return true;
  const { rows } = await pool.query(
    `select group_id from group_members
      where user_id = $1 and is_deleted = false
        and group_id = any($2::bigint[])`,
    [user.id, groupIds],
  );
  const has = new Set(rows.map((r) => Number(r.group_id)));
  for (const g of groupIds) if (!has.has(g)) return false;
  return true;
}

async function findDuplicate(pool, sha256, phash) {
  // sha256 exact against photos.sha256 or photo_masters.sha256.
  const shaHit = await pool.query(
    `select id from photos where sha256 = $1
     union all
     select photo_id from photo_masters where sha256 = $1
     limit 1`, [sha256],
  );
  if (shaHit.rows[0]) return { kind: 'sha256_exact', photo_id: Number(shaHit.rows[0].id), distance: 0 };
  // Also check across pending contribution_files for the same sha (dedupe within uploads).
  const cShaHit = await pool.query(
    `select contribution_id from contribution_files where sha256 = $1 limit 1`, [sha256],
  );
  if (cShaHit.rows[0]) return { kind: 'sha256_contribution', contribution_id: Number(cShaHit.rows[0].contribution_id), distance: 0 };

  // pHash near — 64-bit, distance ≤ 10.
  if (!phash) return null;
  const cand = await pool.query(
    `select id, phash from photos
      where phash is not null and length(phash) = 16
        and is_deleted = false and is_private = false
      order by id desc limit 5000`,
  );
  let best = null;
  for (const r of cand.rows) {
    const d = hamming64Hex(phash, r.phash);
    if (d != null && (best == null || d < best.distance)) best = { photo_id: Number(r.id), distance: d };
  }
  if (best && best.distance <= 10) return { kind: 'phash_near', photo_id: best.photo_id, distance: best.distance };
  return null;
}

module.exports = function apiContributionsRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  // ---- create ---------------------------------------------------------
  router.post('/', express.json({ limit: '32kb' }), async (req, res, next) => {
    const b = req.body || {};
    const groupIds = toIntArr(b.group_ids);
    // Validate group membership.
    if (!(await canUploaderTargetGroups(pool, req.user, groupIds))) {
      return res.status(403).json({ error: 'you are not a member of one of the target groups' });
    }
    const note = String(b.note || '').trim() || null;
    const client = await pool.connect();
    try {
      await client.query('begin');
      const ins = await client.query(
        `insert into contributions (user_id, note, group_ids) values ($1, $2, $3)
         returning id, status`,
        [req.user.id, note, groupIds],
      );
      const cid = Number(ins.rows[0].id);
      await audit(client, {
        actor: req.user.email, action: 'contribution.create',
        entityType: 'contribution', entityId: cid, userId: req.user.id,
        newValue: { group_ids: groupIds },
      });
      await client.query('commit');
      res.status(201).json({ id: cid, status: 'pending' });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // HEAD /api/contributions/:id/files?sha256=<hex>
  router.head('/:id(\\d+)/files', async (req, res, next) => {
    try {
      const cid = Number(req.params.id);
      const sha = String(req.query.sha256 || '');
      if (!/^[a-f0-9]{64}$/i.test(sha)) return res.status(400).end();
      // Ensure caller owns the contribution (or is admin).
      const own = await pool.query(
        `select user_id from contributions where id = $1`, [cid],
      );
      if (!own.rows[0]) return res.status(404).end();
      if (req.user.role !== 'admin' && Number(own.rows[0].user_id) !== Number(req.user.id)) {
        return res.status(404).end();
      }
      const { rows } = await pool.query(
        `select 1 from photos where sha256 = $1
         union all select 1 from photo_masters where sha256 = $1
         union all select 1 from contribution_files where sha256 = $1
         limit 1`, [sha.toLowerCase()],
      );
      return res.status(rows[0] ? 204 : 404).end();
    } catch (err) { next(err); }
  });

  // POST /api/contributions/:id/files — one file per request.
  router.post('/:id(\\d+)/files', upload.single('file'), async (req, res, next) => {
    try {
      const cid = Number(req.params.id);
      if (req.user.role !== 'admin') {
        const { ok, retryMs } = uploadHit(String(req.user.id));
        if (!ok) {
          res.set('Retry-After', String(Math.max(1, Math.ceil(retryMs / 1000))));
          return res.status(429).json({ error: 'too many uploads this hour, try again later' });
        }
      }
      const c = (await pool.query(`select user_id, status from contributions where id = $1`, [cid])).rows[0];
      if (!c) return res.status(404).json({ error: 'not found' });
      if (req.user.role !== 'admin' && Number(c.user_id) !== Number(req.user.id)) {
        return res.status(404).json({ error: 'not found' });
      }
      if (!['pending', 'partial'].includes(c.status)) {
        return res.status(409).json({ error: 'contribution is already resolved' });
      }
      if (!req.file) return res.status(400).json({ error: 'need file field (multipart)' });
      const buffer = req.file.buffer;
      const mime = (req.file.mimetype || 'application/octet-stream').toLowerCase();
      const isImage = IMAGE_MIMES.has(mime);
      const isVideo = VIDEO_MIMES.has(mime);
      if (!isImage && !isVideo) return res.status(400).json({ error: `unsupported mime ${mime}` });

      const sha = sha256Hex(buffer);

      // Reject if this exact sha already exists as a contribution_file
      // (idempotence within a contribution) — but return the existing row
      // shape so a re-upload is a no-op the client can handle.
      const dupCF = await pool.query(
        `select id from contribution_files where sha256 = $1`, [sha],
      );
      if (dupCF.rows[0]) {
        return res.status(200).json({ id: Number(dupCF.rows[0].id), skipped: true, reason: 'already uploaded' });
      }

      // Extract dims + EXIF + pHash (images only).
      let width = null, height = null, exifTakenAt = null, phash = null;
      if (isImage) {
        const exif = extractExif(buffer, mime);
        width = exif.width || null;
        height = exif.height || null;
        exifTakenAt = exif.takenAt || null;
        try {
          const meta = await sharp(buffer, { failOn: 'none' }).metadata();
          if (!width && meta.width) width = meta.width;
          if (!height && meta.height) height = meta.height;
        } catch {}
        try { phash = await dhash64Hex(buffer); } catch {}
      }

      // Duplicate detection against existing photos + contribution_files.
      const dup = await findDuplicate(pool, sha, phash);

      // Write bytes to uploads/<cid>/<file_id>.<ext>.
      // We need the file id first; two-phase insert to reserve the id.
      const client = await pool.connect();
      let fileId = null;
      try {
        await client.query('begin');
        const ext = extForMime(mime) || guessExt(req.file.originalname) || 'bin';
        const insReserve = await client.query(
          `insert into contribution_files
             (contribution_id, original_filename, stored_path, sha256, size, mime,
              width, height, exif_taken_at, phash, is_video, duplicate_of_photo_id, duplicate_distance)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
           returning id`,
          [
            cid,
            req.file.originalname || 'file',
            path.posix.join('uploads', String(cid), `pending.${ext}`),
            sha, buffer.length, mime,
            width, height, exifTakenAt, phash,
            !!isVideo,
            dup && dup.photo_id != null ? dup.photo_id : null,
            dup && dup.distance != null ? dup.distance : null,
          ],
        );
        fileId = Number(insReserve.rows[0].id);
        await storage.ensureDirs();
        const { relative } = await storage.writeUploadFile(cid, fileId, ext, buffer);
        await client.query(`update contribution_files set stored_path = $1 where id = $2`, [relative, fileId]);
        await audit(client, {
          actor: req.user.email, action: 'contribution.file.create',
          entityType: 'contribution_file', entityId: fileId, userId: req.user.id,
          newValue: {
            contribution_id: cid, sha256: sha, mime, size: buffer.length,
            duplicate: dup || null,
          },
        });
        await client.query('commit');
      } catch (err) {
        await client.query('rollback').catch(() => {});
        throw err;
      } finally {
        client.release();
      }

      res.status(201).json({
        id: fileId,
        contribution_id: cid,
        sha256: sha,
        size: buffer.length,
        mime,
        width, height,
        exif_taken_at: exifTakenAt,
        phash,
        duplicate: dup || null,
        is_video: !!isVideo,
      });
    } catch (err) { next(err); }
  });

  router.post('/:id(\\d+)/finish', express.json({ limit: '32kb' }), async (req, res, next) => {
    try {
      const cid = Number(req.params.id);
      const c = (await pool.query(
        `select c.id, c.user_id, c.status, c.note, c.group_ids,
                u.email as user_email, u.display_name as user_display_name
           from contributions c
           left join users u on u.id = c.user_id
          where c.id = $1`, [cid],
      )).rows[0];
      if (!c) return res.status(404).json({ error: 'not found' });
      if (req.user.role !== 'admin' && Number(c.user_id) !== Number(req.user.id)) {
        return res.status(404).json({ error: 'not found' });
      }
      if (c.status !== 'pending') return res.json({ ok: true, already: true });

      const counts = (await pool.query(
        `select
           count(*)::int as total,
           count(*) filter (where duplicate_of_photo_id is not null)::int as dup_count
           from contribution_files where contribution_id = $1`, [cid],
      )).rows[0];
      const client = await pool.connect();
      try {
        await client.query('begin');
        await client.query(`update contributions set finished_at = now() where id = $1`, [cid]);
        await audit(client, {
          actor: req.user.email, action: 'contribution.finish',
          entityType: 'contribution', entityId: cid, userId: req.user.id,
          newValue: { file_count: counts.total, duplicate_count: counts.dup_count },
        });
        await client.query('commit');
      } catch (err) {
        await client.query('rollback').catch(() => {});
        throw err;
      } finally {
        client.release();
      }

      // Email the admin — fire-and-forget through the dev sink in tests.
      const to = process.env.ADMIN_EMAIL;
      if (to) {
        await send({
          to,
          subject: 'A new contribution is waiting',
          template: 'admin-contribution',
          vars: {
            uploaderEmail: c.user_email || 'unknown',
            uploaderName: c.user_display_name || c.user_email || 'unknown',
            fileCount: counts.total,
            duplicateCount: counts.dup_count,
            note: c.note || '(none)',
            approvalUrl: `${baseUrl(req)}/admin/contributions#${cid}`,
          },
        }).catch(() => {});
      }
      res.json({ ok: true });
    } catch (err) { next(err); }
  });

  router.get('/mine', async (req, res, next) => {
    try {
      const { rows } = await pool.query(
        `select c.id, c.status, c.note, c.group_ids, c.created_at, c.finished_at, c.pulled_at,
                (select count(*)::int from contribution_files cf where cf.contribution_id = c.id) as file_count
           from contributions c
          where c.user_id = $1
          order by c.id desc
          limit 200`, [req.user.id],
      );
      const items = [];
      for (const r of rows) {
        const files = (await pool.query(
          `select id, original_filename, sha256, size, mime, status,
                  duplicate_of_photo_id, duplicate_distance, phash, exif_taken_at
             from contribution_files where contribution_id = $1
             order by id asc`, [r.id],
        )).rows;
        items.push({
          id: Number(r.id), status: r.status, note: r.note,
          group_ids: (r.group_ids || []).map(Number),
          created_at: r.created_at, finished_at: r.finished_at, pulled_at: r.pulled_at,
          file_count: r.file_count,
          files: files.map((f) => ({
            id: Number(f.id),
            original_filename: f.original_filename,
            sha256: f.sha256,
            size: f.size != null ? Number(f.size) : null,
            mime: f.mime,
            status: f.status,
            duplicate_of_photo_id: f.duplicate_of_photo_id != null ? Number(f.duplicate_of_photo_id) : null,
            duplicate_distance: f.duplicate_distance,
            exif_taken_at: f.exif_taken_at,
          })),
        });
      }
      res.json({ items });
    } catch (err) { next(err); }
  });

  return router;
};

// -----------------------------------------------------------------------
// Admin contributions router at /api/admin/contributions
// -----------------------------------------------------------------------
module.exports.adminContribRouter = function adminContribRouter({ pool }) {
  const router = express.Router();
  router.use(requireUser);
  router.use(express.json({ limit: '32kb' }));

  // GET /api/admin/contributions — admin sees all; moderator sees only
  // those whose target group_ids intersect their moderator groups.
  router.get('/', async (req, res, next) => {
    try {
      const status = String(req.query.status || 'pending');
      const isAdmin = req.user.role === 'admin';
      const params = [status];
      let scopeClause = '';
      if (!isAdmin) {
        const mods = (await pool.query(
          `select group_id from group_members
            where user_id = $1 and role = 'moderator' and is_deleted = false`, [req.user.id],
        )).rows.map((r) => Number(r.group_id));
        if (mods.length === 0) return res.json({ items: [] });
        params.push(mods);
        scopeClause = `and c.group_ids && $${params.length}::bigint[]`;
      }
      const { rows } = await pool.query(
        `select c.id, c.user_id, c.status, c.note, c.group_ids, c.created_at, c.finished_at,
                u.email as user_email, u.display_name as user_display_name,
                (select count(*)::int from contribution_files cf where cf.contribution_id = c.id) as file_count,
                (select count(*)::int from contribution_files cf where cf.contribution_id = c.id and cf.duplicate_of_photo_id is not null) as duplicate_count
           from contributions c
      left join users u on u.id = c.user_id
          where c.status = $1 ${scopeClause}
          order by c.id desc
          limit 200`,
        params,
      );
      res.json({
        items: rows.map((r) => ({
          id: Number(r.id),
          uploader: r.user_display_name || r.user_email,
          user_id: r.user_id != null ? Number(r.user_id) : null,
          status: r.status, note: r.note,
          group_ids: (r.group_ids || []).map(Number),
          file_count: r.file_count,
          duplicate_count: r.duplicate_count,
          created_at: r.created_at, finished_at: r.finished_at,
        })),
      });
    } catch (err) { next(err); }
  });

  // GET /api/admin/contributions/:id  — detail (with file list).
  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const cid = Number(req.params.id);
      const c = (await pool.query(
        `select c.id, c.user_id, c.status, c.note, c.group_ids, c.created_at, c.finished_at,
                u.email as user_email, u.display_name as user_display_name
           from contributions c left join users u on u.id = c.user_id where c.id = $1`,
        [cid],
      )).rows[0];
      if (!c) return res.status(404).json({ error: 'not found' });
      if (req.user.role !== 'admin') {
        // Moderator scope: at least one target group must be one they moderate.
        const mods = (await pool.query(
          `select group_id from group_members where user_id = $1 and role = 'moderator' and is_deleted = false`,
          [req.user.id],
        )).rows.map((r) => Number(r.group_id));
        const targets = (c.group_ids || []).map(Number);
        if (!targets.some((g) => mods.includes(g))) return res.status(404).json({ error: 'not found' });
      }
      const files = (await pool.query(
        `select id, original_filename, stored_path, sha256, size, mime, width, height,
                exif_taken_at, phash, status, decided_at, decided_by, approved_group_ids,
                duplicate_of_photo_id, duplicate_distance, is_video
           from contribution_files where contribution_id = $1 order by id asc`,
        [cid],
      )).rows;
      res.json({
        id: Number(c.id),
        uploader: c.user_display_name || c.user_email,
        user_id: c.user_id != null ? Number(c.user_id) : null,
        status: c.status, note: c.note,
        group_ids: (c.group_ids || []).map(Number),
        created_at: c.created_at, finished_at: c.finished_at,
        files: files.map((f) => ({
          id: Number(f.id),
          original_filename: f.original_filename,
          sha256: f.sha256,
          size: f.size != null ? Number(f.size) : null,
          mime: f.mime,
          width: f.width, height: f.height,
          exif_taken_at: f.exif_taken_at,
          phash: f.phash,
          status: f.status,
          approved_group_ids: (f.approved_group_ids || []).map(Number),
          duplicate_of_photo_id: f.duplicate_of_photo_id != null ? Number(f.duplicate_of_photo_id) : null,
          duplicate_distance: f.duplicate_distance,
          is_video: f.is_video,
        })),
      });
    } catch (err) { next(err); }
  });

  // Approve a single file. Admin: all target groups get assigned.
  // Moderator: only their group is assigned (answer #6), other targets
  // stay pending for their own moderators or an admin.
  router.post('/:id(\\d+)/files/:fid(\\d+)/approve', async (req, res, next) => {
    const cid = Number(req.params.id);
    const fid = Number(req.params.fid);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const c = (await client.query(
        `select id, status, group_ids from contributions where id = $1 for update`, [cid],
      )).rows[0];
      if (!c) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const f = (await client.query(
        `select id, status, approved_group_ids from contribution_files
          where id = $1 and contribution_id = $2 for update`, [fid, cid],
      )).rows[0];
      if (!f) { await client.query('rollback'); return res.status(404).json({ error: 'file not found' }); }
      if (f.status === 'approved') { await client.query('rollback'); return res.json({ ok: true, already: true }); }
      const targets = (c.group_ids || []).map(Number);
      // Determine which groups this approver assigns.
      let assign;
      if (req.user.role === 'admin') {
        assign = targets;
      } else {
        const mods = (await client.query(
          `select group_id from group_members where user_id = $1 and role = 'moderator' and is_deleted = false`,
          [req.user.id],
        )).rows.map((r) => Number(r.group_id));
        assign = targets.filter((g) => mods.includes(g));
        if (assign.length === 0) { await client.query('rollback'); return res.status(403).json({ error: 'not a moderator of any target group' }); }
      }
      const currentlyApproved = (f.approved_group_ids || []).map(Number);
      const nextApproved = Array.from(new Set([...currentlyApproved, ...assign]));
      // The file becomes approved once any target has approved it.
      // Contribution status becomes 'approved' once every file is approved,
      // 'partial' if at least one file is decided but not all.
      await client.query(
        `update contribution_files
            set status = 'approved',
                decided_by = $1,
                decided_at = now(),
                approved_group_ids = $2::bigint[]
          where id = $3`,
        [req.user.id, nextApproved, fid],
      );
      await audit(client, {
        actor: req.user.email, action: 'contribution.file.approve',
        entityType: 'contribution_file', entityId: fid, userId: req.user.id,
        newValue: { contribution_id: cid, approved_group_ids: nextApproved },
      });
      await rollupContributionStatus(client, cid);
      await client.query('commit');
      res.json({ ok: true, approved_group_ids: nextApproved });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // Reject a single file. Moderator: removes only their group from the
  // contribution's target group_ids; the file is only 'rejected' if no
  // target group remains that could still be approved. Admin: rejects.
  router.post('/:id(\\d+)/files/:fid(\\d+)/reject', async (req, res, next) => {
    const cid = Number(req.params.id);
    const fid = Number(req.params.fid);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const c = (await client.query(
        `select id, group_ids from contributions where id = $1 for update`, [cid],
      )).rows[0];
      if (!c) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const f = (await client.query(
        `select id, status from contribution_files where id = $1 and contribution_id = $2 for update`,
        [fid, cid],
      )).rows[0];
      if (!f) { await client.query('rollback'); return res.status(404).json({ error: 'file not found' }); }
      if (f.status === 'approved') { await client.query('rollback'); return res.status(409).json({ error: 'file already approved' }); }
      const targets = (c.group_ids || []).map(Number);
      if (req.user.role === 'admin') {
        await client.query(
          `update contribution_files set status = 'rejected', decided_by = $1, decided_at = now() where id = $2`,
          [req.user.id, fid],
        );
        await audit(client, {
          actor: req.user.email, action: 'contribution.file.reject',
          entityType: 'contribution_file', entityId: fid, userId: req.user.id,
          newValue: { contribution_id: cid, via: 'admin' },
        });
      } else {
        const mods = (await client.query(
          `select group_id from group_members where user_id = $1 and role = 'moderator' and is_deleted = false`,
          [req.user.id],
        )).rows.map((r) => Number(r.group_id));
        const myTargets = targets.filter((g) => mods.includes(g));
        if (myTargets.length === 0) { await client.query('rollback'); return res.status(403).json({ error: 'not a moderator of any target group' }); }
        const remainingTargets = targets.filter((g) => !mods.includes(g));
        await client.query(`update contributions set group_ids = $1 where id = $2`, [remainingTargets, cid]);
        if (remainingTargets.length === 0) {
          await client.query(
            `update contribution_files set status = 'rejected', decided_by = $1, decided_at = now() where id = $2`,
            [req.user.id, fid],
          );
          await audit(client, {
            actor: req.user.email, action: 'contribution.file.reject',
            entityType: 'contribution_file', entityId: fid, userId: req.user.id,
            newValue: { contribution_id: cid, via: 'moderator', removed_targets: myTargets, remaining: [] },
          });
        } else {
          await audit(client, {
            actor: req.user.email, action: 'contribution.file.reject.partial',
            entityType: 'contribution_file', entityId: fid, userId: req.user.id,
            newValue: { contribution_id: cid, via: 'moderator', removed_targets: myTargets, remaining: remainingTargets },
          });
        }
      }
      await rollupContributionStatus(client, cid);
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/:id(\\d+)/approve-all', requireAdmin, async (req, res, next) => {
    const cid = Number(req.params.id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const c = (await client.query(
        `select group_ids from contributions where id = $1 for update`, [cid],
      )).rows[0];
      if (!c) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const targets = (c.group_ids || []).map(Number);
      const upd = await client.query(
        `update contribution_files
            set status = 'approved',
                decided_by = $1,
                decided_at = now(),
                approved_group_ids = $2::bigint[]
          where contribution_id = $3
            and status = 'pending'
          returning id`,
        [req.user.id, targets, cid],
      );
      await audit(client, {
        actor: req.user.email, action: 'contribution.approve_all',
        entityType: 'contribution', entityId: cid, userId: req.user.id,
        newValue: { approved: upd.rowCount, targets },
      });
      await rollupContributionStatus(client, cid);
      await client.query('commit');
      res.json({ ok: true, approved: upd.rowCount });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/:id(\\d+)/reject-all', requireAdmin, async (req, res, next) => {
    const cid = Number(req.params.id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const upd = await client.query(
        `update contribution_files
            set status = 'rejected', decided_by = $1, decided_at = now()
          where contribution_id = $2 and status = 'pending'
          returning id`,
        [req.user.id, cid],
      );
      await audit(client, {
        actor: req.user.email, action: 'contribution.reject_all',
        entityType: 'contribution', entityId: cid, userId: req.user.id,
        newValue: { rejected: upd.rowCount },
      });
      await rollupContributionStatus(client, cid);
      await client.query('commit');
      res.json({ ok: true, rejected: upd.rowCount });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};

async function rollupContributionStatus(client, cid) {
  const counts = (await client.query(
    `select
       count(*)::int as total,
       count(*) filter (where status = 'pending')::int  as pending,
       count(*) filter (where status = 'approved')::int as approved,
       count(*) filter (where status = 'rejected')::int as rejected
       from contribution_files where contribution_id = $1`, [cid],
  )).rows[0];
  let next = 'pending';
  if (counts.total > 0) {
    if (counts.pending === 0 && counts.approved > 0 && counts.rejected === 0) next = 'approved';
    else if (counts.pending === 0 && counts.approved === 0 && counts.rejected > 0) next = 'rejected';
    else if (counts.pending === 0) next = 'partial'; // mixed approved + rejected
    else if (counts.approved > 0 || counts.rejected > 0) next = 'partial';
  }
  await client.query(
    `update contributions
        set status = $1::contribution_status,
            decided_at = case when $1::contribution_status <> 'pending' then now() else decided_at end
      where id = $2`,
    [next, cid],
  );
}

function extForMime(mime) {
  const m = (mime || '').toLowerCase();
  if (m === 'image/jpeg') return 'jpg';
  if (m === 'image/png')  return 'png';
  if (m === 'image/tiff') return 'tif';
  if (m === 'image/heic') return 'heic';
  if (m === 'image/webp') return 'webp';
  if (m === 'video/mp4')  return 'mp4';
  if (m === 'video/quicktime') return 'mov';
  if (m === 'video/x-msvideo') return 'avi';
  if (m === 'video/webm') return 'webm';
  return null;
}

function guessExt(filename) {
  if (!filename) return null;
  const m = /\.([a-z0-9]+)$/i.exec(filename);
  return m ? m[1].toLowerCase() : null;
}
