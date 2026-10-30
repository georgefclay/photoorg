// Live interpretation of a free-text date suggestion — the same parser the
// POST uses (services/date-parse.js), so what the contributor is shown is
// exactly what gets recorded.

const { parseDateFreetext } = require('./date-parse');
const { dateLabel, dateRange } = require('./format');
const { audit } = require('./audit');

const PRECISION_TEXT = {
  exact: 'exact day', month: 'month', year: 'year', decade: 'decade',
};

function interpretDate(text) {
  const raw = String(text || '').trim();
  if (!raw) return { ok: false, empty: true, message: '' };
  const parsed = parseDateFreetext(raw);
  if (!parsed) {
    return {
      ok: false,
      message: 'Try "1962", "March 1962", "summer 1971" or "sometime in the 60s".',
    };
  }
  const label = dateLabel(parsed.date, parsed.precision);
  const range = dateRange(parsed.date, parsed.precision);
  return {
    ok: true,
    date: parsed.date,
    precision: parsed.precision,
    label,
    range,
    message: `We'll record: ${label}, precision ${PRECISION_TEXT[parsed.precision] || parsed.precision}`,
  };
}

// Clear a photo's confirmed date (Phase 13 fix-up 1). The value goes, not
// just the flag: Browse's sort, the year/decade filters and the search
// index read capture_date without looking at capture_date_confirmed. The old
// value comes back as a pending date suggestion, re-acceptable in one click,
// and stays in the audit row's previous_value. The desktop applies the same
// clear when it pulls the `photo.capture_date.unconfirm` audit row.
// `source` is 'human' when a web accept ever set this date; the web cannot
// see a desktop accept, so a desktop-set date cleared here reads 'import'.
async function clearConfirmedDate(client, photoId, user) {
  const photo = (await client.query(
    // to_char, not a JS Date: node-pg reads a `date` as local midnight.
    `select to_char(capture_date, 'YYYY-MM-DD') as capture_date,
            capture_date_precision, capture_date_confirmed
       from photos where id = $1 and not is_deleted and not is_private for update`, [photoId],
  )).rows[0];
  if (!photo) return { notFound: true };
  if (!photo.capture_date_confirmed) return { cleared: false };
  const prevDate = photo.capture_date || null;
  const prevPrecision = photo.capture_date_precision || 'unknown';
  const human = (await client.query(
    `select exists (select 1 from audit_log
                     where entity_type = 'photo' and entity_id = $1
                       and action = 'photo.capture_date.set') as h`, [photoId],
  )).rows[0].h;
  const source = human ? 'human' : 'import';

  let suggestionId = null;
  if (prevDate) {
    const existing = (await client.query(
      `select id from suggestions
        where photo_id = $1 and kind = 'date' and status = 'pending'
          and payload->>'date' = $2 and coalesce(payload->>'precision', 'exact') = $3
        order by id limit 1`, [photoId, prevDate, prevPrecision],
    )).rows[0];
    if (existing) suggestionId = Number(existing.id);
    else {
      const ins = await client.query(
        `insert into suggestions (photo_id, user_id, kind, payload, confidence, source, status)
         values ($1, $2, 'date', $3, 0.2, $4, 'pending') returning id`,
        [photoId, user.id, {
          date: prevDate, precision: prevPrecision, reason: 'cleared',
          evidence: human ? 'confirmed date, cleared' : 'EXIF date, cleared',
        }, source],
      );
      suggestionId = Number(ins.rows[0].id);
    }
  }
  await client.query(
    `update photos
        set capture_date = null, capture_date_precision = 'unknown', capture_date_confirmed = false
      where id = $1`, [photoId],
  );
  await audit(client, {
    actor: user.email, action: 'photo.capture_date.unconfirm',
    entityType: 'photo', entityId: photoId, userId: user.id,
    previousValue: { capture_date: prevDate, precision: prevPrecision, confirmed: true, source },
    newValue: { capture_date: null, precision: 'unknown', confirmed: false,
                reason: 'cleared', via: 'web.admin', suggestion_id: suggestionId },
  });
  await client.query('select refresh_completeness($1)', [photoId]);
  return { cleared: true, previous: { capture_date: prevDate, precision: prevPrecision }, suggestion_id: suggestionId };
}

module.exports = { interpretDate, clearConfirmedDate };
