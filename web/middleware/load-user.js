// Resolve req.session.userId → req.user on every request (once session is
// initialised). Refuses (destroys the session and treats as anonymous) if the
// user's status is not 'active' — this is how suspension takes effect
// immediately, even though we also delete session rows when suspending.
function loadUserFactory({ pool }) {
  return async function loadUser(req, res, next) {
    req.user = null;
    res.locals.user = null;

    const uid = req.session && req.session.userId;
    if (!uid) return next();

    try {
      const { rows } = await pool.query(
        `select id, email, display_name, role, status
           from users
          where id = $1`,
        [uid],
      );
      const user = rows[0];
      if (!user || user.status !== 'active') {
        // Wipe the session so a suspended (or deleted) account can't hold on.
        return req.session.destroy(() => next());
      }
      req.user = user;
      res.locals.user = user;
      return next();
    } catch (err) {
      return next(err);
    }
  };
}

module.exports = { loadUserFactory };
