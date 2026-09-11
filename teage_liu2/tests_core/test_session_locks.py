"""L-11 会话并发互斥锚定:`SessionLocks` 此前**零测试**。

锚定:①同 session 串行化(不交错)②严格 LRU 淘汰空闲锁 ③持有中的锁不可淘汰
(互斥优先,允许临时超限)。
"""
from __future__ import annotations

import asyncio

from teage_liu2.server.session_locks import SessionLocks


def test_same_session_is_serialized():
    """同 session 两个并发任务不得交错(enter/exit 成对)。"""
    locks = SessionLocks()
    order = []

    async def worker(tag: str):
        lock = await locks.get("s1")
        async with lock:
            order.append(f"{tag}:enter")
            await asyncio.sleep(0.01)
            order.append(f"{tag}:exit")

    async def main():
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(main())
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    )


def test_different_sessions_are_independent():
    """不同 session 各自一把锁,可并行。"""
    locks = SessionLocks()

    async def main():
        l1 = await locks.get("s1")
        l2 = await locks.get("s2")
        assert l1 is not l2
        assert locks.size == 2

    asyncio.run(main())


def test_lru_evicts_idle_lock():
    """超过上限时淘汰最旧的空闲锁(有界缓存)。"""
    locks = SessionLocks(max_size=2)

    async def touch(sid: str):
        await locks.get(sid)

    async def main():
        await touch("s1")
        await touch("s2")
        await touch("s3")

    asyncio.run(main())
    assert locks.size == 2
    assert "s1" not in locks._locks


def test_held_lock_not_evicted():
    """持有中的锁不可淘汰:超限时优先淘汰空闲锁(此处为新建的 s2),s1 保留。"""
    locks = SessionLocks(max_size=1)

    async def main():
        held = await locks.get("s1")
        async with held:
            await locks.get("s2")  # 超限 → 空闲的 s2 被淘汰
            assert locks._locks.get("s1") is held
            assert locks.size == 1

    asyncio.run(main())


def test_drop_and_clear():
    locks = SessionLocks()

    async def main():
        await locks.get("s1")
        await locks.get("s2")
        locks.drop("s1")
        assert locks.size == 1
        locks.clear()
        assert locks.size == 0

    asyncio.run(main())
