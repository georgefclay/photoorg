"""Interactive requests must not wait behind a whole overnight batch."""

from __future__ import annotations

import asyncio

from app.priority import PriorityLock


async def test_interactive_request_cuts_ahead_of_a_queued_batch_item():
    lock = PriorityLock()
    order: list[str] = []

    async def take(name: str, high_priority: bool):
        async with lock.acquire(high_priority=high_priority):
            order.append(name)
            await asyncio.sleep(0)

    async with lock.acquire(high_priority=False):  # a batch item holds the model
        order.append("batch-running")
        batch_next = asyncio.create_task(take("batch-queued", False))
        await asyncio.sleep(0.01)  # let the batch item get in the queue first
        interactive = asyncio.create_task(take("interactive", True))
        await asyncio.sleep(0.01)

    await asyncio.gather(batch_next, interactive)
    assert order == ["batch-running", "interactive", "batch-queued"]


async def test_only_one_holder_at_a_time():
    lock = PriorityLock()
    concurrent = 0
    peak = 0

    async def worker(high_priority: bool):
        nonlocal concurrent, peak
        async with lock.acquire(high_priority=high_priority):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.005)
            concurrent -= 1

    await asyncio.gather(*(worker(i % 2 == 0) for i in range(10)))
    assert peak == 1


async def test_queue_depth_counts_holder_and_waiters():
    lock = PriorityLock()
    assert lock.queue_depth == 0

    async def waiter():
        async with lock.acquire(high_priority=True):
            await asyncio.sleep(0.02)

    async with lock.acquire(high_priority=True):
        assert lock.queue_depth == 1
        tasks = [asyncio.create_task(waiter()) for _ in range(3)]
        await asyncio.sleep(0.01)
        assert lock.queue_depth == 4
    await asyncio.gather(*tasks)
    assert lock.queue_depth == 0


async def test_lock_is_released_when_the_body_raises():
    lock = PriorityLock()
    try:
        async with lock.acquire():
            raise RuntimeError("model blew up")
    except RuntimeError:
        pass
    assert lock.queue_depth == 0
    async with lock.acquire():
        pass


async def test_cancelling_a_waiter_does_not_wedge_the_lock():
    lock = PriorityLock()

    async def waiter():
        async with lock.acquire(high_priority=True):
            await asyncio.sleep(1)

    async with lock.acquire(high_priority=True):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        assert lock.queue_depth == 2
        task.cancel()
        await asyncio.sleep(0.01)
    assert lock.queue_depth == 0
    async with lock.acquire(high_priority=False):
        pass
