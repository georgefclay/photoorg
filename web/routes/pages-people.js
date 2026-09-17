// People (/people, /people/:id), Albums (/albums, /albums/:id), Search (/search).
//
// People and album LISTS are site-wide (answer D6); every photo grid and
// photo count goes through services/photos.js + services/people-pages.js,
// so visibility and the header's group scope always apply. Albums are
// read-only on the web (answer C5). Search is deliberately small (C4).
const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope } = require('../services/scope');
const { listPhotos, parseFilters } = require('../services/photos');
const { listPeople, getPerson } = require('../services/people');
const { listAlbums, getAlbum } = require('../services/albums');
const { search, hasAnyCriteria } = require('../services/search');
const pp = require('../services/people-pages');

const PEOPLE_PAGE = 100;
const PHOTO_PAGE = 60;

function toId(v) {
  const n = Number(v);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

function notFound(res, what) {
  return res.status(404).render('error', {
    title: 'Not found',
    message: `That ${what} does not exist, or it has been removed.`,
  });
}

// Canonical search query string (no cursor) from effective filters.
function searchQueryString(f) {
  const q = new URLSearchParams();
  if (f.q) q.set('q', f.q);
  if (f.year_from != null) q.set('year_from', String(f.year_from));
  if (f.year_to != null) q.set('year_to', String(f.year_to));
  if (f.person_id != null) q.set('person_id', String(f.person_id));
  if (f.place_id != null) q.set('place_id', String(f.place_id));
  if (f.album_id != null) q.set('album_id', String(f.album_id));
  if (f.has_no_date) q.set('has_no_date', '1');
  if (f.has_untagged_faces) q.set('has_untagged_faces', '1');
  return q;
}

module.exports = function peoplePageRoutes({ pool }) {
  const router = express.Router();

  // ---- People list --------------------------------------------------------
  router.get('/people', requireUser, async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const q = String(req.query.q || '').trim().slice(0, 100);
      const cursor = req.query.cursor ? String(req.query.cursor) : null;
      const people = await listPeople(pool, req.user, { q, cursor, limit: PEOPLE_PAGE, scope });
      const base = new URLSearchParams();
      if (q) base.set('q', q);
      const more = new URLSearchParams(base);
      if (people.next) more.set('cursor', people.next);
      const api = new URLSearchParams(base);
      api.set('limit', String(PEOPLE_PAGE));
      res.render('people', {
        people,
        q,
        groupScope: scope,
        isContinuation: Boolean(cursor),
        startUrl: `/people${q ? `?${base}` : ''}`,
        moreUrl: `/people?${more}`,
        apiUrl: `/api/people?${api}`,
        lifeSpan: pp.lifeSpan,
      });
    } catch (err) { next(err); }
  });

  // ---- Person page ---------------------------------------------------------
  async function renderPerson(req, res, id, relForm = {}) {
    const person = await getPerson(pool, id);
    if (!person) return notFound(res, 'person');
    const { scope } = await getScope(req, pool);
    const cursor = req.method === 'GET' && req.query.cursor ? String(req.query.cursor) : null;
    const [photos, photoCount, pending] = await Promise.all([
      listPhotos(pool, req.user, { filters: { person_id: id }, cursor, limit: PHOTO_PAGE, scope }),
      pp.countPhotos(pool, req.user, { filters: { person_id: id }, scope }),
      pp.describePendingRelationships(pool, id, person.pending_relationship_suggestions),
    ]);
    const more = new URLSearchParams();
    if (photos.next) more.set('cursor', photos.next);
    const status = relForm.status || 200;
    res.status(status).render('person', {
      person,
      photos,
      photoCount,
      pending,
      groupScope: scope,
      isContinuation: Boolean(cursor),
      from: `p.${id}`,
      apiUrl: `/api/people/${id}?limit=${PHOTO_PAGE}`,
      moreUrl: `/people/${id}?${more}#photos`,
      relations: pp.RELATIONS,
      relForm: {
        sent: req.method === 'GET' ? String(req.query.rel || '') : '',
        ...relForm,
      },
      lifeSpan: pp.lifeSpan(person),
    });
  }

  router.get('/people/:id(\\d+)', requireUser, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      if (!id) return notFound(res, 'person');
      await renderPerson(req, res, id);
    } catch (err) { next(err); }
  });

  // No-JS relationship suggestion. JS posts JSON to /api/relationships instead.
  router.post('/people/:id(\\d+)/relationships', requireUser, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      if (!id) return notFound(res, 'person');
      const b = req.body || {};
      const relation = String(b.relation || '');
      const otherName = String(b.other_name || '').trim().slice(0, 100);
      const keep = { relation, other_name: otherName };
      if (!pp.RELATIONS[relation]) {
        return renderPerson(req, res, id, { ...keep, status: 422, error: 'Choose how they are related.' });
      }
      // other_id can arrive twice (hidden field + "Which one?" radios).
      let otherId = [].concat(b.other_id || []).map(toId).filter(Boolean).pop() || null;
      if (!otherId) {
        if (!otherName) {
          return renderPerson(req, res, id, { ...keep, status: 422, error: 'Type the name of the other person.' });
        }
        const found = await pp.resolvePersonByName(pool, otherName);
        const candidates = found.candidates.filter((c) => c.id !== id);
        if (!found.person || found.person.id === id) {
          return renderPerson(req, res, id, {
            ...keep,
            status: 422,
            candidates,
            error: candidates.length
              ? 'Which person did you mean?'
              : `We couldn't find anyone called “${otherName}”. Only people already in the archive can be linked.`,
          });
        }
        otherId = found.person.id;
      }
      if (otherId === id) {
        return renderPerson(req, res, id, { ...keep, status: 422, error: 'Pick someone other than this person.' });
      }
      const row = pp.relationToRow(id, otherId, relation);
      const r = await pp.suggestRelationship(pool, req.user, row);
      if (r.error) {
        return renderPerson(req, res, id, { ...keep, status: 422, error: "We couldn't find that person. Please pick from the list." });
      }
      const flag = r.already ? 'known' : r.duplicate ? 'dup' : 'sent';
      res.redirect(303, `/people/${id}?rel=${flag}#suggest`);
    } catch (err) { next(err); }
  });

  // ---- Albums ---------------------------------------------------------------
  router.get('/albums', requireUser, async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const albums = await listAlbums(pool, req.user, scope);
      res.render('albums', { albums, groupScope: scope });
    } catch (err) { next(err); }
  });

  router.get('/albums/:id(\\d+)', requireUser, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      const album = id ? await getAlbum(pool, id) : null;
      if (!album) return notFound(res, 'album');
      const { scope } = await getScope(req, pool);
      const cursor = req.query.cursor ? String(req.query.cursor) : null;
      const [photos, photoCount] = await Promise.all([
        listPhotos(pool, req.user, { filters: { album_id: id }, sort: 'position', cursor, limit: PHOTO_PAGE, scope }),
        pp.countPhotos(pool, req.user, { filters: { album_id: id }, scope }),
      ]);
      const more = new URLSearchParams();
      if (photos.next) more.set('cursor', photos.next);
      res.render('album', {
        album,
        photos,
        photoCount,
        groupScope: scope,
        isContinuation: Boolean(cursor),
        from: `a.${id}`,
        apiUrl: `/api/albums/${id}?limit=${PHOTO_PAGE}`,
        moreUrl: `/albums/${id}?${more}`,
      });
    } catch (err) { next(err); }
  });

  // ---- Search ---------------------------------------------------------------
  router.get('/search', requireUser, async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const raw = req.query || {};
      const f = parseFilters(raw);
      if (f.year_from != null && f.year_to != null && f.year_from > f.year_to) {
        [f.year_from, f.year_to] = [f.year_to, f.year_from];
      }
      const notes = [];

      // Person: hidden id from the autocomplete, or a typed name (no JS).
      let person = null;
      let personSuggestions = [];
      // A no-JS form always sends the text field next to the stale hidden id
      // (search.js disables the text once an id is picked): an emptied or
      // edited name wins over the id.
      const personText = String(raw.person || '').trim().slice(0, 100);
      if (f.person_id != null && raw.person != null) {
        const current = await pp.personName(pool, f.person_id);
        if (!current || current.display_name !== personText) f.person_id = null;
      }
      if (raw.place != null && f.place_id != null) {
        const current = await pp.placeName(pool, f.place_id);
        if (!current || current.name !== String(raw.place).trim()) f.place_id = null;
      }
      if (f.person_id != null) {
        person = await pp.personName(pool, f.person_id);
        if (!person) f.person_id = null;
      } else if (personText) {
        const found = await pp.resolvePersonByName(pool, personText);
        if (found.person) { person = found.person; f.person_id = person.id; }
        else {
          personSuggestions = found.candidates;
          notes.push(found.candidates.length
            ? `More than one person matches “${personText}”. Pick one below.`
            : `We couldn't find anyone called “${personText}”, so that filter was left out.`);
        }
      }
      // Place: same pattern.
      let place = null;
      const placeText = String(raw.place || '').trim().slice(0, 100);
      if (f.place_id != null) {
        place = await pp.placeName(pool, f.place_id);
        if (!place) f.place_id = null;
      } else if (placeText) {
        place = await pp.resolvePlaceByName(pool, placeText);
        if (place) f.place_id = place.id;
        else notes.push(`We couldn't find a place called “${placeText}”, so that filter was left out.`);
      }

      const qs = searchQueryString(f);
      const query = Object.fromEntries(qs);
      const cursor = raw.cursor ? String(raw.cursor) : null;
      const [result, albums] = await Promise.all([
        search(pool, req.user, query, { scope, cursor, limit: PHOTO_PAGE }),
        listAlbums(pool, req.user, scope),
      ]);
      const any = hasAnyCriteria(result.filters);
      const photoCount = any && !cursor
        ? await pp.countPhotos(pool, req.user, { filters: result.filters, scope })
        : null;
      const api = new URLSearchParams(qs);
      api.set('limit', String(PHOTO_PAGE));
      const more = new URLSearchParams(qs);
      if (result.photos.next) more.set('cursor', result.photos.next);
      const advanced = f.year_from != null || f.year_to != null || f.person_id != null
        || f.place_id != null || f.album_id != null || f.has_no_date || f.has_untagged_faces
        || Boolean(personText) || Boolean(placeText);
      res.render('search', {
        searchQuery: f.q || '',
        filters: f,
        person,
        personText: person ? person.display_name : personText,
        personSuggestions,
        place,
        placeText: place ? place.name : placeText,
        albums,
        result,
        any,
        advanced,
        notes,
        photoCount,
        groupScope: scope,
        isContinuation: Boolean(cursor),
        startUrl: `/search?${qs}`,
        apiUrl: `/api/search?${api}`,
        moreUrl: `/search?${more}`,
        suggestionUrl: (pid) => { const s = new URLSearchParams(qs); s.set('person_id', String(pid)); return `/search?${s}`; },
      });
    } catch (err) { next(err); }
  });

  return router;
};
