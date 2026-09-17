// Photo detail page (/photos/:id) and the JSON flows its scripts use.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPhoto, addToGroup,
} = require('./page-helpers');
const { backLinkFor, faceBoxStyle } = require('../services/photo-page');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

test('member sees the photo page with caption, like, comments, actions and the back', async () => {
  const w = await seedWorld();
  await pool.query(`insert into likes (user_id, photo_id) values ($1, $2), ($3, $2)`, [w.bob.id, w.photos.clay1, w.admin.id]);
  await pool.query(`insert into comments (photo_id, user_id, body) values ($1, $2, 'Easter at Nana''s <3')`, [w.photos.clay1, w.bob.id]);
  const alice = await agentFor(app, w.alice);
  const res = await alice.get(`/photos/${w.photos.clay1}`);
  assert.equal(res.status, 200);
  const t = res.text;
  assert.match(t, /<title>Photo, March 1962 · Cyber Dinosaurs<\/title>/);
  assert.match(t, new RegExp(`src="/media/display/${w.photos.clay1}"`));
  assert.match(t, /width="1200" height="800"/, 'no layout shift');
  assert.match(t, /alt="Photo of Clay[^"]*March 1962"|alt="Photo of [^"]+, March 1962"/);
  assert.match(t, /<strong>March 1962<\/strong>\s*<span class="chip date-mark confirmed">/);
  assert.match(t, new RegExp(`href="/people/${w.people.peggy.id}">[^<]+</a>`), 'people chip → person page');
  assert.match(t, /Print:<\/span> Batch 00012 #017/);
  assert.match(t, /Toronto/);
  assert.match(t, /data-like-count[^>]*>2<\/span>/);
  assert.match(t, /aria-pressed="false"/);
  assert.match(t, /Easter at Nana&#39;s &lt;3/, 'comment body escaped');
  assert.match(t, /data-comment-form/);
  assert.match(t, /Tag a face/);
  assert.match(t, /Suggest a date/);
  assert.match(t, /Suggest a place/);
  assert.doesNotMatch(t, /Add to album/);
  assert.doesNotMatch(t, /Rescan wanted/, 'admin only');
  assert.match(t, /Peggy and Chuck, Easter 1962/, 'back transcription');
  assert.match(t, /\/media\/backs\/\d+/);
  assert.match(t, /Who is this\? \(1\)/, 'unknown face invites naming');
  assert.match(t, new RegExp(`data-face-id="${w.faces.unknown}" data-state="unknown"`));
  assert.match(t, new RegExp(`data-face-id="${w.faces.untagged}" data-state="unnamed"`));
  assert.match(t, new RegExp(`data-face-id="${w.faces.peggy}" data-state="named"`));
  assert.match(t, /left:8\.333%;top:12\.500%;width:12\.500%;height:18\.750%/, 'peggy box in percent of 1200×800');
  assert.match(t, /<strong>You<\/strong> suggested April 1962/, 'own pending suggestion');
  // Contributors don't see moderation tools.
  assert.doesNotMatch(t, /Shared with/);
  assert.doesNotMatch(t, /data-remove-group/);
  assert.doesNotMatch(t, /data-comment-toggle/);
  for (const js of ['/js/photo.js', '/js/tagger.js', '/js/like.js', '/js/datefield.js', '/js/autocomplete.js', '/css/photo.css']) {
    assert.ok(t.includes(js), js);
    assert.equal((await request(app).get(js)).status, 200, js);
  }
});

test('guess dates show the range and "Help us date this"', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const res = await alice.get(`/photos/${w.photos.clay2}`);
  assert.equal(res.status, 200);
  assert.match(res.text, /<strong>1970–1979<\/strong>\s*<span class="chip date-mark guess">Guess<\/span>/);
  assert.match(res.text, /Help us date this/);
});

test('anonymous → /login; non-member, private, unfiled and missing → 404', async () => {
  const w = await seedWorld();
  const anon = await request(app).get(`/photos/${w.photos.clay1}`);
  assert.equal(anon.status, 302);
  assert.equal(anon.headers.location, '/login');

  const carol = await agentFor(app, w.carol);
  const r1 = await carol.get(`/photos/${w.photos.clay1}`);
  assert.equal(r1.status, 404);
  assert.match(r1.text, /Not found/);
  assert.doesNotMatch(r1.text, /Batch 00012/);

  const alice = await agentFor(app, w.alice);
  assert.equal((await alice.get(`/photos/${w.photos.priv}`)).status, 404, 'private');
  assert.equal((await alice.get(`/photos/${w.photos.unfiled}`)).status, 404, 'unfiled as contributor');
  assert.equal((await alice.get('/photos/999999')).status, 404, 'missing');
  assert.equal((await alice.get('/photos/99999999999999999999999')).status, 404, 'absurd id');

  const admin = await agentFor(app, w.admin);
  assert.equal((await admin.get(`/photos/${w.photos.priv}`)).status, 404, 'private even for admin');
  assert.equal((await admin.get(`/photos/${w.photos.unfiled}`)).status, 200, 'admin sees unfiled');
});

test('admin sees Rescan wanted and the groups strip', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const res = await admin.get(`/photos/${w.photos.clay1}`);
  assert.equal(res.status, 200);
  assert.match(res.text, /Rescan wanted/);
  assert.match(res.text, /data-rescan[^>]*aria-pressed="false"/);
  assert.match(res.text, /Shared with/);
  assert.match(res.text, new RegExp(`data-url="/api/groups/${w.clay}/photos/${w.photos.clay1}/remove"`));
  assert.match(res.text, /Remove from group/);
  assert.match(res.text, /<strong>Alice<\/strong> suggested April 1962/, 'admins see who suggested');

  const unfiled = await admin.get(`/photos/${w.photos.unfiled}`);
  assert.match(unfiled.text, /Not in any group yet/);
});

test('moderator sees groups strip with remove, and comment hide controls incl. hidden comments', async () => {
  const w = await seedWorld();
  await pool.query(
    `insert into comments (photo_id, user_id, body, is_hidden) values ($1, $2, 'visible one', false), ($1, $2, 'rude one', true)`,
    [w.photos.clay1, w.alice.id],
  );
  const bob = await agentFor(app, w.bob);
  const res = await bob.get(`/photos/${w.photos.clay1}`);
  assert.equal(res.status, 200);
  assert.match(res.text, /Shared with/);
  assert.match(res.text, /Remove from my group/);
  assert.match(res.text, /rude one/);
  assert.match(res.text, /class="comment is-hidden"/);
  assert.match(res.text, />Unhide</);
  assert.match(res.text, />Hide</);
  assert.doesNotMatch(res.text, /Rescan wanted/);

  const alice = await agentFor(app, w.alice);
  const ares = await alice.get(`/photos/${w.photos.clay1}`);
  assert.match(ares.text, /visible one/);
  assert.doesNotMatch(ares.text, /rude one/);
  assert.doesNotMatch(ares.text, /Remove from my group/);
});

test('renders with empty data: no faces, comments, backs, places, date or dimensions', async () => {
  const w = await seedWorld();
  const bare = await insertPhoto({ width: null, height: null, scan_batch: null });
  await addToGroup(bare, w.clay);
  const alice = await agentFor(app, w.alice);
  const res = await alice.get(`/photos/${bare}`);
  assert.equal(res.status, 200);
  assert.match(res.text, /No date yet/);
  assert.match(res.text, /No comments yet/);
  assert.match(res.text, /data-like-count[^>]*>0<\/span>/);
  assert.doesNotMatch(res.text, /On the back/);
  assert.doesNotMatch(res.text, /Print:/);
  assert.doesNotMatch(res.text, /data-face-layer/);
  assert.match(res.text, /data-pending[^>]*hidden/);
});

test('prev/next links follow ?from=, fall back to Browse, and Back maps the list key', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const res = await alice.get(`/photos/${w.photos.clay1}?from=b.recent`);
  assert.equal(res.status, 200);
  assert.match(res.text, new RegExp(`rel="prev" href="/photos/${w.photos.clay2}\\?from=b\\.recent"`));
  assert.doesNotMatch(res.text, /rel="next"/);
  assert.match(res.text, /data-next=""/);
  const r2 = await alice.get(`/photos/${w.photos.clay2}?from=b.recent`);
  assert.match(r2.text, new RegExp(`rel="next" href="/photos/${w.photos.clay1}\\?from=b\\.recent"`));

  // Not in the named list (clay1 has a confirmed date) → Browse order.
  const nd = await alice.get(`/photos/${w.photos.clay1}?from=nd.liked`);
  assert.match(nd.text, new RegExp(`rel="prev" href="/photos/${w.photos.clay2}\\?from=b\\.recent"`));
  const album = await alice.get(`/photos/${w.photos.clay1}?from=a.${w.album}`);
  assert.match(album.text, new RegExp(`href="/albums/${w.album}"`));
  assert.match(album.text, new RegExp(`rel="prev" href="/photos/${w.photos.clay2}\\?from=a\\.${w.album}"`));

  assert.deepEqual(backLinkFor('nd.liked'), { href: '/?has_no_date=1&sort=liked', label: 'Photos with no date' });
  assert.equal(backLinkFor('ut.recent').href, '/?has_untagged_faces=1');
  assert.equal(backLinkFor('wi.oldest').href, '/?has_unknown_faces=1&sort=oldest');
  assert.equal(backLinkFor('p.7').href, '/people/7');
  assert.equal(backLinkFor('b.newest').href, '/?sort=newest');
  assert.equal(backLinkFor('junk').href, '/');
  assert.equal(faceBoxStyle({ x: 1, y: 1, w: 0, h: 5 }, 100, 100), null);
});

test('contributor JSON flows used by the page work end to end', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const id = w.photos.clay1;
  const post = (url, body) => alice.post(url).set('X-CSRF-Token', alice.csrf).send(body);

  // Date field: interpret, then submit the text.
  const interp = await alice.get('/api/dates/interpret').query({ text: 'sometime in the 60s' });
  assert.equal(interp.body.ok, true);
  assert.match(interp.body.message, /We'll record: 1960s, precision decade/);
  const bad = await post(`/api/photos/${id}/suggestions`, { kind: 'date', text: 'the other day' });
  assert.equal(bad.status, 400);
  assert.match(bad.body.error, /couldn't understand/);
  const date = await post(`/api/photos/${id}/suggestions`, { kind: 'date', text: 'summer 1971' });
  assert.equal(date.status, 201);
  assert.equal(date.body.payload.precision, 'year');

  // Name an existing unknown face with someone new (typed name only).
  const named = await post(`/api/photos/${id}/suggestions`, { kind: 'person', face_id: w.faces.unknown, new_person: { given_name: 'Mary Ann', surname: 'Boots' } });
  assert.equal(named.status, 201);
  const split = await post(`/api/photos/${id}/suggestions`, { kind: 'person', face_id: w.faces.untagged, new_person: { display_name: 'Great Aunt Ada' } });
  assert.equal(split.status, 201);
  assert.deepEqual(
    { g: split.body.payload.new_person.given_name, s: split.body.payload.new_person.surname },
    { g: 'Great Aunt', s: 'Ada' },
  );
  const noName = await post(`/api/photos/${id}/suggestions`, { kind: 'person', face_id: w.faces.untagged, new_person: { given_name: '  ' } });
  assert.equal(noName.status, 400);
  const wrongPhoto = await post(`/api/photos/${id}/suggestions`, { kind: 'person', face_id: w.faces.bootsUnknown, person_id: w.people.chuck.id });
  assert.equal(wrongPhoto.status, 400, 'face must be on this photo');

  // Draw a missed face with a new name.
  const drawn = await post(`/api/photos/${id}/faces`, { bbox: { x: 900, y: 300, w: 80, h: 90 }, new_person: { given_name: 'Lola' } });
  assert.equal(drawn.status, 201);
  const face = (await pool.query(`select person_id, bbox, source from faces where id = $1`, [drawn.body.face_id])).rows[0];
  assert.equal(face.person_id, null, 'contributors never assign directly');
  assert.deepEqual(face.bbox, { x: 900, y: 300, w: 80, h: 90 });

  // Place: existing and new.
  assert.equal((await post(`/api/photos/${id}/suggestions`, { kind: 'place', place_id: w.place })).status, 201);
  const newPlace = await post(`/api/photos/${id}/suggestions`, { kind: 'place', new_place: { name: ' Lake  Muskoka ' } });
  assert.equal(newPlace.status, 201);
  assert.deepEqual(newPlace.body.payload.new_place, { name: 'Lake Muskoka' });

  // Dispute Peggy.
  const disp = await post(`/api/faces/${w.faces.peggy}/dispute`, { note: "That's Joan" });
  assert.equal(disp.status, 200);

  // Like toggle.
  const l1 = await post(`/api/photos/${id}/like`, {});
  assert.deepEqual({ liked: l1.body.liked, count: l1.body.count }, { liked: true, count: 1 });
  const l2 = await post(`/api/photos/${id}/like`, {});
  assert.deepEqual({ liked: l2.body.liked, count: l2.body.count }, { liked: false, count: 0 });

  // Comment.
  const c = await post(`/api/photos/${id}/comments`, { body: 'That is the old lake house!' });
  assert.equal(c.status, 201);

  // The page now reflects all of it.
  const page = await alice.get(`/photos/${id}`);
  const t = page.text;
  assert.match(t, /<strong>You<\/strong> suggested 1971/);
  assert.match(t, /<strong>You<\/strong> suggested Mary Ann Boots for a face/);
  assert.match(t, /<strong>You<\/strong> suggested Lola for a face/);
  assert.match(t, /<strong>You<\/strong> suggested Toronto as the place/);
  assert.match(t, /<strong>You<\/strong> suggested Lake Muskoka as the place/);
  assert.match(t, /That is the old lake house!/);
  assert.match(t, new RegExp(`data-face-id="${w.faces.peggy}" data-state="disputed"`));
  assert.match(t, new RegExp(`class="face-box face-unknown suggested"[^>]*data-face-id="${w.faces.unknown}"`));
  assert.doesNotMatch(t, new RegExp(`href="/people/${w.people.peggy.id}"`), 'disputed tag no longer a people chip');

  // Someone else sees "Someone", not Alice's name.
  const bobLike = await agentFor(app, w.bob); // moderator → sees names
  assert.match((await bobLike.get(`/photos/${id}`)).text, /<strong>Alice<\/strong> suggested 1971/);

  // CSRF is required for every write.
  assert.equal((await alice.post(`/api/photos/${id}/like`).send({})).status, 403);
  // Non-members can't write to the photo.
  const carol = await agentFor(app, w.carol);
  assert.equal((await carol.post(`/api/photos/${id}/like`).set('X-CSRF-Token', carol.csrf).send({})).status, 404);
});

test('moderator and admin JSON flows: hide comment, remove from group, rescan wanted', async () => {
  const w = await seedWorld();
  const cid = Number((await pool.query(
    `insert into comments (photo_id, user_id, body) values ($1, $2, 'hmm') returning id`, [w.photos.clay1, w.alice.id],
  )).rows[0].id);
  const bob = await agentFor(app, w.bob);
  const bpost = (url, body) => bob.post(url).set('X-CSRF-Token', bob.csrf).send(body || {});
  assert.equal((await bpost(`/api/comments/${cid}/hide`)).status, 200);
  const alice = await agentFor(app, w.alice);
  assert.doesNotMatch((await alice.get(`/photos/${w.photos.clay1}`)).text, />hmm</);
  assert.equal((await alice.post(`/api/comments/${cid}/unhide`).set('X-CSRF-Token', alice.csrf).send({})).status, 403);
  assert.equal((await bpost(`/api/comments/${cid}/unhide`)).status, 200);

  const rm = await bpost(`/api/groups/${w.clay}/photos/${w.photos.clay2}/remove`);
  assert.equal(rm.status, 200);
  assert.equal(rm.body.unfiled, true);
  assert.equal((await alice.get(`/photos/${w.photos.clay2}`)).status, 404, 'gone for members once unfiled');

  const admin = await agentFor(app, w.admin);
  const rs = await admin.post(`/api/photos/${w.photos.clay1}/rescan_wanted`).set('X-CSRF-Token', admin.csrf).send({ wanted: true });
  assert.equal(rs.body.rescan_wanted, true);
  assert.match((await admin.get(`/photos/${w.photos.clay1}`)).text, /data-rescan[^>]*aria-pressed="true"/);
  assert.equal((await alice.post(`/api/photos/${w.photos.clay1}/rescan_wanted`).set('X-CSRF-Token', alice.csrf).send({ wanted: false })).status, 403);
});
