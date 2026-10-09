// Phase 15 — admin corrections on the web.
//
// Two jobs, both admin-only:
//   * edit the text of ONE pending suggestion (before accepting it);
//   * find & replace across pending suggestions in bulk.
//
// The scope is deliberately narrow. Each tier corrects the text it owns
// (answer 9): the desktop sweeps albums, people, places, aliases, backs
// and physical-reference notes; the web's bulk tool stays on pending
// suggestions, and comments and group text are corrected one at a time in
// their own editors. A cross-tier find & replace over comment bodies would
// mean the desktop reading web-authoritative text it must never push back
// — which is exactly how the `photo_groups` and `person_name_variants`
// faults began.
//
// Every write here stamps `edited_on_web_at`. That is what makes the
// correction survive: the suggestions row was born on the laptop, so the
// next push re-sends it in full, and only the `sync_web_edit_wins` guard
// in `/sync/suggestions` keeps this text instead of the laptop's older
// wording. The laptop then catches up through `/sync/pull/web_edits`.
//
// A resolved suggestion is never touched. Its text is what a decision was
// made on, and rewriting it would falsify the decision; the fact column
// the accept wrote is corrected on its own screen.

const { audit } = require('./audit');
const crypto = require('crypto');

// Which jsonb path holds the words, per suggestion kind. Same table as the
// desktop's `targets.py`, and for the same reason: one declaration beats a
// switch repeated in the finder, the previewer and the writer.
const TEXT_PATHS = {
  description: 'text',
  transcription: 'text',
  date: 'evidence',
};

const SAMPLE_LIMIT = 200;

function replaceText(value, needle, replacement, matchCase) {
  if (!needle || value == null) return value;
  if (matchCase) return String(value).split(needle).join(replacement);
  const re = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  return String(value).replace(re, () => replacement);
}

function likePattern(needle) {
  return `%${needle.replace(/\\/g, '\\\\').replace(/%/g, '\\%').replace(/_/g, '\\_')}%`;
}

// Find matching pending suggestions, grouped by kind.
async function search(pool, needle, replacement = '', { matchCase = true, sampleLimit = SAMPLE_LIMIT } = {}) {
  if (!needle) return [];
  const op = matchCase ? 'like' : 'ilike';
  const pattern = likePattern(needle);
  const groups = [];
  for (const [kind, path] of Object.entries(TEXT_PATHS)) {
    const { rows } = await pool.query(
      `select s.id, s.photo_id, s.payload->>$1 as value
         from suggestions s
        where s.kind = $2::suggestion_kind
          and s.status = 'pending'
          and s.payload->>$1 ${op} $3
        order by s.id`,
      [path, kind, pattern],
    );
    if (!rows.length) continue;
    groups.push({
      kind,
      path,
      count: rows.length,
      truncated: rows.length > sampleLimit,
      rows: rows.slice(0, sampleLimit).map((r) => ({
        id: Number(r.id),
        photo_id: r.photo_id != null ? Number(r.photo_id) : null,
        kind,
        path,
        value: r.value,
        new_value: replaceText(r.value, needle, replacement, matchCase),
      })),
    });
  }
  return groups;
}

// Write one pending suggestion's text. Shared by the inline edit and the
// bulk apply so there is one definition of "what an edit does".
async function writeText(client, { id, path, value }) {
  const res = await client.query(
    `update suggestions
        set payload = jsonb_set(payload, array[$1], to_jsonb($2::text)),
            edited_on_web_at = now()
      where id = $3 and status = 'pending'
      returning id`,
    [path, value, id],
  );
  return res.rowCount > 0;
}

// Inline edit of a single pending suggestion.
async function editOne(client, { id, text, actor, userId }) {
  const sug = (await client.query(
    `select id, photo_id, kind, payload, status from suggestions where id = $1 for update`,
    [id],
  )).rows[0];
  if (!sug) return { ok: false, reason: 'not_found' };
  if (sug.status !== 'pending') return { ok: false, reason: 'resolved' };
  const path = TEXT_PATHS[sug.kind];
  if (!path) return { ok: false, reason: 'kind_has_no_text' };

  const before = sug.payload ? sug.payload[path] : null;
  if (before === text) return { ok: true, changed: false, photo_id: sug.photo_id };

  await writeText(client, { id, path, value: text });
  await audit(client, {
    actor, userId, action: 'suggestion.edit', entityType: 'suggestion', entityId: id,
    previousValue: { path, value: before },
    newValue: { path, value: text },
  });
  return { ok: true, changed: true, photo_id: sug.photo_id };
}

// Bulk apply. Each row is re-read first; one changed since the search is
// skipped and listed, the same rule undo uses, so neither direction can
// quietly overwrite newer work.
async function applyCorrection(client, {
  needle, replacement, matchCase, rows, actor, userId,
}) {
  const batchId = crypto.randomBytes(16).toString('hex');
  const meta = { batch_id: batchId, search: needle, replace: replacement, match_case: !!matchCase };
  const skipped = [];
  let changed = 0;

  for (const row of rows) {
    const id = Number(row.id);
    const path = TEXT_PATHS[row.kind];
    if (!id || !path) { skipped.push(`#${row.id}: no editable text`); continue; }

    const cur = (await client.query(
      `select payload->>$1 as value, status from suggestions where id = $2 for update`,
      [path, id],
    )).rows[0];
    if (!cur) { skipped.push(`#${id}: gone`); continue; }
    if (cur.status !== 'pending') { skipped.push(`#${id}: already resolved`); continue; }
    if (cur.value !== row.value) { skipped.push(`#${id}: changed since the search`); continue; }

    const next = replaceText(cur.value, needle, replacement, matchCase);
    if (next === cur.value) continue;

    await writeText(client, { id, path, value: next });
    await audit(client, {
      actor, userId, action: 'correction.replace',
      entityType: 'suggestion', entityId: id,
      previousValue: { target: `suggestion_${row.kind}`, key: { id, path }, value: cur.value },
      newValue: { ...meta, target: `suggestion_${row.kind}`, key: { id, path }, value: next },
    });
    changed += 1;
  }

  await audit(client, {
    actor, userId, action: 'correction.batch', entityType: 'correction', entityId: null,
    newValue: { ...meta, changed, skipped },
  });
  return { batchId, changed, skipped };
}

async function listBatches(pool, limit = 50) {
  const { rows } = await pool.query(
    `select b.new_value->>'batch_id' as batch_id,
            b.new_value->>'search'   as search,
            b.new_value->>'replace'  as replace_with,
            (b.new_value->>'changed')::int as changed,
            b.actor, b.created_at,
            exists (
              select 1 from audit_log u
               where u.action = 'correction.undo.batch'
                 and u.new_value->>'batch_id' = b.new_value->>'batch_id'
            ) as undone
       from audit_log b
      where b.action = 'correction.batch'
        and b.actor <> 'desktop'
        and (b.new_value->>'changed')::int > 0
      order by b.id desc
      limit $1`,
    [limit],
  );
  return rows.map((r) => ({
    batch_id: r.batch_id,
    search: r.search || '',
    replace: r.replace_with || '',
    changed: r.changed || 0,
    actor: r.actor,
    created_at: r.created_at,
    undone: r.undone,
  }));
}

// Undo reads the audit log, so it works after a restart and after the
// admin who made the correction has gone. A row whose current value is no
// longer what the correction wrote is skipped and listed (answer 4):
// an undo that overwrote newer work would be a second mistake with no
// third chance.
async function undoBatch(client, batchId, { actor, userId }) {
  const { rows: entries } = await client.query(
    `select id, previous_value, new_value
       from audit_log
      where action = 'correction.replace'
        and new_value->>'batch_id' = $1
      order by id desc`,
    [batchId],
  );
  const skipped = [];
  let restored = 0;
  for (const e of entries) {
    const prev = e.previous_value || {};
    const nv = e.new_value || {};
    const key = nv.key || prev.key || {};
    const id = Number(key.id);
    const path = key.path;
    if (!id || !path) { skipped.push(`audit #${e.id}: no key`); continue; }

    const cur = (await client.query(
      `select payload->>$1 as value, status from suggestions where id = $2 for update`,
      [path, id],
    )).rows[0];
    if (!cur) { skipped.push(`#${id}: gone`); continue; }
    if (cur.status !== 'pending') { skipped.push(`#${id}: resolved since`); continue; }
    if (cur.value !== nv.value) { skipped.push(`#${id}: changed since the correction`); continue; }

    await writeText(client, { id, path, value: prev.value });
    await audit(client, {
      actor, userId, action: 'correction.undo', entityType: 'suggestion', entityId: id,
      previousValue: { key, value: nv.value },
      newValue: { key, value: prev.value, batch_id: batchId, undo_of_audit_id: e.id },
    });
    restored += 1;
  }
  await audit(client, {
    actor, userId, action: 'correction.undo.batch', entityType: 'correction', entityId: null,
    newValue: { batch_id: batchId, restored, skipped },
  });
  return { batchId, restored, skipped };
}

module.exports = {
  TEXT_PATHS, SAMPLE_LIMIT,
  replaceText, search, editOne, applyCorrection, listBatches, undoBatch,
};
