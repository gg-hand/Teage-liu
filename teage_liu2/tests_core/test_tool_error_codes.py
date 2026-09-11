"""P-8 工具类错误码日志面锚定(§errors §5 ②/⑤,WP-C)。

三码中两码在此单测锚定:
- TOOL_MODIFY_INVALID: pre_tool_call 返回 modify 但 input 为 None(此前静默忽略、无 else 分支)
- TOOL_EXEC_FAILED: on_tool_call 抛异常(此前日志无 CODE: 前缀)

TOOL_REJECTED_BY_POLICY 由 ``core/loop.py`` 在 tool 路径发射,单测需驱动整个 ReactLoop,
故改由行为套件用例 16(error-responsibility)的日志面断言覆盖(见 WP-C 步骤 5)。
"""
import asyncio
import logging

from teage_liu2.core.actions import ToolDecision
from teage_liu2.core.errors import TOOL_EXEC_FAILED, TOOL_MODIFY_INVALID
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.types import Snapshot


def _snapshot() -> Snapshot:
    return Snapshot(session_id="s-tool", user_input="hi")


class _ModifyNoneBranch(Branch):
    """pre_tool_call 返回 modify 但 input 为 None(非法组合,应记码并降级 allow)。"""

    name = "modify_none"

    async def pre_tool_call(self, snapshot, name, input):
        return ToolDecision(decision="modify", input=None)


class _BoomBranch(Branch):
    """on_tool_call 抛异常(单枝干故障不得杀死对话,但必须留 CODE 级证据)。"""

    name = "boom"

    async def on_tool_call(self, snapshot, name, input):
        raise RuntimeError("执行炸了")


def test_modify_without_input_logs_code(caplog):
    """modify 但 input=None → TOOL_MODIFY_INVALID 日志面(不得静默忽略)。"""
    chain = HookChain()
    chain.register(_ModifyNoneBranch())
    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.hooks"):
        decision, effective = asyncio.run(
            chain.pre_tool_call_all(_snapshot(), "echo", {"x": 1})
        )
    assert decision.decision == "allow", "非法 modify 必须降级为 allow"
    assert effective == {"x": 1}, "入参不得被非法 modify 改动"
    assert TOOL_MODIFY_INVALID in caplog.text


def test_tool_exec_failed_logs_code(caplog):
    """on_tool_call 抛异常 → TOOL_EXEC_FAILED 日志面 + 异常实例返回(loop 转 is_error 回喂)。"""
    chain = HookChain()
    chain.register(_BoomBranch())
    with caplog.at_level(logging.ERROR, logger="teage_liu2.core.hooks"):
        result = asyncio.run(
            chain.dispatch_tool_call(_snapshot(), "echo", {"x": 1})
        )
    assert isinstance(result, RuntimeError), "异常实例必须返回给 loop 转 tool_result is_error"
    assert TOOL_EXEC_FAILED in caplog.text
