"""P-7 双档消息写 + E3 stdio 代理真实子进程锚定 —— 2026-09-11 落地审查补锚定。

被测进程 = P-4 参考后端(`PROTOCOL/behavior-suite/tools/reference_storage_backend.py`,
零 core 依赖);本文件**不依赖 fastapi/httpx**,CI 可跑。
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from teage_liu2.server.storage_stdio_proxy import StdioStorageProxy

_BACKEND = (
    Path(__file__).resolve().parents[1]
    / "PROTOCOL" / "behavior-suite" / "tools" / "reference_storage_backend.py"
)
_LOGGER = "teage_liu2.server.storage_stdio_proxy"


def _spawn(tmp_path) -> StdioStorageProxy:
    proxy = StdioStorageProxy(
        command=[sys.executable, str(_BACKEND), "--db", str(tmp_path / "ref.db")],
        request_timeout=10.0,
    )
    proxy.start()
    return proxy


def test_flush_before_direct_write_preserves_order(tmp_path):
    """直发档先冲刷缓冲 → 帧序 = 调用序(双档保序)。"""
    proxy = _spawn(tmp_path)
    try:
        proxy.ensure_session("s1")
        proxy.log_message_buffered("s1", "assistant", "bg-1")
        proxy.log_message("s1", "user", "flush-1")
        msgs = proxy.get_session_messages("s1")
        assert [m["content"] for m in msgs] == ["bg-1", "flush-1"]
    finally:
        proxy.close()


def test_close_flushes_in_flight_buffered(tmp_path):
    """close() 前冲刷在途缓冲(不丢 background 档写入)。"""
    proxy = _spawn(tmp_path)
    proxy.ensure_session("s2")
    proxy.log_message_buffered("s2", "assistant", "in-flight")
    proxy.close()
    conn = sqlite3.connect(str(tmp_path / "ref.db"))
    try:
        rows = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", ("s2",)
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["in-flight"]


def test_close_is_idempotent(tmp_path):
    """registry.shutdown 会对同一实例调两次 → close() 必须幂等。"""
    proxy = _spawn(tmp_path)
    proxy.ensure_session("s3")
    proxy.close()
    proxy.close()  # 不抛


def test_buffered_write_after_close_logs_error_without_raising(tmp_path, caplog):
    """关停期背景写:记 error 日志,不静默丢弃、不抛异常(降级优先)。"""
    proxy = _spawn(tmp_path)
    proxy.ensure_session("s4")
    proxy.close()
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        proxy.log_message_buffered("s4", "assistant", "after-close")
    assert "已关闭" in caplog.text


def test_metrics_distinguish_background_from_flush(tmp_path):
    """P-7 口径:background 档经 log_messages(帧)发送,flush 档才计 log_message。"""
    proxy = _spawn(tmp_path)
    try:
        proxy.ensure_session("s5")
        proxy.log_message_buffered("s5", "assistant", "a")
        proxy.log_message_buffered("s5", "assistant", "b")
        proxy.log_message("s5", "user", "c")
        snap = proxy.metrics_snapshot()
        assert snap["log_messages"]["count"] >= 1   # 合帧的 background 档
        assert snap["log_message"]["count"] == 1    # 仅 flush 档直发 1 次
    finally:
        proxy.close()
