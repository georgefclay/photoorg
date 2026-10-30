// Phase 12 — downloads (services/downloads.js holds the rules).
//
//   GET  /download?kind=…&id=…        confirmation: count, size, now-or-email
//   GET  /download/zip?kind=…         the immediate zip, streamed (≤ 200 files, ≤ 1 GB)
//   GET  /download/photo/:id          one photo, the full-resolution working file
//   POST /download                    queue a background job (CSRF)
//   GET  /downloads                   my jobs
//   GET  /downloads/:id               one job's status
//   GET  /downloads/:id/file          the finished zip
//
// Nothing here answers 4xx in normal use (the fail2ban rule): not ready,
// expired, too big, one already running, no image yet — all 200 pages that
// say so. Someone else's job id is 404, as a photo you cannot see is.
// Signed out, a GET lands on /login carrying `return_to`, so the emailed
// "your download is ready" link comes back here after the sign-in email.

const express = require('express');
const fsp = require('fs/promises');
const { getScope } = require('../services/scope');
const dl = require('../services/downloads');
const tree = require('../services/zip-tree');

function requireUserHere(req, res, next) {
  if (req.user) return next();
  if (req.method === 'GET') {
    return res.redirect(`/login?return_to=${encodeURIComponent(req.originalUrl)}`);
  }
  return res.status(401).render('error', { title: 'Signed out', message: 'You need to be signed in to do that.' });
}

function toId(v) {
  const n = Number(v);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

module.exports = function downloadRoutes({ pool }) {
  const router = express.Router();

  async function selectionFrom(req, raw) {
    const { scope } = await getScope(req, pool);
    return dl.parseSelection(raw, { scopeKey: scope.key });
  }

  function nothing(res) {
    return res.render('download-confirm', {
      sel: null, g: null, mode: null, query: '', limits: dl.limits(), disk: null,
    });
  }

  // ---- confirmation --------------------------------------------------
  router.get('/download', requireUserHere, async (req, res, next) => {
    try {
      const sel = await selectionFrom(req, req.query);
      if (!sel) return nothing(res);
      if (sel.kind === 'photo') return res.redirect(`/download/photo/${sel.photo_id}`);
      const g = await dl.gather(pool, req.user, sel);
      const mode = dl.pathFor(sel, g.summary);
      const open = mode === 'background' && req.user.role !== 'admin' ? await dl.openJobFor(pool, req.user.id) : null;
      res.render('download-confirm', {
        sel, g, mode, open, query: dl.selectionQuery(sel).toString(), limits: dl.limits(), disk: null,
      });
    } catch (err) { next(err); }
  });

  // ---- the immediate path ---------------------------------------------
  router.get('/download/zip', requireUserHere, async (req, res, next) => {
    try {
      const sel = await selectionFrom(req, req.query);
      if (!sel) return nothing(res);
      const g = await dl.gather(pool, req.user, sel);
      const mode = dl.pathFor(sel, g.summary);
      if (mode !== 'immediate') {
        // Grew past the limit since the confirmation: offer the email path.
        return res.render('download-confirm', {
          sel, g, mode, open: null, query: dl.selectionQuery(sel).toString(), limits: dl.limits(), disk: null,
        });
      }
      const when = new Date();
      res.setHeader('Content-Type', 'application/zip');
      res.setHeader('Content-Disposition', `attachment; filename="${dl.zipFileName(g, when)}"`);
      res.setHeader('Cache-Control', 'private, no-store');
      await dl.auditDownload(pool, req.user, 'download.request', null,
        { selection: sel, label: g.label, path: 'immediate', ...g.summary });
      const p = await dl.writeZip(res, g, { requestedBy: dl.requester(req.user), requestedAt: when });
      await dl.auditDownload(pool, req.user, 'download.fetched', null, {
        selection: sel, label: g.label, path: 'immediate', files: p.files.length,
        missing_count: p.missing.length, bytes: p.bytes,
      });
    } catch (err) {
      if (res.headersSent) { console.error('[downloads] immediate zip failed', err); res.destroy(err); return; }
      next(err);
    }
  });

  // ---- one photo ------------------------------------------------------
  router.get('/download/photo/:id(\\d+)', requireUserHere, async (req, res, next) => {
    try {
      const sel = dl.parseSelection({ kind: 'photo', id: req.params.id });
      const g = await dl.gather(pool, req.user, sel);
      const row = g.rows[0];
      if (!row) {
        return res.status(404).render('error', { title: 'Not found', message: 'That photo does not exist, or it has been removed.' });
      }
      const abs = row.has_file ? dl.workingAbs(row) : null;
      const st = abs ? await fsp.stat(abs).catch(() => null) : null;
      if (!st || !st.isFile()) {
        return res.render('download-none', { photoId: row.id });
      }
      const name = tree.fileName(row);
      await dl.auditDownload(pool, req.user, 'download.fetched', null,
        { selection: sel, path: 'single', photo_id: row.id, bytes: st.size });
      res.setHeader('Cache-Control', 'private, no-store');
      res.download(abs, name);
    } catch (err) { next(err); }
  });

  // ---- the background path ---------------------------------------------
  router.post('/download', requireUserHere, async (req, res, next) => {
    try {
      const sel = await selectionFrom(req, req.body || {});
      if (!sel) return nothing(res);
      const g = await dl.gather(pool, req.user, sel);
      const mode = dl.pathFor(sel, g.summary);
      if (mode !== 'background') {
        return res.redirect(303, `/download/zip?${dl.selectionQuery(sel)}`);
      }
      const r = await dl.createJob(pool, req.user, sel, g);
      if (r.refused) {
        return res.render('download-confirm', {
          sel, g, mode, open: null, query: dl.selectionQuery(sel).toString(), limits: dl.limits(), disk: r.refused,
        });
      }
      if (r.conflict) return res.redirect(303, `/downloads/${r.conflict.id}?already=1`);
      dl.kick(pool).catch((e) => console.error('[downloads] kick', e));
      res.redirect(303, `/downloads/${r.job.id}`);
    } catch (err) { next(err); }
  });

  // ---- my downloads ----------------------------------------------------
  router.get('/downloads', requireUserHere, async (req, res, next) => {
    try {
      res.render('downloads', { jobs: await dl.listJobs(pool, req.user) });
    } catch (err) { next(err); }
  });

  router.get('/downloads/:id(\\d+)', requireUserHere, async (req, res, next) => {
    try {
      const job = await dl.getJob(pool, req.user, toId(req.params.id));
      if (!job) return res.status(404).render('error', { title: 'Not found', message: 'That download does not exist.' });
      res.render('download-job', { job, already: req.query.already === '1' });
    } catch (err) { next(err); }
  });

  router.get('/downloads/:id(\\d+)/file', requireUserHere, async (req, res, next) => {
    try {
      const job = await dl.getJob(pool, req.user, toId(req.params.id));
      if (!job) return res.status(404).render('error', { title: 'Not found', message: 'That download does not exist.' });
      const st = job.status === 'done' && job.live && job.zip_path
        ? await fsp.stat(job.zip_path).catch(() => null) : null;
      if (!st) return res.render('download-job', { job, already: false });
      await dl.auditDownload(pool, req.user, 'download.fetched', Number(job.id), {
        selection: job.selection, path: 'background', bytes: st.size,
        owner: Number(job.user_id) === Number(req.user.id) ? undefined : job.owner_email,
      });
      res.setHeader('Cache-Control', 'private, no-store');
      const name = `family-photos-${job.id}-${new Date(job.created_at).toISOString().slice(0, 10).replace(/-/g, '')}.zip`;
      res.download(job.zip_path, name);
    } catch (err) { next(err); }
  });

  return router;
};
