// Live interpretation of a free-text date suggestion — the same parser the
// POST uses (services/date-parse.js), so what the contributor is shown is
// exactly what gets recorded.

const { parseDateFreetext } = require('./date-parse');
const { dateLabel, dateRange } = require('./format');

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

module.exports = { interpretDate };
