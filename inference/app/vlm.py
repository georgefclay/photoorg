"""The MLX vision model: lazy load, one job at a time, JSON out or a clean 503."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from .config import get_settings
from .jsonparse import extract_json
from .logging_conf import log_event
from .priority import PriorityLock
from .prompts import load_prompt

REPAIR_SUFFIX = (
    "\n\nYour previous reply was not a single valid JSON object. Reply again with "
    "ONLY the JSON object described above: no prose, no markdown, no code fences."
)


class VLMUnavailable(RuntimeError):
    """The model crashed, ran out of memory, or could not be loaded."""


@dataclass
class VLMOutput:
    result: dict[str, Any]
    prompt_version: str
    raw: str
    parsed: bool
    attempts: int
    prompt_tokens: int | None = None
    generation_tokens: int | None = None
    peak_memory_gb: float | None = None


@dataclass
class VLMEngine:
    lock: PriorityLock = field(default_factory=PriorityLock)

    def __post_init__(self) -> None:
        self._model = None
        self._processor = None
        self._config = None
        self._model_name: str | None = None
        # MLX is not designed for concurrent use from many threads; one worker,
        # and the PriorityLock in front of it, keeps every call serialised.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vlm")
        self._load_lock = asyncio.Lock()

    # ---- state -------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._model_name or get_settings().vlm_model

    def unload(self) -> None:
        self._model = None
        self._processor = None
        self._config = None
        self._model_name = None
        try:
            import mlx.core as mx

            clear = getattr(mx, "clear_cache", None) or getattr(
                getattr(mx, "metal", None), "clear_cache", None
            )
            if clear:
                clear()
        except Exception:  # noqa: BLE001 - freeing cache must never mask the real error
            pass

    # ---- loading -----------------------------------------------------------

    def _load_blocking(self) -> None:
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        name = get_settings().vlm_model
        started = time.perf_counter()
        model, processor = load(name)
        self._model = model
        self._processor = processor
        self._config = load_config(name)
        self._model_name = name
        log_event(
            "vlm.loaded",
            model=name,
            load_ms=int((time.perf_counter() - started) * 1000),
            active_memory_gb=active_memory_gb(),
        )

    async def ensure_loaded(self) -> None:
        if self.loaded:
            return
        async with self._load_lock:
            if self.loaded:
                return
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._executor, self._load_blocking)
            except Exception as exc:  # noqa: BLE001
                self.unload()
                log_event("vlm.load_failed", error=str(exc))
                raise VLMUnavailable(f"Could not load {get_settings().vlm_model}: {exc}") from exc

    # ---- generation --------------------------------------------------------

    def _generate_blocking(self, prompt_text: str, image: Image.Image, max_tokens: int):
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        settings = get_settings()
        formatted = apply_chat_template(
            self._processor, self._config, prompt_text, num_images=1
        )
        return generate(
            self._model,
            self._processor,
            formatted,
            image=[image],
            max_tokens=max_tokens,
            temperature=settings.vlm_temperature,
            verbose=False,
        )

    async def run_json(
        self,
        prompt_name: str,
        image: Image.Image,
        *,
        high_priority: bool = True,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> VLMOutput:
        """Run a prompt, insist on JSON, retry once, then give up cleanly.

        `timeout` covers generation only, never the wait for the model. A batch
        item that sits behind a run of interactive requests is queued, not late.
        """
        prompt_text, prompt_version = load_prompt(prompt_name)
        max_tokens = max_tokens or get_settings().vlm_max_tokens

        await self.ensure_loaded()
        loop = asyncio.get_running_loop()

        raw = ""
        parsed: dict[str, Any] | None = None
        attempts = 0
        prompt_tokens = generation_tokens = None
        peak_gb = None

        async with self.lock.acquire(high_priority=high_priority):
            for attempt in range(2):
                attempts = attempt + 1
                text = prompt_text if attempt == 0 else prompt_text + REPAIR_SUFFIX
                try:
                    future = loop.run_in_executor(
                        self._executor, self._generate_blocking, text, image, max_tokens
                    )
                    outcome = (
                        await asyncio.wait_for(future, timeout) if timeout else await future
                    )
                except asyncio.TimeoutError:
                    log_event(
                        "vlm.timeout", prompt=prompt_version, timeout_s=timeout
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - OOM, Metal fault, anything
                    self.unload()
                    log_event("vlm.generate_failed", prompt=prompt_version, error=str(exc))
                    raise VLMUnavailable(str(exc)) from exc

                raw = (getattr(outcome, "text", None) or str(outcome)).strip()
                prompt_tokens = getattr(outcome, "prompt_tokens", None)
                generation_tokens = getattr(outcome, "generation_tokens", None)
                peak = getattr(outcome, "peak_memory", None)
                peak_gb = round(float(peak), 3) if peak else None

                parsed = extract_json(raw)
                if parsed is not None:
                    break

        if parsed is None:
            log_event("vlm.unparseable", prompt=prompt_version, raw=raw[:500])
            return VLMOutput(
                result={"error": "unparseable", "raw": raw},
                prompt_version=prompt_version,
                raw=raw,
                parsed=False,
                attempts=attempts,
                prompt_tokens=prompt_tokens,
                generation_tokens=generation_tokens,
                peak_memory_gb=peak_gb,
            )

        return VLMOutput(
            result=parsed,
            prompt_version=prompt_version,
            raw=raw,
            parsed=True,
            attempts=attempts,
            prompt_tokens=prompt_tokens,
            generation_tokens=generation_tokens,
            peak_memory_gb=peak_gb,
        )


def active_memory_gb() -> float | None:
    """MLX's own accounting of unified memory currently held by arrays."""
    try:
        import mlx.core as mx

        getter = getattr(mx, "get_active_memory", None) or getattr(
            getattr(mx, "metal", None), "get_active_memory", None
        )
        if getter is None:
            return None
        return round(getter() / 1024**3, 3)
    except Exception:  # noqa: BLE001
        return None


vlm_engine = VLMEngine()
