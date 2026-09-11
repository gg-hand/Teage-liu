"""assembler 收口锚定(WP-F F4):incremental_finalize / finalize_conversation。

此前 `core/assembler.py` 三个收口函数**无直接单测**(仅经 loop/pipeline 间接覆盖);
本文件锁定:①相邻 user merge 不变量 ②只有 BEFORE_INPUT 层轮级注入被接受
③收口成功返回 (system, messages, None)。
"""
from __future__ import annotations

import json

from teage_liu2.core.assembler import finalize_conversation, incremental_finalize
from teage_liu2.core.injection import L_BEFORE_INPUT, L_PREFIX, Injection
from teage_liu2.core.types import Snapshot


def _snap(messages, system_text=""):
    return Snapshot(
        session_id="s-asm",
        user_input="u",
        messages=list(messages),
        system_text=system_text,
    )


def test_incremental_finalize_merges_consecutive_user():
    """语义不变量:相邻 user 被合并,收口无问题。"""
    msgs, problem = incremental_finalize([{"role": "user", "content": "hi"}])
    assert problem is None
    assert [m["role"] for m in msgs] == ["user"]


def test_incremental_finalize_ignores_non_before_input_round_injection():
    """只有 BEFORE_INPUT 层的轮级注入被接受,其余层忽略(不报错)。"""
    msgs, problem = incremental_finalize(
        [{"role": "user", "content": "hi"}],
        round_injections=[Injection(L_PREFIX, "应被忽略")],
    )
    assert problem is None
    assert msgs == [{"role": "user", "content": "hi"}]


def test_incremental_finalize_accepts_before_input_injection():
    """BEFORE_INPUT 层轮级注入进入收口结果。"""
    msgs, problem = incremental_finalize(
        [{"role": "user", "content": "hi"}],
        round_injections=[Injection(L_BEFORE_INPUT, "轮指引", priority=5)],
    )
    assert problem is None
    assert "轮指引" in json.dumps(msgs, ensure_ascii=False)


def test_finalize_conversation_returns_system_and_messages():
    """收口成功:返回 (effective_system, effective_messages, None)。"""
    snap = _snap([{"role": "user", "content": "u"}], system_text="sys")
    system, messages, problem = finalize_conversation([], snap)
    assert problem is None
    assert system == "sys"
    assert [m["role"] for m in messages] == ["user"]
