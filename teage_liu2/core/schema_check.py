"""极简 JSON Schema 子集校验（hooks H-20：modify 后重过工具 input_schema）。

为何自研而非引入依赖：core 的依赖面必须保持最小（协议语言无关、宿主零外部
依赖可跑）；工具 schema 实际只使用 JSON Schema 的一小部分子集。

**支持的子集**（工具 schema 的常见形态）：
``type``（含类型数组）/ ``required`` / ``properties`` / ``items`` /
``enum`` / ``const`` / ``additionalProperties``(bool) /
``minimum`` / ``maximum`` / ``minLength`` / ``maxLength`` /
``minItems`` / ``maxItems``

**未知关键字一律忽略**（宽容）—— 与"core 对未知协议元素透传、绝不崩溃"
（evolution V-1）一致；本校验器只做**否定证据**（能判定非法才算非法），
不做完备性承诺。
"""
from __future__ import annotations

from typing import Any, Optional

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_against_schema(value: Any, schema: Any) -> Optional[str]:
    """按 schema 校验 value。合法返回 None，非法返回可读描述。"""
    if not isinstance(schema, dict) or not schema:
        return None
    return _check(value, schema, "$")


def _check(value: Any, schema: dict, path: str) -> Optional[str]:
    type_spec = schema.get("type")
    if type_spec is not None:
        names = type_spec if isinstance(type_spec, list) else [type_spec]
        known = [n for n in names if n in _TYPE_CHECKS]
        # 全部为未知类型名 → 宽容放过（不因 schema 用了新关键字而误拒）
        if known and not any(_TYPE_CHECKS[n](value) for n in known):
            return (
                f"{path} 类型应为 {'/'.join(str(n) for n in names)}"
                f"，实际 {type(value).__name__}"
            )

    if "enum" in schema and value not in (schema.get("enum") or []):
        return f"{path} 取值不在 enum {schema['enum']!r} 内"
    if "const" in schema and value != schema["const"]:
        return f"{path} 应为常量 {schema['const']!r}"

    if isinstance(value, bool):
        return None  # bool 不参与数值约束（Python 中 bool 是 int 子类）

    if isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} 小于 minimum {schema['minimum']!r}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} 大于 maximum {schema['maximum']!r}"

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} 长度小于 minLength {schema['minLength']!r}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} 长度大于 maxLength {schema['maxLength']!r}"

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} 元素数小于 minItems {schema['minItems']!r}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} 元素数大于 maxItems {schema['maxItems']!r}"
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                problem = _check(item, items, f"{path}[{i}]")
                if problem:
                    return problem

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                return f"{path} 缺少必填字段 {req!r}"
        additional = schema.get("additionalProperties")
        for key, sub_value in value.items():
            if key in properties:
                problem = _check(sub_value, properties[key], f"{path}.{key}")
                if problem:
                    return problem
            elif additional is False:
                return f"{path} 不允许字段 {key!r}"

    return None


def tool_input_schema(tools: Any, name: str) -> Any:
    """从工具 schema 列表中取指定工具的 ``input_schema``（未命中返回 None）。"""
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == name:
            return tool.get("input_schema")
    return None
