"""配置加载(搬自老系统 teage_liu/config.py,裁剪掉热更新边界等后置项)。

保留:
- ${ENV_VAR} 占位符递归解析
- 基于文件 mtime 的缓存(高频端点不重复读盘)
- 敏感字段分离(实际值写 .env,运行配置文件保留占位符)+ GET 脱敏 / PUT 还原
- 关键 API Key 启动校验(llm.main_api_key 缺失阻止启动)
- LLM 超时默认值(activity_timeout / stream_total_timeout)

F1/F2(计划 §6.7):core 段严格校验 —— core_config_from(cfg) -> CoreConfig:
类型 + 范围 + **未知键拒绝**(防 typo 静默失效,老系统踩过的坑);
失败抛 ValueError = 启动失败。

宿主段结构校验(2026-09-18):**协议 schema 运行时驱动** ——
PROTOCOL/config/config.schema.json 为唯一源(经零依赖 core/schema_check.py),
消除"schema 作文档 + 实现手写白名单"的双源漂移。语义类检查仍在外壳装载器。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml
from dotenv import load_dotenv

from .errors import (
    CONFIG_INVALID_VALUE,
    CONFIG_MISSING_KEY,
    CONFIG_UNKNOWN_KEY,
)
from .modes import MODE_BARE, MODE_LOOP
from .injection import _ALL_LAYERS
from .schema_check import validate_against_schema
from .types import RESOURCE_LIMITS

# .env 兜底加载(override=True 保证 PUT /config 写入 .env 的新 Key 重启后生效)
load_dotenv(override=True)

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_value(value: Any) -> Any:
    """递归解析配置项中的 ${ENV_VAR} 占位符。

    - 字符串整体匹配单个占位符:返回环境变量原值(缺失时为空串)
    - 部分匹配:逐个替换
    - dict/list 递归处理
    """
    if isinstance(value, str):
        full_match = _ENV_VAR_PATTERN.fullmatch(value)
        if full_match:
            env_name = full_match.group(1)
            return os.environ.get(env_name, "")
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    if isinstance(value, dict):
        return {k: _resolve_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 配置缓存:基于文件 mtime 自动失效
# ---------------------------------------------------------------------------
_config_cache: Optional[dict] = None
_config_cache_mtime: float = 0.0
_config_cache_path: Optional[str] = None


def clear_config_cache() -> None:
    """显式清除配置缓存(由 PUT /config 端点调用)。"""
    global _config_cache, _config_cache_mtime, _config_cache_path
    _config_cache = None
    _config_cache_mtime = 0.0
    _config_cache_path = None


# ---------------------------------------------------------------------------
# 敏感字段分离:PUT /config 时将实际值写入 .env,运行配置文件保留占位符
# ---------------------------------------------------------------------------
SENSITIVE_FIELDS: dict[str, str] = {
    "llm.main_api_key": "LLM_MAIN_API_KEY",
    "llm.consolidation_api_key": "LLM_CONSOLIDATION_API_KEY",
    "security.api_key": "TEAGE_API_KEY",
    "files.ocr.vision_llm.api_key": "FILES_OCR_VISION_LLM_API_KEY",
    "web_search.bing_api_key": "BING_API_KEY",
    "web_search.baidu_api_key": "BAIDU_API_KEY",
}

_MASK_SENTINEL = "****"


def _get_nested(config: dict, path: str) -> Any:
    """按点分隔路径取值。"""
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_nested(config: dict, path: str, value: Any) -> None:
    """按点分隔路径设值。"""
    keys = path.split(".")
    for key in keys[:-1]:
        if key not in config or not isinstance(config[key], dict):
            config[key] = {}
        config = config[key]
    config[keys[-1]] = value


def _update_env_file(env_path: str, updates: dict[str, str]) -> None:
    """更新 .env 文件(追加或覆盖对应行),并同步 os.environ。

    同步 os.environ 是关键:热重载时 load_config 解析占位符读取 os.environ,
    若仅写文件不更新环境变量,热重载后仍用旧值。
    """
    path = Path(env_path)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    existing_keys = {line.split("=")[0] for line in lines if "=" in line}
    for key, value in updates.items():
        if key in existing_keys:
            lines = [
                f"{key}={value}" if line.startswith(f"{key}=") else line
                for line in lines
            ]
        else:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value


def mask_api_key(value: str) -> str:
    """脱敏:仅保留首尾若干字符用于辨别是否已设置。"""
    if not value:
        return ""
    if len(value) <= 8:
        return _MASK_SENTINEL
    return value[:4] + _MASK_SENTINEL + value[-4:]


def is_masked_value(value: Any) -> bool:
    """判断是否为脱敏占位(含 **** 标记)。"""
    return isinstance(value, str) and _MASK_SENTINEL in value


def mask_sensitive_config(config: dict) -> dict:
    """对配置中所有敏感字段脱敏(原地修改并返回)。"""
    for field_path in SENSITIVE_FIELDS:
        actual = _get_nested(config, field_path)
        if actual:
            _set_nested(config, field_path, mask_api_key(str(actual)))
    return config


def unmask_sensitive_config(incoming: dict, existing: dict) -> dict:
    """将前端提交的脱敏敏感字段还原为服务端实际值(原地修改并返回)。

    现有值不存在时清空脱敏值,避免把无效的 '****' 写入 .env 污染配置。
    """
    for field_path in SENSITIVE_FIELDS:
        incoming_val = _get_nested(incoming, field_path)
        if incoming_val and is_masked_value(incoming_val):
            existing_val = _get_nested(existing, field_path)
            if existing_val:
                _set_nested(incoming, field_path, existing_val)
            else:
                _set_nested(incoming, field_path, "")
    return incoming


def write_config_with_sensitive_separation(
    new_config: dict, config_path: str, env_path: str
) -> None:
    """非敏感字段写运行配置文件,敏感字段实际值写 .env。"""
    from copy import deepcopy

    config_to_write = deepcopy(new_config)
    env_updates: dict[str, str] = {}
    for field_path, env_var in SENSITIVE_FIELDS.items():
        actual_value = _get_nested(config_to_write, field_path)
        if actual_value and not str(actual_value).startswith("${"):
            env_updates[env_var] = str(actual_value)
            _set_nested(config_to_write, field_path, f"${{{env_var}}}")
    if env_updates:
        _update_env_file(env_path, env_updates)
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config_path).write_text(
        yaml.dump(config_to_write, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 启动校验
# ---------------------------------------------------------------------------
_CRITICAL_API_KEY_PATHS = ["llm.main_api_key"]


def validate_required_env_vars(config: dict) -> None:
    """关键 API Key 缺失时抛 ValueError 阻止启动;可选缺失仅告警。"""
    import logging

    logger = logging.getLogger(__name__)
    missing = [p for p in _CRITICAL_API_KEY_PATHS if not _get_nested(config, p)]
    if missing:
        raise ValueError(
            f"{CONFIG_MISSING_KEY}: 关键 API Key 未配置(环境变量缺失):{', '.join(missing)}。\n"
            "请设置对应的环境变量或在运行配置文件中使用 ${VAR_NAME} 占位符。"
        )
    for path in ("llm.consolidation_api_key", "security.api_key"):
        if not _get_nested(config, path):
            logger.warning("API Key 未配置(可选): %s。若不使用对应功能可忽略。", path)


# ---------------------------------------------------------------------------
# LLM 超时默认值
# ---------------------------------------------------------------------------
_LLM_ACTIVITY_TIMEOUT_DEFAULT: float = 60.0
_LLM_STREAM_TOTAL_TIMEOUT_DEFAULT: float = 300.0


def get_llm_timeouts(config: dict) -> tuple[float, float]:
    """读取 llm.activity_timeout / llm.stream_total_timeout,缺失时返回默认值。"""
    llm_cfg = config.get("llm") or {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    activity_timeout = float(llm_cfg.get("activity_timeout", _LLM_ACTIVITY_TIMEOUT_DEFAULT))
    stream_total_timeout = float(
        llm_cfg.get("stream_total_timeout", _LLM_STREAM_TOTAL_TIMEOUT_DEFAULT)
    )
    return activity_timeout, stream_total_timeout


def load_config(config_path: str = "config.yaml") -> dict:
    """读取 YAML 配置文件并返回解析后的 dict(mtime 缓存)。

    默认值 `"config.yaml"` 为**历史默认**(老系统仍用它),本函数**保留不改** ——
    它只是通用读盘函数;liu2 的入口 `create_app` 已改为**显式要求**实际路径
    (参数 > `TEAGE2_CONFIG` > `config-liu2.yaml`,且缺失即启动失败,不再回落
    `config.yaml`,见 Phase 2 / 2026-09-18)。

    异常:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: 配置文件格式错误
    """
    global _config_cache, _config_cache_mtime, _config_cache_path

    config_file = Path(config_path)
    if not config_file.is_absolute():
        config_file = Path.cwd() / config_file

    try:
        current_mtime = config_file.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if (
        _config_cache is not None
        and _config_cache_path == str(config_file)
        and current_mtime > 0
        and current_mtime == _config_cache_mtime
    ):
        return _config_cache

    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_file}")

    with config_file.open("r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError(f"配置文件根节点必须是字典,实际类型: {type(raw_config).__name__}")

    resolved = _resolve_value(raw_config)
    _config_cache = resolved
    _config_cache_mtime = current_mtime
    _config_cache_path = str(config_file)
    return resolved


# ---------------------------------------------------------------------------
# 协议 schema 驱动的宿主段校验(2026-09-18)
#
# 为什么:PROTOCOL/config/config.schema.json 此前**只作文档**,运行时校验另有一套
# 手写白名单 → 双源必然漂移(历史上已双向漂移过:`extensions_root` 是"实现先、
# schema 后补";`host_components` 的 required/additionalProperties 则是
# "schema 先、实现从未落实")。现改为:**协议 schema = 宿主段结构校验的运行时唯一源**,
# 复用零依赖 core/schema_check.py(其自研理由同为"core 依赖面必须最小"),
# 与项目"机制保障优于文档纪律"的做法一致(参见 extension_loader.check_import_boundary)。
#
# 职责切分(不可混淆):
#   · 结构类(required / 未知键 / 值类型 / 范围) → 本段交 schema 校验
#   · 语义类(slot∈SLOTS、backend∈BACKENDS、重复接管、options 互斥、kind、ABC)
#     → 仍由外壳 server/host_components.py 校验(JSON Schema 无法表达)
#   · 枝干段 core.branches.<name> → **不在此列**(core 不知道枝干名;由扩展 setup 自校验,§config C-1)
# ---------------------------------------------------------------------------
_PROTOCOL_CONFIG_SCHEMA: Optional[dict] = None


def load_config_schema() -> dict:
    """加载协议配置 schema(带缓存)。

    PROTOCOL 随包发布;缺失/不可解析 = 安装损坏 → 抛 ValueError(= 启动失败),
    绝不静默跳过校验(否则会退化为"校验形同虚设")。
    """
    global _PROTOCOL_CONFIG_SCHEMA
    if _PROTOCOL_CONFIG_SCHEMA is not None:
        return _PROTOCOL_CONFIG_SCHEMA
    schema_file = (
        Path(__file__).resolve().parent.parent / "PROTOCOL" / "config" / "config.schema.json"
    )
    if not schema_file.exists():
        raise ValueError(f"{CONFIG_MISSING_KEY}: 协议配置 schema 缺失: {schema_file}")
    try:
        with schema_file.open("r", encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{CONFIG_INVALID_VALUE}: 协议配置 schema 不可解析: {e}") from e
    if not isinstance(schema, dict):
        raise ValueError(f"{CONFIG_INVALID_VALUE}: 协议配置 schema 根节点必须是对象")
    _PROTOCOL_CONFIG_SCHEMA = schema
    return schema


def _config_code_for(problem: str) -> str:
    """schema_check 的可读描述 → config 域错误码(单一映射点)。

    与 errors.spec §5 面④(异常/启动失败面,`CODE: message`)对齐:
    必填缺失 / 未知键 / 其余(类型·范围·enum)分别映射到既有三码,不新增错误码。
    """
    if "缺少必填字段" in problem:
        return CONFIG_MISSING_KEY
    if "不允许字段" in problem:
        return CONFIG_UNKNOWN_KEY
    return CONFIG_INVALID_VALUE


def check_host_components_segment(raw: Any) -> Optional[str]:
    """按协议 schema 校验 ``host_components`` **整段**;合法返回 None,非法返回带码描述。

    - 缺席 / ``None`` = 契约允许的缺省(内置 SQLite,行为与既有完全一致)。``None``
      由 schema 显式声明(``type: ["array","null"]``,YAML 空段 ``host_components:``
      的写法)—— 不是实现单方面容忍,契约与实现同源;
    - 非数组的**其它** falsy 值(``{}`` / ``0`` / ``""``)→ 失败。此前实现写作
      ``cfg.get("host_components") or []``,把它们一并静默当成缺省,与 schema
      ``type: array`` 漂移(2026-09-18 修正;这是 G4 的实质 —— 类型错误被当缺省);
    - 逐条结构校验(必须为映射、必填 ``slot``/``backend``、条目内未知键、值类型)
      由 schema 的 ``definitions.HostComponent`` 承载 —— 契约改动即生效,无需改代码。
    """
    if raw is None:
        return None
    schema = load_config_schema()
    hc_schema = (schema.get("definitions") or {}).get("HostComponent")
    if not isinstance(hc_schema, dict):
        raise ValueError(f"{CONFIG_MISSING_KEY}: 协议 schema 缺少 definitions.HostComponent")
    if not isinstance(raw, list):
        return f"{CONFIG_INVALID_VALUE}: host_components 必须是数组,实际 {type(raw).__name__}"
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return (
                f"{CONFIG_INVALID_VALUE}: host_components[{i}] 必须是映射,"
                f"实际 {type(entry).__name__}"
            )
        problem = validate_against_schema(entry, hc_schema)
        if problem:
            # schema_check 的路径根为 "$";换成本段的可读定位
            detail = problem[len("$"):] if problem.startswith("$") else f" {problem}"
            return f"{_config_code_for(problem)}: host_components[{i}]{detail}"
    return None


#: 宿主段中**不纳入** `check_declared_segments` 的段(已被更专用的校验接管):
#:   · `core` —— 手写校验承载 schema 未表达的额外语义(`injection_budget_chars` 每层
#:     100–100000 范围、错误文案的「可用:」提示),其键集由契约同步锁测试锁定
#:     (`tests_core/test_config.py::test_core_allowed_keys_matches_protocol_schema`);
#:   · `host_components` —— 外壳装载器逐条校验,以给出 `host_components[i]` 定位。
_DECLARED_SEGMENTS_EXCLUDED = frozenset({"core", "host_components"})


def _closed_segment_definitions() -> Dict[str, dict]:
    """取协议 schema 中**已闭合声明**的宿主段:段名 → 其定义(deref 后)。

    判定 = ``definitions.ConfigFile.properties[段]`` 为 ``$ref``,且被引用定义带
    ``additionalProperties: false``(即"本域已声明完整键集" —— 只有这种段才能判未知键)。
    **校验清单由 schema 推导、不写死在实现里**:契约侧新增一段(补键集 + 改 ``$ref``)
    即自动纳入生效。``_DECLARED_SEGMENTS_EXCLUDED`` 是**刻意的排除名单**(附理由),
    与"清单不写死"不矛盾 —— 它排除的段各有更专用的校验。

    注意(子集校验器的边界,已有测试锁):``core/schema_check.py`` 对**未支持的关键字
    一律宽容跳过**(``$ref`` / ``allOf`` / ``propertyNames`` …),故闭合段定义内出现这类
    关键字会**静默不校验** —— 现行两段(``LlmConfig`` / ``StorageConfig``)均为扁平键集,
    `tests_core/test_config.py::test_declared_segments_have_no_unsupported_keywords`
    在契约侧加此类关键字时即红。
    """
    schema = load_config_schema()
    definitions = schema.get("definitions") or {}
    config_file = definitions.get("ConfigFile") or {}
    properties = config_file.get("properties") or {}
    segments: Dict[str, dict] = {}
    for name, spec in properties.items():
        if name in _DECLARED_SEGMENTS_EXCLUDED or not isinstance(spec, dict):
            continue
        ref = spec.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/definitions/"):
            continue
        definition = definitions.get(ref[len("#/definitions/"):])
        if isinstance(definition, dict) and definition.get("additionalProperties") is False:
            segments[name] = definition
    return segments


def check_declared_segments(cfg: Any) -> None:
    """按协议 schema 校验全部**已闭合声明的宿主段**(当前 `llm` / `storage`)。

    G2 修复(2026-09-18 Phase 3):`llm` / `storage` 段**既非 core 段、也非扩展段**,
    C-1 原二分法未指派归属 → 实现零校验,键名 typo **静默回落**
    (`llm.main_base_urll` → 静默改用 provider 默认端点;`storage.sqlite_pth` →
    静默改用默认库)。现以已建立的架构收口(**零白名单**):键集 / 类型 / 范围一律由
    `config.schema.json` 表达,实现只负责"按 schema 校验这一段";契约改动只改 schema。

    语义类检查不在此列(`slot ∈ SLOTS`、`backend ∈ BACKENDS` 等仍在外壳装载器)。

    缺席 / ``null``(YAML 空段 ``llm:`` 的写法)= 合法缺省(schema 未声明 required,
    且两段类型含 ``null``);失败抛 ``ValueError`` = 启动失败,带 ``CONFIG_*`` 前缀
    (errors 域 §5 面④)。
    """
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: 配置根必须是映射,实际 {type(cfg).__name__}"
        )
    for name, segment_schema in _closed_segment_definitions().items():
        if name not in cfg or cfg[name] is None:
            continue
        problem = validate_against_schema(cfg[name], segment_schema)
        if not problem:
            continue
        code = _config_code_for(problem)
        # schema_check 的路径根为 "$";换成本段的可读定位(同 check_host_components_segment)
        detail = problem[len("$"):] if problem.startswith("$") else f" {problem}"
        if code == CONFIG_UNKNOWN_KEY and detail.startswith(" 不允许字段"):
            available = ", ".join(sorted(segment_schema.get("properties") or {}))
            raise ValueError(f"{code}: {name} 段{detail}(可用: {available})")
        raise ValueError(f"{code}: {name}{detail}")


def reject_unknown_keys(segment: Any, allowed: Iterable[str], segment_name: str) -> None:
    """扩展段配置的**未知键拒绝**助手（2026-09-18，修 G3）。

    背景：config 域行为条款 **C-1** 把扩展段校验委派给扩展在 `setup` 自校验，但
    **只做值校验防不住 typo** —— 键名拼错时扩展取到的是默认值，表现为**静默失效**
    （如 `core.branches.guardrails.denylist` 写成 `denylst` → `denylist` 取到 `[]`
    → 拦截**静默关停**，属安全相关后果）。本助手把"未知键拒绝"降为一行调用，
    使 C-1 的委派链真正达成其目的。

    **归属不变量**：core 只提供**工具**，`allowed` 由扩展自己声明 —— core 不参与
    判定任何枝干键集，不违反"core 不知道枝干名"铁律（依赖铁律）。

    约定（SPI §12 接入三步法）：
    - 缺席（``None``）视为合法缺省（该扩展未被运行配置声明）；
    - `allowed` = 扩展自有键 ∪ 宿主键 ``enabled``（stdio 扩展另加 ``transport``/``command``）；
    - 失败抛 ``ValueError`` = 启动失败，带 ``CONFIG_UNKNOWN_KEY`` 与「可用:」提示
      （对齐 errors 域 §5 面④）。
    """
    if segment is None:
        return
    if not isinstance(segment, dict):
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: {segment_name} 必须是映射，"
            f"实际 {type(segment).__name__}"
        )
    allowed_set = set(allowed)
    unknown = sorted(set(segment) - allowed_set)
    if unknown:
        raise ValueError(
            f"{CONFIG_UNKNOWN_KEY}: {segment_name} 包含未知配置键: {', '.join(unknown)}"
            f"（可用: {', '.join(sorted(allowed_set))}）"
        )


# ---------------------------------------------------------------------------
# F1/F2:core 段严格校验(计划 §6.7)
# ---------------------------------------------------------------------------
_CORE_ALLOWED_KEYS = {
    "mode", "max_loops", "system_prompt", "hook_timeout",
    "history_window_messages", "injection_budget_chars", "branches",
    # §15-A6 资源上限独立配置项(防 DoS;与 history_window_messages/max_loops
    # 语义不同,不得复用;默认 = 协议常量 RESOURCE_LIMITS)
    "max_snapshot_bytes", "max_message_bytes", "max_messages_per_conversation",
    # 2026-09-08 统一扩展目录树:扩展安装根(相对路径相对 cwd,同 storage 语义)
    "extensions_root",
}
_CORE_VALID_MODES = (MODE_BARE, MODE_LOOP)
_INJECTION_LAYERS = set(_ALL_LAYERS)


@dataclass
class CoreConfig:
    """core 段配置(严格校验后的强类型视图,默认值填充)。"""

    mode: str = MODE_LOOP
    max_loops: int = 50
    system_prompt: Optional[str] = None
    hook_timeout: float = 5.0
    history_window_messages: int = 100
    injection_budget_chars: Optional[Dict[str, int]] = None
    branches: Dict[str, Any] = field(default_factory=dict)
    # 2026-09-08 统一扩展目录树:扩展安装根(仓库外,默认 data2/extensions)
    extensions_root: str = "data2/extensions"
    # §15-A6 资源上限(默认 = 协议常量;不可无界)
    max_snapshot_bytes: int = RESOURCE_LIMITS["max_snapshot_bytes"]
    max_message_bytes: int = RESOURCE_LIMITS["max_message_bytes"]
    max_messages_per_conversation: int = RESOURCE_LIMITS["max_messages_per_conversation"]


def _require_int_range(value: Any, key: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{CONFIG_INVALID_VALUE}: core.{key} 必须是整数,实际 {value!r}")
    if not (lo <= value <= hi):
        raise ValueError(f"{CONFIG_INVALID_VALUE}: core.{key} 超出范围 [{lo}, {hi}]: {value}")
    return value


def _require_float_range(value: Any, key: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{CONFIG_INVALID_VALUE}: core.{key} 必须是数字,实际 {value!r}")
    value = float(value)
    if not (lo <= value <= hi):
        raise ValueError(f"{CONFIG_INVALID_VALUE}: core.{key} 超出范围 [{lo}, {hi}]: {value}")
    return value


def core_config_from(cfg: dict) -> CoreConfig:
    """core 段严格校验:未知键拒绝 + 类型 + 范围(防 typo 静默失效)。

    失败抛 ValueError = 启动失败(可读错误);core 段缺失 → 全默认值。
    """
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError(f"配置根必须是映射,实际 {type(cfg).__name__}")
    raw = cfg.get("core") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"core 段必须是映射,实际 {type(raw).__name__}")

    unknown = sorted(set(raw) - _CORE_ALLOWED_KEYS)
    if unknown:
        raise ValueError(
            f"{CONFIG_UNKNOWN_KEY}: core 段包含未知配置键: {', '.join(unknown)}"
            f"(可用: {', '.join(sorted(_CORE_ALLOWED_KEYS))})"
        )

    mode = raw.get("mode", MODE_LOOP)
    if mode not in _CORE_VALID_MODES:
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: core.mode 非法: {mode!r}"
            f"(可选: {', '.join(_CORE_VALID_MODES)})"
        )

    system_prompt = raw.get("system_prompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: core.system_prompt 必须是字符串,"
            f"实际 {system_prompt!r}"
        )

    budget = raw.get("injection_budget_chars")
    if budget is not None:
        if not isinstance(budget, dict):
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: core.injection_budget_chars 必须是映射(层名 → 字符数),"
                f"实际 {type(budget).__name__}"
            )
        unknown_layers = sorted(set(budget) - _INJECTION_LAYERS)
        if unknown_layers:
            raise ValueError(
                f"{CONFIG_INVALID_VALUE}: core.injection_budget_chars 包含未知注入层: "
                f"{', '.join(unknown_layers)}"
                f"(可用: {', '.join(sorted(_INJECTION_LAYERS))})"
            )
        for layer, chars in budget.items():
            _require_int_range(chars, f"injection_budget_chars.{layer}", 100, 100000)

    branches = raw.get("branches", {})
    if not isinstance(branches, dict):
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: core.branches 必须是映射,"
            f"实际 {type(branches).__name__}"
        )

    extensions_root = raw.get("extensions_root", "data2/extensions")
    if not isinstance(extensions_root, str) or not extensions_root.strip():
        raise ValueError(
            f"{CONFIG_INVALID_VALUE}: core.extensions_root 必须是非空字符串,"
            f"实际 {extensions_root!r}"
        )

    return CoreConfig(
        mode=mode,
        max_loops=_require_int_range(raw.get("max_loops", 50), "max_loops", 1, 200),
        system_prompt=system_prompt,
        hook_timeout=_require_float_range(raw.get("hook_timeout", 5.0), "hook_timeout", 0.1, 60.0),
        history_window_messages=_require_int_range(
            raw.get("history_window_messages", 100), "history_window_messages", 1, 10000
        ),
        injection_budget_chars=budget,
        branches=branches,
        extensions_root=extensions_root,
        # §15-A6 资源上限(默认 = 协议常量;可配置但不可无界)
        max_snapshot_bytes=_require_int_range(
            raw.get("max_snapshot_bytes", RESOURCE_LIMITS["max_snapshot_bytes"]),
            "max_snapshot_bytes", 1 * 1024 * 1024, 256 * 1024 * 1024,
        ),
        max_message_bytes=_require_int_range(
            raw.get("max_message_bytes", RESOURCE_LIMITS["max_message_bytes"]),
            "max_message_bytes", 1024, 16 * 1024 * 1024,
        ),
        max_messages_per_conversation=_require_int_range(
            raw.get("max_messages_per_conversation",
                    RESOURCE_LIMITS["max_messages_per_conversation"]),
            "max_messages_per_conversation", 100, 100000,
        ),
    )
