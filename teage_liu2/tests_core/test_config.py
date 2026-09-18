"""配置严格校验专项测试(F1/F2,计划 §6.7)。

core_config_from:类型 + 范围 + **未知键拒绝**(防 typo 静默失效,
老系统踩过的坑);失败抛 ValueError = 启动失败。
check_declared_segments(2026-09-18 Phase 3,修 G2):宿主段 llm / storage
由协议 schema 运行时驱动校验(实现零白名单)。
"""

from __future__ import annotations

import pytest

from teage_liu2.core.config import (
    CoreConfig,
    check_declared_segments,
    core_config_from,
    validate_required_env_vars,
)
from teage_liu2.core.errors import (
    CONFIG_INVALID_VALUE,
    CONFIG_MISSING_KEY,
    CONFIG_UNKNOWN_KEY,
)


def test_defaults_when_core_missing():
    """验收:core 段缺失 → 全部默认值。"""
    cfg = core_config_from({})
    assert isinstance(cfg, CoreConfig)
    assert cfg.mode == "loop"
    assert cfg.max_loops == 50
    assert cfg.system_prompt is None
    assert cfg.hook_timeout == 5.0
    assert cfg.history_window_messages == 100
    assert cfg.injection_budget_chars is None
    assert cfg.branches == {}


def test_values_filled():
    """验收:合法值正确填充。"""
    cfg = core_config_from({"core": {
        "mode": "bare",
        "max_loops": 10,
        "system_prompt": "助手",
        "hook_timeout": 3.0,
        "history_window_messages": 200,
        "branches": {"guardrails": {"enabled": True}},
    }})
    assert cfg.mode == "bare"
    assert cfg.max_loops == 10
    assert cfg.system_prompt == "助手"
    assert cfg.hook_timeout == 3.0
    assert cfg.history_window_messages == 200
    assert cfg.branches == {"guardrails": {"enabled": True}}


def test_unknown_key_rejected():
    """验收:未知键拒绝(可读错误列未知键)—— 防 typo 静默失效。"""
    with pytest.raises(ValueError, match="modex"):
        core_config_from({"core": {"modex": "loop"}})


def test_unknown_mode_rejected():
    """验收:mode 未知值拒绝(与 E6 形态校验一致)。"""
    with pytest.raises(ValueError, match="mode"):
        core_config_from({"core": {"mode": "loopx"}})


def test_type_rejected():
    """验收:类型错误拒绝。"""
    with pytest.raises(ValueError, match="max_loops"):
        core_config_from({"core": {"max_loops": "五十"}})
    with pytest.raises(ValueError, match="hook_timeout"):
        core_config_from({"core": {"hook_timeout": "5s"}})
    with pytest.raises(ValueError, match="system_prompt"):
        core_config_from({"core": {"system_prompt": 123}})


def test_range_rejected():
    """验收:范围错误拒绝。"""
    with pytest.raises(ValueError, match="max_loops"):
        core_config_from({"core": {"max_loops": 0}})
    with pytest.raises(ValueError, match="history_window_messages"):
        core_config_from({"core": {"history_window_messages": -1}})
    with pytest.raises(ValueError, match="hook_timeout"):
        core_config_from({"core": {"hook_timeout": 0}})


def test_injection_budget_validation():
    """验收:分层预算 —— 合法层名通过,未知层名拒绝。"""
    cfg = core_config_from({"core": {"injection_budget_chars": {"PREFIX": 3000}}})
    assert cfg.injection_budget_chars == {"PREFIX": 3000}
    with pytest.raises(ValueError, match="WEIRD"):
        core_config_from({"core": {"injection_budget_chars": {"WEIRD": 100}}})


def test_non_dict_cfg_rejected():
    """验收(P3-3):配置根非映射 → ValueError(而非 AttributeError 崩溃)。"""
    with pytest.raises(ValueError, match="配置根"):
        core_config_from(["not", "dict"])
    assert core_config_from(None).mode == "loop"  # None 兼容默认值


def test_branches_must_be_dict():
    """验收:branches 段类型校验。"""
    with pytest.raises(ValueError, match="branches"):
        core_config_from({"core": {"branches": ["guardrails"]}})

def test_extensions_root_default_and_validation():
    """验收(2026-09-08 扩展目录树):extensions_root 默认值 + 类型校验。"""
    assert core_config_from({}).extensions_root == "data2/extensions"
    assert core_config_from(
        {"core": {"extensions_root": "d:/my_extensions"}}
    ).extensions_root == "d:/my_extensions"
    with pytest.raises(ValueError, match="extensions_root"):
        core_config_from({"core": {"extensions_root": ""}})
    with pytest.raises(ValueError, match="extensions_root"):
        core_config_from({"core": {"extensions_root": 123}})


def test_missing_api_key_raises_config_missing_key():
    """P-8② 零锚定码补齐:关键 API Key 缺失 → CONFIG_MISSING_KEY(启动失败)。"""
    with pytest.raises(ValueError, match=CONFIG_MISSING_KEY):
        validate_required_env_vars({"llm": {}})


def test_core_allowed_keys_matches_protocol_schema():
    """**契约同步锁**(2026-09-18):core 段白名单与协议 schema 不得漂移。

    core 段校验仍为手写实现 —— 它承载 schema **未表达**的额外语义
    (如 `injection_budget_chars` 每层数值范围 100–100000、错误文案的「可用:」提示),
    强行迁到 schema 驱动会**弱化**校验,故不迁。为消除"实现/schema 双源静默漂移"
    (历史上已发生:`extensions_root` 曾是"实现先、schema 后补"),改以**机制**锁定:
    白名单必须与 `config.schema.json#/definitions/CoreConfig.properties` 键集一致。
    """
    from teage_liu2.core.config import _CORE_ALLOWED_KEYS, load_config_schema

    core_schema = (load_config_schema().get("definitions") or {}).get("CoreConfig") or {}
    schema_keys = set((core_schema.get("properties") or {}).keys())
    assert schema_keys, "协议 schema 的 CoreConfig.properties 不应为空"
    assert _CORE_ALLOWED_KEYS == schema_keys, (
        f"core 段白名单与协议 schema 漂移 —— "
        f"仅实现有: {sorted(_CORE_ALLOWED_KEYS - schema_keys)}; "
        f"仅 schema 有: {sorted(schema_keys - _CORE_ALLOWED_KEYS)}"
    )
    assert core_schema.get("additionalProperties") is False, (
        "协议 schema 的 CoreConfig 必须 additionalProperties:false"
        "(这是 core 段「未知键拒绝」的契约依据)"
    )


def test_reject_unknown_keys_helper():
    """G3 修复锚定(2026-09-18):扩展段「未知键拒绝」助手。

    core 只提供**工具**、`allowed` 由扩展自己声明 —— 不违反"core 不知道枝干名"铁律。
    """
    from teage_liu2.core.config import reject_unknown_keys

    allowed = {"enabled", "denylist"}

    # 缺席(None)= 合法缺省;合法键 = 通过
    reject_unknown_keys(None, allowed, "guardrails")
    reject_unknown_keys({"enabled": True, "denylist": []}, allowed, "guardrails")

    # 键名拼错 → CONFIG_UNKNOWN_KEY,且消息含未知键与可用键(G3 的核心场景)
    with pytest.raises(ValueError, match=CONFIG_UNKNOWN_KEY) as ei:
        reject_unknown_keys({"enabled": True, "denylst": []}, allowed, "guardrails")
    assert "denylst" in str(ei.value)
    assert "denylist" in str(ei.value)

    # 非映射 → CONFIG_INVALID_VALUE
    with pytest.raises(ValueError, match="CONFIG_INVALID_VALUE"):
        reject_unknown_keys(["x"], allowed, "guardrails")


# ---------------------------------------------------------------------------
# 宿主段(llm / storage)严格校验:2026-09-18 Phase 3,修 G2
# ---------------------------------------------------------------------------
_VALID_LLM = {
    "main_provider": "deepseek",
    "main_model": "deepseek-v4-flash",
    "main_api_key": "${LLM_MAIN_API_KEY}",
    "main_base_url": "https://api.deepseek.com",
    "max_context_tokens": 200000,
    "activity_timeout": 60,
    "stream_total_timeout": 300,
}


def test_host_segment_unknown_key_rejected():
    """G2 修复锚定:宿主段未知键拒绝 + 「可用:」提示。

    此前 C-1 二分法(core 段 / 扩展段)未给 `llm`/`storage` 指派归属 → 实现零校验,
    键名 typo **静默回落**:`llm.main_base_urll` 拼错 → 静默改用 provider 默认端点。
    """
    with pytest.raises(ValueError, match=CONFIG_UNKNOWN_KEY) as ei:
        check_declared_segments({"llm": {"main_base_urll": "https://x"}})
    assert "main_base_urll" in str(ei.value)
    assert "main_base_url" in str(ei.value)  # 「可用:」提示(就地纠正 typo)

    with pytest.raises(ValueError, match=CONFIG_UNKNOWN_KEY) as ei2:
        check_declared_segments({"storage": {"sqlite_pth": "x.db"}})
    assert "sqlite_path" in str(ei2.value)


def test_old_system_only_keys_rejected():
    """老系统专属键 = 未知键 → 启动失败("这份配置不属于 liu2")。

    `llm.context_threshold` / `storage.session_ttl_days` / `storage.cleanup_interval_hours`
    在 liu2 内零消费(Phase 1 键级复核)。Phase 2 已取消对老系统 `config.yaml` 的
    回落,故默认路径不会读到(仅 `TEAGE2_CONFIG` 显式指向老系统文件时才触发)。
    """
    for cfg in (
        {"llm": {"context_threshold": 0.8}},
        {"storage": {"session_ttl_days": 7}},
        {"storage": {"cleanup_interval_hours": 6}},
    ):
        with pytest.raises(ValueError, match=CONFIG_UNKNOWN_KEY):
            check_declared_segments(cfg)


def test_host_segment_type_and_range_rejected():
    """类型收紧:数字字符串不再被 int()/float() 宽容吞下。

    `max_context_tokens`(`int()`)/`activity_timeout`(`float()`)此前容忍
    `"200000"`/`"60"`;schema 声明 `integer`/`number` 后 = 启动失败(实现对齐契约)。
    """
    for cfg in (
        {"llm": {"max_context_tokens": "200000"}},
        {"llm": {"activity_timeout": "60"}},
        {"llm": {"stream_total_timeout": "300"}},
        {"llm": {"main_provider": ""}},      # minLength 1
        {"llm": "deepseek"},                 # 非映射
        {"storage": ["x"]},
    ):
        with pytest.raises(ValueError, match=CONFIG_INVALID_VALUE):
            check_declared_segments(cfg)
    with pytest.raises(ValueError, match=CONFIG_INVALID_VALUE):
        check_declared_segments({"llm": {"activity_timeout": 0}})  # minimum 0.1


def test_host_segment_absent_or_empty_is_default():
    """缺席 / YAML 空段(`llm:` → null)/ 空映射 = 合法缺省。"""
    for cfg in (
        {},
        {"llm": None},
        {"storage": None},
        {"storage": {}},
        {"core": {"mode": "bare"}},
        {"llm": _VALID_LLM, "storage": {"sqlite_path": "data2/sessions.db"}},
    ):
        check_declared_segments(cfg)


def test_host_segment_non_mapping_root_and_none():
    """配置根非映射 → CONFIG_INVALID_VALUE;None 兼容缺省。"""
    with pytest.raises(ValueError, match=CONFIG_INVALID_VALUE):
        check_declared_segments(["not", "dict"])
    check_declared_segments(None)


def test_declared_segments_are_schema_driven():
    """**零白名单机制锁**(2026-09-18):闭合宿主段清单由协议 schema 推导。

    实现内没有任何键名清单 —— 段 = `definitions.ConfigFile.properties` 中
    `$ref` 指向 `additionalProperties: false` 定义者(`core` / `host_components`
    已由更专用校验接管,显式排除)。故契约增删键只改 schema 即生效。
    """
    from teage_liu2.core.config import _closed_segment_definitions, load_config_schema

    definitions = load_config_schema()["definitions"]
    segments = _closed_segment_definitions()
    assert set(segments) == {"llm", "storage"}, (
        f"闭合宿主段清单变化(新增段须同步本断言与套件用例): {sorted(segments)}"
    )
    assert segments["llm"] == definitions["LlmConfig"]
    assert segments["storage"] == definitions["StorageConfig"]
    assert all(d.get("additionalProperties") is False for d in segments.values())
    # core / host_components 不重复校验(前者有契约同步锁,后者由外壳逐条定位)
    assert "core" not in segments and "host_components" not in segments


#: 允许出现在闭合段定义里的关键字 = `core/schema_check.py` **支持**的校验关键字
#: ∪ **纯注记**关键字(注记不参与校验、跳过无害,与"静默丢失校验"是两回事)。
#: 未列者一律视为"看着校验了其实没校验"的隐患 —— 见下方测试。
_SUPPORTED_SCHEMA_KEYWORDS = {
    # 支持的校验关键字(schema_check 模块 docstring 的清单)
    "type", "required", "properties", "items", "enum", "const",
    "additionalProperties", "minimum", "maximum",
    "minLength", "maxLength", "minItems", "maxItems",
    # 纯注记(无校验语义)
    "description", "title", "$comment", "default", "examples",
    "deprecated", "readOnly", "writeOnly",
}


def test_declared_segments_have_no_unsupported_keywords():
    """**机制锁**(2026-09-18 审查补):闭合段定义内不得出现子集校验器**不支持的关键字**。

    ``core/schema_check.py`` 对未知关键字一律宽容跳过 → 契约侧给闭合段加一层
    ``$ref`` / ``propertyNames`` 等会变成"看着校验了、其实没校验"(静默假绿,
    且实现侧无从察觉)。本测试把该缺口挡在门禁上 —— 比 docstring 提醒硬。
    """
    from teage_liu2.core.config import _closed_segment_definitions

    def _walk(node: object, path: str, in_properties: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if in_properties:
                    # `properties` 的键是**属性名**(任意标识符),不是 schema 关键字
                    _walk(value, f"{path}.{key}")
                    continue
                assert key in _SUPPORTED_SCHEMA_KEYWORDS or key.startswith("x-"), (
                    f"闭合宿主段定义用了子集校验器不支持的关键字: {path}.{key} "
                    f"—— 该层会**静默不校验**;要么在 core/schema_check.py 实现它,"
                    f"要么改用受支持写法"
                )
                _walk(value, f"{path}.{key}", in_properties=(key == "properties"))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]", in_properties)

    for name, definition in _closed_segment_definitions().items():
        _walk(definition, name)


def test_null_defaults_are_declared_in_schema():
    """**契约/实现同源锁**(2026-09-18 审查补):`None` 缺省的容忍度必须写在 schema 里。

    实现侧对空段(`llm:` / `storage:` / `host_components:` / `options:`)的容忍
    (`check_declared_segments` 跳过 `None`、`check_host_components_segment` 早退)
    **不是单方面宽容** —— 对应定义必须声明 `null`。否则就是"实现容忍、契约未声明"
    的漂移(G4 的同类):行为上无人可查、契约上无从得知。
    """
    from teage_liu2.core.config import load_config_schema

    defs = load_config_schema()["definitions"]

    def _types(spec) -> set:
        t = spec.get("type")
        return set(t) if isinstance(t, list) else {t}

    assert "null" in _types(defs["ConfigFile"]["properties"]["host_components"])
    assert "null" in _types(defs["LlmConfig"])
    assert "null" in _types(defs["StorageConfig"])
    assert "null" in _types(defs["HostComponent"]["properties"]["options"])


def test_config_file_segments_are_all_accounted_for():
    """**段账目机制锁**(2026-09-18 审查补):`ConfigFile.properties` 的每段都必须被显式归类。

    三类归属(config 域 C-1)在实现里各有一条路径:专用手写校验(`core`)、条目级
    schema 校验(`host_components`)、schema 驱动的闭合段(由 `_closed_segment_definitions`
    推导)、以及 **liu2 不消费的段**。新增段若未归类,本测试即红 —— 否则新段会
    **静默零校验**地进契约(正是 G2 的成因)。
    """
    from teage_liu2.core.config import (
        _DECLARED_SEGMENTS_EXCLUDED,
        _closed_segment_definitions,
        load_config_schema,
    )

    properties = set(load_config_schema()["definitions"]["ConfigFile"]["properties"])
    closed = set(_closed_segment_definitions())
    # liu2 不消费、且 schema 已注明用途的段(2026-09-18 声明);
    # 新增此类段须在此登记 —— 这是本测试的"账目"意义。
    not_consumed_by_liu2 = {"server"}
    assert properties == closed | set(_DECLARED_SEGMENTS_EXCLUDED) | not_consumed_by_liu2, (
        f"配置段未被显式归类 —— 仅 schema 有: "
        f"{sorted(properties - closed - set(_DECLARED_SEGMENTS_EXCLUDED) - not_consumed_by_liu2)}; "
        f"仅实现有: {sorted(closed | set(_DECLARED_SEGMENTS_EXCLUDED) - properties)}"
    )
