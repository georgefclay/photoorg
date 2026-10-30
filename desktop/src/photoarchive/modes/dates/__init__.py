"""Date hygiene (Phase 13 fix-up 1).

Ingest confirmed every EXIF date it found, and on a scan that date is the
scanner's clock; on a camera whose clock was never set it is 1 January at
midnight. A wrong confirmed date is a wrong fact, so:

* `rules` — which ingest-sourced dates are suspect (rules a–d) and which
  EXIF dates ingest must not confirm in the first place. Pure.
* `repo` — the one audited way a confirmed date stops being a fact
  (`unconfirm`), used by the `date_hygiene` tool, the desktop's
  **Clear confirmed date** button and the pull of a web clear.
"""
