"""types T-3 不可变只读 / T-5 结构共享(禁 deepcopy)—— 2026-09-11 落地审查补锚定。

T-3:`Snapshot` 对外呈现为不可变:冻结属性赋值被拒,推进返回新实例,原实例不变。
T-5:推进走 O(n) 浅拷贝 + 共享元素引用,**绝不 deepcopy**。

已知边界(不在本文件修复范围,见 `CORE-缺口记录.md` H-17):`messages` / `extra`
是可**变容器**,扩展原地改写仍会成功;本文件只锚定"内核自身不原地改写、不 deepcopy"。
"""
from __future__ import annotations

import copy
import dataclasses

import pytest

from teage_liu2.core.actions import make_action
from teage_liu2.core.snapshot import apply_action_batch
from teage_liu2.core.types import Snapshot


def _base(messages=None) -> Snapshot:
    return Snapshot(
        session_id="s-id",
        user_input="u",
        messages=list(messages if messages is not None else [{"role": "user", "content": "u1"}]),
    )


def test_frozen_attribute_assignment_rejected():
    """T-3:冻结属性不可赋值。"""
    snap = _base()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.round = 1


def test_with_messages_returns_new_instance_and_keeps_original():
    """T-3:推进返回新实例,原实例不被改写。"""
    snap = _base()
    nxt = snap.with_messages(snap.messages + [{"role": "assistant", "content": "a1"}])
    assert nxt is not snap
    assert len(snap.messages) == 1
    assert len(nxt.messages) == 2


def test_with_messages_shares_element_references():
    """T-5:未变元素对象同一(`is`),不做深拷贝。"""
    first = {"role": "user", "content": "u1"}
    snap = _base([first])
    nxt = snap.with_messages(snap.messages + [{"role": "assistant", "content": "a1"}])
    assert nxt.messages[0] is snap.messages[0] is first


def test_apply_action_batch_shares_untouched_elements():
    """T-5:批次推进后,既有消息元素仍是同一对象。"""
    first = {"role": "user", "content": "u1"}
    cur = _base([first])
    nxt, invalid = apply_action_batch(
        cur, [make_action("AppendMessage", message={"role": "user", "content": "u2"})]
    )
    assert invalid == []
    assert nxt.messages[0] is first
    assert nxt.messages[1]["content"] == "u2"
    assert len(cur.messages) == 1


def test_no_deepcopy_during_advance(monkeypatch):
    """T-5:推进全程不得调用 `copy.deepcopy`(计数为 0)。"""
    calls = {"n": 0}
    real = copy.deepcopy

    def _counting(x, *a, **kw):
        calls["n"] += 1
        return real(x, *a, **kw)

    monkeypatch.setattr(copy, "deepcopy", _counting)
    cur = _base()
    for i in range(5):
        cur, _ = apply_action_batch(
            cur, [make_action("AppendMessage", message={"role": "user", "content": f"m{i}"})]
        )
    assert calls["n"] == 0
