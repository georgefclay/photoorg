// View helpers for the photo detail page (/photos/:id). The photo itself,
// its visibility and its neighbours come from services/photos.js; this file
// only turns that data into what the page shows: suggestion sentences,
// face-box geometry, the "Back to …" link for the list the viewer came from.
//
// People and places are site-wide (not visibility-gated), so looking up the
// names a suggestion refers to is safe here.

const fmt = require('./format');

const toNum = (v) => {
  const n = Number(v);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
};

function newPersonName(np) {
  if (!np) return null;
  if (typeof np === 'string') return np.trim() || null;
  const parts = [np.given_name, np.surname].map((s) => String(s || '').trim()).filter(Boolean);
  if (parts.length) return parts.join(' ');
  return String(np.display_name || np.name || '').trim() || null;
}

// Pending suggestions → [{ id, kind, ai, mine, who, text, face_id }].
// `who` is "You", "AI", "Someone" or (for admins/moderators) a name.
async function describeSuggestions(pool, suggestions) {
  const personIds = new Set();
  const placeIds = new Set();
  for (const s of suggestions) {
    const p = s.payload || {};
    if (s.kind === 'person' && toNum(p.person_id)) personIds.add(toNum(p.person_id));
    if (s.kind === 'place' && toNum(p.place_id)) placeIds.add(toNum(p.place_id));
  }
  const [people, places] = await Promise.all([
    personIds.size
      ? pool.query(`select id, display_name from people where id = any($1::bigint[])`, [[...personIds]])
      : { rows: [] },
    placeIds.size
      ? pool.query(`select id, name from places where id = any($1::bigint[])`, [[...placeIds]])
      : { rows: [] },
  ]);
  const personName = new Map(people.rows.map((r) => [Number(r.id), r.display_name]));
  const placeName = new Map(places.rows.map((r) => [Number(r.id), r.name]));

  const out = [];
  for (const s of suggestions) {
    const p = s.payload || {};
    let text = null;
    // Read as "<who> suggested <text>".
    if (s.kind === 'date') {
      text = fmt.dateLabel(p.date, p.precision);
    } else if (s.kind === 'person') {
      const name = toNum(p.person_id) ? personName.get(toNum(p.person_id)) : newPersonName(p.new_person);
      text = name ? `${name}${p.face_id ? ' for a face' : ' is in this photo'}` : null;
    } else if (s.kind === 'place') {
      const name = toNum(p.place_id) ? placeName.get(toNum(p.place_id)) : newPersonName(p.new_place);
      text = name ? `${name} as the place` : null;
    } else if (s.kind === 'description') {
      const t = String(p.text || '').trim();
      text = t ? `the description “${t.length > 200 ? `${t.slice(0, 200)}…` : t}”` : null;
    }
    if (!text) continue;
    const ai = s.source === 'ai';
    const who = s.mine ? 'You' : ai ? 'AI'
      : (s.author_display_name === 'someone' ? 'Someone' : s.author_display_name);
    out.push({
      id: s.id, kind: s.kind, ai, mine: !!s.mine, who, text,
      face_id: toNum(p.face_id),
    });
  }
  return out;
}

// Face box → percentage geometry inside the photo's display frame.
// null when the photo has no dimensions or the bbox is unusable.
function faceBoxStyle(bbox, width, height) {
  if (!bbox || !width || !height) return null;
  const x = Number(bbox.x), y = Number(bbox.y), w = Number(bbox.w), h = Number(bbox.h);
  if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return null;
  const pct = (v, of) => `${Math.max(0, Math.min(100, (v / of) * 100)).toFixed(3)}%`;
  return `left:${pct(x, width)};top:${pct(y, height)};width:${pct(w, width)};height:${pct(h, height)}`;
}

// List key (services/photos.parseListKey) → where "Back" goes.
function backLinkFor(fromKey) {
  const [kind, arg] = String(fromKey || '').split('.');
  const sortQ = (extra) => {
    const q = new URLSearchParams(extra);
    if (arg && arg !== 'recent') q.set('sort', arg);
    const s = q.toString();
    return s ? `/?${s}` : '/';
  };
  switch (kind) {
    case 'nd': return { href: sortQ({ has_no_date: '1' }), label: 'Photos with no date' };
    case 'ut': return { href: sortQ({ has_untagged_faces: '1' }), label: 'Photos with untagged faces' };
    case 'wi': return { href: sortQ({ has_unknown_faces: '1' }), label: 'Photos with unnamed faces' };
    case 'p': return toNum(arg) ? { href: `/people/${toNum(arg)}`, label: 'Back to person' } : { href: '/', label: 'Photos' };
    case 'a': return toNum(arg) ? { href: `/albums/${toNum(arg)}`, label: 'Back to album' } : { href: '/', label: 'Photos' };
    case 'b': return { href: sortQ({}), label: 'Photos' };
    default: return { href: '/', label: 'Photos' };
  }
}

// Short alt text: "Photo of Peggy Clay and Chuck Clay, March 1962".
function altText(photo) {
  const d = fmt.captureDate(photo);
  const names = photo.people.map((p) => p.display_name).filter(Boolean);
  let who = '';
  if (names.length === 1) who = ` of ${names[0]}`;
  else if (names.length > 1) who = ` of ${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  const when = d.text ? `, ${d.guess ? 'about ' : ''}${d.text}` : ', date unknown';
  return `Photo${who}${when}`;
}

module.exports = { describeSuggestions, faceBoxStyle, backLinkFor, altText, newPersonName };
