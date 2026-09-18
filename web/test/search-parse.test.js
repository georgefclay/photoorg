// Query parser (services/search-parse.js) — pure, no database.
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseQuery, describeQuery } = require('../services/search-parse');

function terms(q) { return parseQuery(q).terms; }
function dateOf(q) {
  const t = terms(q);
  assert.equal(t.length, 1, `expected one term for ${JSON.stringify(q)}, got ${t.length}`);
  return t[0].date;
}

test('a bare year, a range, a decade and an open end', () => {
  assert.deepEqual(dateOf('1962'), { from: '1962-01-01', to: '1962-12-31', label: '1962' });
  assert.deepEqual(dateOf('1962-1965'), { from: '1962-01-01', to: '1965-12-31', label: '1962–1965' });
  assert.deepEqual(dateOf('1965-1962'), { from: '1962-01-01', to: '1965-12-31', label: '1962–1965' });
  assert.deepEqual(dateOf('1962 to 1965'), { from: '1962-01-01', to: '1965-12-31', label: '1962–1965' });
  assert.deepEqual(dateOf('1960s'), { from: '1960-01-01', to: '1969-12-31', label: 'the 1960s' });
  assert.deepEqual(dateOf('60s'), { from: '1960-01-01', to: '1969-12-31', label: 'the 1960s' });
  assert.deepEqual(dateOf('the sixties'), { from: '1960-01-01', to: '1969-12-31', label: 'the 1960s' });
  assert.deepEqual(dateOf('before 1950'), { from: null, to: '1949-12-31', label: 'before 1950' });
  assert.deepEqual(dateOf('after 1980'), { from: '1981-01-01', to: null, label: 'after 1980' });
  assert.deepEqual(dateOf('since 1980'), { from: '1981-01-01', to: null, label: 'after 1980' });
  assert.deepEqual(dateOf('March 1962'), { from: '1962-03-01', to: '1962-03-31', label: 'March 1962' });
  assert.deepEqual(dateOf('1962-03'), { from: '1962-03-01', to: '1962-03-31', label: 'March 1962' });
  assert.deepEqual(dateOf('summer 1971'), { from: '1971-01-01', to: '1971-12-31', label: '1971' });
  // February in a leap year and a non-leap year.
  assert.equal(dateOf('February 1960').to, '1960-02-29');
  assert.equal(dateOf('February 1961').to, '1961-02-28');
});

test('a word that is also a name stays a word ("May", "June", "Rose")', () => {
  for (const name of ['May', 'June', 'Rose', 'April']) {
    const t = terms(name);
    assert.equal(t.length, 1);
    assert.equal(t[0].date, null, `${name} must not parse as a date on its own`);
    assert.equal(t[0].text, name);
  }
  // With a year next to it, it IS a date.
  assert.deepEqual(dateOf('May 1962'), { from: '1962-05-01', to: '1962-05-31', label: 'May 1962' });
});

test('quoted phrases stay whole and are never read as dates', () => {
  const t = terms('"the lake" 1962');
  assert.equal(t.length, 2);
  assert.equal(t[0].text, 'the lake');
  assert.equal(t[0].quoted, true);
  assert.equal(t[0].date, null);
  assert.equal(t[1].date.label, '1962');
  // A quoted year is a phrase, not a date filter.
  const q = terms('"1962"');
  assert.equal(q[0].quoted, true);
  assert.equal(q[0].date, null);
});

test('nonsense stays free text, and nothing throws', () => {
  for (const q of ['', '   ', 'xyzzy', '???', '1', '12345', '99999', 'before', 'after 20',
    '1962-', '-1962', 'a'.repeat(500), '"unclosed', 'peggy & chuck']) {
    const parsed = parseQuery(q);
    assert.ok(Array.isArray(parsed.terms));
    for (const t of parsed.terms) assert.equal(typeof t.text, 'string');
  }
  assert.equal(terms('xyzzy')[0].date, null);
  assert.equal(terms('before')[0].text, 'before');
  assert.equal(terms('1')[0].date, null);
  assert.equal(terms('12345')[0].date, null);
});

test('several terms keep their order and are capped', () => {
  const t = terms('peggy porch 1962');
  assert.deepEqual(t.map((x) => x.text), ['peggy', 'porch', '1962']);
  assert.deepEqual(t.map((x) => x.ix), [0, 1, 2]);
  const many = parseQuery('a b c d e f g h i j k');
  assert.equal(many.terms.length, 8);
  assert.equal(many.truncated, true);
});

test('filler in front of a date joins the date term', () => {
  const t = terms('peggy in the 60s');
  assert.equal(t.length, 2);
  assert.equal(t[0].text, 'peggy');
  assert.equal(t[1].text, 'in the 60s');
  assert.deepEqual(t[1].date, { from: '1960-01-01', to: '1969-12-31', label: 'the 1960s' });
});

test('describeQuery says what we understood', () => {
  assert.equal(describeQuery(parseQuery('peggy before 1950')), 'peggy + before 1950');
});
