"""Phase 7 — scan cleanup.

Deskew, crop to the print, split multi-print scans, correct colour cast and
fade — every one a *proposal* until George accepts it.

    analyse.py        measure one scan (OpenCV, at <= 2000 px)
    geometry.py       the one affine; face boxes travel with the pixels
    ops.py            the pixel operations, dtype-agnostic
    render.py         plan + ticked ops -> pixels (preview, full, 1:1 window)
    repo.py           scope selector and proposal rows
    accept.py         accept / reject / undo one proposal
    split.py          one photos row per print on a multi-print scan
    job.py            the `cleanup_analyse` batch job and bulk accept
    report.py         counts, timings, before/after contact sheet
    remote/           pluggable remote enhance (null, claid)
    remote_enhance.py send E, turn the result into a new proposal
    ui.py             the review queue
"""
from __future__ import annotations

from .ui import CleanupPanel

__all__ = ["CleanupPanel"]
