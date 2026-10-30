// Phase 12 — the zip tree, against the fixtures the desktop's port reads
// too (shared/zip-tree-fixtures.json). No database.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const tree = require('../services/zip-tree');

const FIX = require(path.join(__dirname, '..', '..', 'shared', 'zip-tree-fixtures.json'));

for (const c of FIX.paths) {
  test(`path: ${c.name}`, () => {
    assert.equal(tree.photoPath(c.in), c.out);
  });
}

for (const c of FIX.people_folders) {
  test(`people folders: ${c.name}`, () => {
    const got = Object.fromEntries([...tree.peopleFolders(c.in)].map(([k, v]) => [String(k), v]));
    assert.deepEqual(got, c.out);
  });
}

for (const c of FIX.people_paths) {
  test(`people path: ${c.name}`, () => {
    assert.equal(tree.peoplePath(c.in.photo, c.in.folder), c.out);
  });
}

test('a Date from node-pg places the same as its string', () => {
  const p = { id: 1, ext: 'jpg', capture_date: new Date(1962, 6, 4), precision: 'exact', confirmed: true };
  assert.equal(tree.photoPath(p), '1962/07/1962-07-04_00000001.jpg');
});

test('file names sort by date and are unique by id', () => {
  const ps = [
    { id: 3, capture_date: '1962-07-04', precision: 'exact', confirmed: true },
    { id: 2, capture_date: '1962-07-04', precision: 'exact', confirmed: true },
    { id: 1, capture_date: '1962-08-01', precision: 'exact', confirmed: true },
  ].map((p) => tree.fileName({ ...p, ext: 'jpg' }));
  assert.equal(new Set(ps).size, 3);
  assert.deepEqual([...ps].sort(), [ps[1], ps[0], ps[2]]);
});
