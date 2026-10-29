"""Phase 13 — the metadata writer.

Embeds the settled truth about each photo (confirmed date, who is in it and
where, place, what the back says, how to find the print, the archive
identity) in its working copy, so the archive survives this software.

    payload.py   what is written (pure)
    regions.py   face boxes: display frame -> stored pixels (pure)
    exiftool.py  the exiftool process and the tags the archive owns
    repo.py      facts in, write bookkeeping out
    writer.py    one photo / one chunk, safely
    run.py       the chunked orchestrator and its report
    ui.py        the Metadata sidebar mode
"""
from __future__ import annotations


def __getattr__(name: str):
    # Lazy, so importing the package for the CLI never pulls in Qt.
    if name == "MetadataPanel":
        from .ui import MetadataPanel
        return MetadataPanel
    raise AttributeError(name)
