const path = require('path');
const express = require('express');
const session = require('express-session');
const connectPgSimple = require('connect-pg-simple');

const { csrfMiddleware } = require('./middleware/csrf');
const { loadUserFactory } = require('./middleware/load-user');
const { makeAuthLimiter } = require('./middleware/rate-limit');

const { requireService } = require('./middleware/require-service');
const { layoutMiddleware } = require('./middleware/layout');
const { scopeLocals } = require('./services/scope');
const fmt = require('./services/format');

const homeRoutes = require('./routes/home');
const requestAccessRoutes = require('./routes/request-access');
const loginRoutes = require('./routes/login');
const magicRoutes = require('./routes/magic');
const adminRoutes = require('./routes/admin');
const mediaRoutes = require('./routes/media');
const apiPhotosRoutes = require('./routes/api-photos');
const apiPeopleModule = require('./routes/api-people');
const apiAlbumsRoutes = require('./routes/api-albums');
const apiCsrfRoutes = require('./routes/api-csrf');
const apiContribRoutes = require('./routes/api-contrib');
const apiAdminRoutes = require('./routes/api-admin');
const apiGroupsModule = require('./routes/api-groups');
const syncRoutes = require('./routes/sync');
const apiContributionsModule = require('./routes/api-contributions');
const apiMiscRoutes = require('./routes/api-misc');
const pageRoutes = require('./routes/pages');
const photoPageRoutes = require('./routes/pages-photo');
const contribPageRoutes = require('./routes/pages-contrib');
const peoplePageRoutes = require('./routes/pages-people');
const adminPageRoutes = require('./routes/pages-admin');

function createApp({ pool }) {
  const app = express();

  // Caddy terminates TLS; trust one hop so req.ip is the client's real IP.
  app.set('trust proxy', 1);
  app.set('view engine', 'ejs');
  app.set('views', path.join(__dirname, 'views'));
  app.locals.fmt = fmt;

  const staticOpts = { maxAge: process.env.NODE_ENV === 'production' ? '1h' : 0 };
  app.use('/css', express.static(path.join(__dirname, 'public', 'css'), staticOpts));
  app.use('/js', express.static(path.join(__dirname, 'public', 'js'), staticOpts));
  app.get('/healthz', (_req, res) => res.type('text/plain').send('ok'));
  app.get('/favicon.svg', (_req, res) => res.sendFile(path.join(__dirname, 'public', 'favicon.svg'), { maxAge: '7d' }));
  app.get('/favicon.ico', (_req, res) => res.redirect(301, '/favicon.svg'));

  // Every res.render goes through views/layout.ejs (page.title etc.).
  app.use(layoutMiddleware);

  app.use(express.urlencoded({ extended: false, limit: '32kb' }));

  const PgStore = connectPgSimple(session);
  app.use(session({
    store: new PgStore({ pool, tableName: 'session' }),
    name: 'photoarchive.sid',
    secret: process.env.SESSION_SECRET || 'change-me-in-env',
    resave: false,
    saveUninitialized: false,
    rolling: true,
    cookie: {
      httpOnly: true,
      sameSite: 'lax',
      secure: process.env.NODE_ENV === 'production',
      maxAge: 180 * 24 * 60 * 60 * 1000, // 180 days
    },
  }));

  // loadUser must run before csrfMiddleware so the CSRF check can key on
  // req.user (only authed POSTs need a token).
  app.use(loadUserFactory({ pool }));
  app.use(csrfMiddleware);

  // Header group switcher for every server-rendered page.
  const scopeForPages = scopeLocals({ pool });
  const NO_PAGE = /^\/(api|media|sync|service)\//;
  app.use((req, res, next) => (NO_PAGE.test(req.path) ? next() : scopeForPages(req, res, next)));

  app.use('/', pageRoutes({ pool }));
  app.use('/', homeRoutes({ pool }));
  // Independent limiter per route so exhausting one doesn't block the other.
  app.use('/', requestAccessRoutes({ pool, authLimiter: makeAuthLimiter() }));
  app.use('/', loginRoutes({ pool, authLimiter: makeAuthLimiter() }));
  app.use('/', magicRoutes({ pool }));
  app.use('/admin', adminRoutes({ pool }));
  app.use('/admin', adminPageRoutes({ pool }));
  app.use('/', photoPageRoutes({ pool }));
  app.use('/', contribPageRoutes({ pool }));
  app.use('/', peoplePageRoutes({ pool }));
  app.use('/media', mediaRoutes({ pool }));
  app.use('/api/csrf', apiCsrfRoutes());
  app.use('/api/photos', apiPhotosRoutes({ pool }));
  app.use('/api/people', apiPeopleModule({ pool }));
  app.use('/api/relationships', apiPeopleModule.relationshipsRouter({ pool }));
  app.use('/api/albums', apiAlbumsRoutes({ pool }));
  app.use('/api', apiMiscRoutes({ pool }));
  app.use('/api', apiContribRoutes({ pool }));
  app.use('/api/groups', apiGroupsModule({ pool }));
  app.use('/api/admin/groups', apiGroupsModule.adminGroupsRouter({ pool }));
  app.use('/api/admin/photos', apiGroupsModule.adminBulkAssignRouter({ pool }));
  app.use('/api/admin', apiAdminRoutes({ pool }));
  app.use('/api/contributions', apiContributionsModule({ pool }));
  app.use('/api/admin/contributions', apiContributionsModule.adminContribRouter({ pool }));

  // Phase 9 sync endpoints (service-token authed).
  app.use('/sync', syncRoutes({ pool }));

  // Kept for Phase 8 smoke tests.
  app.get('/service/ping', requireService, (_req, res) => {
    res.json({ ok: true, service: true });
  });

  // Basic 404
  app.use((req, res) => {
    res.status(404).render('error', {
      title: 'Not found',
      message: 'That page does not exist.',
    });
  });

  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, _next) => {
    console.error('[web] error', err);
    res.status(500).render('error', {
      title: 'Something went wrong',
      message: 'An unexpected error occurred. Please try again.',
    });
  });

  return app;
}

module.exports = { createApp };
