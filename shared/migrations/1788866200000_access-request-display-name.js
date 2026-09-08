export const shorthands = undefined;

// Phase 8: capture the requester's preferred display name on the
// request-access form and carry it through to the users row on approval.
export const up = (pgm) => {
  pgm.addColumn('access_requests', {
    display_name: { type: 'text' },
  });
};

export const down = (pgm) => {
  pgm.dropColumn('access_requests', 'display_name');
};
