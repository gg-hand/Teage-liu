"""P-4 参考存储后端单测(WP-A 任务 A1)。

参考后端 = `PROTOCOL/behavior-suite/tools/reference_storage_backend.py`,
**不 import teage_liu2.core**(保证"任意语言按 P-4 可实现"的可信度);
本文件以两种方式驱动它:
  ① 真实子进程 stdio 往返(与 runner 用例 22 同路径,验证握手/UTF-8/bye);
  ② 进程内直接调用(快速覆盖条款②③④⑤与 before_id 翻页)。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

BACKEND = (
    Path(__file__).resolve().parents[1]
    / "PROTOCOL" / "behavior-suite" / "tools" / "reference_storage_backend.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("ref_storage_backend", BACKEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hello_and_bye_roundtrip(tmp_path):
    """真实子进程:hello 握手 → 主版本匹配宿主 0.x → bye 退出码 0。"""
    db = tmp_path / "ref.db"
    p = subprocess.Popen(
        [sys.executable, str(BACKEND), "--db", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        p.stdin.write(json.dumps({"id": 1, "op": "hello",
                                  "p": {"protocol": "storage-stdio", "version": "0.1.0"}}) + "\n")
        p.stdin.flush()
        hello = json.loads(p.stdout.readline())
        assert hello["ok"] is True and hello["id"] == 1
        # 宿主握手契约:StdioStorageProxy 读 r["version"] 并比对主版本
        assert hello["r"]["version"].split(".")[0] == "0"
        p.stdin.write(json.dumps({"id": 2, "op": "bye", "p": {}}) + "\n")
        p.stdin.flush()
        assert json.loads(p.stdout.readline())["ok"] is True
        assert p.wait(timeout=5) == 0
    finally:
        if p.poll() is None:
            p.kill()


def test_frames_are_utf8_roundtrip(tmp_path):
    """条款⑦:三流 UTF-8 —— 中文消息经真实子进程写入后原样读回。"""
    db = tmp_path / "ref.db"
    p = subprocess.Popen(
        [sys.executable, str(BACKEND), "--db", str(db)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        def _call(op, payload, req_id):
            p.stdin.write(json.dumps({"id": req_id, "op": op, "p": payload},
                                     ensure_ascii=False) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        assert _call("ensure_session", {"session_id": "s-中文"}, 1)["ok"] is True
        assert _call("log_message", {"session_id": "s-中文", "role": "user",
                                     "content": "你好，海豚"}, 2)["ok"] is True
        got = _call("get_session_messages", {"session_id": "s-中文"}, 3)
        assert [m["content"] for m in got["r"]] == ["你好，海豚"]
    finally:
        p.kill()
        p.wait(timeout=5)


def test_write_batch_is_atomic_and_returns_id_array(tmp_path):
    """条款③④:批量含非法元素 → 整批回滚;合法批量 → 返回 doc_id 数组。"""
    ref = _load_module()
    backend = ref.RefBackend(str(tmp_path / "ref.db"))
    try:
        ids = backend.write("batch_atomic", [{"a": 1}, {"a": 2}])
        assert isinstance(ids, list) and len(ids) == 2
        assert all(isinstance(i, str) and i for i in ids)

        # ① 非对象元素 → 前置校验拒绝(不进入写入循环,非承重断言)
        try:
            backend.write("batch_atomic", [{"a": 3}, 123])
        except ValueError:
            pass
        else:  # pragma: no cover - 断言失败路径
            raise AssertionError("含非对象元素的批量写必须抛 ValueError")
        assert len(backend.query("batch_atomic")) == 2

        # ② 写入**中途**失败(元素不可 JSON 序列化)→ 整批回滚。
        #    这是真正承重的原子性断言:逐条 commit 的退化实现会残留 a=4。
        try:
            backend.write("batch_atomic", [{"a": 4}, {"bad": object()}])
        except Exception:  # noqa: BLE001 - TypeError(json.dumps) 等,均为预期失败
            pass
        else:  # pragma: no cover - 断言失败路径
            raise AssertionError("写入中途失败必须抛错")
        assert len(backend.query("batch_atomic")) == 2, "非原子实现会让 a=4 残留"
    finally:
        backend.conn.close()


def test_single_doc_write_still_returns_array(tmp_path):
    """条款④:write 响应恒为数组(单条也不例外;单条解包是宿主代理的职责)。"""
    ref = _load_module()
    backend = ref.RefBackend(str(tmp_path / "ref.db"))
    try:
        ids = backend.write("single_doc", {"x": 1})
        assert isinstance(ids, list) and len(ids) == 1
        assert backend.read("single_doc", ids[0]) == {"x": 1}
    finally:
        backend.conn.close()


def test_query_filters_apply_before_limit(tmp_path):
    """条款②:filters 过滤先于 limit(与 core 的 filters 后置语义相反,显式锚定)。"""
    ref = _load_module()
    backend = ref.RefBackend(str(tmp_path / "ref.db"))
    try:
        backend.write("q", [{"tag": "a"}, {"tag": "b"}, {"tag": "a"}])
        got = backend.query("q", limit=1, tag="a")
        assert len(got) == 1 and got[0]["tag"] == "a", "limit 若先于 filters 会取到 tag=b"
    finally:
        backend.conn.close()


def test_log_messages_atomic_paging_and_substring_search(tmp_path):
    """P-7 批量原子 + 条款⑤子串搜索 + before_id 向上翻页。"""
    ref = _load_module()
    backend = ref.RefBackend(str(tmp_path / "ref.db"))
    try:
        backend.ensure_session("s1")
        ids = backend.log_messages([
            {"session_id": "s1", "role": "user", "content": "第一条"},
            {"session_id": "s1", "role": "assistant", "content": "第二条"},
            {"session_id": "s1", "role": "user", "content": "第三条 海豚"},
        ])
        assert len(ids) == 3

        # ① 缺 session_id → 前置校验拒绝(非承重)
        try:
            backend.log_messages([
                {"session_id": "s1", "role": "user", "content": "第四条"},
                {"role": "user", "content": "缺 session_id"},
            ])
        except ValueError:
            pass
        else:  # pragma: no cover - 断言失败路径
            raise AssertionError("非法 messages 批次必须抛 ValueError")

        # ② 写入**中途**失败 → 整批回滚(承重断言:退化实现会残留"第五条")
        try:
            backend.log_messages([
                {"session_id": "s1", "role": "user", "content": "第五条"},
                {"session_id": "s1", "role": "user", "content": object()},
            ])
        except Exception:  # noqa: BLE001 - 绑定失败(sqlite3.InterfaceError)等
            pass
        else:  # pragma: no cover - 断言失败路径
            raise AssertionError("写入中途失败必须抛错")

        all_msgs = backend.get_session_messages("s1")
        assert [m["content"] for m in all_msgs] == ["第一条", "第二条", "第三条 海豚"]

        # before_id 游标:取 id < 最后一条 → 得到前两条(正序)
        older = backend.get_session_messages("s1", before_id=all_msgs[-1]["id"])
        assert [m["content"] for m in older] == ["第一条", "第二条"]

        hits = backend.search_messages("海豚")
        assert [m["content"] for m in hits] == ["第三条 海豚"]
    finally:
        backend.conn.close()
