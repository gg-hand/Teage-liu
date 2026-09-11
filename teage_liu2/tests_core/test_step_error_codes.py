"""step.py 四个 LLM_* 错误码事件面锚定 —— 2026-09-11 落地审查(P-8② 零锚定码补齐)。

`LLM_TIMEOUT` / `LLM_CANCELED` / `LLM_STREAM_FAILED` 三码此前**零锚定**(仅有发射点);
`LLM_API_ERROR` 已由 `test_error_codes_events.py` 覆盖,此处一并回归四码统一口径。
"""
from __future__ import annotations

import asyncio

from teage_liu2.core.errors import (
    LLM_API_ERROR,
    LLM_CANCELED,
    LLM_STREAM_FAILED,
    LLM_TIMEOUT,
)
from teage_liu2.core.llm import ActivityTimeout, StreamCancelled
from teage_liu2.core.step import StepExecutor


class _Base:
    activity_timeout = 60.0
    stream_total_timeout = 300.0


class _ApiErrorLLM(_Base):
    async def chat_main_stream(self, *a, **kw):
        raise RuntimeError("boom")
        yield  # pragma: no cover


class _TimeoutLLM(_Base):
    async def chat_main_stream(self, *a, **kw):
        raise ActivityTimeout("60s 无输出")
        yield  # pragma: no cover


class _CancelLLM(_Base):
    async def chat_main_stream(self, *a, **kw):
        raise StreamCancelled()
        yield  # pragma: no cover


class _SlowLLM(_Base):
    async def chat_main_stream(self, *a, **kw):
        await asyncio.sleep(5)
        yield  # pragma: no cover


def _run(executor: StepExecutor) -> list:
    async def _collect():
        return [ev async for ev in executor.execute(
            [{"role": "user", "content": "hi"}], session_id="s-err"
        )]

    return asyncio.run(_collect())


def _code(events: list) -> str:
    errs = [e for e in events if e.get("type") == "error"]
    assert errs, f"未产生 error 事件: {events}"
    assert all(e.get("type") != "step_end" for e in events), "error 路径不得再产 step_end"
    return errs[0]["code"]


def test_llm_api_error_code():
    assert _code(_run(StepExecutor(_ApiErrorLLM()))) == LLM_API_ERROR


def test_llm_timeout_code():
    """LLM 流抛 ActivityTimeout → LLM_TIMEOUT。"""
    assert _code(_run(StepExecutor(_TimeoutLLM()))) == LLM_TIMEOUT


def test_llm_canceled_code():
    """LLM 流抛 StreamCancelled → LLM_CANCELED。"""
    assert _code(_run(StepExecutor(_CancelLLM()))) == LLM_CANCELED


def test_llm_stream_failed_code_on_total_timeout():
    """流总超时(asyncio.timeout 到期)→ LLM_STREAM_FAILED。"""
    events = _run(StepExecutor(_SlowLLM(), stream_total_timeout=0.05))
    assert _code(events) == LLM_STREAM_FAILED
