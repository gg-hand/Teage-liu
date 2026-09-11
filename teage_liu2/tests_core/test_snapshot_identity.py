"""types T-3 不可变只读 / T-5 结构共享(禁 deepcopy)—— 2026-09-11 落地审查补锚定。

T-3:`Snapshot` 对外呈现为不可变:冻结属性赋值被拒,推进返回新实例,原实例不变;
**钩子边界交给扩展的是只读视图**(`readonly_view`:`messages`/`tools`/`history` → tuple、
`extra` → `MappingProxyType`),扩展原地篡改被拒(H-17 由 S2 于 2026-09-11 落地)。
T-5:推进走 O(n) 浅拷贝 + 共享元素引用,**绝不 deepcopy**。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
from types import MappingProxyType

import pytest

from teage_liu2.core.actions import make_action
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.snapshot import apply_action_batch
from teage_liu2.core.types import Snapshot, readonly_view


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


# ---------------------------------------------------------------------------
# T-3 / H-17:钩子边界的只读视图(2026-09-11 S2 落地)
# ---------------------------------------------------------------------------
class _TamperBranch(Branch):
    """尝试原地篡改收到的快照(只读视图应拒绝全部四种改写)。"""

    name = "tamper"

    def __init__(self) -> None:
        self.rejected: list[str] = []

    async def before(self, snapshot):  # noqa: ANN001 - 桩
        try:
            snapshot.messages.append({"role": "assistant", "content": "x"})
        except (AttributeError, TypeError):
            self.rejected.append("messages.append")
        try:
            snapshot.tools.append({"name": "t"})
        except (AttributeError, TypeError):
            self.rejected.append("tools.append")
        try:
            snapshot.history.append({"role": "user", "content": "h"})
        except (AttributeError, TypeError):
            self.rejected.append("history.append")
        try:
            snapshot.extra["hacked"] = True
        except (AttributeError, TypeError):
            self.rejected.append("extra.setitem")
        return []


def test_readonly_view_shares_element_references():
    """T-5:只读视图不 deepcopy,元素仍为同一引用,但容器不可变。"""
    first = {"role": "user", "content": "u1"}
    view = readonly_view(_base([first]))
    assert isinstance(view.messages, tuple)
    assert isinstance(view.tools, tuple)
    assert isinstance(view.history, tuple)
    assert isinstance(view.extra, MappingProxyType)
    assert view.messages[0] is first  # T-5:元素共享引用


def test_hook_boundary_snapshot_is_readonly():
    """T-3/H-17:钩子边界交给扩展的快照只读,原地篡改全被拒,core 状态不受影响。"""
    snap = _base()
    branch = _TamperBranch()
    chain = HookChain()
    chain.register(branch)

    out, stop_reason = asyncio.run(chain.before_all(snap))

    assert stop_reason is None
    assert branch.rejected == [
        "messages.append", "tools.append", "history.append", "extra.setitem",
    ]
    # core 自身快照不受影响,且仍是可变 list(视图不回流)
    assert out.messages == [{"role": "user", "content": "u1"}]
    assert out.extra == {}
    assert isinstance(out.messages, list)
    assert isinstance(out.extra, dict)
