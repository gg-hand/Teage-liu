"""P-4 storage-stdio 线协议参考后端(独立实现,**不依赖 teage_liu2.core**)。

用途:
  ① 协议语言无关性的可执行参照(任意语言按 PENDING P-4 可实现);
  ② behavior-suite 真实子进程场景的被测进程(WP-A 任务 A2/A3/A5)。

线协议(与 `server/storage_stdio_proxy.py` 的 `_BackendConnection` 逐字对齐):
  请求 = 一行 JSON `{"id": <int>, "op": <str>, "p": {...}}`
  响应 = 一行 JSON `{"id": <int>, "ok": true, "r": <result>}`
                  或 `{"id": <int>, "ok": false, "e": "<message>"}`
  握手 = op=hello,p={protocol, version} → r={"protocol", "version"}(主版本须匹配宿主 0.x)

启动:python reference_storage_backend.py --db <path>
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

PROTOCOL_NAME = "storage-stdio"
PROTOCOL_VERSION = "0.1.0"

#: P-4 条款①:kind 白名单(与 core/types.is_valid_kind 同口径)
KIND_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_.")


def _now() -> str:
    return datetime.now().isoformat()


class RefBackend:
    """单进程串行应答的 P-4 后端(标准库 sqlite3)。"""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # P-4 条款⑩:WAL + busy_timeout 显式化;synchronous 保持默认 FULL(不启用 NORMAL)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._ensure_base_tables()

    def _ensure_base_tables(self) -> None:
        # 写法纪律:DDL 不声明 FOREIGN KEY(文本化 schema 检查按此判定)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                title TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                tool_name TEXT, tool_call_id TEXT, token_count INTEGER DEFAULT 0,
                is_error INTEGER DEFAULT 0, reasoning TEXT,
                content_blocks TEXT, message_type TEXT, created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # docs 通道(P-4 条款②③④)
    # ------------------------------------------------------------------
    def write(self, kind: str, docs) -> list:
        self._check_kind(kind)
        items = docs if isinstance(docs, list) else [docs]
        if not items:
            raise ValueError("批量写入 docs 不能为空列表")
        table = self._table(kind)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table}"
            " (doc_id TEXT PRIMARY KEY, doc TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        ids = [uuid.uuid4().hex for _ in items]
        with self.conn:  # 单事务:任一失败整批回滚
            for doc_id, doc in zip(ids, items):
                # 条款③:元素校验**在事务内**——含非法元素时"前几条已插入"必须一并回滚
                # (校验若前置到循环外,JSON 用例无法区分"原子"与"非原子"实现,断言不承重)
                if not isinstance(doc, dict):
                    raise ValueError(f"docs 元素必须是对象, 实际 {type(doc).__name__}")
                self.conn.execute(
                    f"INSERT INTO {table} (doc_id, doc, created_at) VALUES (?, ?, ?)",
                    (doc_id, json.dumps(doc, ensure_ascii=False), _now()),
                )
        return ids  # 条款④:恒返数组(不因单条而返回标量)

    def read(self, kind: str, doc_id: str):
        self._check_kind(kind)
        row = self.conn.execute(
            f"SELECT doc FROM {self._table(kind)} WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return json.loads(row["doc"]) if row else None

    def delete(self, kind: str, doc_id: str) -> None:
        self._check_kind(kind)
        with self.conn:
            self.conn.execute(
                f"DELETE FROM {self._table(kind)} WHERE doc_id = ?", (doc_id,)
            )
        return None

    def query(self, kind: str, limit=None, **filters):
        """条款②:filters 过滤**先于** limit;按 rowid 写入序返回。"""
        self._check_kind(kind)
        rows = [
            json.loads(r["doc"])
            for r in self.conn.execute(f"SELECT doc FROM {self._table(kind)} ORDER BY rowid ASC")
        ]
        rows = [d for d in rows if all(d.get(k) == v for k, v in filters.items())]
        return rows[:limit] if limit is not None else rows

    # ------------------------------------------------------------------
    # sessions / messages 通道
    # ------------------------------------------------------------------
    def ensure_session(self, session_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, _now(), _now()),
            )
        return None

    def _insert_message(self, item: dict) -> int:
        session_id = item.get("session_id")
        if not session_id:
            raise ValueError("消息元素缺少 session_id")
        blocks = item.get("content_blocks")
        cur =         self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, tool_call_id,"
            " token_count, is_error, reasoning, content_blocks, message_type, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, item.get("role"), item.get("content") or "",
                item.get("tool_name"), item.get("tool_call_id"),
                int(item.get("token_count") or 0), 1 if item.get("is_error") else 0,
                item.get("reasoning"),
                json.dumps(blocks, ensure_ascii=False) if blocks is not None else None,
                item.get("message_type"), _now(),
            ),
        )
        # 注意:此处**不得**再开 `with self.conn:` —— sqlite3 的内层 with 退出会
        # 提交外层未完成事务,使 log_messages 的单事务原子性失效(逐条落盘)。
        # updated_at 与外层同事务,由调用方(log_message / log_messages)包裹。
        self.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
        )
        return int(cur.lastrowid)

    def log_message(self, **p) -> int:
        """flush 档单条消息:载荷为**扁平 dict**(proxy 把 kwargs 直接作为 p 发送)。"""
        with self.conn:
            return self._insert_message(p)

    def log_messages(self, items: list) -> list:
        """P-7 批量写:payload = `{"messages": [...]}`(proxy 约 L484),元素与 log_message 同结构。

        单事务原子:任一元素非法(非对象/缺 session_id)整批回滚。
        """
        if not isinstance(items, list) or not items:
            raise ValueError("log_messages 的 messages 必须是非空数组")
        for it in items:
            if not isinstance(it, dict):
                raise ValueError(f"messages 元素必须是对象, 实际 {type(it).__name__}")
            if not it.get("session_id"):
                raise ValueError("messages 元素缺少 session_id")
        ids: list = []
        with self.conn:  # 单事务:整批原子
            for it in items:
                ids.append(self._insert_message(it))
        return ids

    def update_session_title(self, session_id: str, title: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, _now(), session_id),
            )
        return None

    def get_session_title(self, session_id: str):
        row = self.conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row["title"] if row else None

    def get_session_messages(self, session_id: str, limit=None, before_id=None):
        """按时间正序返回;`before_id` 为向上翻页游标(id < before_id 取更早)。"""
        sql = "SELECT * FROM messages WHERE session_id = ?"
        args: list = [session_id]
        if before_id is not None:
            sql += " AND id < ?"
            args.append(before_id)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        for r in rows:
            if r.get("content_blocks"):
                r["content_blocks"] = json.loads(r["content_blocks"])
            r["is_error"] = bool(r["is_error"])
        return list(reversed(rows))

    def search_messages(self, keyword: str, session_id=None, limit=None):
        """条款⑤:子串匹配即可(FTS 是 SQLite 实现细节,非契约)。"""
        sql = "SELECT * FROM messages WHERE content LIKE ?"
        args: list = [f"%{keyword}%"]
        if session_id is not None:
            sql += " AND session_id = ?"
            args.append(session_id)
        sql += " ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ------------------------------------------------------------------
    # 分派
    # ------------------------------------------------------------------
    def dispatch(self, op: str, p: dict):
        table = {
            "hello": lambda: {"protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION},
            "bye": lambda: None,
            "write": lambda: self.write(p["kind"], p["docs"]),
            "read": lambda: self.read(p["kind"], p["doc_id"]),
            "query": lambda: self.query(p["kind"], p.get("limit"), **(p.get("filters") or {})),
            "delete": lambda: self.delete(p["kind"], p["doc_id"]),
            "ensure_session": lambda: self.ensure_session(p["session_id"]),
            "log_message": lambda: self.log_message(**p),
            "log_messages": lambda: self.log_messages(p["messages"]),
            "update_session_title": lambda: self.update_session_title(p["session_id"], p["title"]),
            "get_session_title": lambda: self.get_session_title(p["session_id"]),
            "get_session_messages": lambda: self.get_session_messages(
                p["session_id"], p.get("limit"), p.get("before_id")
            ),
            "search_messages": lambda: self.search_messages(
                p["keyword"], p.get("session_id"), p.get("limit")
            ),
        }
        if op not in table:
            raise ValueError(f"未知 op: {op!r}")
        return table[op]()

    @staticmethod
    def _table(kind: str) -> str:
        return f"doc_{kind.replace('.', '_')}"

    @staticmethod
    def _check_kind(kind: str) -> None:
        """条款①:kind 白名单(宿主先行校验,此处为双重防线)。"""
        if not kind or any(c not in KIND_OK for c in kind):
            raise ValueError(f"非法 kind: {kind!r}")


def _parse_db_arg(argv: list) -> str:
    if "--db" not in argv:
        sys.stderr.write("用法: reference_storage_backend.py --db <path>\n")
        raise SystemExit(2)
    return argv[argv.index("--db") + 1]


def main() -> int:
    # 条款⑦:三流 UTF-8(Windows 默认 GBK 会让含中文的帧解码崩溃)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    backend = RefBackend(_parse_db_arg(sys.argv[1:]))
    try:
        for line in sys.stdin:  # 条款⑥:单进程串行,一请求一响应
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            try:
                resp = {"id": req.get("id"), "ok": True,
                        "r": backend.dispatch(req["op"], req.get("p") or {})}
            except Exception as e:  # noqa: BLE001 - 协议要求以 ok:false 应答,绝不崩溃
                resp = {"id": req.get("id"), "ok": False, "e": f"{type(e).__name__}: {e}"}
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            if req.get("op") == "bye":  # 条款⑧:bye 后退出
                break
    finally:
        backend.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
