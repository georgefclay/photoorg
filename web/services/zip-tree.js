// Phase 12 — where a photo goes inside a download zip (and inside the
// desktop's full export, which ports this file to
// desktop/src/photoarchive/export_tree.py). Pure: no database, no I/O.
//
// Both ports are tested against the same fixtures,
// shared/zip-tree-fixtures.json, so the two trees cannot drift.
//
// The rules (PROJECT-PLAN §2, spec §11):
//   * a date counts only when `capture_date_confirmed` — a pending
//     suggestion is not a date;
//   * exact / month → YYYY/MM/;  year → YYYY/ (top of the year folder —
//     never invent a January);  decade → 1960s/;  anything else → Unsorted/;
//   * file names `{YYYY-MM-DD|YYYY-MM|YYYY|1960s|undated}_{id:08d}.{ext}`:
//     sortable, unique by id, and the embedded metadata carries the rest;
//   * People/<common name>/<same file name> for the optional parallel tree.

const UNSORTED = 'Unsorted';
const MAX_FOLDER = 80;

function normExt(ext) {
  let e = String(ext || '').trim().toLowerCase().replace(/^\.+/, '');
  if (e === 'jpeg') e = 'jpg';
  if (e === 'tiff') e = 'tif';
  return e || 'jpg';
}

function pad8(id) { return String(id).padStart(8, '0'); }

// { dir, stem } for one photo. `photo` = { id, capture_date ('YYYY-MM-DD'
// or a Date), precision, confirmed }.
function placement(photo) {
  const d = dateString(photo.capture_date);
  const m = photo.confirmed && d ? /^(\d{4})-(\d{2})-(\d{2})/.exec(d) : null;
  if (m) {
    const [, y, mo, day] = m;
    switch (photo.precision) {
      case 'exact': return { dir: `${y}/${mo}`, stem: `${y}-${mo}-${day}` };
      case 'month': return { dir: `${y}/${mo}`, stem: `${y}-${mo}` };
      case 'year': return { dir: y, stem: y };
      case 'decade': {
        const dec = `${Math.floor(Number(y) / 10) * 10}s`;
        return { dir: dec, stem: dec };
      }
      default: break;
    }
  }
  return { dir: UNSORTED, stem: 'undated' };
}

function dateString(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) {
    // node-pg hands `date` columns over as local-midnight Dates.
    const y = v.getFullYear();
    const mo = String(v.getMonth() + 1).padStart(2, '0');
    const d = String(v.getDate()).padStart(2, '0');
    return `${y}-${mo}-${d}`;
  }
  return String(v);
}

function fileName(photo) {
  return `${placement(photo).stem}_${pad8(photo.id)}.${normExt(photo.ext)}`;
}

// Path inside the zip, forward slashes.
function photoPath(photo) {
  return `${placement(photo).dir}/${fileName(photo)}`;
}

function peoplePath(photo, folder) {
  return `People/${folder}/${fileName(photo)}`;
}

// ---- People folder names ------------------------------------------------

function clean(s) { return String(s == null ? '' : s).split(/\s+/).filter(Boolean).join(' '); }
function join(...parts) { return parts.map(clean).filter(Boolean).join(' '); }

// The Phase 13 keyword form: nickname-or-given + surname + suffix.
function commonName(p) {
  const first = clean(p.nickname) || clean(p.given_name);
  return join(first, p.surname, p.suffix) || clean(p.display_name) || `Person ${p.id}`;
}

const RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$/i;

// A folder name every desktop OS will accept.
function sanitizeFolder(name) {
  let s = clean(name)
    // eslint-disable-next-line no-control-regex
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .slice(0, MAX_FOLDER)
    .replace(/[ .]+$/, '');
  if (RESERVED.test(s)) s += '_';
  return s;
}

// people: [{ id, given_name, nickname, surname, suffix, display_name,
// birth_year }] → Map(id → folder name), unique case-insensitively (the
// zip will be unpacked on Windows). A collision gets " (b. YYYY)", and
// anything still colliding gets " #id".
function peopleFolders(people) {
  const base = new Map();
  for (const p of people) {
    base.set(Number(p.id), sanitizeFolder(commonName(p)) || `Person ${p.id}`);
  }
  const out = new Map(base);
  const groups = (m) => {
    const g = new Map();
    for (const [id, name] of m) {
      const k = name.toLowerCase();
      if (!g.has(k)) g.set(k, []);
      g.get(k).push(id);
    }
    return [...g.values()].filter((ids) => ids.length > 1);
  };
  const byId = new Map(people.map((p) => [Number(p.id), p]));
  for (const ids of groups(base)) {
    for (const id of ids) {
      const by = byId.get(id).birth_year;
      out.set(id, by ? `${base.get(id)} (b. ${by})` : `${base.get(id)} #${id}`);
    }
  }
  for (const ids of groups(out)) {
    for (const id of ids) out.set(id, `${base.get(id)} #${id}`);
  }
  return out;
}

module.exports = {
  UNSORTED, normExt, placement, fileName, photoPath, peoplePath,
  commonName, sanitizeFolder, peopleFolders,
};
