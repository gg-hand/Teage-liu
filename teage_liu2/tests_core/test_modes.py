"""modes.py 锚定(WP-F F4):MODE_* 常量 + BareMode 单次调用形态。

此前 `BareMode` 无直接单测;本文件锁定:"单次 LLM 调用 → 补 done"的 bare 语义,
以及 `stop_reason != end_turn` 时 `is_complete=False` 的边界。
"""
from __future__ import annotations

import asyncio

from teage_liu2.core.modes import MODE_BARE, MODE_LOOP, BareMode
from teage_liu2.core.types import Snapshot


class _StepStub:
    """最小 step 执行器:产出 step_start → text_delta → step_end。"""

    def __init__(self, stop_reason: str = "end_turn"):
        self.calls = 0
        self._stop_reason = stop_reason

    async def execute(self, messages, tools=None, system=None,
                      cancel_event=None, session_id=None, step=1):
        self.calls += 1
        yield {"type": "step_start", "session_id": session_id, "step": step}
        yield {"type": "text_delta", "session_id": session_id, "text": "hi"}
        yield {
            "type": "step_end",
            "session_id": session_id,
            "content_blocks": [{"type": "text", "text": "hi"}],
            "stop_reason": self._stop_reason,
            "usage": None,
        }


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]

    return asyncio.run(_run())


def test_mode_constants_are_distinct():
    assert MODE_BARE == "bare"
    assert MODE_LOOP == "loop"


def test_bare_mode_single_step_then_done():
    """BareMode:仅一次 LLM 调用,收到 step_end 即补 done。"""
    stub = _StepStub()
    mode = BareMode(stub)
    snap = Snapshot(session_id="s-bare", user_input="u")
    events = _collect(
        mode.run_stream(snap, [{"role": "user", "content": "u"}], session_id="s-bare")
    )
    assert stub.calls == 1
    assert events[0]["type"] == "step_start"
    assert events[-1]["type"] == "done"
    assert events[-1]["is_complete"] is True
    assert events[-1]["termination_reason"] == "normal"


def test_bare_mode_non_end_turn_is_incomplete():
    """stop_reason 非 end_turn → 不完整(单次形态下不会续跑)。"""
    mode = BareMode(_StepStub(stop_reason="tool_use"))
    snap = Snapshot(session_id="s-bare2", user_input="u")
    events = _collect(
        mode.run_stream(snap, [{"role": "user", "content": "u"}], session_id="s-bare2")
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["is_complete"] is False
