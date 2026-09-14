// Very forgiving free-text date parser for contributor date suggestions.
// Accepts: "1962", "March 1962", "3/1962", "1962-03", "1962-03-01",
// "in the 60s", "sometime in the 60s", "60s", "1960s". Returns null
// on failure. Precision is derived: `exact` (day given), `month`,
// `year`, `decade`.
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

function pad2(n) { return String(n).padStart(2, '0'); }
function pad4(n) { return String(n).padStart(4, '0'); }

// Returns { date: 'YYYY-MM-DD', precision: 'exact'|'month'|'year'|'decade' }
// or null.
function parseDateFreetext(input) {
  if (input == null) return null;
  const raw = String(input).trim().toLowerCase();
  if (!raw) return null;

  // "1960s" or "in the 60s" or "sometime in the 60s"
  let m = raw.match(/(?:^|\D)(\d{2,4})\s*['’]?s\b/);
  if (m) {
    let year = parseInt(m[1], 10);
    if (year < 100) {
      year = year >= 30 ? 1900 + year : 2000 + year;
    }
    const decade = Math.floor(year / 10) * 10;
    return { date: `${pad4(decade)}-01-01`, precision: 'decade' };
  }

  // ISO-ish: YYYY-MM-DD, YYYY-MM, YYYY-M
  m = raw.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/);
  if (m) {
    const y = parseInt(m[1], 10);
    const mo = parseInt(m[2], 10);
    const d = m[3] ? parseInt(m[3], 10) : null;
    if (mo < 1 || mo > 12) return null;
    if (d != null && (d < 1 || d > 31)) return null;
    if (d != null) return { date: `${pad4(y)}-${pad2(mo)}-${pad2(d)}`, precision: 'exact' };
    return { date: `${pad4(y)}-${pad2(mo)}-01`, precision: 'month' };
  }

  // "3/1962" or "3-1962"
  m = raw.match(/^(\d{1,2})[\/\-](\d{4})$/);
  if (m) {
    const mo = parseInt(m[1], 10);
    const y = parseInt(m[2], 10);
    if (mo < 1 || mo > 12) return null;
    return { date: `${pad4(y)}-${pad2(mo)}-01`, precision: 'month' };
  }

  // "March 1962" or "March 1st, 1962"
  m = raw.match(/^([a-z]+)\.?\s+(?:(\d{1,2})(?:st|nd|rd|th)?[,\s]+)?(\d{4})$/);
  if (m) {
    const mo = MONTHS[m[1]];
    if (!mo) return null;
    const y = parseInt(m[3], 10);
    const d = m[2] ? parseInt(m[2], 10) : null;
    if (d != null) {
      if (d < 1 || d > 31) return null;
      return { date: `${pad4(y)}-${pad2(mo)}-${pad2(d)}`, precision: 'exact' };
    }
    return { date: `${pad4(y)}-${pad2(mo)}-01`, precision: 'month' };
  }

  // Bare 4-digit year
  m = raw.match(/^(\d{4})$/);
  if (m) {
    const y = parseInt(m[1], 10);
    return { date: `${pad4(y)}-01-01`, precision: 'year' };
  }

  // "sometime in 1962" / "around 1962" / "circa 1962" / "abt 1962"
  m = raw.match(/(?:around|circa|c\.|ca\.|abt\.?|sometime in|in)\s+(\d{4})\b/);
  if (m) {
    const y = parseInt(m[1], 10);
    return { date: `${pad4(y)}-01-01`, precision: 'year' };
  }

  return null;
}

module.exports = { parseDateFreetext };
