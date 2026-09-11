"""L-11 会话并发互斥锚定:`SessionLocks` 此前**零测试**。

锚定:①同 session 串行化(不交错)②严格 LRU 淘汰空闲锁 ③持有中的锁不可淘汰
(互斥优先,允许临时超限)④**边界**:满缓存时同一 session 连续 get 必须拿到同一把锁
(否则互斥失效 —— 见 2026-09-11 分析发现的老实现漏洞)。
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
    """持有中的锁不可淘汰:其余锁全被持有时,缓存**临时超限**(互斥优先)。"""
    locks = SessionLocks(max_size=1)

    async def main():
        held = await locks.get("s1")
        async with held:
            returned = await locks.get("s2")  # 其余锁(s1)全被持有 → 临时超限,不淘汰
            assert locks._locks.get("s1") is held
            assert locks._locks.get("s2") is returned
            assert locks.size == 2

    asyncio.run(main())


def test_just_returned_lock_not_evicted():
    """边界:满缓存时同一 session 连续 get 必须拿到**同一把锁**(否则互斥失效)。"""
    locks = SessionLocks(max_size=1)

    async def main():
        held = await locks.get("s1")
        async with held:
            first = await locks.get("s2")
            second = await locks.get("s2")
            assert first is second, "同 session 两次 get 返回了不同锁 → 互斥被破坏"
            assert locks._locks.get("s2") is first

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
