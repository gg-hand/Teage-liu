"""L-5 每扩展一进程 / L-6 崩溃隔离与僵死重建 —— 2026-09-11 落地审查补锚定。

此前 `core/supervisor.py` 与 `core/stdio.py` 的心跳链**零测试**。
本文件锚定:①stdio 配置扫描(启用/禁用)②僵死重建成功计数 ③重建失败降级
④通道层心跳连续失败 → `on_dead`(僵死判定)。
"""
from __future__ import annotations

import asyncio
import types

from teage_liu2.core.supervisor import Supervisor
from teage_liu2.core.transport import TransportBus


class _FakeChannel:
    fail_start = False

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.on_dead = None

    def set_on_dead(self, cb):
        self.on_dead = cb

    async def start(self):
        if type(self).fail_start:
            raise RuntimeError("spawn failed")

    async def close(self, reason="host_shutdown", shutdown_timeout=1.0):
        self.closed = True


class _FailChannel(_FakeChannel):
    fail_start = True


class _FakeAdapter:
    def __init__(self, name, declaration, channel):
        self.name = name
        self.channel = channel

    async def setup(self, config, host=None):
        return None

    async def teardown(self):
        return None


def _patch(monkeypatch, channel_cls):
    monkeypatch.setattr("teage_liu2.core.supervisor.StdioChannel", channel_cls)
    monkeypatch.setattr("teage_liu2.core.supervisor.RemoteBranchAdapter", _FakeAdapter)


def test_iter_stdio_entries_only_enabled_stdio():
    """L-5:只有启用的 stdio 扩展各占一个进程条目(禁用的跳过)。"""
    cfg = {"core": {"branches": {
        "ext_a": {"transport": "stdio", "command": ["py", "-c", ""]},
        "ext_c": {"enabled": False, "transport": "stdio", "command": ["py", "-c", ""]},
    }}}
    entries = Supervisor._iter_stdio_entries(cfg)
    assert [name for name, _ in entries] == ["ext_a"]


def test_auto_rebuild_success_counts_and_clears_degraded(monkeypatch):
    """L-6:僵死重建成功 → 计数 +1、不降级、通道已替换。"""
    _patch(monkeypatch, _FakeChannel)
    sup = Supervisor(TransportBus(), heartbeat_interval=0.01,
                     restart_max_retries=2, restart_backoff_base=0.0)
    sup._ext_configs["ext1"] = {"command": ["py", "-c", ""], "capabilities": []}
    asyncio.run(sup._auto_rebuild("ext1", "heartbeat_failed_3"))
    assert sup.process_restarts == 1
    assert "ext1" not in sup.degraded
    assert isinstance(sup._channels["ext1"], _FakeChannel)


def test_auto_rebuild_failure_marks_degraded(monkeypatch):
    """L-6:重建失败(重试耗尽)→ 降级标记,不无限重试。"""
    _patch(monkeypatch, _FailChannel)
    sup = Supervisor(TransportBus(), restart_max_retries=2, restart_backoff_base=0.0)
    sup._ext_configs["ext1"] = {"command": ["py", "-c", ""]}
    asyncio.run(sup._auto_rebuild("ext1", "heartbeat_failed_3"))
    assert sup.process_restarts == 0
    assert "ext1" in sup.degraded


def test_heartbeat_failures_trigger_on_dead(monkeypatch):
    """L-6:通道层心跳连续失败达阈值 → on_dead(僵死判定)。"""
    from teage_liu2.core.stdio import StdioChannel

    ch = StdioChannel("ext1", ["py", "-c", ""], lambda name, frame: None,
                      heartbeat_interval=0.01)
    ch._proc = types.SimpleNamespace(returncode=None)

    async def _boom(*args, **kwargs):
        raise RuntimeError("heartbeat failed")

    monkeypatch.setattr(ch, "request", _boom)
    dead = []

    async def _on_dead(name, reason):
        dead.append((name, reason))

    ch.set_on_dead(_on_dead)

    async def _run():
        task = asyncio.create_task(ch._heartbeat_loop())
        for _ in range(300):
            if dead:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert dead and dead[0][0] == "ext1"
    assert dead[0][1].startswith("heartbeat_failed_")
