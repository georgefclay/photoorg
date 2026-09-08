const path = require('path');
const express = require('express');
const session = require('express-session');
const connectPgSimple = require('connect-pg-simple');

const { csrfMiddleware } = require('./middleware/csrf');
const { loadUserFactory } = require('./middleware/load-user');
const { makeAuthLimiter } = require('./middleware/rate-limit');

const { requireService } = require('./middleware/require-service');

const homeRoutes = require('./routes/home');
const requestAccessRoutes = require('./routes/request-access');
const loginRoutes = require('./routes/login');
const magicRoutes = require('./routes/magic');
const adminRoutes = require('./routes/admin');

function createApp({ pool }) {
  const app = express();

  // Caddy terminates TLS; trust one hop so req.ip is the client's real IP.
  app.set('trust proxy', 1);
  app.set('view engine', 'ejs');
  app.set('views', path.join(__dirname, 'views'));

  app.use('/css', express.static(path.join(__dirname, 'public', 'css')));
  app.get('/healthz', (_req, res) => res.type('text/plain').send('ok'));

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

  app.use('/', homeRoutes({ pool }));
  // Independent limiter per route so exhausting one doesn't block the other.
  app.use('/', requestAccessRoutes({ pool, authLimiter: makeAuthLimiter() }));
  app.use('/', loginRoutes({ pool, authLimiter: makeAuthLimiter() }));
  app.use('/', magicRoutes({ pool }));
  app.use('/admin', adminRoutes({ pool }));

  // Phase 9 will mount the real sync endpoints here. For Phase 8 we expose
  // just a ping so the middleware is exercised end-to-end.
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
