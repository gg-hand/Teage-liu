"""2026-09-11 综合评审修复锚定(P1-1/P1-3/P1-4/P1-5 + P2 项)。

对应计划 `docs/plans/2026-09-11-综合评审修复计划.md` WP-C/D/F。
每条测试对应一个 P1 修复,变异验证见计划 §2(退化必红)。
"""
from __future__ import annotations

import asyncio

import pytest

from teage_liu2.core.actions import SetTools
from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain, ToolDecision
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.registry import BranchRegistry
from teage_liu2.core.errors import HOOK_INVALID_ACTION, TOOL_REJECTED_BY_POLICY
from teage_liu2.core.supervisor import Supervisor
from teage_liu2.core.transport import TransportBus
from teage_liu2.core.types import EV_DONE, EV_ERROR, EV_TOOL_RESULT
from .fake_llm import FakeLLMClient
from .test_supervisor import _FakeAdapter, _FakeChannel


async def _collect(agen):
    return [ev async for ev in agen]


def _make_pipeline(tmp_path, llm, branches, **kwargs):
    store = SQLiteHistoryStore(str(tmp_path / "rev_sessions.db"))
    hooks = HookChain()
    for b in branches:
        hooks.register(b)
    pipeline = ChatPipeline(
        llm_client=llm, history_store=store, hooks=hooks,
        base_system_prompt="评审修复测试", **kwargs,
    )
    return pipeline, store


# ---------------------------------------------------------------------------
# P1-1:入口 user 消息超限 → 拒绝且**不落盘**(此前先落盘后校验,污染历史库)
# ---------------------------------------------------------------------------
def test_oversize_input_rejected_and_not_persisted(tmp_path):
    llm = FakeLLMClient()
    pipeline, store = _make_pipeline(
        tmp_path, llm, [], resource_limits={"max_message_bytes": 64}
    )
    events = asyncio.run(_collect(pipeline.chat_stream("s-p11", "x" * 200)))
    errors = [e for e in events if e.get("type") == EV_ERROR]
    assert errors and errors[0].get("code") == HOOK_INVALID_ACTION
    # 修复点:被拒输入不得进入历史库(修复前 flush=True 先落盘)
    assert store.get_session_messages("s-p11") == []


# ---------------------------------------------------------------------------
# P1-4:轮间增量收口失败 error 事件带 code(与首轮孪生路径一致)。
# 注:真实序列被 merge 防护,轮间 problem 属防御分支,故 monkeypatch 收口函数
# 直接锚定 loop 的 error 事件构造(带码与否)。
# ---------------------------------------------------------------------------
def test_interround_finalize_problem_error_has_code(tmp_path, monkeypatch):
    import teage_liu2.core.loop as loop_mod

    def _fake_finalize(messages, round_injections=None):
        return messages, "构造的轮间校验失败"

    monkeypatch.setattr(loop_mod, "incremental_finalize", _fake_finalize)
    llm = FakeLLMClient()
    pipeline, _ = _make_pipeline(tmp_path, llm, [])
    events = asyncio.run(_collect(pipeline.chat_stream("s-p14", "触发轮间失败")))
    error_events = [e for e in events if e.get("type") == EV_ERROR]
    # 轮间增量收口失败 → error 事件必须带事件面①错误码(修复前缺失)
    assert error_events and error_events[0].get("code") == HOOK_INVALID_ACTION
    assert not any(e.get("type") == EV_DONE for e in events)


# ---------------------------------------------------------------------------
# P1-5:畸形 input_schema(minLength 为字符串)→ 校验器失能 fail-open,不炸对话
# ---------------------------------------------------------------------------
def test_malformed_tool_schema_does_not_crash_conversation(tmp_path):
    class _ModifyWithBadSchema(Branch):
        name = "badschema"
        capabilities: list = []

        async def before(self, snapshot):
            return [SetTools(tools=[{
                "name": "t1", "description": "d",
                # 畸形:minLength 应为整数,实际字符串 → 比较抛 TypeError
                "input_schema": {"type": "object", "properties": {"x": {"minLength": "3"}}},
            }])]

        async def pre_tool_call(self, snapshot, name, input):
            return ToolDecision(decision="modify", input={"x": "ab"})

        async def on_tool_call(self, snapshot, name, input):
            return "ok"

    llm = FakeLLMClient([
        {"content": [{"type": "tool_use", "id": "u1", "name": "t1", "input": {}}],
         "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
    ])
    pipeline, _ = _make_pipeline(tmp_path, llm, [_ModifyWithBadSchema()])
    # 修复前:TypeError 从 pre_tool_call_all 穿透 → 对话中断;修复后 fail-open 放行
    events = asyncio.run(_collect(pipeline.chat_stream("s-p15", "畸形 schema")))
    assert any(e.get("type") == EV_DONE for e in events)
    tool_results = [e for e in events if e.get("type") == EV_TOOL_RESULT]
    assert tool_results and tool_results[0].get("is_error") is not True


# ---------------------------------------------------------------------------
# observe 扩展 pre_tool_call 决策忽略(E-9③;此前只忽略 action,决策仍生效)
# ---------------------------------------------------------------------------
def test_observe_pre_tool_call_decision_ignored(tmp_path):
    class _ObserverReject(Branch):
        name = "obsreject"
        capabilities = ["observe"]

        async def pre_tool_call(self, snapshot, name, input):
            return ToolDecision(decision="reject", reason="观测扩展不得干预")

    class _Executor(Branch):
        name = "exec"
        capabilities: list = []

        async def on_tool_call(self, snapshot, name, input):
            return "executed"

    llm = FakeLLMClient([
        {"content": [{"type": "tool_use", "id": "u1", "name": "t1", "input": {}}],
         "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
    ])
    pipeline, _ = _make_pipeline(tmp_path, llm, [_ObserverReject(), _Executor()])
    events = asyncio.run(_collect(pipeline.chat_stream("s-obs", "observe 只读")))
    tool_results = [e for e in events if e.get("type") == EV_TOOL_RESULT]
    # observe 的 reject 决策被忽略 → 工具正常执行
    assert tool_results and tool_results[0].get("is_error") is False
    assert tool_results[0].get("result") == "executed"


# ---------------------------------------------------------------------------
# H-5 post_tool_call 分支:轮中 SetStop 短路后续扩展 + 剩余工具拦截配对
# ---------------------------------------------------------------------------
def test_post_tool_call_setstop_short_circuits_chain_and_rest_tools(tmp_path):
    calls = {"second_post": 0}

    from teage_liu2.core.actions import SetStop

    class _Stopper(Branch):
        name = "stopper"
        capabilities: list = []

        async def post_tool_call(self, snapshot, name, input, result, duration):
            return [SetStop(reason="安全拦截")]

    class _Second(Branch):
        name = "second"
        capabilities: list = []

        async def post_tool_call(self, snapshot, name, input, result, duration):
            calls["second_post"] += 1
            return []

    class _Executor(Branch):
        name = "exec"
        capabilities: list = []

        async def on_tool_call(self, snapshot, name, input):
            return f"ran:{name}"

    llm = FakeLLMClient([
        {"content": [
            {"type": "tool_use", "id": "u1", "name": "t1", "input": {}},
            {"type": "tool_use", "id": "u2", "name": "t2", "input": {}},
        ], "stop_reason": "tool_use"},
    ])
    pipeline, _ = _make_pipeline(tmp_path, llm, [_Stopper(), _Second(), _Executor()])
    events = asyncio.run(_collect(pipeline.chat_stream("s-h5post", "两工具拦截")))
    # ① stopper 的 post 短路 second 的同名钩子
    assert calls["second_post"] == 0
    # ② 剩余工具 u2 被拦截且事件面配对完整(H-19)
    results = [e for e in events if e.get("type") == EV_TOOL_RESULT]
    by_id = {e.get("tool_use_id"): e for e in results}
    assert by_id["u2"].get("is_error") is True
    assert by_id["u2"].get("code") == TOOL_REJECTED_BY_POLICY
    # ③ done(intercepted)
    dones = [e for e in events if e.get("type") == EV_DONE]
    assert dones and dones[0].get("termination_reason") == "intercepted"


# ---------------------------------------------------------------------------
# P1-2 依赖契约:registry.shutdown 对 None 存储跳过 close(外壳重排的前提)
# 且幂等标志置位后不再关闭
# ---------------------------------------------------------------------------
def test_registry_shutdown_partial_close_skips_stores():
    class _Store:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    reg = BranchRegistry()
    ms, sp = _Store(), _Store()
    asyncio.run(reg.shutdown(task_registry=None, message_store=None, storage_provider=None))
    assert not ms.closed and not sp.closed
    # 幂等标志已置位:补传真实存储也不会再关(外壳须自行 close,见 app.py 重排)
    asyncio.run(reg.shutdown(task_registry=None, message_store=ms, storage_provider=sp))
    assert not ms.closed and not sp.closed


# ---------------------------------------------------------------------------
# P1-3:同名扩展热重载失败回滚后,旧扩展 bus 身份恢复(修复前注销且不恢复)
# ---------------------------------------------------------------------------
class _SpawnByNameChannel(_FakeChannel):
    """按 name 决定 start 是否失败的通道(ext_b 触发回滚)。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._name = kwargs.get("name", "")

    async def start(self):
        if self._name == "ext_b":
            raise RuntimeError("boom")

def test_reload_rollback_restores_bus_identity(monkeypatch):
    monkeypatch.setattr("teage_liu2.core.supervisor.StdioChannel", _SpawnByNameChannel)
    monkeypatch.setattr("teage_liu2.core.supervisor.RemoteBranchAdapter", _FakeAdapter)
    sup = Supervisor(TransportBus(), heartbeat_interval=0.01)
    # 预置同名旧扩展:已 spawn/已注册身份
    old_cfg = {"transport": "stdio", "command": ["old"], "capabilities": []}
    sup._ext_configs["ext_a"] = dict(old_cfg)
    sup._channels["ext_a"] = _FakeChannel()
    sup._adapters["ext_a"] = _FakeAdapter("ext_a", old_cfg, sup._channels["ext_a"])
    sup._bus.register_extension("ext_a", [])
    assert sup._bus.is_registered("ext_a")

    new_cfg = {"core": {"branches": {
        "ext_a": {"transport": "stdio", "command": ["new"]},
        "ext_b": {"transport": "stdio", "command": ["boom"]},
    }}}
    with pytest.raises(Exception):
        asyncio.run(sup.reload(new_cfg, BranchRegistry()))
    # 修复点:回滚恢复旧扩展的 bus 注册(修复前 _shutdown_channel 注销后无人恢复)
    assert sup._bus.is_registered("ext_a"), (
        "热重载回滚后旧扩展 bus 身份未恢复 → 宿主通道全死(P1-3)"
    )
