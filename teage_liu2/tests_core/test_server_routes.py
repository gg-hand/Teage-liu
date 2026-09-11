"""WP-E·E1/E4:server/routes 层锚定(此前 `teage_liu2.server` 在测试中 0 命中)。

锚定内容:
- E1 生产修复回归:`/chat` 拦截取 `done.response`(非空拼接)、`/chat/stream`
  不提前关闭生成器(终态 after 钩子仍触发)、`/sessions/{id}/messages` 的
  `before_id` 游标分页、`/reload` 返回**分支名字符串**(而非 Branch 实例 → 500)。
- E4 L-11:`/chat` 在**路由层**真的持有 session 锁(同一 session 并发请求串行化)。

依赖:`fastapi` + `httpx`(TestClient)。二者已入 CI 最小依赖集(2026-09-11 批准),
故本模块在 CI 可跑;不需要真实 LLM(LLMClient 以假客户端替换)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.requests import Request

from teage_liu2.core.hooks import Branch, HookChain

from .fake_llm import FakeLLMClient


class _DummyBranch(Branch):
    """极简测试枝干(经目录发现装载,验证 /reload 的名字序列化)。"""

    name = "dummy"


_DUMMY_MANIFEST = (
    "name: dummy\n"
    "version: 0.1.0\n"
    "language: python\n"
    "entry: main.py\n"
    "capabilities: []\n"
    "description: 测试用极简枝干(仅用于 /reload 名字序列化锚定)\n"
)

_DUMMY_ENTRY = (
    "from teage_liu2.core.hooks import Branch\n"
    "\n"
    "class DummyBranch(Branch):\n"
    "    name = \"dummy\"\n"
    "\n"
    "def create_branch(config):\n"
    "    return DummyBranch()\n"
)


def _install_dummy_ext(ext_dir) -> None:
    """在临时 extensions_root 装一个最小可发现扩展(生产唯一装载通道)。"""
    pkg = ext_dir / "dummy"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(_DUMMY_MANIFEST, encoding="utf-8")
    (pkg / "main.py").write_text(_DUMMY_ENTRY, encoding="utf-8")


def _make_app(
    tmp_path,
    monkeypatch,
    yaml_branches: Optional[Dict[str, Any]] = None,
    install_dummy: bool = False,
):
    """装配最小可测 app:假 LLM + 临时库 + 空 extensions_root。

    ``yaml_branches``:写入**临时 config.yaml** 的 `core.branches`(供 /reload 读取);
    初始 `cfg` 的 branches 恒为空(避免 create_app 时经目录发现找不到枝干)。
    ``install_dummy``:在临时 extensions_root 落一个最小扩展(配合 yaml_branches)。
    """
    import teage_liu2.server.app as app_mod

    monkeypatch.setattr(
        app_mod,
        "LLMClient",
        lambda *a, **kw: FakeLLMClient(
            [{"content": [{"type": "text", "text": "回复"}], "stop_reason": "end_turn"}] * 50
        ),
    )

    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir(exist_ok=True)
    if install_dummy:
        _install_dummy_ext(ext_dir)
    llm_cfg = {
        "main_provider": "anthropic",
        "main_model": "m",
        "main_api_key": "dummy",
        "main_base_url": "http://127.0.0.1:1",
    }
    storage_cfg = {"sqlite_path": str(tmp_path / "s.db")}
    cfg = {
        "llm": llm_cfg,
        "core": {"extensions_root": str(ext_dir), "max_loops": 3},
        "storage": storage_cfg,
    }
    yaml_core: Dict[str, Any] = dict(cfg["core"])
    if yaml_branches:
        yaml_core["branches"] = yaml_branches
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {"core": yaml_core, "llm": llm_cfg, "storage": storage_cfg},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    cfg["_config_path"] = str(cfg_path)
    return app_mod.create_app(config=cfg)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with TestClient(_make_app(tmp_path, monkeypatch)) as c:
        yield c


# ---------------------------------------------------------------------------
# E1:routes 生产修复回归
# ---------------------------------------------------------------------------
def test_chat_uses_done_response_on_intercept(client, monkeypatch):
    """回归 154e6ae:拦截路径无 text_delta → /chat 必须取 done.response(而非空拼接)。"""

    async def _intercept(self, snapshot):  # noqa: ANN001 - 桩
        return snapshot, "blocked"

    monkeypatch.setattr(HookChain, "before_all", _intercept)
    r = client.post("/chat", json={"session_id": "s1", "user_input": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["termination_reason"] == "intercepted"
    assert body["response"], "拦截响应必须来自 done.response(空字符串 = 回归)"


def test_chat_stream_runs_terminal_after_hook(client):
    """回归 154e6ae:SSE 不得提前关闭生成器 → 终态 after 钩子必须仍被触发。"""
    called: Dict[str, Any] = {}

    class _Recorder(Branch):
        name = "rec_after"

        async def after(self, snapshot, response):  # noqa: ANN001 - 桩
            called["sid"] = snapshot.session_id
            return []

    client.app.state.pipeline.hooks.register(_Recorder())
    r = client.post("/chat/stream", json={"session_id": "s-stream", "user_input": "hi"})
    assert r.status_code == 200
    assert "data:" in r.text
    assert called.get("sid") == "s-stream", "done/error 后生成器被提前关闭 → after 未触发"


def test_history_pagination_before_id(client):
    """回归 00eb545:before_id 游标向上翻页只返回更早的消息。"""
    for i in range(3):
        assert client.post(
            "/chat", json={"session_id": "s-page", "user_input": f"u{i}"}
        ).status_code == 200

    r = client.get("/sessions/s-page/messages", params={"limit": 10})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) >= 3

    pivot = msgs[len(msgs) // 2]["id"]
    r2 = client.get(
        "/sessions/s-page/messages", params={"limit": 10, "before_id": pivot}
    )
    older = r2.json()["messages"]
    assert older, "before_id 应返回更早的消息"
    assert all(m["id"] < pivot for m in older)


def test_reload_returns_branch_names(tmp_path, monkeypatch):
    """回归 154e6ae:/reload 返回分支**名字符串**(Branch 实例会触发 500 序列化)。"""
    app = _make_app(
        tmp_path,
        monkeypatch,
        yaml_branches={"dummy": {"enabled": True}},
        install_dummy=True,
    )
    with TestClient(app) as c:
        r = c.post("/reload")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["reloaded"] is True
    assert data["branches"] == ["dummy"]
    assert all(isinstance(n, str) for n in data["branches"])


def test_reload_replays_resource_limits(tmp_path, monkeypatch):
    """P1-6(2026-09-11 执行后审查补锚定):/reload 成功路径必须重放 core.max_*
    —— configure_limits 仅在 ChatPipeline.__init__ 消费,不重放则运行中修改
    三键静默失效(退化:routes 删 configure_limits 调用 → 本测试红)。
    """
    app = _make_app(
        tmp_path,
        monkeypatch,
        yaml_branches={"dummy": {"enabled": True}},
        install_dummy=True,
    )
    yaml_max = {"max_snapshot_bytes": 1048576, "max_message_bytes": 4096,
                "max_messages_per_conversation": 500}
    # 把三键写进临时 config.yaml(供 /reload 读取;_make_app 固定写 tmp/config.yaml)
    with TestClient(app) as c:
        cfg_path = tmp_path / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            ycfg = yaml.safe_load(f)
        ycfg["core"].update(yaml_max)
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(ycfg, f, allow_unicode=True)
        r = c.post("/reload")
    assert r.status_code == 200, r.text
    from teage_liu2.core.snapshot import active_limits
    limits = active_limits()
    assert limits["max_snapshot_bytes"] == yaml_max["max_snapshot_bytes"]
    assert limits["max_message_bytes"] == yaml_max["max_message_bytes"]
    assert limits["max_messages_per_conversation"] == yaml_max["max_messages_per_conversation"]


def test_app_shutdown_drains_writer_before_store_close(tmp_path, monkeypatch):
    """P1-2(2026-09-11 执行后审查补锚定):lifespan shutdown 顺序 ——
    StorageWriter.close()(排空写队列)必须先于 history/storage close
    (退化:app.py 顺序对调回旧版"先关存储后排空" → 本测试红)。
    """
    from teage_liu2.core.history import SQLiteHistoryStore
    from teage_liu2.core.storage import SQLiteStorageProvider
    from teage_liu2.core.storage_writer import StorageWriter

    order: list[str] = []

    def _recorder(name):
        def _close(self, *a, **kw):
            order.append(name)
        return _close

    monkeypatch.setattr(StorageWriter, "close", _recorder("storage_writer"))
    monkeypatch.setattr(SQLiteHistoryStore, "close", _recorder("history_store"))
    monkeypatch.setattr(SQLiteStorageProvider, "close", _recorder("storage_provider"))

    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        pass  # lifespan 进入后正常退出 → 触发 shutdown
    assert order.count("storage_writer") == 1
    assert order.count("history_store") == 1
    assert order.count("storage_provider") == 1
    assert order.index("storage_writer") < order.index("history_store")
    assert order.index("storage_writer") < order.index("storage_provider")


# ---------------------------------------------------------------------------
# E4:L-11 路由层真并发(确定性:第一个请求持锁阻塞,第二个必须等)
# ---------------------------------------------------------------------------
def _req(app) -> Request:
    """构造仅用于取 app.state 的最小 Request(路由只读 request.app.state)。"""
    return Request({"type": "http", "app": app, "method": "POST", "path": "/chat"})


def test_route_level_same_session_is_serialized(tmp_path, monkeypatch):
    """L-11:同一 session 的两个并发 /chat 在**路由层**被 session 锁串行化。

    若 routes 未持锁(删掉 `async with lock`),两请求会交错 → 顺序断言变红。
    """
    app = _make_app(tmp_path, monkeypatch)
    pipeline = app.state.pipeline
    order: list[str] = []
    gate = asyncio.Event()

    async def fake_stream(session_id, user_input, system=None, cancel_event=None):
        order.append(f"enter:{user_input}")
        if user_input == "first":
            await gate.wait()  # 占住锁,直到测试放行
        yield {
            "type": "done",
            "session_id": session_id,
            "response": f"r:{user_input}",
            "termination_reason": "normal",
        }
        order.append(f"exit:{user_input}")

    monkeypatch.setattr(pipeline, "chat_stream", fake_stream)

    from teage_liu2.server.routes import chat as chat_route

    async def main():
        t1 = asyncio.create_task(
            chat_route(_req(app), {"session_id": "s", "user_input": "first"})
        )
        t2 = asyncio.create_task(
            chat_route(_req(app), {"session_id": "s", "user_input": "second"})
        )
        await asyncio.sleep(0.05)  # 两者都已尝试进入(second 应被锁挡住)
        gate.set()
        return await asyncio.gather(t1, t2)

    asyncio.run(main())
    assert order == [
        "enter:first", "exit:first", "enter:second", "exit:second",
    ], f"同 session 请求未串行化(锁未生效): {order}"
