// Admin area pages (/admin, /admin/*) except the Phase 8 access/users
// routes in routes/admin.js. OWNER: admin-pages agent.
//
// Data is server-rendered; actions are JS calls to the JSON API
// (public/js/admin*.js), which enforces the same rules again.
//
//   /admin                    dashboard            admin + moderators (scoped)
//   /admin/suggestions        suggestions queue    admin
//   /admin/disputes           disputed face tags   admin
//   /admin/contributions      upload review        admin + moderators (scoped)
//   /admin/groups             groups list          admin + moderators (their groups)
//   /admin/groups/:id         group + members      admin + moderator of that group (else 404)
//   /admin/unfiled            unfiled + bulk assign admin
//   /admin/rescan             printable rescan list admin
//   /admin/report             monthly report       admin
//   /admin/audit              audit browser        admin

const express = require('express');
const { requireUser, requireAdmin } = require('../middleware/require-user');
const svc = require('../services/admin');

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

// Admins, and moderators of at least one group (res.locals.isModerator is
// set for every page by services/scope.scopeLocals). Contributors → 403.
function requireModeratorPage(req, res, next) {
  if (req.user.role === 'admin' || res.locals.isModerator) return next();
  return res.status(403).render('error', {
    title: 'Not allowed',
    message: 'This page is for admins and group moderators.',
  });
}

const modGate = [requireUser, requireModeratorPage];

function qs(obj) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v != null && v !== '') q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : '';
}

module.exports = function adminPageRoutes({ pool }) {
  const router = express.Router();

  // ---- dashboard ------------------------------------------------------
  router.get('/', ...modGate, async (req, res, next) => {
    try {
      const d = await svc.dashboard(pool, req.user);
      res.render('admin/dashboard', { d, isAdmin: req.user.role === 'admin' });
    } catch (err) { next(err); }
  });

  // ---- suggestions ----------------------------------------------------
  router.get('/suggestions', requireAdmin, async (req, res, next) => {
    try {
      const counts = await svc.suggestionCounts(pool);
      const rawSource = String(req.query.source || '');
      const source = rawSource === 'all' || svc.SUGGESTION_SOURCES.includes(rawSource) ? rawSource : svc.defaultSource(counts);
      const kind = svc.SUGGESTION_KINDS.includes(req.query.kind) ? req.query.kind : null;
      const cursor = svc.toInt(req.query.cursor);
      const { items, next: nextCursor } = await svc.listSuggestions(pool, { source, kind, cursor: cursor > 0 ? cursor : null, limit: 40 });

      const sourceTotals = { all: 0, human: 0, ai: 0, import: 0 };
      const kindTotals = {};
      for (const c of counts) {
        sourceTotals[c.source] += c.n;
        sourceTotals.all += c.n;
        if (source === 'all' || c.source === source) kindTotals[c.kind] = (kindTotals[c.kind] || 0) + c.n;
      }
      res.render('admin/suggestions', {
        items, source, kind, cursor,
        sourceTotals, kindTotals,
        kinds: svc.SUGGESTION_KINDS, kindLabels: svc.KIND_LABELS, sourceLabels: svc.SOURCE_LABELS,
        nextUrl: nextCursor ? `/admin/suggestions${qs({ source, kind, cursor: nextCursor })}` : null,
        firstUrl: `/admin/suggestions${qs({ source, kind })}`,
        link: (over) => `/admin/suggestions${qs({ source, kind, ...over })}`,
      });
    } catch (err) { next(err); }
  });

  // ---- disputes -------------------------------------------------------
  router.get('/disputes', requireAdmin, async (req, res, next) => {
    try {
      res.render('admin/disputes', { items: await svc.listDisputes(pool) });
    } catch (err) { next(err); }
  });

  // ---- contributions --------------------------------------------------
  router.get('/contributions', ...modGate, async (req, res, next) => {
    try {
      const filter = Object.prototype.hasOwnProperty.call(svc.CONTRIB_FILTERS, req.query.status) ? req.query.status : 'review';
      const cursor = svc.toInt(req.query.cursor);
      const { items, next: nextCursor } = await svc.listContributions(pool, req.user, {
        filter, cursor: cursor > 0 ? cursor : null, limit: 20,
      });
      res.render('admin/contributions', {
        items, filter, filters: svc.CONTRIB_FILTERS, cursor,
        isAdmin: req.user.role === 'admin',
        nextUrl: nextCursor ? `/admin/contributions${qs({ status: filter, cursor: nextCursor })}` : null,
        firstUrl: `/admin/contributions${qs({ status: filter })}`,
      });
    } catch (err) { next(err); }
  });

  // ---- groups ---------------------------------------------------------
  router.get('/groups', ...modGate, async (req, res, next) => {
    try {
      res.render('admin/groups', {
        groups: await svc.moderatorGroups(pool, req.user),
        isAdmin: req.user.role === 'admin',
      });
    } catch (err) { next(err); }
  });

  router.get('/groups/:id(\\d+)', ...modGate, async (req, res, next) => {
    try {
      const group = await svc.groupDetail(pool, req.user, Number(req.params.id));
      if (!group) {
        return res.status(404).render('error', { title: 'Not found', message: 'That group does not exist.' });
      }
      res.render('admin/group', { group, isAdmin: req.user.role === 'admin' });
    } catch (err) { next(err); }
  });

  // ---- unfiled --------------------------------------------------------
  router.get('/unfiled', requireAdmin, async (req, res, next) => {
    try {
      const filter = svc.parseBulkFilter(req.query);
      const cursor = svc.toInt(req.query.cursor);
      const [page, options] = await Promise.all([
        svc.unfiledPage(pool, filter, { cursor: cursor > 0 ? cursor : null, limit: 60 }),
        svc.unfiledFilterOptions(pool),
      ]);
      let personName = null;
      if (filter.person_id) {
        const r = await pool.query(`select display_name from people where id = $1`, [filter.person_id]);
        personName = r.rows[0] ? r.rows[0].display_name : `#${filter.person_id}`;
      }
      res.render('admin/unfiled', {
        result: page, options, filter, personName, cursor,
        filtered: svc.hasBulkFilter(filter),
        nextUrl: page.next ? `/admin/unfiled${qs({ ...filter, cursor: page.next })}` : null,
        firstUrl: `/admin/unfiled${qs(filter)}`,
      });
    } catch (err) { next(err); }
  });

  // ---- rescan list ----------------------------------------------------
  router.get('/rescan-list', requireAdmin, (req, res) => res.redirect(301, `/admin/rescan${qs({ thumbs: req.query.thumbs })}`));
  router.get('/rescan', requireAdmin, async (req, res, next) => {
    try {
      const { batches, total } = await svc.rescanBatches(pool);
      res.render('admin/rescan', { batches, total, thumbs: req.query.thumbs === '1' });
    } catch (err) { next(err); }
  });

  // ---- monthly report -------------------------------------------------
  router.get('/report', requireAdmin, async (req, res, next) => {
    try {
      const now = await svc.currentMonth(pool);
      const month = MONTH_RE.test(String(req.query.month || '')) ? req.query.month : now;
      const [report, extras] = await Promise.all([svc.monthlyReport(pool, month), svc.monthlyExtras(pool, month)]);
      res.render('admin/report', {
        report, extras, month,
        prevMonth: svc.shiftMonth(month, -1),
        nextMonth: month < now ? svc.shiftMonth(month, 1) : null,
      });
    } catch (err) { next(err); }
  });

  // ---- audit ----------------------------------------------------------
  router.get('/audit', requireAdmin, async (req, res, next) => {
    try {
      const f = {
        entity_type: String(req.query.entity_type || '').trim().slice(0, 80),
        entity_id: svc.toInt(req.query.entity_id),
        action: String(req.query.action || '').trim().slice(0, 120),
        actor: String(req.query.actor || '').trim().slice(0, 200),
      };
      const cursor = svc.toInt(req.query.cursor);
      const [page, entityTypes] = await Promise.all([
        svc.auditPage(pool, f, { cursor: cursor > 0 ? cursor : null, limit: 50 }),
        svc.auditEntityTypes(pool),
      ]);
      res.render('admin/audit', {
        result: page, f, entityTypes, cursor,
        filtered: !!(f.entity_type || f.entity_id != null || f.action || f.actor),
        nextUrl: page.next ? `/admin/audit${qs({ ...f, cursor: page.next })}` : null,
        firstUrl: `/admin/audit${qs(f)}`,
        compactJson: svc.compactJson,
        entityHref: svc.auditEntityHref,
      });
    } catch (err) { next(err); }
  });

  return router;
};
