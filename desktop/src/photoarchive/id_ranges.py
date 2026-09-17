"""Web-origin id range (Phase 9 fix-up 1).

One shared constant in ``shared/id-ranges.json``, read by the web, the
shared migration, and here. Rows in the listed tables with
``id >= WEB_ID_FLOOR`` were created on the web: the desktop never pushes
them (the web refuses with 400) and receives them through
``/sync/pull/web_origin`` with the same ids. The desktop's own sequences
stay below the floor — never apply the web floor to ``photoorg``.
"""

from __future__ import annotations

import json
from pathlib import Path

_RANGES_PATH = Path(__file__).resolve().parents[3] / "shared" / "id-ranges.json"

try:
    _RANGES = json.loads(_RANGES_PATH.read_text("utf-8"))
except FileNotFoundError as e:  # pragma: no cover - packaging error
    raise RuntimeError(f"shared id ranges not found at {_RANGES_PATH}") from e

WEB_ID_FLOOR: int = int(_RANGES["web_id_floor"])
WEB_ORIGIN_TABLES: tuple[str, ...] = tuple(_RANGES["web_origin_tables"])


def is_web_origin(id_: int | None) -> bool:
    return id_ is not None and int(id_) >= WEB_ID_FLOOR
