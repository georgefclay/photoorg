"""A lock where interactive requests cut in front of batch items.

One VLM job at a time (MLX + 16 GB), but a batch must not make the Triage UI wait
for the rest of the run: batch items only take the lock when nothing interactive
is queued.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class PriorityLock:
    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._locked = False
        self._waiting_high = 0
        self._waiting_low = 0

    @property
    def queue_depth(self) -> int:
        return self._waiting_high + self._waiting_low + (1 if self._locked else 0)

    @asynccontextmanager
    async def acquire(self, high_priority: bool = True):
        async with self._cond:
            if high_priority:
                self._waiting_high += 1
                try:
                    while self._locked:
                        await self._cond.wait()
                finally:
                    self._waiting_high -= 1
            else:
                self._waiting_low += 1
                try:
                    while self._locked or self._waiting_high > 0:
                        await self._cond.wait()
                finally:
                    self._waiting_low -= 1
            self._locked = True
        try:
            yield
        finally:
            async with self._cond:
                self._locked = False
                self._cond.notify_all()
