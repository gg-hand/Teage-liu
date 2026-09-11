"""P-8 条件③机械校验:`core/errors.py::ERROR_CODES` 与 errors.schema.json 的
ErrorCode 枚举**逐字一致** —— 2026-09-11 落地审查补回归防护。

此前"逐字一致"只是人工核对事实,无任何测试守护;本文件把它变成机检。
"""
from __future__ import annotations

import json
from pathlib import Path

from teage_liu2.core.errors import ERROR_CODES

_SCHEMA = (
    Path(__file__).resolve().parents[1] / "PROTOCOL" / "errors" / "errors.schema.json"
)


def _enum(name: str) -> set:
    doc = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    return set(doc["definitions"][name]["enum"])


def test_error_codes_match_schema_enum():
    """ERROR_CODES 与 schema ErrorCode 枚举必须逐字一致(双向)。"""
    assert set(ERROR_CODES) == _enum("ErrorCode")


def test_error_code_count_is_18():
    assert len(ERROR_CODES) == 18
