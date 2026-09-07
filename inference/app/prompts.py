"""VLM prompts live in prompts/*.txt, versioned by filename. Highest version wins."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_VERSIONED = re.compile(r"^(?P<name>.+)\.v(?P<version>\d+)\.txt$")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> tuple[str, str]:
    """Return (text, version_label) for the highest version of `name`.

    version_label is the filename stem, e.g. "transcribe-back.v1", and is echoed
    back to the caller so a re-run after a prompt change is identifiable.
    """
    best: tuple[int, Path] | None = None
    for candidate in PROMPT_DIR.glob(f"{name}.v*.txt"):
        match = _VERSIONED.match(candidate.name)
        if not match or match.group("name") != name:
            continue
        version = int(match.group("version"))
        if best is None or version > best[0]:
            best = (version, candidate)
    if best is None:
        raise FileNotFoundError(f"No prompt file for {name!r} in {PROMPT_DIR}")
    return best[1].read_text(encoding="utf-8").strip(), best[1].name[: -len(".txt")]
