"""P0-1 回归:跨 session 并发的终态快照隔离(2026-09-11 综合评审)。

缺陷背景:形态实例(ReactLoop/BareMode)构造一次跨对话复用,``final_snapshot``
是实例字段、各推进点覆写 —— 不同 session 并发对话时互相覆写:A 会话
await LLM/钩子期间 B 覆写共享字段,A 收尾时 after/on_error 拿到 B 的终态
快照、且 ``_persist_session_extra`` 把 B 的 extra 写进 A 的 SessionStore。
修复:形态实例改为每对话新建(pipeline._make_mode_instance)。
"""

from __future__ import annotations

import asyncio

from teage_liu2.core.actions import SetExtra
from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.session import SessionStore
from teage_liu2.core.types import EV_DONE
from .fake_llm import FakeLLMClient


class _GatedLLM(FakeLLMClient):
    """按首条 user 输入对指定会话的 LLM 流式调用制造延迟(交错窗口)。

    窗口设计:bare 形态下 run_stream 仅在开头覆写一次 final_snapshot、done
    不再覆写 —— 慢会话的 LLM await 期间,快会话完整跑完并覆写共享字段;
    慢会话醒来后直接收尾(after/extra 写回),正是共享实例缺陷的暴露窗口。
    """

    async def chat_main_stream(self, messages, **kwargs):
        if messages and messages[0].get("content") == "慢会话":
            await asyncio.sleep(0.15)
        async for ev in super().chat_main_stream(messages, **kwargs):
            yield ev


class _ExtraWriterBranch(Branch):
    """before 钩子写入会话归属标记(验证 extra 写回不串会话)。"""

    name = "extrawriter"
    capabilities: list = []

    async def before(self, snapshot):
        return [SetExtra(key="extrawriter.owner", value=snapshot.session_id)]


class _FinalRecorderBranch(Branch):
    """终态钩子观测:记录 after 收到的快照与 done 事件(会话归属交叉验证)。"""

    name = "recorder"
    capabilities = ["observe"]

    def __init__(self) -> None:
        self.after_seen: list = []

    async def after(self, snapshot, response):
        done_session = None
        if getattr(response, "done_event", None):
            done_session = response.done_event.get("session_id")
        self.after_seen.append(
            {"snapshot_session": snapshot.session_id, "done_session": done_session}
        )
        return []


def _make_concurrent_pipeline(tmp_path, llm, branches, mode="loop"):
    store = SQLiteHistoryStore(str(tmp_path / "conc_sessions.db"))
    session_store = SessionStore()
    hooks = HookChain()
    for b in branches:
        hooks.register(b)
    pipeline = ChatPipeline(
        llm_client=llm,
        history_store=store,
        hooks=hooks,
        mode=mode,
        session_store=session_store,
        base_system_prompt="并发测试",
    )
    return pipeline, session_store


def test_concurrent_sessions_isolated_final_snapshot(tmp_path):
    """双 session 并发(bare 形态):after 终态快照归属正确 + extra 不跨会话串写。"""
    llm = _GatedLLM()
    recorder = _FinalRecorderBranch()
    pipeline, session_store = _make_concurrent_pipeline(
        tmp_path, llm, [_ExtraWriterBranch(), recorder], mode="bare"
    )

    async def drive(session_id: str, user_input: str):
        events = []
        async for ev in pipeline.chat_stream(session_id, user_input):
            events.append(ev)
        return events

    async def main():
        # s1 的 LLM 慢调用制造窗口;s2 在窗口内完整跑完并收尾
        return await asyncio.gather(drive("s1", "慢会话"), drive("s2", "快会话"))

    e1, e2 = asyncio.run(main())
    assert any(ev.get("type") == EV_DONE for ev in e1)
    assert any(ev.get("type") == EV_DONE for ev in e2)

    # ① after 终态快照归属:快照 session 必须与本次对话 done 的 session 一致
    # (修复前共享 final_snapshot 被后完成者覆写 → 拿到对方会话的快照)
    assert len(recorder.after_seen) == 2
    for seen in recorder.after_seen:
        assert seen["snapshot_session"] == seen["done_session"], (
            f"after 钩子收到跨会话快照: {seen}"
        )
    seen_sessions = {s["snapshot_session"] for s in recorder.after_seen}
    assert seen_sessions == {"s1", "s2"}

    # ② extra 写回不跨会话串写:各自 store 只含本会话 before 写入的 owner
    assert session_store.get("s1").get("extrawriter.owner") == "s1"
    assert session_store.get("s2").get("extrawriter.owner") == "s2"


def test_rebind_hooks_uses_new_chain_next_conversation(tmp_path):
    """rebind_hooks 后,新对话使用新链(每对话新建实例以 self.hooks 构造)。"""

    class _Marker(Branch):
        name = "marker"
        capabilities: list = []

        def __init__(self) -> None:
            self.called = False

        async def before(self, snapshot):
            self.called = True
            return []

    llm = FakeLLMClient()
    pipeline, _ = _make_concurrent_pipeline(tmp_path, llm, [])
    new_chain = HookChain()
    marker = _Marker()
    new_chain.register(marker)
    pipeline.rebind_hooks(new_chain)
    assert pipeline.hooks is new_chain

    async def main():
        events = []
        async for ev in pipeline.chat_stream("s-rebind", "你好"):
            events.append(ev)
        return events

    asyncio.run(main())
    assert marker.called, "rebind 后新对话未经过新链枝干"
