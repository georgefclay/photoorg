// Deliberately small search (answer C4) — Phase 11 replaces it with
// nickname / Metaphone / tolerant dates. Today: people by name, photos by
// free text (description, comments, back transcriptions via search_tsv;
// batch / folder names; names of tagged people) plus the grid filters.

const { listPhotos, parseFilters } = require('./photos');
const { autocompletePeople } = require('./people');

function hasAnyCriteria(f) {
  return Boolean(f.q || f.year_from != null || f.year_to != null || f.person_id != null
    || f.place_id != null || f.album_id != null || f.has_no_date || f.has_untagged_faces);
}

async function search(pool, user, query, { scope = null, cursor = null, limit = 40 } = {}) {
  const f = parseFilters(query);
  // Search exposes these filters only.
  const filters = {
    q: f.q, year_from: f.year_from, year_to: f.year_to, person_id: f.person_id,
    place_id: f.place_id, album_id: f.album_id,
    has_no_date: f.has_no_date, has_untagged_faces: f.has_untagged_faces,
  };
  if (!hasAnyCriteria(filters)) return { filters, people: [], photos: { items: [], next: null }, empty: true };
  const [people, photos] = await Promise.all([
    f.q && !cursor ? autocompletePeople(pool, f.q) : Promise.resolve([]),
    listPhotos(pool, user, { filters, sort: 'recent', cursor, limit, scope }),
  ]);
  return { filters, people, photos, empty: false };
}

module.exports = { search, hasAnyCriteria };
