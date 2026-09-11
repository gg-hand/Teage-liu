"""events 域契约锚定（2026-09-11 交叉评审修正 P1-1 / P1-2 / P1-3）。

锚定内容：
- **P1-1**：`tool_result` 的失败原因经 errors 域 `code` 表达（
  `TOOL_REJECTED_BY_POLICY` / `TOOL_EXEC_FAILED`），**不得**使用 done 专用枚举名
  `termination_reason`；事件必须通过 `events.schema.json` 的 `ToolResultEvent`
  （`additionalProperties: false` —— 越界键即非法）。
- **P1-2**：`step_start.step` = 真实调用序号（1-based）；loop 多轮为 1..N。
- **P1-3**：provider 未上报 usage 时 `step_end.usage` 为 `null` 且事件合法
  （与 `DoneEvent` / `AfterResponse` 同口径）。

变异验证（任一退化应使本文件变红）：
- loop 回退 `step` 常量或不传 `cur.round` → `test_step_start_sequence_in_loop` 红；
- 回退 `code` 发射（或改回 `termination_reason`）→ 两条 tool_result 用例红；
- schema 的 `usage` 改回 `type: object` → `test_step_end_usage_null_is_valid` 红。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from teage_liu2.core.errors import TOOL_EXEC_FAILED, TOOL_REJECTED_BY_POLICY
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.loop import ReactLoop
from teage_liu2.core.step import StepExecutor
from teage_liu2.core.types import Snapshot

from .fake_llm import FakeLLMClient

_PROTOCOL = Path(__file__).resolve().parents[1] / "PROTOCOL"


def _event_def(name: str) -> Dict[str, Any]:
    spec = json.loads(
        (_PROTOCOL / "events" / "events.schema.json").read_text(encoding="utf-8")
    )
    return spec["definitions"][name]


def _validate(event: Dict[str, Any], def_name: str) -> None:
    """按 events.schema.json 校验事件（越界键/类型不符即抛错）。"""
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=event, schema=_event_def(def_name))


class _EchoTool(Branch):
    """可执行工具的最小枝干。"""

    name = "echo_tool"
    capabilities = ["tool_executor"]

    async def on_tool_call(self, snapshot, name, input):
        return "ok"


class _BoomTool(Branch):
    name = "boom_tool"
    capabilities = ["tool_executor"]

    async def on_tool_call(self, snapshot, name, input):
        raise RuntimeError("执行炸了")


class _RejectPolicy(Branch):
    name = "reject_policy"

    async def pre_tool_call(self, snapshot, name, input):
        from teage_liu2.core.actions import ToolDecision

        return ToolDecision(decision="reject", input="denied")


def _tool_round_llm() -> FakeLLMClient:
    """第一轮请求工具 → 第二轮自然结束（loop 两轮）。"""
    return FakeLLMClient([
        {
            "content": [{"type": "tool_use", "id": "u1", "name": "echo", "input": {}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "content": [{"type": "text", "text": "完成"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 2, "output_tokens": 2},
        },
    ])


def _run_loop(llm: Any, chain: HookChain, sid: str = "s-ev") -> List[Dict[str, Any]]:
    loop = ReactLoop(llm, chain, max_loops=5)
    snap = Snapshot(session_id=sid, user_input="hi")

    async def _collect():
        return [
            ev
            async for ev in loop.run_stream(
                snap, [{"role": "user", "content": "hi"}], session_id=sid
            )
        ]

    return asyncio.run(_collect())


# ---------------------------------------------------------------------------
# P1-2: step_start.step = 真实序号（1-based）
# ---------------------------------------------------------------------------
def test_step_executor_reports_given_step():
    """StepExecutor 逐字透传 step（默认 1）→ 违反 schema minimum:1 的 0 不可能出现。"""
    events = asyncio.run(_collect_step(StepExecutor(FakeLLMClient([])), step=3))
    start = next(e for e in events if e["type"] == "step_start")
    assert start["step"] == 3
    _validate(start, "StepStartEvent")

    default_events = asyncio.run(_collect_step(StepExecutor(FakeLLMClient([]))))
    default_start = next(e for e in default_events if e["type"] == "step_start")
    assert default_start["step"] == 1, "默认步号必须为 1（schema minimum:1）"


async def _collect_step(executor: StepExecutor, step: int = 1) -> List[Dict[str, Any]]:
    return [
        ev
        async for ev in executor.execute(
            [{"role": "user", "content": "hi"}],
            session_id="s-step",
            step=step,
        )
    ]


def test_step_start_sequence_in_loop():
    """loop 两轮 → step_start.step 序列 [1, 2]（每轮递增）。"""
    chain = HookChain()
    chain.register(_EchoTool())
    events = _run_loop(_tool_round_llm(), chain, sid="s-step-loop")
    steps = [e["step"] for e in events if e["type"] == "step_start"]
    assert steps == [1, 2], f"step 应逐轮递增 1..N，实际 {steps}"
    for e in events:
        if e["type"] == "step_start":
            _validate(e, "StepStartEvent")


# ---------------------------------------------------------------------------
# P1-3: provider 未上报 usage → null 合法（与 done/AfterResponse 同口径）
# ---------------------------------------------------------------------------
def test_step_end_usage_null_is_valid():
    """provider 未上报 usage → step_end.usage 为 None，且通过 StepEndEvent schema。"""
    executor = StepExecutor(FakeLLMClient([{"content": [], "stop_reason": "end_turn"}]))
    events = asyncio.run(_collect_step(executor))
    step_end = next(e for e in events if e["type"] == "step_end")
    assert step_end["usage"] is None
    _validate(step_end, "StepEndEvent")


# ---------------------------------------------------------------------------
# P1-1: tool_result 的失败原因 = errors 域 code（非 termination_reason）
# ---------------------------------------------------------------------------
def test_tool_result_reject_carries_code():
    """reject 短路 → tool_result.code=TOOL_REJECTED_BY_POLICY，且无越界键。"""
    chain = HookChain()
    chain.register(_RejectPolicy())
    chain.register(_EchoTool())
    events = _run_loop(_tool_round_llm(), chain, sid="s-reject")
    results = [e for e in events if e["type"] == "tool_result"]
    assert results, "reject 路径必须发 tool_result（H-19 事件流不缺环）"
    first = results[0]
    assert first["is_error"] is True
    assert first["code"] == TOOL_REJECTED_BY_POLICY
    assert "termination_reason" not in first, "不得使用 done 专用枚举名（越界键）"
    _validate(first, "ToolResultEvent")


def test_tool_result_exec_error_carries_code():
    """工具执行异常 → tool_result.code=TOOL_EXEC_FAILED（事件面与日志面同码）。"""
    chain = HookChain()
    chain.register(_BoomTool())
    events = _run_loop(_tool_round_llm(), chain, sid="s-boom")
    results = [e for e in events if e["type"] == "tool_result"]
    assert results
    first = results[0]
    assert first["is_error"] is True
    assert first["code"] == TOOL_EXEC_FAILED
    _validate(first, "ToolResultEvent")


def test_success_tool_result_has_no_code():
    """成功路径不带 code（仅失败才携带原因）。"""
    chain = HookChain()
    chain.register(_EchoTool())
    events = _run_loop(_tool_round_llm(), chain, sid="s-ok")
    first = next(e for e in events if e["type"] == "tool_result")
    assert first["is_error"] is False
    assert "code" not in first
    _validate(first, "ToolResultEvent")


def test_tool_result_code_within_error_codes():
    """P-9③ 机械校验：tool_result.code 取值必须落在 errors 域错误码全集内。"""
    from teage_liu2.core.errors import ERROR_CODES

    assert TOOL_REJECTED_BY_POLICY in ERROR_CODES
    assert TOOL_EXEC_FAILED in ERROR_CODES
