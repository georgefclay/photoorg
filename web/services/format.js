// Display helpers shared by views and JSON. Exposed to every EJS view as
// `fmt` (app.locals.fmt).

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];

// Postgres `date` → 'YYYY-MM-DD' without a timezone shift. Accepts the
// string form (preferred: services select to_char) or a Date built by pg
// at local midnight.
function dateStr(d) {
  if (d == null) return null;
  if (typeof d === 'string') return d.slice(0, 10);
  if (d instanceof Date && !isNaN(d)) {
    const y = d.getFullYear(), m = d.getMonth() + 1, day = d.getDate();
    return `${String(y).padStart(4, '0')}-${String(m).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  }
  return null;
}

// "12 March 1962" / "March 1962" / "1962" / "1960s".
function dateLabel(d, precision) {
  const s = dateStr(d);
  if (!s) return null;
  const [y, m, day] = s.split('-').map(Number);
  switch (precision) {
    case 'exact': return `${day} ${MONTHS[m - 1]} ${y}`;
    case 'month': return `${MONTHS[m - 1]} ${y}`;
    case 'year': return String(y);
    case 'decade': return `${Math.floor(y / 10) * 10}s`;
    default: return String(y);
  }
}

// A guess is shown as a range: "1960–1969", "1962", "March 1962".
function dateRange(d, precision) {
  const s = dateStr(d);
  if (!s) return null;
  const y = Number(s.slice(0, 4));
  if (precision === 'decade') { const d0 = Math.floor(y / 10) * 10; return `${d0}–${d0 + 9}`; }
  if (precision === 'unknown') return `around ${y}`;
  return dateLabel(s, precision);
}

// { text, confirmed, guess } for the caption strip.
function captureDate(photo) {
  const s = dateStr(photo.capture_date);
  if (!s) return { text: null, confirmed: false, guess: false };
  if (photo.capture_date_confirmed) {
    return { text: dateLabel(s, photo.capture_date_precision), confirmed: true, guess: false };
  }
  return { text: dateRange(s, photo.capture_date_precision), confirmed: false, guess: true };
}

const PRECISION_WORDS = { exact: 'exact day', month: 'month', year: 'year', decade: 'decade' };

// "Batch 00012 #017" (+ physical_ref_note).
function physicalRef(photo) {
  const parts = [];
  if (photo.scan_batch) {
    const seq = photo.scan_sequence != null ? ` #${String(photo.scan_sequence).padStart(3, '0')}` : '';
    parts.push(`${photo.scan_batch}${seq}`);
  }
  if (photo.physical_ref_note) parts.push(photo.physical_ref_note);
  return parts.length ? parts.join(' | ') : null;
}

function yearSpan(a, b) {
  if (a == null && b == null) return null;
  if (a != null && b != null) return a === b ? String(a) : `${a}–${b}`;
  return a != null ? `${a}–` : `–${b}`;
}

function plural(n, one, many) {
  return `${Number(n).toLocaleString('en-US')} ${Number(n) === 1 ? one : (many || `${one}s`)}`;
}

function initials(user) {
  const src = (user && (user.display_name || user.email)) || '?';
  const parts = src.replace(/@.*/, '').replace(/\([^)]*\)/g, ' ')
    .split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  return ((parts[0] || '?')[0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
}

function relTime(ts) {
  if (!ts) return '';
  const d = ts instanceof Date ? ts : new Date(ts);
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} days ago`;
  return d.toISOString().slice(0, 10);
}

module.exports = {
  MONTHS, PRECISION_WORDS, dateStr, dateLabel, dateRange, captureDate, physicalRef,
  yearSpan, plural, initials, relTime,
};
