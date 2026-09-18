// Query parsing for search (Phase 11). Pure — no database, no I/O — so it
// can be unit-tested exhaustively.
//
//   parseQuery('peggy porch "the lake" before 1950')
//     → { raw, terms: [ {text:'peggy'}, {text:'porch'},
//                       {text:'the lake', quoted:true},
//                       {text:'before 1950', date:{from:null,to:'1949-12-31',…}} ] }
//
// Terms are AND-ed by the search; the layers inside one term (person,
// place, date, free text) are OR-ed. A term can carry a date AND still be
// matched as text — "1962" is both a year and a word someone wrote on the
// back of a print.
//
// Dates reuse the one date parser (services/date-parse.js); the forms this
// adds on top are the ones only a search box needs: `1962-1965`,
// `1962 to 1965`, `before 1950`, `after 1980`.

const { parseDateFreetext } = require('./date-parse');
const { MONTHS } = require('./format');

const MAX_TERMS = 8;
const MAX_LEN = 200;

// Words that carry no meaning on their own in front of a date.
const LEADERS = new Set(['the', 'in', 'on', 'at', 'around', 'about', 'circa', 'ca',
  'approximately', 'approx', 'sometime', 'during', 'from']);
const MONTH_WORDS = new Set([
  ...MONTHS.map((m) => m.toLowerCase()),
  'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
]);

function isYear(s) { return /^\d{4}$/.test(s) && Number(s) >= 1800 && Number(s) <= 2100; }
function pad2(n) { return String(n).padStart(2, '0'); }

// { date, precision } → the span that date actually covers. The same rule
// as photo_date_range() in SQL: a decade-precision date covers its decade.
function spanOf(date, precision) {
  const [y, m, d] = String(date).split('-').map(Number);
  switch (precision) {
    case 'decade': {
      const d0 = Math.floor(y / 10) * 10;
      return { from: `${d0}-01-01`, to: `${d0 + 9}-12-31`, label: `the ${d0}s` };
    }
    case 'year':
      return { from: `${y}-01-01`, to: `${y}-12-31`, label: String(y) };
    case 'month': {
      const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
      return {
        from: `${y}-${pad2(m)}-01`, to: `${y}-${pad2(m)}-${pad2(last)}`,
        label: `${MONTHS[m - 1]} ${y}`,
      };
    }
    default:
      return { from: date, to: date, label: `${d} ${MONTHS[m - 1]} ${y}` };
  }
}

// Split on whitespace, keeping "quoted phrases" whole.
function tokenize(raw) {
  const out = [];
  const re = /"([^"]*)"|(\S+)/g;
  let m;
  while ((m = re.exec(raw)) !== null) {
    if (m[1] != null) {
      const t = m[1].trim();
      if (t) out.push({ text: t, quoted: true });
    } else {
      out.push({ text: m[2], quoted: false });
    }
  }
  return out;
}

// Try to read a date out of the tokens starting at `i`.
// Returns { term, used } or null.
function readDate(tokens, i) {
  const at = (k) => (tokens[k] && !tokens[k].quoted ? tokens[k].text.toLowerCase() : null);
  const word = (s) => (s || '').replace(/[.,]+$/, '');
  const t0 = word(at(i));
  if (!t0) return null;

  // before / after a year (also "pre 1950", "since 1980", "until 1950")
  const openers = { before: 'to', pre: 'to', until: 'to', till: 'to', after: 'from', since: 'from', post: 'from' };
  if (openers[t0] != null) {
    const y = word(at(i + 1));
    if (y && isYear(y)) {
      const n = Number(y);
      const term = openers[t0] === 'to'
        ? { from: null, to: `${n - 1}-12-31`, label: `before ${n}` }
        : { from: `${n + 1}-01-01`, to: null, label: `after ${n}` };
      return { term, used: 2, text: `${t0} ${y}` };
    }
    return null;
  }

  // 1962-1965 / 1962–1965 (one token) and 1962 to 1965 (three)
  const rangeOne = /^(\d{4})\s*[-–—]\s*(\d{4})$/.exec(t0);
  if (rangeOne && isYear(rangeOne[1]) && isYear(rangeOne[2])) {
    const [a, b] = [Number(rangeOne[1]), Number(rangeOne[2])].sort((x, y2) => x - y2);
    return { term: { from: `${a}-01-01`, to: `${b}-12-31`, label: `${a}–${b}` }, used: 1, text: t0 };
  }
  if (isYear(t0) && ['to', '-', '–', 'until'].includes(word(at(i + 1))) && isYear(word(at(i + 2)))) {
    const [a, b] = [Number(t0), Number(word(at(i + 2)))].sort((x, y2) => x - y2);
    return {
      term: { from: `${a}-01-01`, to: `${b}-12-31`, label: `${a}–${b}` },
      used: 3, text: `${t0} ${at(i + 1)} ${at(i + 2)}`,
    };
  }

  // "March 1962", "summer 1971", "Christmas 1965" — two tokens.
  const t1 = word(at(i + 1));
  if (t1 && isYear(t1)) {
    const two = parseDateFreetext(`${t0} ${t1}`);
    if (two) return { term: spanOf(two.date, two.precision), used: 2, text: `${t0} ${t1}` };
  }

  // One token: 1962, 1960s, 60s, sixties, 1962-03, 3/1962.
  // A bare month name ("May", "June") is NOT a date — it is far more
  // likely to be a person.
  if (MONTH_WORDS.has(t0)) return null;
  const one = parseDateFreetext(t0);
  if (one) return { term: spanOf(one.date, one.precision), used: 1, text: t0 };
  return null;
}

function parseQuery(raw) {
  const text = String(raw == null ? '' : raw).trim().slice(0, MAX_LEN);
  const out = { raw: text, terms: [], truncated: false };
  if (!text) return out;

  const tokens = tokenize(text);
  let i = 0;
  while (i < tokens.length) {
    if (out.terms.length >= MAX_TERMS) { out.truncated = true; break; }
    const tok = tokens[i];

    if (!tok.quoted) {
      // Filler in front of a date ("in the 60s") joins the date term.
      let j = i;
      const leaders = [];
      while (tokens[j] && !tokens[j].quoted && LEADERS.has(tokens[j].text.toLowerCase())) {
        leaders.push(tokens[j].text);
        j += 1;
      }
      const d = readDate(tokens, j);
      if (d) {
        out.terms.push({
          ix: out.terms.length,
          text: [...leaders, d.text].join(' '),
          quoted: false,
          date: { from: d.term.from, to: d.term.to, label: d.term.label },
        });
        i = j + d.used;
        continue;
      }
    }

    out.terms.push({ ix: out.terms.length, text: tok.text, quoted: tok.quoted, date: null });
    i += 1;
  }
  return out;
}

// A one-line description of what we understood, for the page and the API.
function describeQuery(parsed) {
  return parsed.terms.map((t) => (t.date ? t.date.label : t.text)).join(' + ');
}

module.exports = { parseQuery, describeQuery, spanOf, MAX_TERMS };
