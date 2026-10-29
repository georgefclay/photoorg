// Phase 13 — the metadata writer.
//
// The writer embeds the settled truth about a photo (confirmed date, who is
// in it, where, what the back says, how to find the print) in its working
// copy. It is incremental: it composes a canonical payload per photo, hashes
// it, and rewrites a file only when something changed. Three columns record
// what it last did, and they are its own — it never stamps
// `edited_on_desktop_at` / `edited_on_web_at`, which belong to human edits.
//
// * `metadata_hash` — sha256 of the canonical payload last written.
// * `metadata_written_at` — when.
// * `metadata_file_version` — the `file_version` right after that write.
//   A payload hash alone cannot notice that the bytes it described have
//   gone: a Cleanup accept, a rescan accept or a `check_working_files`
//   rebuild replaces the working file (and bumps `file_version`) without
//   any fact changing. The selector writes when the hash differs OR
//   `file_version <> metadata_file_version`, so no other writer needs to
//   know Phase 13 exists.
//
// Desktop-only bookkeeping: nothing pushes these columns, but the schema is
// the same on every tier, so the VM carries them unused.
//
// `down` drops three bookkeeping columns. No row's visibility or membership
// depends on them, so there is nothing a drop could silently resurrect; the
// next `up` simply makes every photo eligible for one rewrite.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumns('photos', {
    metadata_written_at: { type: 'timestamptz' },
    metadata_hash: { type: 'text' },
    metadata_file_version: { type: 'integer' },
  });
};

export const down = (pgm) => {
  pgm.dropColumns('photos', ['metadata_written_at', 'metadata_hash', 'metadata_file_version']);
};
