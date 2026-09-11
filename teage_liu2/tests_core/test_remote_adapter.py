"""L-5/L-6 邻近锚定:RemoteBranchAdapter 协议往返 + stdio 关闭后快速失败 + 版本协商拒绝。

`core/remote_adapter.py` 此前**零测试**;本文件锚定钩子声明不变量、error 响应的
可读异常、`close()` 后 `request()` 立即失败(不悬挂)、major 版本不匹配拒绝。
"""
from __future__ import annotations

import asyncio

import pytest

from teage_liu2.core.remote_adapter import RemoteBranchAdapter
from teage_liu2.core.stdio import StdioChannel, StdioError
from teage_liu2.core.transport import negotiate_protocol_version
from teage_liu2.core.types import Snapshot


class _Frame:
    def __init__(self, payload):
        self.payload = payload


class _FakeChannel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    async def request(self, msg_type, payload, timeout=None):
        self.sent.append((msg_type, payload, timeout))
        return _Frame(self._responses.pop(0))


def _snap() -> Snapshot:
    return Snapshot(session_id="s-ra", user_input="u")


def test_invoke_hook_roundtrip_and_declaration():
    """未声明钩子不发往返;声明的钩子经 invoke_hook 转发并还原 Injection。"""
    ch = _FakeChannel([{"result": {"injections": [
        {"layer": "PREFIX", "content": "x", "priority": 1, "key": None}]}}])
    adapter = RemoteBranchAdapter(
        "ext1",
        {"hooks_implemented": ["build_injections"], "capabilities": [],
         "protocol_version": "v1.0.0"},
        ch,
    )
    assert adapter.implements("build_injections") is True
    assert adapter.implements("before") is False
    injections = asyncio.run(adapter.build_injections(_snap()))
    assert injections[0].layer == "PREFIX"
    assert ch.sent[0][0] == "invoke_hook"


def test_invoke_hook_error_is_readable_runtime_error():
    """扩展返回 error → 抛可读 RuntimeError(码在消息中)。"""
    ch = _FakeChannel([{"error": {"code": "HOOK_BOOM", "message": "内爆"}}])
    adapter = RemoteBranchAdapter("ext1", {"hooks_implemented": ["build_injections"]}, ch)
    with pytest.raises(RuntimeError, match="HOOK_BOOM"):
        asyncio.run(adapter.build_injections(_snap()))


def test_channel_request_after_close_raises_readable():
    """close() 后 request() 立即抛可读 StdioError(不悬挂)。"""
    ch = StdioChannel("ext1", ["py", "-c", ""], lambda name, frame: None)
    asyncio.run(ch.close())
    with pytest.raises(StdioError, match="已关闭"):
        asyncio.run(ch.request("invoke_hook", {}))


def test_major_version_mismatch_rejects_handshake():
    """major 不匹配 → 握手拒绝(compatible=False / action=reject)。"""
    result = negotiate_protocol_version("v1.0.0", "v2.0.0")
    assert result["compatible"] is False
    assert result["action"] == "reject"
