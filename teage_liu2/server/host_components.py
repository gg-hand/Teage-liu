"""宿主组件插槽装载器(P-5 插槽契约的宿主侧参考实现,设计 §4)。

插槽白名单制:每个插槽绑定 core ABC,声明"该装配位置允许由扩展提供的
backend 接管"。装载器不知道任何扩展名,只认 config 的 backend 注册表;
isinstance 快速失败(不满足插槽 ABC = 启动失败,不静默降级)。

config 形态::

    host_components:            # 缺省省略 = 全部插槽用 core 默认实现
      - slot: storage
        backend: stdio-proxy    # 必填,∈ BACKENDS 注册表(协议 schema required;无默认值)
        options: {...}          # backend 私有选项

未来扩展:新插槽 = SLOTS 加一行;新 backend = BACKENDS 注册一行,本体不改。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..core.config import check_host_components_segment
from ..core.errors import CONFIG_INVALID_VALUE
from ..core.extension_loader import ExtensionSpec, find_host_component, resolve_command
from ..core.history import HistoryStore, SQLiteHistoryStore
from ..core.storage import MessageStore, SQLiteStorageProvider, StorageProvider

logger = logging.getLogger(__name__)

#: 插槽白名单:插槽名 → 该插槽对象必须满足的全部 core ABC
SLOTS: Dict[str, tuple] = {
    "storage": (StorageProvider,),
    "history": (HistoryStore, MessageStore),
}


def _sqlite_backend(cfg: dict, options: dict,
                    specs: Optional[Dict[str, ExtensionSpec]]) -> Dict[str, Any]:
    """core 默认 backend:SQLite 双通道(与既有装配语义一致)。"""
    storage_cfg = cfg.get("storage", {}) or {}
    path = options.get("sqlite_path") or storage_cfg.get(
        "sqlite_path", "data2/sessions.db"
    )
    return {
        "storage": SQLiteStorageProvider(path),
        "history": SQLiteHistoryStore(path),
    }


def _stdio_proxy_backend(cfg: dict, options: dict,
                         specs: Optional[Dict[str, ExtensionSpec]]) -> Dict[str, Any]:
    """stdio-proxy backend:P-4 通用代理,一个实例覆盖 storage+history 双插槽。

    backend 定位二选一(P-6):options.command 直写 / options.extension 目录发现
    （从 extensions_root manifest 取 command,相对 manifest 目录解析）;
    options.args 可选追加启动参数（如 --db 传库路径）。
    """
    from .storage_stdio_proxy import StdioStorageProxy

    command = options.get("command")
    extension = options.get("extension")
    if command is not None and extension is not None:
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: stdio-proxy 的 options.command 与 options.extension 不可同时给出"
        )
    if extension is not None:
        if specs is None:
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: options.extension 目录发现需要扩展扫描结果 specs"
                "(load_host_components(cfg, specs) 须传入 wire_extensions 产物)"
            )
        if not isinstance(extension, str) or not extension:
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: options.extension 必须是非空字符串,实际 {extension!r}"
            )
        spec = find_host_component(specs, extension, "storage")
        find_host_component(specs, extension, "history")  # stdio-proxy 双插槽都须声明
        args = options.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: options.args 必须是字符串数组,实际 {args!r}"
            )
        command = resolve_command(spec.command or [], Path(spec.path)) + list(args)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(c, str) and c for c in command)
    ):
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: stdio-proxy backend 需要 options.command(非空字符串数组)"
            f"或 options.extension(扩展名),实际 {command!r}"
        )
    timeout = options.get("request_timeout_seconds", 10.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: stdio-proxy 的 request_timeout_seconds 必须为正数,实际 {timeout!r}"
        )
    proxy = StdioStorageProxy(command=command, request_timeout=float(timeout))
    # P2-12(2026-09-11 执行后审查):启动**前**先过插槽 ABC 自检 —— 此前 start()
    # 先行、ABC 校验在 load_host_components 事后进行,校验失败时子进程已 spawn
    # 完成握手,proxy 对象被丢弃且不在 loaded 中 → 孤儿子进程无人清理。
    # 自检失败抛 TypeError = 启动失败,子进程尚未创建。
    for _slot, _abcs in SLOTS.items():
        for _abc in _abcs:
            if not isinstance(proxy, _abc):
                raise TypeError(
                    f"stdio-proxy 未满足插槽 {_slot!r} 的 {_abc.__name__}"
                )
    proxy.start()  # 握手失败抛 = 启动失败(不静默降级)
    return {"storage": proxy, "history": proxy}


#: 插槽 → backend 名 → 工厂(factory(cfg, options, specs) -> {slot 名: 对象})
BACKENDS: Dict[str, Dict[str, Callable[[dict, dict, Optional[Dict[str, ExtensionSpec]]], Dict[str, Any]]]] = {
    "storage": {
        "sqlite": _sqlite_backend,
        "stdio-proxy": _stdio_proxy_backend,
    },
}


def load_host_components(
    cfg: dict, specs: Optional[Dict[str, ExtensionSpec]] = None
) -> Dict[str, Any]:
    """按 config 装载宿主组件插槽;返回 {slot 名: 对象}。

    specs = wire_extensions 的扩展扫描产物(P-6 目录发现依赖);不传时
    stdio-proxy 仅支持 options.command 直写形态。
    任何非法配置(未知插槽/未知 backend/重复接管/ABC 不满足)抛
    ValueError/TypeError = 启动失败(可读错误,不静默降级)。
    """
    # 结构类校验(数组 / 元素为映射 / 必填 slot·backend / 条目内未知键 / 值类型)
    # = 协议 schema 唯一源(2026-09-18)。此前两处与 §config 契约冲突:
    #   ① `cfg.get("host_components") or []` 使 null / {} / 0 / "" 等 falsy 非数组
    #      被静默当成"缺省"(schema `type: array` 不允许);
    #   ② `entry.get("backend", "sqlite")` 使键名拼错时静默回落内置实现
    #      (schema `required` + spec §3"不静默降级、不回落内置实现"均不允许)。
    raw_entries = cfg.get("host_components", None)
    if raw_entries is None:
        return {}   # 缺席/None = 契约允许的缺省:全部插槽用 core 默认实现
    segment_problem = check_host_components_segment(raw_entries)
    if segment_problem:
        raise ValueError(segment_problem)
    loaded: Dict[str, Any] = {}
    for i, entry in enumerate(raw_entries):
        slot = entry["slot"]        # 必填由 schema 保证(缺失已在上一行拦截)
        if slot not in SLOTS:
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: host_components[{i}] 未知插槽 {slot!r}"
                f"(白名单: {sorted(SLOTS)})"
            )
        if slot in loaded:
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: 插槽 {slot!r} 被重复接管(host_components[{i}])"
            )
        backend = entry["backend"]  # 必填由 schema 保证;不再回落内置 sqlite
        registry = BACKENDS.get(slot, {})
        if backend not in registry:
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: 插槽 {slot!r} 未知 backend {backend!r}"
                f"(可选: {sorted(registry)})"
            )
        # options 的类型由协议 schema 保证(非映射已在段校验处拦截),此处不再重复检查
        options = entry.get("options") or {}
        objs = registry[backend](cfg, options, specs)
        if not isinstance(objs, dict) or not objs:
            raise TypeError(
                f"backend {backend!r} 必须返回非空 {{slot 名: 对象}} 映射"
            )
        for name, obj in objs.items():
            if name not in SLOTS:
                raise TypeError(f"backend {backend!r} 返回未知插槽 {name!r}")
            if name in loaded:
                raise ValueError(
                    f"插槽 {name!r} 被重复接管(host_components[{i}] backend {backend!r})"
                )
            for abc in SLOTS[name]:
                if not isinstance(obj, abc):
                    raise TypeError(
                        f"插槽 {name!r} 的实现未满足 {abc.__name__}"
                        f"(backend {backend!r},实际 {type(obj).__name__})"
                    )
            loaded[name] = obj
            logger.info("宿主组件插槽 %s 已由 backend %s 接管", name, backend)
    return loaded
