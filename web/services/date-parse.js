// Very forgiving free-text date parser for contributor date suggestions.
// Accepts: "1962", "March 1962", "3/1962", "1962-03", "1962-03-01",
// "12 March 1962", "March 12, 1962", "3/12/1962" (US order),
// "in the 60s", "sometime in the 60s", "60s", "1960s", "the sixties",
// "early 70s", "summer 1971", "Christmas 1965", "around 1962",
// "circa 1962", "1962?". Returns null on failure. Precision is derived:
// `exact` (day given), `month`, `year`, `decade`.
//
// Seasons record the year (precision `year`) with a mid-season month so
// date sorting places them sensibly; the contributor's original text is
// kept as the suggestion's evidence. Christmas records December.
//
// Not a full parser; we don't care about "next Thursday" or timestamps.
// If it doesn't fit one of these shapes it's rejected with a helpful
// message so the contributor can tighten it up.
const MONTHS = {
  january: 1, jan: 1,
  february: 2, feb: 2,
  march: 3, mar: 3,
  april: 4, apr: 4,
  may: 5,
  june: 6, jun: 6,
  july: 7, jul: 7,
  august: 8, aug: 8,
  september: 9, sep: 9, sept: 9,
  october: 10, oct: 10,
  november: 11, nov: 11,
  december: 12, dec: 12,
};

const SEASONS = { spring: 4, summer: 7, autumn: 10, fall: 10, winter: 1 };

const DECADE_WORDS = {
  twenties: 20, thirties: 30, forties: 40, fifties: 50, sixties: 60,
  seventies: 70, eighties: 80, nineties: 90,
};

function pad2(n) { return String(n).padStart(2, '0'); }
function pad4(n) { return String(n).padStart(4, '0'); }

function validYear(y) { return y >= 1800 && y <= 2100; }
function shortYear(y) { return y >= 30 ? 1900 + y : 2000 + y; }
function decadeOf(y) { return { date: `${pad4(Math.floor(y / 10) * 10)}-01-01`, precision: 'decade' }; }
function validDay(y, mo, d) {
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return false;
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCMonth() === mo - 1;
}

// Returns { date: 'YYYY-MM-DD', precision: 'exact'|'month'|'year'|'decade' }
// or null.
function parseDateFreetext(input) {
  if (input == null) return null;
  let raw = String(input).trim().toLowerCase();
  if (!raw) return null;
  // Hedges and filler words carry no date information.
  raw = raw
    .replace(/[?!]+$/g, '')
    .replace(/^(?:(?:maybe|probably|possibly|approx\.?|approximately|about|around|circa|c\.|ca\.|abt\.?|sometime|some time|in|during|the|early|mid|mid-|late)\s*)+/g, '')
    .trim();
  if (!raw) return null;

  let m;

  // "sixties", "the sixties"
  m = raw.match(/^(twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)$/);
  if (m) return decadeOf(1900 + DECADE_WORDS[m[1]]);

  // "1960s", "60s", "60's", "the 60s"
  m = raw.match(/^(\d{2}|\d{4})\s*['’]?s$/);
  if (m) {
    let year = parseInt(m[1], 10);
    if (year < 100) year = shortYear(year);
    if (!validYear(year)) return null;
    return decadeOf(year);
  }

  // ISO-ish: YYYY-MM-DD, YYYY-MM, YYYY-M
  m = raw.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/);
  if (m) {
    const y = parseInt(m[1], 10);
    const mo = parseInt(m[2], 10);
    const d = m[3] ? parseInt(m[3], 10) : null;
    if (!validYear(y) || mo < 1 || mo > 12) return null;
    if (d != null) {
      if (!validDay(y, mo, d)) return null;
      return { date: `${pad4(y)}-${pad2(mo)}-${pad2(d)}`, precision: 'exact' };
    }
    return { date: `${pad4(y)}-${pad2(mo)}-01`, precision: 'month' };
  }

  // "3/12/1962" (US month/day/year)
  m = raw.match(/^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$/);
  if (m) {
    const mo = parseInt(m[1], 10), d = parseInt(m[2], 10), y = parseInt(m[3], 10);
    if (!validYear(y) || !validDay(y, mo, d)) return null;
    return { date: `${pad4(y)}-${pad2(mo)}-${pad2(d)}`, precision: 'exact' };
  }

  // "3/1962" or "3-1962"
  m = raw.match(/^(\d{1,2})[/\-](\d{4})$/);
  if (m) {
    const mo = parseInt(m[1], 10);
    const y = parseInt(m[2], 10);
    if (!validYear(y) || mo < 1 || mo > 12) return null;
    return { date: `${pad4(y)}-${pad2(mo)}-01`, precision: 'month' };
  }

  // "summer 1971", "summer of 1971"
  m = raw.match(/^(spring|summer|autumn|fall|winter)\s+(?:of\s+)?(\d{4})$/);
  if (m) {
    const y = parseInt(m[2], 10);
    if (!validYear(y)) return null;
    return { date: `${pad4(y)}-${pad2(SEASONS[m[1]])}-01`, precision: 'year' };
  }

  // "christmas 1965", "xmas 1965"
  m = raw.match(/^(?:christmas|xmas)\s+(?:of\s+)?(\d{4})$/);
  if (m) {
    const y = parseInt(m[1], 10);
    if (!validYear(y)) return null;
    return { date: `${pad4(y)}-12-01`, precision: 'month' };
  }

  // "March 1962", "March 1st, 1962", "march of 1962"
  m = raw.match(/^([a-z]+)\.?\s+(?:of\s+)?(?:(\d{1,2})(?:st|nd|rd|th)?[,\s]+)?(\d{4})$/);
  if (m && MONTHS[m[1]]) {
    const mo = MONTHS[m[1]];
    const y = parseInt(m[3], 10);
    if (!validYear(y)) return null;
    const d = m[2] ? parseInt(m[2], 10) : null;
    if (d != null) {
      if (!validDay(y, mo, d)) return null;
      return { date: `${pad4(y)}-${pad2(mo)}-${pad2(d)}`, precision: 'exact' };
    }
    return { date: `${pad4(y)}-${pad2(mo)}-01`, precision: 'month' };
  }

  // "12 March 1962", "12th of March, 1962"
  m = raw.match(/^(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?,?\s+(\d{4})$/);
  if (m && MONTHS[m[2]]) {
    const d = parseInt(m[1], 10), mo = MONTHS[m[2]], y = parseInt(m[3], 10);
    if (!validYear(y) || !validDay(y, mo, d)) return null;
    return { date: `${pad4(y)}-${pad2(mo)}-${pad2(d)}`, precision: 'exact' };
  }

  // Bare 4-digit year
  m = raw.match(/^(\d{4})$/);
  if (m) {
    const y = parseInt(m[1], 10);
    if (!validYear(y)) return null;
    return { date: `${pad4(y)}-01-01`, precision: 'year' };
  }

  return null;
}

module.exports = { parseDateFreetext };
