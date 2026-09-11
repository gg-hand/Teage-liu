"""P-5 插槽契约(宿主侧)锚定 —— 2026-09-11 落地审查补锚定。

①**缺省省略 / 空数组 = 全部插槽用 core 默认实现**(返回空映射)—— 此前零覆盖;
②快速失败矩阵(未知插槽 / 未知 backend / 重复接管 / 非映射 / backend 契约不满足)。
"""
from __future__ import annotations

import pytest

from teage_liu2.core.history import HistoryStore, SQLiteHistoryStore
from teage_liu2.core.storage import MessageStore, StorageProvider
from teage_liu2.server import host_components as hc


def test_missing_host_components_means_empty_mapping():
    """缺省省略 host_components = 全部插槽用 core 默认实现(返回空映射,不报错)。"""
    assert hc.load_host_components({}) == {}
    assert hc.load_host_components({"core": {}}) == {}


def test_empty_array_means_empty_mapping():
    """显式空数组同样返回空映射。"""
    assert hc.load_host_components({"host_components": []}) == {}


def test_default_backend_is_sqlite_and_covers_both_slots(tmp_path):
    """backend 缺省 = sqlite;一次接管 storage + history 双槽。"""
    loaded = hc.load_host_components({
        "host_components": [{"slot": "storage"}],
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


@pytest.mark.parametrize("entry", [
    {"slot": "nope"},                                  # 未知插槽
    {"slot": "storage", "backend": "nope"},            # 未知 backend
    {"slot": "storage", "options": "not-a-mapping"},   # options 非映射
])
def test_single_entry_fast_fail(entry, tmp_path):
    with pytest.raises(ValueError):
        hc.load_host_components({
            "host_components": [entry],
            "storage": {"sqlite_path": str(tmp_path / "s.db")},
        })


def test_duplicate_takeover_rejected(tmp_path):
    with pytest.raises(ValueError, match="重复接管"):
        hc.load_host_components({
            "host_components": [{"slot": "storage"}, {"slot": "storage"}],
            "storage": {"sqlite_path": str(tmp_path / "s.db")},
        })


def test_non_list_host_components_rejected():
    with pytest.raises(ValueError, match="数组"):
        hc.load_host_components({"host_components": "storage"})


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


def test_sqlite_history_store_is_reachable_symbol():
    """防御性:默认 backend 依赖的符号必须存在(避免文档/代码漂移)。"""
    assert SQLiteHistoryStore is not None
