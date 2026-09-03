from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    factory: Callable[[], QWidget]


def default_modes() -> list[Mode]:
    # Lazy imports so the placeholder modes never pull in heavy code paths.
    from ..modes.ingest.ui import IngestPanel
    from ..modes.triage import TriagePanel
    from ..modes.dedupe import DedupePanel
    from ..modes.cleanup import CleanupPanel
    from ..modes.faces import FacesPanel
    from ..modes.sync import SyncPanel

    return [
        Mode("ingest", "Ingest", IngestPanel),
        Mode("triage", "Triage", TriagePanel),
        Mode("dedupe", "Dedupe", DedupePanel),
        Mode("cleanup", "Cleanup", CleanupPanel),
        Mode("faces", "Faces", FacesPanel),
        Mode("sync", "Sync", SyncPanel),
    ]
