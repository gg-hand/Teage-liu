"""P-5 插槽契约(宿主侧)锚定 —— 2026-09-11 落地审查补锚定;2026-09-18 对齐协议 schema。

①**缺省省略 / 显式 null / 空数组 = 全部插槽用 core 默认实现**(返回空映射)—— 此前零覆盖;
②快速失败矩阵(未知插槽 / 未知 backend / **缺必填 backend** / **条目内未知键** /
  **非数组(含 falsy 非数组)** / options 非映射 / backend 契约不满足)。

⚠ `backend` 为**必填**(协议 schema `HostComponent.required` + `config.spec.md` §3),
**不存在"缺省 sqlite"**:内置 SQLite 要么显式写 `backend: sqlite`,
要么省略整个 `host_components` 段(那是另一条语义,见 ①)。
"""
from __future__ import annotations

import pytest

from teage_liu2.core.errors import (
    CONFIG_INVALID_VALUE,
    CONFIG_MISSING_KEY,
    CONFIG_UNKNOWN_KEY,
)
from teage_liu2.core.history import HistoryStore, SQLiteHistoryStore
from teage_liu2.core.storage import MessageStore, StorageProvider
from teage_liu2.server import host_components as hc


def test_missing_host_components_means_empty_mapping():
    """缺省省略 host_components = 全部插槽用 core 默认实现(返回空映射,不报错)。"""
    assert hc.load_host_components({}) == {}
    assert hc.load_host_components({"core": {}}) == {}


def test_explicit_null_means_empty_mapping():
    """显式 null 与缺席同义(契约允许的缺省);但**不等价于任意 falsy 值**。"""
    assert hc.load_host_components({"host_components": None}) == {}


def test_empty_array_means_empty_mapping():
    """显式空数组同样返回空映射。"""
    assert hc.load_host_components({"host_components": []}) == {}


def test_explicit_sqlite_backend_covers_both_slots(tmp_path):
    """显式 `backend: sqlite` → 一次接管 storage + history 双槽。"""
    loaded = hc.load_host_components({
        "host_components": [{"slot": "storage", "backend": "sqlite"}],
        "storage": {"sqlite_path": str(tmp_path / "s.db")},
    })
    try:
        assert set(loaded) == {"storage", "history"}
        assert isinstance(loaded["storage"], StorageProvider)
        assert isinstance(loaded["history"], HistoryStore)
        assert isinstance(loaded["history"], MessageStore)
    finally:
        close = getattr(loaded["storage"], "close", None)
        if callable(close):
            close()
        close_h = getattr(loaded["history"], "close", None)
        if callable(close_h):
            close_h()


@pytest.mark.parametrize("entry,code", [
    ({"slot": "nope", "backend": "sqlite"}, CONFIG_INVALID_VALUE),                 # 未知插槽
    ({"slot": "storage", "backend": "nope"}, CONFIG_INVALID_VALUE),               # 未知 backend
    ({"slot": "storage"}, CONFIG_MISSING_KEY),                                    # 缺必填 backend
    ({"slot": "storage", "backend": "sqlite", "optons": {}}, CONFIG_UNKNOWN_KEY),  # 条目内未知键
    # options 类型非法 —— **由协议 schema 拦截**(loader 不再重复检查,故文案来自 schema)
    ({"slot": "storage", "backend": "sqlite", "options": "not-a-mapping"},
     CONFIG_INVALID_VALUE),
])
def test_single_entry_fast_fail(entry, code, tmp_path):
    with pytest.raises(ValueError, match=code):
        hc.load_host_components({
            "host_components": [entry],
            "storage": {"sqlite_path": str(tmp_path / "s.db")},
        })


@pytest.mark.parametrize("value", [{}, 0, "", "storage"])
def test_non_list_host_components_rejected(value):
    """非数组一律失败 —— 含 **falsy 非数组**。

    此前实现写作 `cfg.get("host_components") or []`,使 `{}` / `0` / `""` 被静默
    当成"缺省",与协议 schema `type: array` 漂移(2026-09-18 修正)。
    """
    with pytest.raises(ValueError, match=CONFIG_INVALID_VALUE):
        hc.load_host_components({"host_components": value})


def test_duplicate_takeover_rejected(tmp_path):
    with pytest.raises(ValueError, match="重复接管"):
        hc.load_host_components({
            "host_components": [{"slot": "storage", "backend": "sqlite"},
                                {"slot": "storage", "backend": "sqlite"}],
            "storage": {"sqlite_path": str(tmp_path / "s.db")},
        })


def test_non_mapping_entry_rejected():
    with pytest.raises(ValueError, match="映射"):
        hc.load_host_components({"host_components": [123]})


def test_backend_returning_empty_mapping_rejected(monkeypatch):
    """backend 返回空映射 → TypeError(契约:必须返回非空 {slot: obj})。"""
    monkeypatch.setitem(hc.BACKENDS["storage"], "empty", lambda cfg, options, specs: {})
    with pytest.raises(TypeError):
        hc.load_host_components({"host_components": [{"slot": "storage", "backend": "empty"}]})


def test_backend_returning_unknown_slot_rejected(monkeypatch):
    monkeypatch.setitem(
        hc.BACKENDS["storage"], "weird", lambda cfg, options, specs: {"nope": object()}
    )
    with pytest.raises(TypeError, match="未知插槽"):
        hc.load_host_components({"host_components": [{"slot": "storage", "backend": "weird"}]})


def test_backend_object_not_satisfying_abc_rejected(monkeypatch):
    """返回值不满足插槽 ABC → TypeError(快速失败,不静默降级)。"""
    monkeypatch.setitem(
        hc.BACKENDS["storage"], "bad", lambda cfg, options, specs: {"storage": object()}
    )
    with pytest.raises(TypeError, match="未满足"):
        hc.load_host_components({"host_components": [{"slot": "storage", "backend": "bad"}]})
