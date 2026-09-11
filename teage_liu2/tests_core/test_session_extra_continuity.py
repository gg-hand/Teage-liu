"""L-10 会话态 extra 跨对话延续锚定 —— 2026-09-11 落地审查补锚定。

此前仅有"容器语义"测试(`test_session.py`),**无跨对话延续**锚定:
第一轮 `before` 写入 `SetExtra` → 结束后写回 `SessionStore` → 第二轮快照
`extra` 基座恢复该值。
"""
from __future__ import annotations

import asyncio

from teage_liu2.core.actions import SetExtra
from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import MODE_LOOP, ChatPipeline
from teage_liu2.core.session import SessionStore

from .fake_llm import FakeLLMClient


class _MarkerBranch(Branch):
    """在 ``before`` 记录本轮快照 extra,并写入 extra.marker=1。"""

    name = "marker"
    capabilities = []

    def __init__(self) -> None:
        self.seen = []

    async def before(self, snapshot):
        self.seen.append(dict(snapshot.extra))
        return [SetExtra(key="extra.marker", value=1)]


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]

    return asyncio.run(_run())


def test_session_extra_continues_across_turns(tmp_path):
    store = SQLiteHistoryStore(str(tmp_path / "s.db"))
    session_store = SessionStore()
    branch = _MarkerBranch()
    hooks = HookChain()
    hooks.register(branch)
    llm = FakeLLMClient([
        {"content": [{"type": "text", "text": "r1"}], "stop_reason": "end_turn"},
        {"content": [{"type": "text", "text": "r2"}], "stop_reason": "end_turn"},
    ])
    pipeline = ChatPipeline(
        llm_client=llm,
        history_store=store,
        hooks=hooks,
        mode=MODE_LOOP,
        session_store=session_store,
    )

    _collect(pipeline.chat_stream("s-extra", "第一轮"))
    # 第一轮结束 → 写回 SessionStore
    assert session_store.get("s-extra").get("extra.marker") == 1

    _collect(pipeline.chat_stream("s-extra", "第二轮"))
    assert branch.seen[0] == {}                    # 第一轮基座为空
    assert branch.seen[1].get("extra.marker") == 1  # 第二轮延续上一轮写入
    store.close()
