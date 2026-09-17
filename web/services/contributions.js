// Contributor-facing reads for /upload/mine. Always keyed on the caller's
// own user id — never lists anyone else's contributions, admins included.

const FILE_STATUS_LABELS = { pending: 'Awaiting approval', approved: 'Approved', rejected: 'Rejected' };

// { summary: { total, pending, approved, rejected }, items: [...] }
// Contributions with no files (a Send where everything was already on the
// server) are left out — there is nothing to show for them.
async function listMyContributions(pool, userId, { limit = 100 } = {}) {
  const lim = Math.max(1, Math.min(Number(limit) || 100, 500));
  const summaryQ = pool.query(
    `select count(*)::int as total,
            count(*) filter (where cf.status = 'pending')::int  as pending,
            count(*) filter (where cf.status = 'approved')::int as approved,
            count(*) filter (where cf.status = 'rejected')::int as rejected
       from contribution_files cf
       join contributions c on c.id = cf.contribution_id
      where c.user_id = $1`,
    [userId],
  );
  const contribQ = pool.query(
    `select c.id, c.status, c.note, c.group_ids, c.created_at, c.finished_at
       from contributions c
      where c.user_id = $1
        and exists (select 1 from contribution_files cf where cf.contribution_id = c.id)
      order by c.created_at desc, c.id desc
      limit $2`,
    [userId, lim],
  );
  const [summaryRes, contribRes] = await Promise.all([summaryQ, contribQ]);
  const contribs = contribRes.rows;
  const ids = contribs.map((c) => c.id);

  const [filesRes, groupsRes] = ids.length
    ? await Promise.all([
      pool.query(
        `select id, contribution_id, original_filename, size, mime, status, is_video,
                duplicate_of_photo_id
           from contribution_files
          where contribution_id = any($1::bigint[])
          order by id asc`,
        [ids],
      ),
      pool.query(
        `select id, name from groups
          where id = any($1::bigint[])`,
        [[...new Set(contribs.flatMap((c) => (c.group_ids || []).map(Number)))]],
      ),
    ])
    : [{ rows: [] }, { rows: [] }];

  const groupNames = new Map(groupsRes.rows.map((g) => [Number(g.id), g.name]));
  const filesBy = new Map();
  for (const f of filesRes.rows) {
    const cid = Number(f.contribution_id);
    if (!filesBy.has(cid)) filesBy.set(cid, []);
    filesBy.get(cid).push({
      id: Number(f.id),
      name: f.original_filename,
      size: f.size != null ? Number(f.size) : null,
      mime: f.mime,
      status: f.status,
      status_label: FILE_STATUS_LABELS[f.status] || f.status,
      is_video: f.is_video,
      is_duplicate: f.duplicate_of_photo_id != null,
    });
  }

  const s = summaryRes.rows[0] || {};
  return {
    summary: { total: s.total || 0, pending: s.pending || 0, approved: s.approved || 0, rejected: s.rejected || 0 },
    items: contribs.map((c) => {
      const files = filesBy.get(Number(c.id)) || [];
      return {
        id: Number(c.id),
        status: c.status,
        note: c.note,
        created_at: c.created_at,
        finished: c.finished_at != null,
        groups: (c.group_ids || []).map(Number).map((id) => groupNames.get(id)).filter(Boolean),
        files,
        counts: {
          pending: files.filter((f) => f.status === 'pending').length,
          approved: files.filter((f) => f.status === 'approved').length,
          rejected: files.filter((f) => f.status === 'rejected').length,
        },
      };
    }),
  };
}

// "You've sent 14 photos, 12 awaiting approval, 2 approved" (+ ", 1 rejected").
function summaryLine(summary) {
  const n = (x) => Number(x).toLocaleString('en-US');
  const parts = [`You've sent ${n(summary.total)} ${summary.total === 1 ? 'photo' : 'photos'}`];
  parts.push(`${n(summary.pending)} awaiting approval`);
  parts.push(`${n(summary.approved)} approved`);
  if (summary.rejected) parts.push(`${n(summary.rejected)} rejected`);
  return parts.join(', ');
}

module.exports = { listMyContributions, summaryLine, FILE_STATUS_LABELS };
