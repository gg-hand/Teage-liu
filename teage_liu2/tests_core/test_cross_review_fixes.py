"""交叉评审修正批次的承重锚定（2026-09-11）。

覆盖本轮修复的"此前零锚定"项，每条都做了变异验证（退化必红）：

- **H-5 轮中短路**：post_tool_call / after_step 的 SetStop 必须短路后续扩展。
- **H-20**：modify 后入参重过工具 `input_schema`，非法 → 视同策略拒绝。
- **observe 只读**：observe 扩展的 `ToolDecision` 一律忽略（E-9 只读约束）。
- **入口上限**：`chat_stream` 的 user 输入与扩展 AppendMessage 同一套资源上限。
- **资源上限接线（P1-4）**：`configure_limits` / `ChatPipeline(resource_limits=)`
  真正改变生效上限。
- **SetExtra 可序列化**：非 JSON 值被 schema 级校验拒绝（否则体积记账失效）。
- **query filters 先于 limit**（P-4 条款⑧）。
- **import 边界（lifecycle §3.3）**：入口 import 外壳/其他枝干/老系统 → 拒绝装载。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from teage_liu2.core.actions import SetExtra, SetStop, ToolDecision, validate_action
from teage_liu2.core.extension_loader import ExtensionSpec, check_import_boundary
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.storage import SQLiteStorageProvider
from teage_liu2.core.snapshot import active_limits, apply_action_batch, configure_limits
from teage_liu2.core.types import RESOURCE_LIMITS, Snapshot


def _snapshot(tools=None) -> Snapshot:
    snap = Snapshot(session_id="s-x", user_input="hi")
    if tools is not None:
        snap = snap.with_tools(tools)
    return snap


# ---------------------------------------------------------------------------
# H-5：轮中 SetStop 短路后续扩展（此前只对 before 短路）
# ---------------------------------------------------------------------------
class _Recorder(Branch):
    def __init__(self, name: str, stop: bool = False) -> None:
        self.name = name
        self.calls: list = []
        self._stop = stop

    async def post_tool_call(self, snapshot, name, input, result, duration):
        self.calls.append("post_tool_call")
        return [SetStop(reason="blocked")] if self._stop else []

    async def after_step(self, snapshot, summary):
        self.calls.append("after_step")
        return [SetStop(reason="blocked")] if self._stop else []


def _chain_two_hooks(stop_first: bool):
    first = _Recorder("first", stop=stop_first)
    second = _Recorder("second")
    chain = HookChain()
    chain.register(first)
    chain.register(second)
    return chain, first, second


def test_post_tool_call_set_stop_short_circuits():
    chain, first, second = _chain_two_hooks(stop_first=True)
    snap = _snapshot()
    out = asyncio.run(chain.post_tool_call_all(snap, "echo", {}, "ok", 0.1))
    assert out.stop is True
    assert first.calls == ["post_tool_call"]
    assert second.calls == [], "H-5：SetStop 必须短路后续扩展的同名钩子"


def test_after_step_set_stop_short_circuits():
    chain, first, second = _chain_two_hooks(stop_first=True)
    snap = _snapshot()
    out = asyncio.run(chain.after_step_all(snap, object()))
    assert out.stop is True
    assert second.calls == []


def test_without_stop_all_extensions_called():
    """反例守卫：不置 stop 时两个扩展都必须被调用（防"短路"实现过度）。"""
    chain, first, second = _chain_two_hooks(stop_first=False)
    asyncio.run(chain.after_step_all(_snapshot(), object()))
    assert first.calls == ["after_step"] and second.calls == ["after_step"]


# ---------------------------------------------------------------------------
# H-20：modify 后重过工具 input_schema
# ---------------------------------------------------------------------------
_TOOL = {
    "name": "echo",
    "description": "回显",
    "input_schema": {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    },
}


class _ModifyTo(Branch):
    def __init__(self, payload):
        self.name = "modifier"
        self._payload = payload

    async def pre_tool_call(self, snapshot, name, input):
        return ToolDecision(decision="modify", input=self._payload)


def test_h20_modify_invalid_input_is_rejected():
    chain = HookChain()
    chain.register(_ModifyTo({"x": "not-an-int"}))
    decision, effective = asyncio.run(
        chain.pre_tool_call_all(_snapshot([_TOOL]), "echo", {"x": 1})
    )
    assert decision.is_reject, "H-20：modify 后不合约的入参必须视同策略拒绝"
    assert decision.reason
    assert effective == {"x": "not-an-int"}


def test_h20_modify_valid_input_passes():
    chain = HookChain()
    chain.register(_ModifyTo({"x": 7}))
    decision, effective = asyncio.run(
        chain.pre_tool_call_all(_snapshot([_TOOL]), "echo", {"x": 1})
    )
    assert decision.decision == "allow"
    assert effective == {"x": 7}


# ---------------------------------------------------------------------------
# observe 只读：ToolDecision 一律忽略（E-9）
# ---------------------------------------------------------------------------
class _ObserveReject(Branch):
    name = "observer"
    capabilities = ["observe"]

    async def pre_tool_call(self, snapshot, name, input):
        return ToolDecision(decision="reject", reason="想拦但我是只读的")


def test_observe_tool_decision_is_ignored(caplog):
    chain = HookChain()
    chain.register(_ObserveReject())
    import logging

    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.hooks"):
        decision, _ = asyncio.run(chain.pre_tool_call_all(_snapshot(), "echo", {"x": 1}))
    assert decision.decision == "allow", "observe 扩展不得影响工具执行（只读约束）"


# ---------------------------------------------------------------------------
# SetExtra 可序列化（否则资源上限失效）
# ---------------------------------------------------------------------------
def test_setextra_non_json_value_rejected():
    problem = validate_action(SetExtra(key="a.b", value=object()))
    assert problem is not None and "序列化" in problem


def test_setextra_json_value_accepted():
    assert validate_action(SetExtra(key="a.b", value={"k": [1, 2]})) is None


# ---------------------------------------------------------------------------
# 资源上限接线（P1-4）
# ---------------------------------------------------------------------------
def test_configure_limits_takes_effect():
    try:
        applied = configure_limits({"max_message_bytes": 64})
        assert applied["max_message_bytes"] == 64
        assert active_limits()["max_message_bytes"] == 64
        snap = _snapshot()
        from teage_liu2.core.actions import AppendMessage

        out, invalid = apply_action_batch(
            snap, [AppendMessage(message={"role": "user", "content": "x" * 200})]
        )
        assert invalid and "超上限" in invalid[0], "收紧后的上限必须真的生效"
        assert out.messages == snap.messages
    finally:
        configure_limits(None)
    assert active_limits()["max_message_bytes"] == RESOURCE_LIMITS["max_message_bytes"]


def test_unknown_limit_key_ignored():
    try:
        applied = configure_limits({"nope": 1, "max_loops": 5})
        assert "nope" not in applied
        assert applied["max_snapshot_bytes"] == RESOURCE_LIMITS["max_snapshot_bytes"]
    finally:
        configure_limits(None)


def test_entry_user_message_hits_resource_limit(tmp_path):
    """入口 user 消息与扩展 AppendMessage 同一套上限（此前入口绕过上限）。"""
    from teage_liu2.core.history import SQLiteHistoryStore
    from teage_liu2.core.pipeline import ChatPipeline

    from .fake_llm import FakeLLMClient

    try:
        configure_limits({"max_message_bytes": 128})
        pipe = ChatPipeline(
            llm_client=FakeLLMClient([]),
            history_store=SQLiteHistoryStore(str(tmp_path / "h.db")),
        )

        async def _run():
            return [ev async for ev in pipe.chat_stream("s-entry", "x" * 500)]

        events = asyncio.run(_run())
    finally:
        configure_limits(None)
    assert events and events[-1]["type"] == "error", "超限入口消息必须转 error 事件"
    assert events[-1]["code"] == "HOOK_INVALID_ACTION"


# ---------------------------------------------------------------------------
# P-4 条款⑧：query 的 filters 先于 limit
# ---------------------------------------------------------------------------
def test_query_filters_before_limit():
    tmp = tempfile.mkdtemp(prefix="bs_queryfix_")
    db = Path(tmp) / "q.db"
    provider = SQLiteStorageProvider(str(db))
    try:
        provider.write("t", [{"tag": "a", "n": 1}])   # rowid 1
        provider.write("t", [{"tag": "b", "n": 2}])   # rowid 2
        provider.write("t", [{"tag": "a", "n": 3}])   # rowid 3
        # 先过滤再限条：tag=a 且 limit=1 → 必须返回 rowid 1（而非"最早 1 条里过滤"）
        rows = provider.query("t", limit=1, tag="a")
        assert [r["n"] for r in rows] == [1]
        # 不带 limit 时全量匹配
        assert [r["n"] for r in provider.query("t", tag="a")] == [1, 3]
    finally:
        provider.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# import 边界（lifecycle §3.3）
# ---------------------------------------------------------------------------
def _spec(tmp_dir: Path, entry: str) -> ExtensionSpec:
    return ExtensionSpec(
        name="bad", version="1.0.0", description="", language="python", entry=entry,
        transport=None, command=None, protocol_version=None, capabilities=[],
        requirements=[], kind="branch", slots=[], path=str(tmp_dir),
        manifest_hash="x",
    )


@pytest.mark.parametrize(
    "stmt",
    [
        "import teage_liu2.server",
        "from teage_liu2.branches import x",
        "from teage_liu import y",
        "import teage_liu.tasks.scheduler",
    ],
)
def test_import_boundary_rejects_forbidden(stmt):
    with tempfile.TemporaryDirectory(prefix="bs_import_") as tmp:
        entry = Path(tmp) / "main.py"
        entry.write_text(f"{stmt}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="import 边界"):
            check_import_boundary(_spec(Path(tmp), "main.py"), entry)


def test_import_boundary_allows_core_contract():
    with tempfile.TemporaryDirectory(prefix="bs_import_ok_") as tmp:
        entry = Path(tmp) / "main.py"
        entry.write_text(
            "import json\nfrom teage_liu2.core.hooks import Branch\n\n\n"
            "class B(Branch):\n    name = 'b'\n",
            encoding="utf-8",
        )
        check_import_boundary(_spec(Path(tmp), "main.py"), entry)  # 不抛即通过
