// requireModerator(pool, { groupIdParam }) → middleware that permits an
// admin or a live moderator of the group identified by req.params[groupIdParam].
// Anyone else gets 403 (not 404 — the moderator UI is only reachable by
// people who at least know the group exists; hiding it further just
// confuses moderators who bookmarked a page and lost their role).
//
// Suspension is not checked here — loadUser destroyed suspended sessions
// upstream.
const { isModerator } = require('./visibility');
const { requireUser } = require('./require-user');

function requireModerator({ pool, groupIdParam = 'groupId' } = {}) {
  return [
    requireUser,
    async function (req, res, next) {
      const raw = req.params[groupIdParam];
      const groupId = Number(raw);
      if (!Number.isInteger(groupId) || groupId <= 0) {
        return res.status(400).json({ error: 'bad group id' });
      }
      try {
        if (await isModerator(pool, req.user, groupId)) {
          req.moderatedGroupId = groupId;
          return next();
        }
        return res.status(403).json({ error: 'moderator access required' });
      } catch (err) {
        return next(err);
      }
    },
  ];
}

module.exports = { requireModerator };
