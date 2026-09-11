"""行为套件统一 Runner(协议交付物,语言无关黄金用例的 Python 宿主执行器)。

用法::

    python runner.py                 # 跑全部用例并报告通过率
    python runner.py --case 03       # 只跑指定用例(前缀匹配 id)
    python runner.py --verbose       # 逐用例输出详情

每个用例按 JSON 定义执行(expected 的三类断言):
- ``expected.event_stream``:事件流有序逐项匹配;``absent: true`` 项为全程负断言
- ``expected.invocations``:钩子调用序断言(正序/逆序/跳过/短路)+ snapshot_assert + actions_assert
- ``expected.final``:收尾断言(termination_reason / is_complete / persisted)

用例执行分两类(宿主协议层对拍):
- pipeline 类:经 ChatPipeline 跑真实对话流(ScriptedBranch 按 inputs 行为脚本化)
- 协议层类:直接对拍宿主 transport/registry/supervisor 协议语义

匹配器(与 matcher.schema.json 对齐):``{regex}`` / ``{length}`` / ``{range}`` / ``{type}`` / 值相等。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 仓库根(teage_liu2/ 的父目录):使 teage_liu2.* 可 import
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from teage_liu2.core.actions import ToolDecision, make_action  # noqa: E402
from teage_liu2.core.errors import ERROR_CODES  # noqa: E402
from teage_liu2.core.event_stream import L3BatchSink  # noqa: E402
from teage_liu2.core.hooks import Branch, HookChain  # noqa: E402
from teage_liu2.core.injection import Injection  # noqa: E402
from teage_liu2.core.modes import MODE_LOOP  # noqa: E402
from teage_liu2.core.pipeline import ChatPipeline  # noqa: E402
from teage_liu2.core.registry import BranchRegistry  # noqa: E402
from teage_liu2.core.storage import SQLiteStorageProvider  # noqa: E402
from teage_liu2.core.tasks import TaskRegistry  # noqa: E402
from teage_liu2.core.transport import (  # noqa: E402
    FRAME_MAX_BYTES,
    JSON_MAX_DEPTH,
    DeltaFrame,
    TransportBus,
    TransportFrame,
    check_frame_limits,
    negotiate_protocol_version,
    parse_protocol_version,
)
from teage_liu2.core.types import (  # noqa: E402
    EV_DONE,
    EV_ERROR,
    EV_STEP_END,
    EV_TOOL_RESULT,
    EV_TOOL_USE,
)
from teage_liu2.tests_core.fake_llm import FakeLLMClient  # noqa: E402

import yaml

try:  # jsonschema 已在 requirements.txt:13 声明;缺失时降级(仅跳过用例结构校验)
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

_CASES_DIR = os.path.join(os.path.dirname(__file__), "cases")

# ---------------------------------------------------------------------------
# 匹配器解释器(与 matcher.schema.json 对齐)
# ---------------------------------------------------------------------------
_TYPE_MAP = {
    "boolean": bool, "integer": int, "string": str,
    "array": list, "object": dict, "null": type(None),
}


def match_value(assertion: Any, actual: Any) -> bool:
    """单值匹配:{regex}/{length}/{range}/{type}/精确值/嵌套对象。"""
    if isinstance(assertion, dict):
        keys = set(assertion)
        if "regex" in keys:
            return isinstance(actual, str) and bool(re.search(assertion["regex"], actual))
        if "length" in keys:
            return isinstance(actual, (list, dict, str)) and len(actual) == assertion["length"]
        if "range" in keys:
            lo, hi = assertion["range"]
            return isinstance(actual, (int, float)) and not isinstance(actual, bool) and lo <= actual <= hi
        if "type" in keys:
            t = _TYPE_MAP.get(assertion["type"])
            return t is not None and isinstance(actual, t)
        if isinstance(actual, dict):
            return all(k in actual and match_value(v, actual[k]) for k, v in assertion.items())
        return False
    if isinstance(assertion, list):
        return (
            isinstance(actual, list)
            and len(assertion) == len(actual)
            and all(match_value(a, b) for a, b in zip(assertion, actual))
        )
    return assertion == actual


def _snapshot_fields(snapshot: Any) -> Dict[str, Any]:
    """快照关键字段(snapshot_assert 用):messages/system_text/extra/revision/round/stop。"""
    return {
        "messages": list(getattr(snapshot, "messages", [])),
        "system_text": getattr(snapshot, "system_text", ""),
        "extra": dict(getattr(snapshot, "extra", {})),
        "revision": getattr(snapshot, "revision", 0),
        "round": getattr(snapshot, "round", 0),
        "stop": getattr(snapshot, "stop", False),
    }


# ---------------------------------------------------------------------------
# 断言能力守卫:用例出现 runner 未实现的断言键 → 显式失败(绝不静默通过)
# 2026-09-10 深度审计 F-1:此前 persisted / isolation 被静默忽略 → 假绿
# ---------------------------------------------------------------------------
_SUPPORTED_TOP_KEYS = {
    "event_stream": None, "invocations": None, "final": None, "error_codes": None,
}
_SUPPORTED_FINAL_KEYS = {"termination_reason", "is_complete", "persisted", "llm_assert"}
#: llm_assert 子键白名单 —— 未识别子键**显式失败**(同 _check_supported 纪律,防"假绿")
_LLM_ASSERT_KEYS = {"system_contains", "system_regex", "messages_roles", "message_count"}
_SUPPORTED_INVOCATION_KEYS = {
    "hook", "extension", "order", "absent", "snapshot_assert", "actions_assert",
    "isolation",
}
_SUPPORTED_EVENT_KEYS = {"type", "absent", "payload"}


def _check_supported(expected: Dict[str, Any]) -> List[str]:
    """校验 expected 只使用已实现的断言键;返回不支持项描述(空 = 通过)。"""
    problems: List[str] = []
    for key in expected:
        if key not in _SUPPORTED_TOP_KEYS:
            problems.append(f"expected.{key} 未被 runner 实现")
    for key in (expected.get("final") or {}):
        if key not in _SUPPORTED_FINAL_KEYS:
            problems.append(f"expected.final.{key} 未被 runner 实现")
    for item in expected.get("invocations") or []:
        for key in item:
            if key not in _SUPPORTED_INVOCATION_KEYS:
                problems.append(f"expected.invocations[].{key} 未被 runner 实现")
    for item in expected.get("event_stream") or []:
        for key in item:
            if key not in _SUPPORTED_EVENT_KEYS:
                problems.append(f"expected.event_stream[].{key} 未被 runner 实现")
    return problems


# ---------------------------------------------------------------------------
# 错误码断言(事件面 error.code + 日志面 "CODE: " 前缀)
# 2026-09-10 深度审计 F-3:errors 域 18 码此前在套件中 0 命中
# ---------------------------------------------------------------------------
_CODE_RE = re.compile(r"\b(?:" + "|".join(sorted(ERROR_CODES)) + r")\b")


def _extract_codes(text: str) -> List[str]:
    """从日志文本中提取 v1.0 错误码(按 errors.py 全集精确匹配)。"""
    return _CODE_RE.findall(text or "")


class _LogCapture(logging.Handler):
    """捕获 teage_liu2 日志并提取错误码(错误码"日志面"断言用)。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.extend(_extract_codes(record.getMessage()))
        except Exception:  # noqa: BLE001 - 捕获器自身绝不干扰对话
            pass


def _assert_error_codes(expected: Dict[str, Any], observed: List[str],
                        captured: List[str]) -> List[str]:
    """断言错误码的事件面 + 日志面覆盖(must_include / must_exclude)。"""
    errors: List[str] = []
    seen = list(dict.fromkeys(list(observed) + list(captured)))
    for code in expected.get("must_include") or []:
        if code not in seen:
            errors.append(f"错误码 {code} 未出现(事件面+日志面均无), 实际={seen}")
    for code in expected.get("must_exclude") or []:
        if code in seen:
            errors.append(f"错误码 {code} 不应出现, 实际={seen}")
    return errors


def _count_assertions(expected: Dict[str, Any]) -> int:
    """统计用例的有效断言条数(空壳用例必须失败,见 _check_min_assertions)。"""
    ec = expected.get("error_codes") or {}
    return (
        len(expected.get("event_stream") or [])
        + len(expected.get("invocations") or [])
        + len(expected.get("final") or {})
        + len(ec.get("must_include") or [])
        + len(ec.get("must_exclude") or [])
    )


def _check_min_assertions(expected: Dict[str, Any]) -> List[str]:
    """空壳守卫(2026-09-10 评审 P-4):`expected` 为空或全是空容器 ⇒ 断言恒真。

    此前这类用例会静默 PASS —— 与"禁止静默忽略"的套件纪律冲突。
    """
    if _count_assertions(expected) == 0:
        return ["用例没有任何有效断言(expected 为空容器) —— 空壳用例一律失败"]
    return []


def _schema_errors(case: Dict[str, Any]) -> List[str]:
    """按 suite.schema.json 校验用例结构(2026-09-10 评审 P-4)。

    schema 此前从未被机器加载(schemas 只作文档);jsonschema 缺失时降级为不校验。
    """
    if jsonschema is None:
        return []
    schema_path = os.path.join(_CASES_DIR, "..", "suite.schema.json")
    matcher_path = os.path.join(_CASES_DIR, "..", "matcher.schema.json")
    try:
        with open(schema_path, encoding="utf-8") as f:
            suite = json.load(f)
    except OSError as e:  # pragma: no cover - 仓库内文件应恒在
        return [f"无法读取 suite.schema.json: {e}"]
    try:
        with open(matcher_path, encoding="utf-8") as f:
            matcher = json.load(f)
    except OSError:
        matcher = {}
    # 合并两份 schema 的内联文档并把外部 $ref 降级为内部引用 —— 避免使用
    # 已弃用的 jsonschema.RefResolver(会向 stderr 打 DeprecationWarning,
    # 进而被严格模式的 shell / 审计脚本判为失败)。
    merged = _inline_external_refs({
        "definitions": {
            **(suite.get("definitions") or {}),
            **(matcher.get("definitions") or {}),
        },
        **suite["definitions"]["TestCase"],
    })
    validator = jsonschema.Draft7Validator(merged)
    return [
        f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(case), key=lambda e: list(e.path))
    ]


def _inline_external_refs(obj: Any) -> Any:
    """把 `matcher.schema.json#/definitions/X` 内联为 `#/definitions/X`。

    用于避免 `jsonschema.RefResolver`（v4.18 起已弃用，会向 stderr 打警告）。
    """
    if isinstance(obj, dict):
        return {
            k: (
                v.replace("matcher.schema.json#/definitions/", "#/definitions/")
                if k == "$ref" and isinstance(v, str)
                else _inline_external_refs(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_inline_external_refs(v) for v in obj]
    return obj


def _persisted_view(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """落盘调用视图(供 expected.final.persisted 对拍)。

    text_blob = 全部落盘内容的拼接(用于"注入永不落盘"类负断言:
    正则 ``(?s)^(?!.*标记)`` 可断言整个拼接文本不含某标记)。
    """
    return {
        "roles": [r.get("role") for r in records],
        "channels": [r.get("channel") for r in records],
        "contents": [r.get("content") for r in records],
        "message_types": [r.get("message_type") for r in records],
        # D1 契约:assistant/tool_results 必须携带 content_blocks(供 LLM 重建)
        "has_blocks": [bool(r.get("has_blocks")) for r in records],
        "text_blob": "\n".join(str(r.get("content") or "") for r in records),
        "count": len(records),
    }


# ---------------------------------------------------------------------------
# ScriptedBranch:按 inputs 行为脚本化的同语言扩展
# ---------------------------------------------------------------------------
class ScriptedBranch(Branch):
    """按用例 inputs 行为的可编程扩展:记录钩子调用序 + 返回预设行为。"""

    def __init__(self, name: str, decl: Dict[str, Any], behaviors: Dict[str, Any],
                 invocations: List[Dict[str, Any]], host_port: Any = None) -> None:
        self.name = name
        self.capabilities = list(decl.get("capabilities") or [])
        self._hooks_implemented = set(decl.get("hooks_implemented") or [])
        self._behaviors = behaviors
        self._invocations = invocations
        self.host_port = host_port
        #: 抛错过的钩子名(isolation 断言用;抛错发生在 _record 之前)
        self.raised: List[str] = []

    def implements(self, hook: str) -> bool:
        return hook in self._hooks_implemented

    def _should_record(self, hook: str) -> bool:
        """record 判定:build_injections 是隐式基础钩子(所有扩展默认参与);
        其余钩子仅在 hooks_implemented 声明时记录(§lifecycle Extension.hooks_implemented)。
        """
        return hook == "build_injections" or hook in self._hooks_implemented

    def _record(self, hook: str, snapshot: Any = None, actions: Any = None, extra: Any = None) -> None:
        if not self._should_record(hook):
            return
        rec: Dict[str, Any] = {"hook": hook, "extension": self.name}
        if snapshot is not None:
            rec["snapshot"] = _snapshot_fields(snapshot)
        if actions is not None:
            rec["actions"] = actions
        if extra is not None:
            rec.update(extra)
        self._invocations.append(rec)

    def _actions(self, key: str) -> List[Any]:
        return list(self._behaviors.get(key) or [])

    def _maybe_raise(self, hook: str) -> None:
        """isolation 用例:按 behaviors.raise_on 在钩子内抛错(验证 H-6 隔离)。"""
        exc = (self._behaviors.get("raise_on") or {}).get(hook)
        if exc is not None:
            self.raised.append(hook)
            raise exc

    async def setup(self, config: dict, host: Any) -> None:
        self._record("setup")

    async def teardown(self) -> None:
        self._record("teardown")

    async def build_injections(self, snapshot: Any) -> List[Injection]:
        self._record("build_injections", snapshot)
        return list(self._behaviors.get("injections") or [])

    async def inject_round(self, snapshot: Any) -> Optional[Injection]:
        self._record("inject_round", snapshot)
        items = self._behaviors.get("round_injections") or []
        return items[0] if items else None

    def _maybe_mutate_snapshot(self, snapshot: Any) -> bool:
        """H-17/T-3 只读验证:`inputs.mutate_snapshot` 为真时尝试**原地**改写快照。

        Snapshot 是 frozen dataclass,但 ``messages`` / ``extra`` 是可变容器 ——
        属性重绑定会抛 FrozenInstanceError,而容器原地变更**会成功**。本行为
        用于检验"扩展篡改快照后 core 状态是否受影响"。返回是否有任一处改写成功。
        """
        if not self._behaviors.get("mutate_snapshot"):
            return False
        mutated = False
        try:
            # 追加 assistant(而非 user):user 会被收口 merge 吸收,无法从 LLM 入参区分;
            # assistant 追加在"无只读保护"时会让 llm_assert.messages_roles 多出一项 ——
            # 这正是用例 31 用来区分"有保护/无保护"的承重点。
            snapshot.messages.append({"role": "assistant", "content": "注入篡改"})
            mutated = True
        except Exception:  # noqa: BLE001 - frozen/只读视图均视为"被拒绝"
            pass
        try:
            snapshot.extra["hacked"] = True
            mutated = True
        except Exception:  # noqa: BLE001
            pass
        return mutated

    async def before(self, snapshot: Any) -> List[Any]:
        self._maybe_raise("before")
        self._maybe_mutate_snapshot(snapshot)
        actions = self._actions("before_actions")
        self._record("before", snapshot, actions=actions)
        return actions

    async def pre_tool_call(self, snapshot: Any, name: str, input: dict) -> ToolDecision:
        self._record("pre_tool_call", snapshot, extra={"name": name, "input": input})
        d = self._behaviors.get("pre_decision") or {}
        return ToolDecision(
            decision=d.get("decision", "allow"),
            input=d.get("input"),
            reason=d.get("reason"),
        )

    async def on_tool_call(self, snapshot: Any, name: str, input: dict) -> Any:
        self._record("on_tool_call", snapshot, extra={"name": name, "input": input})
        if not (self.implements("on_tool_call") or "tool_executor" in self.capabilities):
            return NotImplemented
        result = self._behaviors.get("tool_result")
        return result if result is not None else "ok"

    async def post_tool_call(self, snapshot: Any, name: str, input: dict,
                             result: Any, duration: float) -> List[Any]:
        actions = self._actions("post_actions")
        self._record("post_tool_call", snapshot, actions=actions)
        return actions

    async def after_step(self, snapshot: Any, summary: Any) -> List[Any]:
        self._maybe_raise("after_step")
        actions = self._actions("after_step_actions")
        self._record("after_step", snapshot, actions=actions)
        return actions

    async def after(self, snapshot: Any, response: Any) -> List[Any]:
        self._maybe_raise("after")
        actions = self._actions("after_actions")
        self._record("after", snapshot, actions=actions)
        return actions

    async def on_error(self, snapshot: Any, error: Any) -> List[Any]:
        self._maybe_raise("on_error")
        actions = self._actions("on_error_actions")
        self._record("on_error", snapshot, actions=actions)
        return actions


# ---------------------------------------------------------------------------
# 行为解析:inputs → {extension_name: behaviors}
# ---------------------------------------------------------------------------
def _build_behaviors(name: str, inputs: Dict[str, Any], decl: Dict[str, Any]) -> Dict[str, Any]:
    hooks = set(decl.get("hooks_implemented") or [])
    b: Dict[str, Any] = {}

    def _make(x: Dict[str, Any]) -> Any:
        return make_action(x["op"], **{k: v for k, v in x.items() if k != "op"})

    for key, val in inputs.items():
        if key == f"{name}_actions":
            # 归属判定:声明 after/on_error(且非 before)→ 终态钩子 action;否则 before
            if ("after" in hooks or "on_error" in hooks) and "before" not in hooks:
                b["after_actions"] = [_make(x) for x in val]
                b["on_error_actions"] = list(b["after_actions"])
            else:
                b["before_actions"] = [_make(x) for x in val]
        elif key == f"{name}_action":
            b["before_actions"] = [_make(val)]
        elif key == f"{name}_decision":
            b["pre_decision"] = val
        elif key == f"{name}_result":
            b["tool_result"] = val
        elif key == f"{name}_injections":
            b["injections"] = [
                Injection(layer=i.get("layer", ""), content=i.get("content", ""),
                          priority=int(i.get("priority", 0) or 0), key=i.get("key"))
                for i in val
            ]
        elif key == f"{name}_inject_round":
            b["round_injections"] = [
                Injection(layer=i.get("layer", "BEFORE_INPUT"), content=i.get("content", ""),
                          priority=int(i.get("priority", 0) or 0), key=i.get("key"))
                for i in val
            ]
        elif key == f"{name}_after_step_actions":
            # P0-1 锚定(H-5 轮中短路):after_step 返回 action(含 SetStop)
            b["after_step_actions"] = [_make(x) for x in val]
        elif key == f"{name}_terminal_actions":
            b["after_actions"] = [_make(x) for x in val]
            b["on_error_actions"] = list(b["after_actions"])
    # tool-path-modify:pre_modify_input → 声明 pre_tool_call 的扩展执行 modify 变形
    if "pre_modify_input" in inputs and "pre_tool_call" in hooks:
        b["pre_decision"] = {"decision": "modify", "input": inputs["pre_modify_input"]}
    # isolation 用例:`<name>_raise_on`: {"before": "boom"} → 该钩子内抛错
    raise_on = inputs.get(f"{name}_raise_on")
    if raise_on:
        b["raise_on"] = {
            h: RuntimeError(f"脚本化异常:{h}") for h in raise_on
        }
    # H-17/T-3 只读验证(WP-A A4):inputs.mutate_snapshot = true → 脚本扩展在 before 内
    # 尝试**原地**篡改快照(messages.append / extra 写),用于检验 core 状态是否被污染
    if inputs.get("mutate_snapshot"):
        b["mutate_snapshot"] = True
    return b


#: `<ext>_<suffix>` 形式的 inputs 键，后缀白名单 = `_build_behaviors` 已实现的行为
_EXTENSION_BEHAVIOR_SUFFIXES = frozenset({
    "actions", "action", "decision", "result", "injections",
    "inject_round", "after_step_actions", "terminal_actions", "raise_on",
})


def _check_unknown_extension_inputs(
    inputs: Dict[str, Any], extensions: List[Dict[str, Any]]
) -> List[str]:
    """守卫：``<扩展名>_<后缀>`` 形式的 inputs 键，后缀必须在已知集合内。

    2026-09-11 交叉评审 P1-8：此前 runner 只对 ``expected`` 做白名单守卫，
    ``inputs`` 中写错/未实现的行为键被**静默忽略** —— 用例"看起来覆盖了实际
    未覆盖的行为"（假绿向量，与 README 的"禁止静默忽略"纪律相悖）。
    """
    problems: List[str] = []
    names = [e.get("name") for e in extensions or [] if isinstance(e, dict)]
    for key in inputs:
        if not isinstance(key, str):
            continue
        for name in names:
            if not name:
                continue
            prefix = f"{name}_"
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                if suffix not in _EXTENSION_BEHAVIOR_SUFFIXES:
                    problems.append(
                        f"inputs.{key} 不是已实现的扩展行为键"
                        f"（后缀 {suffix!r} 未识别；已实现 = {sorted(_EXTENSION_BEHAVIOR_SUFFIXES)}）"
                    )
                break
    return problems


# ---------------------------------------------------------------------------
# 事件断言
# ---------------------------------------------------------------------------
def _assert_event_stream(expected_events: List[Dict[str, Any]], actual_events: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    absent_items = [e for e in expected_events if e.get("absent")]
    ordered_items = [e for e in expected_events if not e.get("absent")]
    actual_types = [e.get("type") for e in actual_events]
    # 事件源契约 E-1:done/error 为终态事件,恰好一次(重复 = 实现缺陷)
    for terminal in (EV_DONE, EV_ERROR):
        n = actual_types.count(terminal)
        if n > 1:
            errors.append(f"终止事件 {terminal} 出现 {n} 次(契约要求恰好一次)")
    # absent:全程负断言
    for item in absent_items:
        if item["type"] in actual_types:
            errors.append(f"负断言失败: 不应出现事件 {item['type']} 但出现了")
    # 有序逐项匹配(跳过 absent)
    pos = 0
    for item in ordered_items:
        found = None
        while pos < len(actual_events):
            ev = actual_events[pos]
            pos += 1
            if ev.get("type") == item["type"]:
                found = ev
                break
        if found is None:
            errors.append(f"缺少有序事件 {item['type']}")
            break
        if "payload" in item:
            # custom:* 事件 payload 嵌套在事件 dict 的 payload 键;其余事件字段扁平
            target = found.get("payload") if str(found.get("type", "")).startswith("custom:") else found
            if not match_value(item["payload"], target):
                errors.append(f"事件 {item['type']} payload 不匹配: expect={item['payload']} actual={target}")
    return errors


def _assert_invocations(expected_invocations: List[Dict[str, Any]],
                        actual: List[Dict[str, Any]],
                        raised: Optional[List[str]] = None) -> List[str]:
    errors: List[str] = []
    for item in expected_invocations:
        hook, ext = item.get("hook"), item.get("extension")
        if item.get("absent"):
            if any(r.get("hook") == hook and r.get("extension") == ext for r in actual):
                errors.append(f"负断言失败: {ext} 的 {hook} 不应被调用但发生了")
            continue
        iso = item.get("isolation")
        if iso is not None:
            # H-6 隔离断言:该扩展的该钩子抛错 → 被跳过并记录,对话不中断
            # (抛错发生在 _record 之前,故该扩展不会出现在 actual 中)
            if iso.get("error") and ext not in (raised or []):
                errors.append(
                    f"{ext} 期望钩子抛错被隔离,但未抛出(实际 raised={raised or []})"
                )
            continue
        order = item.get("order")
        candidates = [i for i, r in enumerate(actual) if r.get("hook") == hook and r.get("extension") == ext]
        if not candidates:
            errors.append(f"缺少调用 {ext} 的 {hook}")
            continue
        idx = candidates[0]
        # order 语义:同一 hook 内调用序号(1-based);'last' = 末位(逆序钩子用)
        if isinstance(order, int):
            hook_calls = [r for r in actual if r.get("hook") == hook]
            pos = next((i for i, r in enumerate(hook_calls) if r.get("extension") == ext), None)
            if pos is None or pos + 1 != order:
                errors.append(f"{ext} 的 {hook} 调用序应为 #{order},实际 #{pos + 1 if pos is not None else '?'}")
        elif order == "last":
            if idx != len(actual) - 1:
                errors.append(f"{ext} 的 {hook} 应在末位,实际 idx={idx}")
        rec = actual[idx]
        if "snapshot_assert" in item and not match_value(item["snapshot_assert"], rec.get("snapshot")):
            errors.append(f"{ext} 的 {hook} snapshot 不匹配: expect={item['snapshot_assert']} actual={rec.get('snapshot')}")
        if "actions_assert" in item and not match_value(item["actions_assert"], rec.get("actions") or []):
            errors.append(f"{ext} 的 {hook} actions 不匹配: {rec.get('actions')}")
    return errors


def _assert_final(expected_final: Dict[str, Any], done_event: Optional[Dict[str, Any]],
                  error_events: List[Dict[str, Any]],
                  persisted_view: Optional[Dict[str, Any]] = None,
                  llm: Any = None) -> List[str]:
    errors: List[str] = []
    if not expected_final:
        return errors
    if expected_final.get("termination_reason") is not None:
        if done_event is None:
            errors.append(f"期望 termination_reason={expected_final['termination_reason']} 但无 done 事件")
        elif done_event.get("termination_reason") != expected_final["termination_reason"]:
            errors.append(f"termination_reason 不匹配: {done_event.get('termination_reason')}")
    if expected_final.get("is_complete") is not None:
        if done_event is not None and done_event.get("is_complete") != expected_final["is_complete"]:
            errors.append(f"is_complete 不匹配: {done_event.get('is_complete')}")
        elif done_event is None and expected_final.get("is_complete"):
            errors.append("期望 is_complete=true 但无 done 事件")
    if expected_final.get("persisted") is not None:
        # storage S-1 落盘时机断言(§storage S-1):
        if persisted_view is None:
            errors.append(
                "expected.final.persisted 出现在无落盘观测的用例类型中(该类型不支持)"
            )
        elif not match_value(expected_final["persisted"], persisted_view):
            errors.append(
                f"persisted 不匹配: expect={expected_final['persisted']} actual={persisted_view}"
            )
    spec = expected_final.get("llm_assert")
    if spec:
        # 事实源 = FakeLLMClient.last_system / last_messages(末次 LLM 调用的入参快照)
        unknown = sorted(set(spec) - _LLM_ASSERT_KEYS)
        if unknown:
            errors.append(f"llm_assert 含未识别子键: {unknown}(支持: {sorted(_LLM_ASSERT_KEYS)})")
            return errors
        if llm is None:
            errors.append("llm_assert 出现在无 LLM 观测的用例类型中(该类型不支持)")
            return errors
        system = getattr(llm, "last_system", None) or ""
        messages = getattr(llm, "last_messages", None) or []
        if spec.get("system_contains") and spec["system_contains"] not in system:
            errors.append(f"llm system 未包含 {spec['system_contains']!r}: {system!r}")
        if spec.get("system_regex") and not re.search(spec["system_regex"], system):
            errors.append(f"llm system 不匹配正则 {spec['system_regex']!r}: {system!r}")
        if spec.get("message_count") is not None and len(messages) != spec["message_count"]:
            errors.append(f"llm message_count 不匹配: expect={spec['message_count']} actual={len(messages)}")
        if spec.get("messages_roles") is not None:
            roles = [m.get("role") for m in messages]
            if roles != spec["messages_roles"]:
                errors.append(f"llm messages_roles 不匹配: expect={spec['messages_roles']} actual={roles}")
    return errors


# ---------------------------------------------------------------------------
# 临时存储
# ---------------------------------------------------------------------------
class _EphemeralStorage:
    """临时 SQLite 存储 + 落盘调用记录(用完删除)。

    2026-09-10 深度审计 F-1:原实现 log_message 为空操作,导致 storage 域
    S-1(落盘时机)无法被断言。现按调用序记录:
      - channel="direct"   : flush 档(user 前置,断连不丢)
      - channel="buffered" : background 档(assistant/tool_results,P-7 双档可选能力)
    """

    def __init__(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="bs_runner_")
        self.db_path = os.path.join(self._tmpdir, "cases.db")
        self.provider = SQLiteStorageProvider(self.db_path)
        self.records: List[Dict[str, Any]] = []
        self.sessions: List[str] = []

    def ensure_session(self, sid: str) -> None:
        self.sessions.append(sid)

    def get_session_messages(
        self, sid: str, limit: Optional[int] = None, before_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        return []

    def _record(self, channel: str, flush: bool, session_id: str, role: str,
                content: str, kw: Dict[str, Any]) -> None:
        self.records.append({
            "session_id": session_id,
            "role": role,
            "content": content,
            "channel": channel,
            "flush": flush,
            "message_type": kw.get("message_type"),
            "has_blocks": bool(kw.get("content_blocks")),
        })

    def log_message(self, session_id: str, role: str, content: str, **kw: Any) -> None:
        self._record("direct", True, session_id, role, content, kw)

    def log_message_buffered(self, session_id: str, role: str, content: str, **kw: Any) -> None:
        self._record("buffered", False, session_id, role, content, kw)

    def close(self) -> None:
        try:
            self.provider.close()
        finally:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# pipeline 类用例执行
# ---------------------------------------------------------------------------
class _CaseResult:
    def __init__(self, case_id: str, ok: bool, detail: str) -> None:
        self.case_id = case_id
        self.ok = ok
        self.detail = detail


async def _run_pipeline_case(case: Dict[str, Any], verbose: bool,
                             capture_codes: Optional[List[str]] = None) -> _CaseResult:
    case_id = case["id"]
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    storage = _EphemeralStorage()
    hooks = HookChain()
    invocations: List[Dict[str, Any]] = []

    l3_sink = L3BatchSink()
    l3_received: Dict[str, List[Dict[str, Any]]] = {}

    def make_l3_collector(ext: str):
        async def deliver(events: List[dict]) -> None:
            l3_received.setdefault(ext, []).extend(events)
        return deliver

    for decl in case.get("setup", {}).get("extensions", []):
        name = decl["name"]
        behaviors = _build_behaviors(name, inputs, decl)
        branch = ScriptedBranch(name, decl, behaviors, invocations)
        if "observe" in (decl.get("capabilities") or []):
            l3_sink.subscribe(name, make_l3_collector(name))
        hooks.register(branch)

    # FakeLLM 脚本:按 inputs 构造
    llm = _build_fake_llm(inputs)
    # 可选注入:storage_writer(落盘链路断言) / session_store(会话态 L-10) / core_overrides。
    # core_overrides **仅**覆盖 ChatPipeline 构造参数 —— resource_limits 经
    # ChatPipeline(resource_limits=) 接线生效(2026-09-11 综合评审,此前未接线
    # 该键为死输入;类型 T-8 超限拒绝用例经此驱动)。
    overrides = inputs.get("core_overrides") or {}
    writer = None
    if inputs.get("storage_writer"):
        from teage_liu2.core.storage_writer import StorageWriter
        writer = StorageWriter()
    session_store = None
    if inputs.get("session_store"):
        from teage_liu2.core.session import SessionStore
        session_store = SessionStore()
    pipeline = ChatPipeline(
        llm_client=llm,
        history_store=storage,
        hooks=hooks,
        max_loops=int(overrides.get("max_loops", inputs.get("max_loops", 50)) or 50),
        mode=overrides.get("mode", MODE_LOOP),
        injection_budget=overrides.get("injection_budget"),
        history_window_messages=overrides.get(
            "history_window_messages", inputs.get("history_window_messages", 100)),
        storage_writer=writer,
        session_store=session_store,
        resource_limits=overrides.get("resource_limits"),
    )
    # L3 拦截点接线
    from teage_liu2.core.event_stream import EventStream
    pipeline.event_stream = EventStream(l3_sink=l3_sink)

    cancel_event = None
    if inputs.get("cancel"):
        cancel_event = threading.Event()
        cancel_event.set()

    events: List[Dict[str, Any]] = []
    try:
        async for ev in pipeline.chat_stream(
            inputs.get("session_id", "s-runner"),
            inputs.get("user_input", "测试"),
            system=inputs.get("system"),
            cancel_event=cancel_event,
        ):
            events.append(ev)
    except Exception as e:  # noqa: BLE001 - runner 断言层
        if writer is not None:
            writer.close()
        return _CaseResult(case_id, False, f"pipeline 异常: {e}")

    # 收集 L3 观测(等待批处理冲刷)
    await asyncio.sleep(0.15)

    errors: List[str] = []
    errors += _check_supported(expected)
    errors += _check_min_assertions(expected)
    errors += _check_unknown_extension_inputs(
        inputs, case.get("setup", {}).get("extensions", [])
    )
    # L3 观测结果暴露(events 域 E-4/E-7/E-8 断言用):把各 observe 扩展收到的事件
    # 类型汇总成一个 custom:pipeline_l3_result 事件,供 expected.event_stream 断言
    if l3_received:
        delivered = {ext: [e.get("type") for e in evs] for ext, evs in l3_received.items()}
        events.append({
            "type": "custom:pipeline_l3_result",
            "payload": {
                "extensions": sorted(delivered),
                "delivered": delivered,
                "type_counts": {
                    ext: {t: types.count(t) for t in sorted(set(types))}
                    for ext, types in delivered.items()
                },
                "l1_leaked": any(
                    t in ("text_delta", "reasoning_delta")
                    for types in delivered.values() for t in types
                ),
            },
        })

    if case_id.startswith("l1-invariant"):
        # L1 不变量:event_stream 的 absent 项(text_delta/reasoning_delta)对 L3 投递流断言;
        # done 项对外壳事件流断言(§3.2 三层事件流)
        l3_all = [ev for evlist in l3_received.values() for ev in evlist]
        l3_types = {ev.get("type") for ev in l3_all}
        if "text_delta" in l3_types or "reasoning_delta" in l3_types:
            errors.append("L1 事件(text_delta/reasoning_delta)泄漏进 L3/transport(违反 L1 不变量)")
        done_event = next((e for e in events if e.get("type") == EV_DONE), None)
        if done_event is None or done_event.get("termination_reason") != "normal" or not done_event.get("is_complete"):
            errors.append("l1-invariant: 期望外壳事件流 done(normal, is_complete=true)")
    else:
        errors += _assert_event_stream(expected.get("event_stream") or [], events)
    errors += _assert_invocations(
        expected.get("invocations") or [], invocations,
        [
            getattr(b, "name", "")
            for b in getattr(hooks, "_branches", [])
            if getattr(b, "raised", [])
        ],
    )
    done_event = next((e for e in events if e.get("type") == EV_DONE), None)
    error_events = [e for e in events if e.get("type") == EV_ERROR]
    errors += _assert_final(
        expected.get("final") or {}, done_event, error_events,
        _persisted_view(storage.records), llm=llm,
    )
    errors += _assert_error_codes(
        expected.get("error_codes") or {},
        [e.get("code") for e in error_events if e.get("code")],
        capture_codes or [],
    )

    if writer is not None:
        writer.close()  # 同步方法(不可 await):排空在途写,落盘序断言才成立
    storage.close()
    return _CaseResult(case_id, not errors, "; ".join(errors))


def _build_fake_llm(inputs: Dict[str, Any]) -> FakeLLMClient:
    """按 inputs 构造 FakeLLM 脚本(对话类用例共用)。"""
    script: List[Any] = []

    def _text_block(text: str) -> dict:
        return {"type": "text", "text": text}

    def _tool_block(name: str, tool_input: dict, uid: str = "u1") -> dict:
        return {"type": "tool_use", "id": uid, "name": name, "input": tool_input}

    # 01/02/05/07 拦截路径:无 LLM;正常路径 end_turn 单轮
    tool_name = inputs.get("tool_name") or inputs.get("tool_use_name") or "echo"
    tool_input = inputs.get("tool_input") or inputs.get("tool_use_input") or {}
    text_deltas = inputs.get("llm_text_deltas")
    reasoning_deltas = inputs.get("llm_reasoning_deltas")

    if text_deltas is not None or reasoning_deltas is not None:
        # 04 L1 不变量:流式增量
        blocks = [_text_block(t) for t in (text_deltas or [])]
        if not blocks:
            blocks = [_text_block("流式输出")]
        return _StreamingLLM([(text_deltas or []), (reasoning_deltas or [])])

    if "scenarios" in inputs:
        # 16 错误责任矩阵:多场景由 runner 特判
        return FakeLLMClient([])

    if tool_name and inputs.get("tool_use_name") is not None or inputs.get("tool_name") is not None:
        # 03/08 工具路径:tool_use → end_turn
        if inputs.get("llm_script_mode") == "forever":
            return _ForeverToolLLM(tool_name, tool_input)
        script.append({
            "content": [_tool_block(tool_name, tool_input)],
            "stop_reason": "tool_use",
        })
        script.append({
            "content": [_text_block("完成")],
            "stop_reason": "end_turn",
        })
        return FakeLLMClient(script)

    # 默认:单轮 end_turn(02/05/09/13 等)
    script.append({
        "content": [_text_block("好的")],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    })
    return FakeLLMClient(script)


class _StreamingLLM(FakeLLMClient):
    """流式增量 LLM(L1 不变量用例):先 text 增量再 reasoning 增量,最后 done。"""

    def __init__(self, deltas: Tuple[List[str], List[str]]) -> None:
        super().__init__([])
        self.text_deltas, self.reasoning_deltas = deltas

    async def chat_main_stream(self, messages, tools=None, system=None, max_tokens=None,
                               cancel_event=None, activity_timeout=None):
        self.calls += 1
        for t in self.text_deltas:
            yield {"type": "text", "text": t}
        for r in self.reasoning_deltas:
            yield {"type": "reasoning", "text": r, "signature": None}
        yield {"type": "done", "stop_reason": "end_turn",
               "content_blocks": [{"type": "text", "text": "".join(self.text_deltas)}],
               "usage": {"input_tokens": 1, "output_tokens": 1}}


class _ForeverToolLLM(FakeLLMClient):
    """每轮都请求工具(16 max_loops 场景):驱动循环到 max_loops。"""

    def __init__(self, tool_name: str, tool_input: dict) -> None:
        super().__init__([])
        self.tool_name = tool_name
        self.tool_input = tool_input

    async def chat_main_stream(self, messages, tools=None, system=None, max_tokens=None,
                               cancel_event=None, activity_timeout=None):
        self.calls += 1
        yield {"type": "done", "stop_reason": "tool_use",
               "content_blocks": [{"type": "tool_use", "id": f"u{self.calls}",
                                   "name": self.tool_name, "input": self.tool_input}],
               "usage": None}


# ---------------------------------------------------------------------------
# 协议层用例执行(不经 pipeline,直接对拍宿主协议层)
# ---------------------------------------------------------------------------
async def _run_protocol_case(case: Dict[str, Any], verbose: bool,
                             capture_codes: Optional[List[str]] = None) -> _CaseResult:
    case_id = case["id"]
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    errors: List[str] = []
    errors += _check_supported(expected)
    errors += _check_min_assertions(expected)
    # P1-7(2026-09-11 综合评审):协议类用例同样受扩展行为键守卫 —— 此前守卫
    # 仅覆盖 pipeline 类用例,22 条协议类用例的 inputs 死键零拦截。
    errors += _check_unknown_extension_inputs(
        inputs, case.get("setup", {}).get("extensions", [])
    )
    custom_events: List[Dict[str, Any]] = []

    if case_id.startswith("transport-frame"):
        frames = inputs.get("frames") or {}
        limits = inputs.get("limits") or {}
        r = {}
        # limits 落实(2026-09-11 交叉评审):此前读入即弃 —— 用例声明的边界值
        # 从未生效(假绿输入)。现改为 ①与协议常量机械比对 ②真实驱动超限拒绝。
        if limits:
            r["limits_match_protocol"] = (
                limits.get("frame_max_bytes") == FRAME_MAX_BYTES
                and limits.get("json_max_depth") == JSON_MAX_DEPTH
            )
            limit_bytes = limits.get("frame_max_bytes")
            if isinstance(limit_bytes, int) and limit_bytes > 0:
                r["frame_limit_enforced"] = bool(
                    check_frame_limits({"p": "x" * (limit_bytes + 1)})
                )
        try:
            f = TransportFrame.decode(json.dumps(frames["valid_full"], ensure_ascii=False))
            r["valid_full_decoded"] = f.type == "invoke_hook"
        except Exception:
            r["valid_full_decoded"] = False
        for key, label in [("invalid_type", "invalid_type_rejected"),
                           ("invalid_encoding", "invalid_encoding_rejected"),
                           ("invalid_json", "invalid_json_rejected"),
                           ("missing_fields", "missing_fields_rejected"),
                           ("delta_bad_revision", "delta_bad_revision_rejected")]:
            try:
                TransportFrame.decode(json.dumps(frames[key], ensure_ascii=False))
                r[label] = False
            except (ValueError, TypeError):
                r[label] = True
        try:
            d = TransportFrame.decode(json.dumps(frames["delta_valid"], ensure_ascii=False))
            r["delta_valid_decoded"] = d.encoding == "delta" and d.payload["base_revision"] == 3
        except Exception:
            r["delta_valid_decoded"] = False
        custom_events.append({"type": "custom:frame_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)

    elif case_id.startswith(("storage-spi", "storage-prefix-transport", "host-port-inprocess")):
        tmp = tempfile.mkdtemp(prefix="bs_storage_")
        db = os.path.join(tmp, "s.db")
        storage = SQLiteStorageProvider(db)

        class _FakeRoleLLM:
            async def chat_role(self, role, messages, system=None, max_tokens=None, tools=None):
                return type("R", (), {"content": [{"type": "text", "text": f"fake:{role}"}],
                                      "stop_reason": "end_turn", "usage": {}})()

        tasks = TaskRegistry()
        bus = TransportBus(llm_client=_FakeRoleLLM(), storage_provider=storage, task_registry=tasks)
        ext_name = (case.get("setup") or {}).get("extensions", [{}])[0].get("name", "audit")
        caps = (case.get("setup") or {}).get("extensions", [{}])[0].get("capabilities") or []
        bus.register_extension(ext_name, caps)

        r: Dict[str, Any] = {}
        ops = list(inputs.get("storage_ops") or []) + list(inputs.get("host_port_ops") or [])
        # P1-7(2026-09-11 综合评审):op 枚举白名单 —— storage_ops 与 host_port_ops
        # 合并分派,未实现的 op 一律显式失败(此前 read 等死输入被静默跳过)
        _SUPPORTED_OPS = frozenset({
            "write", "query", "read", "write_cross_prefix",
            "invoke_llm", "task_register", "task_cancel",
        })
        for op in ops:
            _raw = str(op.get("op", ""))
            _check = _raw[len("storage_"):] if _raw.startswith("storage_") else _raw
            if _check not in _SUPPORTED_OPS:
                return _CaseResult(case_id, False, f"inputs 声明了未实现的 op: {_raw!r}")
        for op in ops:
            opname = op["op"]
            if opname.startswith("storage_"):
                opname = opname[len("storage_"):]  # write/query/write_cross_prefix 统一
            kind = op.get("kind", "")
            if opname == "write":
                resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": kind, "docs": op["docs"]}))
                if "result" in resp:
                    ids = resp["result"].get("doc_ids") or []
                    if len(op.get("docs") or []) > 1:
                        r["write_batch_ids"] = ids  # 批量写(§18.4)结果
                elif "error" in resp and resp["error"].get("code") == "invalid_payload":
                    r["illegal_kind_rejected"] = True  # 非法 kind(如 Bad;DROP)
            elif opname == "query":
                resp = await bus.handle(ext_name, TransportFrame("storage_query", {"kind": kind, "limit": op.get("limit")}))
                if "result" in resp:
                    r["query_limit_docs"] = resp["result"].get("docs") or []
            elif opname == "read":
                # P1-7 接线(2026-09-11):read 此前声明即跳过(假绿)。doc_id 支持
                # "__first__" = 首个 write 返回 id;往返比对 write 写入的首个 doc。
                doc_id = op.get("doc_id", "")
                if doc_id == "__first__":
                    ids = r.get("write_batch_ids") or []
                    doc_id = ids[0] if ids else ""
                resp = await bus.handle(ext_name, TransportFrame(
                    "storage_read", {"kind": kind, "doc_id": doc_id}))
                doc = (resp.get("result") or {}).get("doc")
                expect_doc = (op.get("docs") or [None])[0]
                r["read_roundtrip_ok"] = doc is not None and doc == expect_doc
            elif opname == "write_cross_prefix":
                resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": kind, "docs": op["docs"]}))
                r["cross_prefix_rejected"] = "error" in resp and resp["error"]["code"] == "kind_prefix_violation"
            elif opname == "invoke_llm":
                resp = await bus.handle(ext_name, TransportFrame("invoke_llm", {"role": op.get("role", "main"), "messages": op["messages"]}))
                r["invoke_llm_ok"] = "result" in resp
            elif opname == "task_register":
                resp = await bus.handle(ext_name, TransportFrame("task_register", {"task_id": op["task_id"], "description": op.get("description", "")}))
                r["task_register_ok"] = "result" in resp and tasks.registered_count == 1
            elif opname == "task_cancel":
                resp = await bus.handle(ext_name, TransportFrame("task_cancel", {"task_id": op["task_id"]}))
                r["task_cancel_ok"] = "result" in resp and tasks.registered_count == 0
        # 非法 kind 拒绝(06)
        if inputs.get("illegal_kind"):
            resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": inputs["illegal_kind"], "docs": [{"x": 1}]}))
            r["illegal_kind_rejected"] = "error" in resp
        # 跨前缀拒绝(未在 ops 显式覆盖时兜底验证)
        if "cross_prefix_rejected" not in r:
            resp = await bus.handle(ext_name, TransportFrame("storage_write", {"kind": "other.cross_prefix", "docs": [{"x": 1}]}))
            r["cross_prefix_rejected"] = "error" in resp and resp["error"]["code"] == "kind_prefix_violation"
        # 前缀隔离 = 前缀内批量写成功 AND 跨前缀拒绝(§15-A3)
        r["prefix_isolation"] = bool(r.get("write_batch_ids")) and bool(r.get("cross_prefix_rejected"))

        event_type = "custom:host_port_result" if case_id.startswith("host-port-inprocess") else "custom:storage_result"
        custom_events.append({"type": event_type, "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
        errors += _assert_final(
            expected.get("final") or {}, {"type": EV_DONE, "is_complete": True}, [], None
        )
        storage.close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    elif case_id.startswith("invoke-llm"):
        tmp = tempfile.mkdtemp(prefix="bs_llm_")
        db = os.path.join(tmp, "s.db")

        class _CountingLLM:
            def __init__(self):
                self.max_concurrency = 0
                self.current = 0

            async def chat_role(self, role, messages, system=None, max_tokens=None, tools=None):
                self.current += 1
                self.max_concurrency = max(self.max_concurrency, self.current)
                await asyncio.sleep(0.02)
                self.current -= 1
                return type("R", (), {"content": [{"type": "text", "text": f"fake:{role}"}],
                                      "stop_reason": "end_turn", "usage": {}})()

        llm = _CountingLLM()
        tasks = TaskRegistry()
        bus = TransportBus(llm_client=llm, storage_provider=None, task_registry=tasks)
        bus.register_extension("llm_ext", ["llm"])
        bus.register_extension("plain_ext", [])
        r: Dict[str, Any] = {}
        for op in inputs.get("llm_ops") or []:
            ext = op.get("extension", "llm_ext")
            if op["op"] == "invoke_llm":
                resp = await bus.handle(ext, TransportFrame("invoke_llm", {"role": op.get("role", "main"), "messages": op["messages"]}))
                if op["extension"] == "llm_ext":
                    r["llm_ext_success"] = "result" in resp
                else:
                    r["plain_ext_rejected"] = "error" in resp and resp["error"]["code"] == "capability_not_declared"
            elif op["op"] == "invoke_llm_unknown_role":
                resp = await bus.handle(ext, TransportFrame("invoke_llm", {"role": op.get("role", "consolidation"), "messages": op["messages"]}))
                r["unknown_role_fallback_main"] = "result" in resp
            elif op["op"] == "invoke_llm_concurrent":
                results = await asyncio.gather(*[
                    bus.handle("llm_ext", TransportFrame("invoke_llm", {"role": "main", "messages": [{"role": "user", "content": "x"}]}))
                    for _ in range(op.get("count", 10))
                ])
                r["concurrency_max"] = llm.max_concurrency
        custom_events.append({"type": "custom:llm_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    elif case_id.startswith("l3-observe"):
        # L3 观测批处理旁路(§18.5):EventStream.route_l3 只投 L3 事件(tool_use/tool_result/step_end);
        # L1(text_delta/reasoning_delta)与外壳直通(done/error/step_start)不投 L3;有界队列满丢最旧+计数
        from teage_liu2.core.event_stream import EventStream
        sink = L3BatchSink(batch_window=0.02, batch_max=64)
        received: List[Dict[str, Any]] = []

        async def deliver(events: List[dict]) -> None:
            received.extend(events)

        sink.subscribe("obs", deliver)
        es = EventStream(l3_sink=sink)
        for ev in inputs.get("l3_events") or []:
            es.route_l3(ev)
        # L1 / 外壳直通事件不投 L3(§3.2)
        es.route_l3({"type": "text_delta", "session_id": "s", "text": "x"})
        es.route_l3({"type": "reasoning_delta", "session_id": "s", "text": "r", "signature": None})
        es.route_l3({"type": "step_start", "session_id": "s", "step": 1})
        es.route_l3({"type": "done", "session_id": "s"})
        await asyncio.sleep(0.1)
        received_types = [ev.get("type") for ev in received]
        r: Dict[str, Any] = {
            "observe_received_types": received_types,
            "l1_never_in_transport": "text_delta" not in received_types and "reasoning_delta" not in received_types,
            "bounded_queue_no_drop": sink.dropped_for("obs") == 0,
        }
        custom_events.append({"type": "custom:l3_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
        await sink.close()

    elif case_id.startswith("evolution-negotiation"):
        core_version = inputs.get("core_version", "v1.0.0")
        r: Dict[str, Any] = {}
        counts = {"proceed": 0, "downgrade": 0, "reject": 0}
        all_match = True
        for n in inputs.get("negotiations") or []:
            ext_ver = n.get("extension_version")
            try:
                result = negotiate_protocol_version(core_version, ext_ver)
            except ValueError:
                # 非法版本字符串 → reject(§evolution V-2 可读错误)
                result = {"compatible": False, "action": "reject"}
            actual = result["action"]
            counts[actual] = counts.get(actual, 0) + 1
            if actual != n.get("expect"):
                all_match = False
                # P0-2(2026-09-11 综合评审):逐场景失败立即进 errors(硬校验),
                # 不依赖 all_actions_match 的 payload 回环 —— 此前 mismatch 只写
                # payload,matcher {type:integer}/{type:boolean} 对错值同样通过,
                # 删掉三态判定用例仍绿(假绿)。
                errors.append(
                    f"[{case_id}] 版本协商 {n.get('label', '?')}: "
                    f"expect {n.get('expect')!r}, got {actual!r}"
                )
        r["proceed_cases"] = counts.get("proceed", 0)
        r["downgrade_cases"] = counts.get("downgrade", 0)
        r["reject_cases"] = counts.get("reject", 0)
        r["all_actions_match"] = all_match
        custom_events.append({"type": "custom:negotiation_result", "payload": r})
        errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)

    elif case_id.startswith("lifecycle-reload"):
        errors += await _run_lifecycle_reload(case)

    elif case_id.startswith("error-responsibility"):
        errors += await _run_error_matrix(case)

    elif case_id.startswith("error-codes"):
        errors += await _run_error_code_matrix(case, custom_events)

    elif case_id.startswith("config-domain"):
        errors += await _run_config_case(case, custom_events)

    elif case_id.startswith(("stdio-proxy", "storage-batch-atomic", "types-doc-opaque")):
        # 三者共用同一分支:22 走全链往返,26 走批量原子性(expect_error + final_probe),
        # 37 走 doc 透明性(nested 结构往返)—— 同 `_run_stdio_case`,由 inputs 驱动差异
        errors += await _run_stdio_case(case, custom_events)

    elif case_id.startswith("host-component"):
        errors += await _run_host_component_case(case, custom_events)

    elif case_id.startswith("events-"):
        errors += await _run_events_case(case, custom_events)

    else:
        errors.append(f"未知用例类型: {case_id}")

    # 错误码断言:事件面(用例 payload 内出现的码)+ 日志面(捕获的前缀码)
    errors += _assert_error_codes(
        expected.get("error_codes") or {},
        _extract_codes(json.dumps(custom_events, ensure_ascii=False)),
        capture_codes or [],
    )

    return _CaseResult(case["id"], not errors, "; ".join(errors))


async def _run_lifecycle_reload(case: Dict[str, Any]) -> List[str]:
    """14 热重载生命周期:registry.rebuild 原子替换 + setup 失败回滚保旧链。"""
    errors: List[str] = []
    registry = BranchRegistry()

    class _GoodBranch(Branch):
        name = "a"

        async def setup(self, config, host):
            pass

    class _BadBranch(Branch):
        name = "boom"

        async def setup(self, config, host):
            raise ValueError("setup fail")

    # register_factory = 测试注入通道(设计 §2.1 定位):行为套件经此注入内存
    # ScriptedBranch 做协议对拍;生产扩展一律走 extensions_root 目录发现。
    registry.register_factory("a", lambda cfg: _GoodBranch())
    registry.register_factory("boom", lambda cfg: _BadBranch())
    cfg1 = {"core": {"branches": {"a": True}}}
    registry.build(cfg1)
    await registry.setup_all(cfg1, host_builder=lambda n: {"protocol_domains": [], "storage": {"channel": "transport.storage_*", "kind_prefix": n}})

    reload_success_entries = len(registry.entries)
    rollback_preserved = False
    setup_idempotent = False
    # 第一次 reload 成功(同配置)
    await registry.rebuild(cfg1)
    reload_success_entries = len(registry.entries)
    # setup 幂等可重入:再次 setup 不抛
    try:
        await registry.setup_all(cfg1, host_builder=lambda n: {"protocol_domains": [], "storage": {"channel": "transport.storage_*", "kind_prefix": n}})
        setup_idempotent = True
    except Exception:
        setup_idempotent = False
    # 回滚:加 boom(setup 失败)
    cfg_bad = {"core": {"branches": {"a": True, "boom": True}}}
    try:
        await registry.rebuild(cfg_bad)
    except ValueError:
        rollback_preserved = len(registry.entries) == 1 and registry.entries[0][0].name == "a"
    await registry.teardown_all()

    custom = {
        "type": "custom:reload_result",
        "payload": {
            "reload_success_entries": reload_success_entries,
            "rollback_preserved_old_chain": rollback_preserved,
            "setup_idempotent_reentrant": setup_idempotent,
        },
    }
    expected = case.get("expected") or {}
    errors += _assert_event_stream(expected.get("event_stream") or [], [custom])
    return errors


async def _run_error_matrix(case: Dict[str, Any]) -> List[str]:
    """16 错误责任矩阵:7 终止原因实际路径全覆盖。"""
    errors: List[str] = []
    inputs = case.get("inputs") or {}
    scenarios = inputs.get("scenarios") or []
    covered = set()
    llm_error_no_done = True

    for sc in scenarios:
        label = sc.get("label", "")
        storage = _EphemeralStorage()
        hooks = HookChain()
        invocations: List[Dict[str, Any]] = []
        # 扩展注册按场景精确匹配(no_tool_executor 不注册执行者)
        if sc.get("ext_before") == "set_stop":  # intercepted
            b = ScriptedBranch("ext_stop", {"name": "ext_stop", "hooks_implemented": ["before"], "capabilities": []},
                               {"before_actions": [make_action("SetStop", reason="test")]}, invocations)
            hooks.register(b)
        if sc.get("ext_policy_decision"):  # tool_rejected
            b = ScriptedBranch("ext_policy", {"name": "ext_policy", "hooks_implemented": ["pre_tool_call"], "capabilities": []},
                               {"pre_decision": sc["ext_policy_decision"]}, invocations)
            hooks.register(b)
        if label in ("max_loops", "tool_rejected"):  # 有工具执行者
            b = ScriptedBranch("ext_tools", {"name": "ext_tools", "hooks_implemented": ["on_tool_call"], "capabilities": ["tool_executor"]},
                               {"tool_result": "executed"}, invocations)
            hooks.register(b)

        if label == "llm_error":
            class _ErrLLM(FakeLLMClient):
                async def chat_main_stream(self, *a, **kw):
                    self.calls += 1
                    raise RuntimeError("boom")
                    yield  # pragma: no cover
            llm = _ErrLLM()
        elif label == "max_loops":
            llm = _ForeverToolLLM("t", {})
        elif label in ("no_tool_executor", "tool_rejected"):
            llm = FakeLLMClient([{"content": [{"type": "tool_use", "id": "u1", "name": "t", "input": {}}], "stop_reason": "tool_use"}])
        else:  # normal / user_cancel / intercepted
            llm = FakeLLMClient([{"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}])

        cancel_event = threading.Event() if label == "user_cancel" else None
        if cancel_event is not None:
            cancel_event.set()
        pipeline = ChatPipeline(
            llm_client=llm,
            history_store=storage,
            hooks=hooks,
            max_loops=int(sc.get("max_loops", 50) or 50),
            storage_writer=None,
        )
        events: List[Dict[str, Any]] = []
        async for ev in pipeline.chat_stream("s-16", "错误矩阵测试", cancel_event=cancel_event):
            events.append(ev)

        expect = sc.get("expect") or {}
        etype = expect.get("type")
        if etype == "error":
            if any(e.get("type") == EV_ERROR for e in events):
                covered.add(label)
            else:
                errors.append(f"场景 {label}: 期望 error 事件")
            if any(e.get("type") == EV_DONE for e in events):
                llm_error_no_done = False
        elif etype == "tool_result":
            trs = [e for e in events if e.get("type") == EV_TOOL_RESULT]
            if trs and trs[0].get("is_error"):
                covered.add(label)
            else:
                errors.append(f"场景 {label}: 期望 tool_result is_error")
        else:
            done = next((e for e in events if e.get("type") == EV_DONE), None)
            if done and done.get("termination_reason") == expect.get("termination_reason"):
                covered.add(label)
            else:
                errors.append(f"场景 {label}: 期望 done({expect.get('termination_reason')})")
        storage.close()

    custom = {
        "type": "custom:error_matrix_result",
        "payload": {
            "scenario_count": len(scenarios),
            "all_termination_reasons_covered": len(covered) == 7,
            "llm_error_has_no_done": llm_error_no_done,
        },
    }
    expected = case.get("expected") or {}
    errors += _assert_event_stream(expected.get("event_stream") or [], [custom])
    if len(covered) != 7:
        errors.append(f"终止原因未全覆盖: {sorted(covered)}")
    return errors


class _RaisingLLM:
    """错误码矩阵:``chat_main_stream`` 抛指定异常(驱动 LLM_* 日志面)。"""

    activity_timeout = 60.0
    stream_total_timeout = 300.0

    def __init__(self, exc_factory: Any) -> None:
        self._exc_factory = exc_factory

    async def chat_main_stream(self, *args: Any, **kwargs: Any):
        raise self._exc_factory()
        yield  # pragma: no cover

    def close(self) -> None:
        pass


class _FaultHistoryStore:
    """错误码矩阵:落盘必失败(驱动 STORAGE_WRITE_FAILED 日志面)。"""

    def ensure_session(self, session_id: str):
        return None

    def get_session_messages(self, session_id: str, limit=None, before_id=None):
        return []

    def log_message(self, *args: Any, **kwargs: Any):
        raise RuntimeError("矩阵:落盘故障")

    def log_message_buffered(self, *args: Any, **kwargs: Any):
        raise RuntimeError("矩阵:落盘故障")

    def close(self):
        return None


# ---------------------------------------------------------------------------
# 20 错误码矩阵 / 21 config 域
# ---------------------------------------------------------------------------
async def _run_error_code_matrix(case: Dict[str, Any],
                                 custom_events: List[Dict[str, Any]]) -> List[str]:
    """20 错误码矩阵:按 scenarios 驱动各码的发射路径。

    双面锚定 —— ①**事件面** = 各场景实际产生的 `error` 事件 `code`(汇总进
    `custom:error_matrix`,由 _run_protocol_case 末尾统一断言);②**日志面** =
    _LogCapture 捕获的 `CODE: message` 前缀。此前的实现只取日志面、`custom_events`
    为空 → 事件面形同虚设;且 LLM_CANCELED 属规范面①(用户取消,info 级日志不入
    warning+ 捕获),故此前仅单测锚定。本函数补上事件面通道。
    """
    inputs = case.get("inputs") or {}
    errors: List[str] = []
    observed: List[str] = []  # 事件面:各场景 error 事件的 code

    async def _drive(chain: HookChain, llm: Any, session_id: str) -> None:
        storage = _EphemeralStorage()
        try:
            pipeline = ChatPipeline(
                llm_client=llm, history_store=storage, hooks=chain,
                max_loops=int(inputs.get("max_loops", 3) or 3), storage_writer=None,
            )
            async for ev in pipeline.chat_stream(session_id, "错误码矩阵"):
                if ev.get("type") == "error" and ev.get("code"):
                    observed.append(ev["code"])
        finally:
            storage.close()

    for sc in inputs.get("scenarios") or []:
        kind = sc.get("kind")
        try:
            if kind == "raise_on":  # HOOK_EXCEPTION
                chain = HookChain()
                chain.register(ScriptedBranch(
                    "ext_boom",
                    {"name": "ext_boom", "hooks_implemented": ["before"], "capabilities": []},
                    {"raise_on": {sc.get("hook", "before"): RuntimeError("脚本化异常")}},
                    [],
                ))
                await _drive(chain, _build_fake_llm({"llm_text_deltas": ["ok"]}), "s-20-exc")
            elif kind == "terminal_action":  # HOOK_TERMINAL_ACTION_IGNORED
                chain = HookChain()
                chain.register(ScriptedBranch(
                    "ext_terminal",
                    {"name": "ext_terminal", "hooks_implemented": ["after"], "capabilities": []},
                    {"after_actions": [make_action("SetSystem", text="被忽略")]},
                    [],
                ))
                await _drive(chain, _build_fake_llm({"llm_text_deltas": ["ok"]}), "s-20-terminal")
            elif kind == "tool_path_no_executor":  # TOOL_NO_EXECUTOR
                chain = HookChain()
                await _drive(chain, _build_fake_llm({"tool_name": "t"}), "s-20-noexec")
            elif kind == "loop_max":  # LOOP_MAX_REACHED
                chain = HookChain()
                chain.register(ScriptedBranch(
                    "ext_tools",
                    {"name": "ext_tools", "hooks_implemented": ["on_tool_call"],
                     "capabilities": ["tool_executor"]},
                    {"tool_result": "executed"}, [],
                ))
                await _drive(chain, _ForeverToolLLM("t", {}), "s-20-loopmax")
            elif kind == "llm_error":  # LLM_TIMEOUT / LLM_STREAM_FAILED / LLM_API_ERROR(日志面)
                from teage_liu2.core.llm import ActivityTimeout, StreamCancelled

                code = sc.get("code")
                exc_map = {
                    "LLM_TIMEOUT": lambda: ActivityTimeout("矩阵:空闲超时"),
                    "LLM_CANCELED": lambda: StreamCancelled(),
                    "LLM_STREAM_FAILED": lambda: asyncio.TimeoutError(),
                    "LLM_API_ERROR": lambda: RuntimeError("矩阵:API 故障"),
                }
                if code not in exc_map:
                    errors.append(f"未知 LLM 错误码: {code!r}")
                else:
                    await _drive(HookChain(), _RaisingLLM(exc_map[code]),
                                 f"s-20-{str(code).lower()}")
            elif kind == "storage_write_fail":  # STORAGE_WRITE_FAILED(日志面)
                pipeline = ChatPipeline(
                    llm_client=_build_fake_llm({"llm_text_deltas": ["ok"]}),
                    history_store=_FaultHistoryStore(),
                    hooks=HookChain(),
                )
                async for ev in pipeline.chat_stream("s-20-storefail", "错误码矩阵"):
                    if ev.get("type") == "error" and ev.get("code"):
                        observed.append(ev["code"])
            else:
                errors.append(f"未知错误码场景: {kind!r}")
        except Exception as e:  # noqa: BLE001 - 矩阵驱动,异常计入用例结果
            errors.append(f"场景 {kind} 驱动异常: {e}")
    custom_events.append({
        "type": "custom:error_matrix",
        "payload": {"event_codes": list(dict.fromkeys(observed))},
    })
    return errors


async def _run_config_case(case: Dict[str, Any],
                           custom_events: List[Dict[str, Any]]) -> List[str]:
    """21 config 域:core 段严格校验对拍(码写入 payload 供事件面断言)。

    custom_events 由调用方传入并就地追加 —— 错误码断言在 _run_protocol_case
    末尾统一做(它扫描 custom_events 序列化文本取码)。
    """
    from teage_liu2.core.config import core_config_from

    inputs = case.get("inputs") or {}
    errors: List[str] = []
    observed: List[str] = []

    try:
        cfg = core_config_from(inputs.get("valid") or {})
        valid_ok = cfg.mode == "loop" and cfg.max_loops == 10
    except Exception as e:  # noqa: BLE001 - 合法配置被拒 = 用例失败
        valid_ok = False
        errors.append(f"合法配置被拒: {e}")

    all_carry = True
    invalid = inputs.get("invalid") or []
    for item in invalid:
        try:
            core_config_from(item["cfg"])
            all_carry = False
            errors.append(f"{item['label']}: 期望抛错但通过")
        except ValueError as e:
            found = _extract_codes(str(e))
            observed.extend(found)
            if item["code"] not in str(e):
                all_carry = False
                errors.append(f"{item['label']}: 错误消息未携带 {item['code']}: {e}")

    custom_events.append({
        "type": "custom:config_result",
        "payload": {
            "valid_ok": valid_ok,
            "all_invalid_carry_code": all_carry,
            "invalid_count": len(invalid),
            "observed_codes": list(dict.fromkeys(observed)),
        },
    })

    # C-2 敏感字段(WP-B 用例 39):${VAR} 占位注入 + 脱敏 —— 密钥明文不得出现在错误消息
    env_spec = inputs.get("env") or {}
    yaml_text = inputs.get("yaml")
    if yaml_text is not None or env_spec:
        import os as _os

        from teage_liu2.core.config import (
            _resolve_value,
            is_masked_value,
            mask_sensitive_config,
        )

        saved_env = {k: _os.environ.get(k) for k in env_spec}
        _os.environ.update({k: str(v) for k, v in env_spec.items()})
        try:
            resolved = _resolve_value(yaml.safe_load(yaml_text) or {}) if yaml_text else {}
            field_path = (inputs.get("probe") or {}).get("field", "")
            value: Any = resolved
            for part in (field_path.split(".") if field_path else []):
                value = value.get(part) if isinstance(value, dict) else None
            # 触发一个可读错误,断言其中不含任何密钥明文
            error_text = ""
            try:
                core_config_from({"core": {"no_such_key": 1}})
            except ValueError as e:
                error_text = str(e)
            secrets = [str(v) for v in env_spec.values()]
            masked_view = mask_sensitive_config(
                {"llm": {"main_api_key": value}} if field_path else {}
            )
            custom_events.append({
                "type": "custom:config_secret_result",
                "payload": {
                    "placeholder_resolved": bool(value) and "${" not in str(value),
                    "secret_not_in_errors": all(s not in error_text for s in secrets),
                    "masked_hides_secret": is_masked_value(
                        (((masked_view or {}).get("llm") or {}).get("main_api_key"))
                    ),
                },
            })
        finally:
            for key, old in saved_env.items():
                if old is None:
                    _os.environ.pop(key, None)
                else:
                    _os.environ[key] = old
    errors += _assert_event_stream(
        (case.get("expected") or {}).get("event_stream") or [], custom_events
    )
    return errors


# ---------------------------------------------------------------------------
# 22 真实子进程 stdio 场景(WP-A 任务 A2;被测进程 = P-4 参考后端)
# ---------------------------------------------------------------------------
async def _run_stdio_case(case: Dict[str, Any],
                          custom_events: List[Dict[str, Any]]) -> List[str]:
    """spawn 参考后端 → hello 握手 → 逐 op 调用 → bye 关闭。

    runner 此处**首次依赖宿主实现**(``teage_liu2.server.storage_stdio_proxy``)——
    定位见 ``PROTOCOL/README.md``「依赖边界」段;观测结果统一经
    ``custom:stdio_result`` 事件交给 ``expected.event_stream`` 断言。

    返回 errors 列表(与 ``_run_config_case`` 同契约)。
    """
    from teage_liu2.server.storage_stdio_proxy import StdioStorageProxy

    errors: List[str] = []
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    backend = Path(__file__).resolve().parent / "tools" / "reference_storage_backend.py"
    results: List[Dict[str, Any]] = []
    pid = 0
    # 默认 False:只有真正观察到"中文原样读回"才置 True(否则无 op 时该断言恒真、不承重)
    utf8_ok = False
    # 批量原子性(A5):被拒的具体原因 + 探针回读条数(默认 0,只有真探测才赋值)
    rejected_non_dict = False
    final_rows = 0

    with tempfile.TemporaryDirectory(prefix="bs_stdio_") as td:
        proxy = StdioStorageProxy(
            command=[sys.executable, str(backend), "--db", str(Path(td) / "ref.db")],
            request_timeout=10.0,
        )
        proxy.start()  # 握手失败/主版本不符 → 抛(start 非幂等,只调一次)
        try:
            # 子进程句柄在 _conn._proc(没有 proxy._proc)
            proc = getattr(getattr(proxy, "_conn", None), "_proc", None)
            pid = int(getattr(proc, "pid", 0) or 0)
            for spec in inputs.get("storage_ops") or []:
                op_name = spec["op"]
                fn = getattr(proxy, op_name)
                try:
                    value = fn(*spec.get("args") or [], **(spec.get("kwargs") or {}))
                except Exception as e:  # noqa: BLE001 - expect_error 场景依赖之
                    if spec.get("expect_error"):
                        # 承重判据:拒绝原因必须是"元素非对象"(非"任意异常都算过")
                        rejected_non_dict = rejected_non_dict or ("必须是对象" in str(e))
                        results.append({"op": op_name, "error": str(e)})
                        continue
                    errors.append(f"op {op_name} 意外失败: {type(e).__name__}: {e}")
                    continue
                if spec.get("expect_error"):
                    errors.append(f"op {op_name} 期望报错但成功返回")
                want = spec.get("assert")
                if want is not None and not match_value(want, value):
                    errors.append(f"op {op_name} 断言失败: expect={want} actual={value!r}")
                if op_name == "get_session_messages":
                    utf8_ok = any(m.get("content") == "你好" for m in (value or []))
                results.append({"op": op_name, "value": value})
            # 收尾探针(A5):回读目标 kind 的实际条数,证明"批次失败后无残留"
            probe = inputs.get("final_probe")
            if probe:
                probed = getattr(proxy, probe["op"])(*(probe.get("args") or []))
                final_rows = len(probed or [])
        finally:
            proxy.close()
        # P-7 双档口径(2026-09-11 落地审查):background 档经 log_messages(帧)发送、
        # flush 档才计 log_message —— metrics 为本地快照,close 后仍可读
        metrics = proxy.metrics_snapshot()

    custom_events.append({
        "type": "custom:stdio_result",
        "payload": {
            "handshake_ok": True,
            "op_count": len(results),
            "child_pid_gt_0": pid > 0,
            "message_count": sum(
                len(e.get("value") or [])
                for e in results
                if e["op"] == "get_session_messages"
            ),
            "utf8_roundtrip": utf8_ok,
            "rejected_non_dict": rejected_non_dict,
            "final_rows": final_rows,
            "buffered_frames": int(metrics.get("log_messages", {}).get("count", 0)),
            "flush_direct_calls": int(metrics.get("log_message", {}).get("count", 0)),
        },
    })
    errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
    errors += _assert_final(
        expected.get("final") or {},
        {"type": EV_DONE, "is_complete": True},   # 协议层无真实 done:按 storage 分支范式合成
        [],
        None,
    )
    return errors


# ---------------------------------------------------------------------------
# 23/24 host-component 插槽场景(WP-A 任务 A3;P-5 插槽契约 + P-6 目录发现)
# ---------------------------------------------------------------------------
def _hc_materialize_extensions_root(td: str, manifest_kind: str, backend: Path) -> str:
    """在临时目录写 `extensions_root/ref_storage/manifest.yaml`(P-6 目录发现形态)。

    manifest_kind = "host-component"(默认)或 "branch"(用于"引用非宿主组件"负例);
    命令**直接写绝对路径**——manifest 无占位符/模板机制(`resolve_command` 只做
    "路径在扩展目录内且存在 → 绝对化"),故不可写 `{python}` 之类占位。

    返回 extensions_root 路径。
    """
    root = Path(td) / "extensions"
    ext_dir = root / "ref_storage"
    ext_dir.mkdir(parents=True, exist_ok=True)
    if manifest_kind == "branch":
        # 负例:kind=branch(language=python 需 entry 文件真实存在,故写占位)
        (ext_dir / "main.py").write_text("# 占位:仅用于 manifest 解析\n", encoding="utf-8")
        manifest: Dict[str, Any] = {
            "name": "ref_storage", "version": "1.0.0", "language": "python",
            "entry": "main.py", "capabilities": [],
        }
    else:
        manifest = {
            "name": "ref_storage", "version": "1.0.0",
            "kind": "host-component", "slots": ["storage", "history"],
            "language": "other", "transport": "stdio",
            "command": [sys.executable, str(backend)],
            "capabilities": [],   # host-component 强制空(P-6)
        }
    (ext_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    return str(root)


def _hc_inject_db_placeholder(hc: List[Dict[str, Any]], placeholder: str,
                              db: str) -> None:
    """把 host_components[*].options.args 里的 `@db` 占位替换为真实库路径。"""
    for entry in hc:
        if not isinstance(entry, dict):
            continue
        opts = entry.setdefault("options", {})
        if not isinstance(opts, dict):
            continue
        opts["args"] = [db if a == placeholder else a for a in (opts.get("args") or [])]


async def _run_host_component_case(case: Dict[str, Any],
                                   custom_events: List[Dict[str, Any]]) -> List[str]:
    """host-component 场景:正常装载(同实例覆盖双插槽) + 快速失败矩阵。

    观测经 ``custom:host_component_result`` / ``custom:host_component_reject``。
    """
    from teage_liu2.core.extension_loader import wire_extensions
    from teage_liu2.core.history import HistoryStore
    from teage_liu2.core.storage import MessageStore, StorageProvider
    from teage_liu2.server.host_components import load_host_components

    errors: List[str] = []
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    placeholder = inputs.get("db_placeholder", "@db")
    backend = Path(__file__).resolve().parent / "tools" / "reference_storage_backend.py"

    if case["id"].startswith("host-component-reject"):
        results: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="bs_hc_rej_") as td:
            for spec in inputs.get("rejects") or []:
                label = spec.get("label", "")
                try:
                    hc = copy.deepcopy(spec.get("host_components") or [])
                    _hc_inject_db_placeholder(hc, placeholder, str(Path(td) / "hc.db"))
                    for entry in hc:
                        # sqlite backend 的库路径也指到临时目录(避免写入仓库 data2/)
                        if isinstance(entry, dict) and entry.get("backend") == "sqlite":
                            entry.setdefault("options", {}).setdefault(
                                "sqlite_path", str(Path(td) / "hc.db"))
                    root = _hc_materialize_extensions_root(
                        td, spec.get("manifest_kind", "host-component"), backend)
                    cfg = {"core": {"extensions_root": root}, "host_components": hc}
                    merged, specs, _ = wire_extensions(cfg)
                    load_host_components(merged, specs)
                except Exception as e:  # noqa: BLE001 - 负例:期望抛错
                    results.append({
                        "label": label, "rejected": True,
                        "matched": bool(re.search(spec.get("expect_regex", ""), str(e))),
                        "error": f"{type(e).__name__}: {e}",
                    })
                    # 快速失败发生在"已创建部分对象"之后时(如首条 sqlite 接管成功、
                    # 第二条因重复接管抛错),未返回的对象会持库文件句柄 → 临时目录
                    # 清理失败(Windows WinError 32)。显式 GC 触发其析构关闭连接。
                    gc.collect()
                else:
                    results.append({"label": label, "rejected": False, "matched": False,
                                    "error": "非法配置未抛错(违反快速失败)"})
        rejected = sum(1 for r in results if r["rejected"])
        matched = sum(1 for r in results if r["matched"])
        custom_events.append({
            "type": "custom:host_component_reject",
            "payload": {
                "case_count": len(results),
                "rejected_count": rejected,
                "matched_count": matched,
                "all_rejected": bool(results) and rejected == len(results),
            },
        })
    else:
        mode = inputs.get("mode", "load")
        with tempfile.TemporaryDirectory(prefix="bs_hc_") as td:
            hc = copy.deepcopy(inputs.get("host_components") or [])
            _hc_inject_db_placeholder(hc, placeholder, str(Path(td) / "ref.db"))
            root = _hc_materialize_extensions_root(
                td, inputs.get("manifest_kind", "host-component"), backend)
            cfg: Dict[str, Any] = {"core": {"extensions_root": root}}
            if mode != "default_path":
                cfg["host_components"] = hc
            loaded: Dict[str, Any] = {}
            disabled: List[str] = []
            if mode == "disabled_probe":
                # P-1 条件③:已装未启用统计(目录内有 branch 扩展,core.branches 未声明)
                try:
                    _merged, specs, disabled = wire_extensions(cfg)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"wire_extensions 失败: {type(e).__name__}: {e}")
                    specs = {}
                custom_events.append({
                    "type": "custom:host_component_result",
                    "payload": {
                        "disabled": list(disabled),
                        "installed_count": len(specs),
                        "has_disabled": bool(disabled),
                    },
                })
            elif mode == "default_path":
                # P-5 条件③:缺省省略 host_components = 全部插槽用 core 默认实现
                try:
                    merged, specs, _ = wire_extensions(cfg)
                    loaded = load_host_components(merged, specs)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"缺省路径装载失败: {type(e).__name__}: {e}")
                custom_events.append({
                    "type": "custom:host_component_result",
                    "payload": {
                        "default_empty": loaded == {},
                        "slots": sorted(loaded.keys()),
                    },
                })
            elif mode == "uninstalled_probe":
                # P-1 条件③:声明了未安装的 branch 扩展 → 启动失败(不静默降级)。
                # P1-7(2026-09-11):inputs.extension_name 真实消费 —— 声明名即被
                # probe 的"未装"名;若声明的是已物化名(ref_storage)则用例语义失效
                declared = inputs.get("extension_name", "ghost_ext")
                if not declared or declared == "ref_storage":
                    errors.append(
                        f"inputs.extension_name={declared!r} 与'声明未装'语义冲突"
                        "(须为未物化的名字)"
                    )
                cfg["core"]["branches"] = {declared: {"enabled": True}}
                try:
                    wire_extensions(cfg)
                except Exception as e:  # noqa: BLE001
                    custom_events.append({
                        "type": "custom:host_component_result",
                        "payload": {
                            "uninstalled_rejected": bool(re.search("未安装", str(e))),
                        },
                    })
                else:
                    custom_events.append({
                        "type": "custom:host_component_result",
                        "payload": {"uninstalled_rejected": False},
                    })
            else:
                # P1-7(2026-09-11):inputs.extension_name 消费 —— 校验用例声明与
                # runner 物化的 manifest 目录名一致(防声明面与实现面漂移)
                declared = inputs.get("extension_name")
                if declared and declared != "ref_storage":
                    errors.append(
                        f"inputs.extension_name={declared!r} 与 runner 物化的 "
                        "manifest 名 'ref_storage' 不一致"
                    )
                try:
                    merged, specs, disabled = wire_extensions(cfg)
                    loaded = load_host_components(merged, specs)
                except Exception as e:  # noqa: BLE001 - 装载失败计入用例结果
                    errors.append(f"host_components 装载失败: {type(e).__name__}: {e}")
                if loaded:
                    proxy = loaded.get("storage")
                    try:
                        # 审查 R7:两侧都 sorted 后比较
                        custom_events.append({
                            "type": "custom:host_component_result",
                            "payload": {
                                "ok": sorted(loaded.keys()) == ["history", "storage"],
                                "slots": sorted(loaded.keys()),
                                "same_instance": loaded.get("history") is proxy,  # P-5 同实例覆盖双槽
                                "storage_isinstance": isinstance(proxy, StorageProvider),
                                "history_isinstance": (
                                    isinstance(loaded.get("history"), HistoryStore)
                                    and isinstance(loaded.get("history"), MessageStore)
                                ),
                                "disabled": list(disabled),
                            },
                        })
                    finally:
                        proxy.close()

    errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
    errors += _assert_final(
        expected.get("final") or {},
        {"type": EV_DONE, "is_complete": True},
        [],
        None,
    )
    return errors


# ---------------------------------------------------------------------------
# 33 events 域:未知事件类型宽容(E-2;L3 旁路层驱动)
# ---------------------------------------------------------------------------
async def _run_events_case(case: Dict[str, Any],
                           custom_events: List[Dict[str, Any]]) -> List[str]:
    """E-2:消费方(L3 旁路)遇到未知 type 事件 —— 透传或记录,绝不崩溃。

    未知类型不得阻断其后已知 L3 事件的投递(顺序完整性)。
    """
    from teage_liu2.core.event_stream import EventStream

    errors: List[str] = []
    inputs = case.get("inputs") or {}
    expected = case.get("expected") or {}
    sink = L3BatchSink(batch_window=0.02, batch_max=64)
    received: List[Dict[str, Any]] = []

    async def deliver(events: List[dict]) -> None:
        received.extend(events)

    sink.subscribe("obs", deliver)
    es = EventStream(l3_sink=sink)
    crashed = False
    try:
        for ev in inputs.get("route_events") or []:
            es.route_l3(ev)
        await asyncio.sleep(0.1)   # 等批处理窗口冲刷
    except Exception:  # noqa: BLE001 - "未知类型致崩溃"正是本用例要抓的缺陷
        crashed = True
    received_types = [ev.get("type") for ev in received]
    custom_events.append({
        "type": "custom:events_result",
        "payload": {
            "crashed": crashed,
            "received_types": received_types,
            "known_l3_delivered": "tool_use" in received_types,
        },
    })
    errors += _assert_event_stream(expected.get("event_stream") or [], custom_events)
    await sink.close()
    return errors


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run_case(case: Dict[str, Any], verbose: bool) -> _CaseResult:
    case_id = case.get("id", "")
    protocol_pipeline_ids = {
        "storage-spi-06", "transport-frame-10", "storage-prefix-transport-11",
        "invoke-llm-12", "l3-observe-13", "lifecycle-reload-14",
        "evolution-negotiation-15", "error-responsibility-16",
        "host-port-inprocess-17", "error-codes-20", "config-domain-21",
        "stdio-proxy-roundtrip-22",
        "stdio-proxy-dual-channel-42",
        "host-component-slot-23", "host-component-reject-24",
        "host-component-disabled-40", "host-component-default-41",
        "host-component-uninstalled-43",
        "storage-batch-atomic-26",   # 注:25(injection-dedup)是 pipeline 类,不入本表
        "events-unknown-type-33",    # 注:34/35/36 是 pipeline 类,不入本表
        "types-doc-opaque-37",       # 注:38 是 pipeline 类,不入本表
        "config-domain-secret-39",
    }
    # 错误码"日志面"捕获(错误码断言的唯一日志通道)
    capture = _LogCapture()
    pkg_logger = logging.getLogger("teage_liu2")
    pkg_logger.addHandler(capture)
    try:
        if case_id in protocol_pipeline_ids:
            return await _run_protocol_case(case, verbose, capture.records)
        return await _run_pipeline_case(case, verbose, capture.records)
    finally:
        pkg_logger.removeHandler(capture)


async def main() -> int:
    parser = argparse.ArgumentParser(description="teage_liu2 行为套件 runner")
    parser.add_argument("--case", default=None, help="只跑指定用例(id 前缀匹配,如 03)")
    parser.add_argument("--verbose", action="store_true", help="逐用例输出详情")
    args = parser.parse_args()

    case_files = sorted(f for f in os.listdir(_CASES_DIR) if f.endswith(".json"))
    if args.case:
        case_files = [f for f in case_files if f.startswith(args.case)]
    if not case_files:
        print(f"未找到用例(prefix={args.case!r})")
        return 1

    results: List[_CaseResult] = []
    for fname in case_files:
        path = os.path.join(_CASES_DIR, fname)
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        schema_errors = _schema_errors(case)
        if schema_errors:
            # 用例结构必须自洽(语言无关交付物):违反 suite.schema.json 直接判失败
            result = _CaseResult(
                case.get("id", fname), False,
                "用例结构违反 suite.schema.json: " + "; ".join(schema_errors),
            )
        else:
            result = await run_case(case, args.verbose)
        results.append(result)
        status = "PASS" if result.ok else "FAIL"
        print(f"  [{status}] {result.case_id}" + (f"  <- {result.detail}" if not result.ok else ""))
        if args.verbose:
            print(f"          {case.get('description', '')[:80]}")

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    print(f"\n=== 行为套件: {passed}/{total} 通过({100.0 * passed / total:.1f}%) ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
