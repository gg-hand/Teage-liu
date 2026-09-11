"""P-8② 零锚定码补齐:`STORAGE_WRITE_FAILED` / `STORAGE_READ_FAILED`。

①写失败(落盘通道):pipeline 直写路径异常 → `STORAGE_WRITE_FAILED` 日志前缀;
②读失败(transport storage 通道):storage query 异常 → `STORAGE_READ_FAILED` 日志前缀。
"""
from __future__ import annotations

import asyncio
import logging

from teage_liu2.core.errors import STORAGE_READ_FAILED, STORAGE_WRITE_FAILED
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.transport import (
    MSG_STORAGE_QUERY,
    TransportBus,
    TransportFrame,
)

from .fake_llm import FakeLLMClient


class _FaultStore:
    """最小 history_store:log_message 抛错(模拟库故障)。"""

    def ensure_session(self, session_id):
        return None

    def get_session_messages(self, session_id, limit=None, before_id=None):
        return []

    def log_message(self, *args, **kwargs):
        raise RuntimeError("db down")

    def close(self):
        return None


class _FaultProvider:
    """storage provider:读写均抛错。"""

    def query(self, kind, limit=None, **filters):
        raise RuntimeError("query boom")

    def read(self, kind, doc_id):
        raise RuntimeError("read boom")

    def write(self, kind, docs):
        return []

    def delete(self, kind, doc_id):
        return None


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]

    return asyncio.run(_run())


def test_storage_write_failed_logs_code(caplog):
    """落盘失败(直写路径)→ STORAGE_WRITE_FAILED 日志面。"""
    pipeline = ChatPipeline(
        llm_client=FakeLLMClient([
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        ]),
        history_store=_FaultStore(),
    )
    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.pipeline"):
        _collect(pipeline.chat_stream("s-fault", "hi"))
    assert STORAGE_WRITE_FAILED in caplog.text


def test_storage_read_failed_logs_code(caplog):
    """storage query 异常 → STORAGE_READ_FAILED 日志面 + error 响应。"""
    bus = TransportBus(storage_provider=_FaultProvider())
    bus.register_extension("ext1", [])
    frame = TransportFrame(MSG_STORAGE_QUERY, {"kind": "ext1.docs"})
    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.transport"):
        resp = asyncio.run(bus.handle("ext1", frame))
    assert STORAGE_READ_FAILED in caplog.text
    assert "error" in resp
