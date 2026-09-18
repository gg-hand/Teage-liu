# config 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `config.schema.json` 是 config 域的语言无关规范。

## 1. 配置格式

- 配置文件 YAML（语言无关）；`${ENV_VAR}` 占位注入，敏感字段分离（.env 不入库）；
- 配置 schema（JSON Schema）；严格校验——未知键拒绝、类型/范围校验，失败 = 启动失败（可读错误列未知键）。**严格面 = core 段 + 宿主段（`llm`·`storage`·`host_components`）**；根级 `additionalProperties: true` **仅对未声明段**（老系统段与未来扩展段）放行（2026-09-11 注明"扩展段键名由扩展声明面决定"；2026-09-18 修 G2：原表述"严格校验限 core 段"把 `llm`/`storage` 悬空成无归属段 → 零校验、typo 静默回落，现改为三段分类，见 C-1）。
- **schema 的运行时地位（2026-09-18 起）**：本域 `config.schema.json` **不再只是文档** —— 宿主段的结构校验由 `core/config.py` 在启动期**直接消费本 schema** 执行（经零依赖 `core/schema_check.py`），实现侧不再手写键集白名单：`host_components` 条目级（`check_host_components_segment`，逐条给 `host_components[i]` 定位）+ `llm`/`storage` 段级（`check_declared_segments`，数据驱动 —— 凡 `definitions.ConfigFile.properties` 中以 `$ref` 指向"带 `additionalProperties: false` 的定义"的段自动纳入，**校验清单不写死在实现里**；`core` / `host_components` 因已有更专用的校验而**显式排除**，排除名单是刻意的实现常量并附理由）。理由：此前"schema 作文档 + 实现手写白名单"是**双源**，必然漂移（历史上已双向漂移：`extensions_root` 属"实现先、schema 后补"；`HostComponent` 的 `required`/`additionalProperties` 属"schema 先、实现从未落实"）。契约改动自此**只改 schema 即生效**。`core` 段例外：它承载 schema 未表达的额外语义（`injection_budget_chars` 每层 100–100000 范围、「可用:」提示），仍为手写校验，其键集由测试锁与 `CoreConfig.properties` 对齐（`tests_core/test_config.py::test_core_allowed_keys_matches_protocol_schema`）。

**行为条款 C-1（配置段归属）**: ① **core 段**由 core 校验；② **宿主段**（`llm`·`storage`·`host_components`）由 core 与外壳**按本域 schema** 校验 —— **键集由本域 schema 声明（`LlmConfig`/`StorageConfig`/`HostComponent`），非扩展可决定**；③ **扩展段**由扩展在 setup 自校验（**构造时**收到自己的配置段，键集由扩展声明面决定）。
> **C-1 完整性要求（2026-09-18，修 G3）**：委派不等于达标 —— "值校验"防不住 **typo**：键名拼错时扩展取到的是默认值，表现为**静默失效**（如 `core.branches.guardrails.denylist` 写成 `denylst` → `denylist` 取到 `[]` → 拦截**静默关停**）。故扩展的 setup 自校验**必须包含未知键拒绝**；为此 core 提供零依赖助手 `core.config.reject_unknown_keys(segment, allowed, segment_name)`（失败 = 启动失败，带 `CONFIG_UNKNOWN_KEY` + 「可用:」提示）。**归属不变量**：`allowed` 由扩展自己声明，core 不参与判定枝干键集（依赖铁律：core 不知道枝干名）。接入要求见 SPI §12 / CORE §8 接入三步法。
**行为条款 C-2（敏感字段）**: `${VAR}` 占位注入 + `.env` 分离 + 脱敏函数是既有配置功能资产（回指 §15-A8：core 敏感信息不泄漏——配置功能与安全边界区分，不重复声明）。

## 2. core 段结构

```yaml
core:
  mode: loop                    # bare / loop(未知值启动失败)
  max_loops: 50                 # 1-200
  system_prompt: ...            # 字符串(默认内置)
  hook_timeout: 5.0             # 0.1-60 秒
  history_window_messages: 100  # 1-10000
  injection_budget_chars:       # 分层预算(层名白名单)
    PREFIX: 8000
  max_snapshot_bytes: 2097152   # 2 MiB(1MiB-256MiB,§types T-8 资源上限)
  max_message_bytes: 524288     # 512 KiB(1KiB-16MiB)
  max_messages_per_conversation: 2000   # 单次对话消息条数上限(§types T-8)
  extensions_root: data2/extensions     # 扩展安装根(非空字符串,目录发现)
  branches:                     # 扩展声明, 顺序 = 注册顺序
    guardrails: { enabled: true }
```

> **补录说明（2026-09-10）**：`max_snapshot_bytes` / `max_message_bytes` / `max_messages_per_conversation`（§types T-8 资源上限）与 `extensions_root`（2026-09-08 统一扩展目录树）此前实现已支持但本表遗漏，现补录。按 §3 演进规则，新增 core 配置键属 minor 演进面：`extensions_root` 与 manifest 规范一并登记于 PENDING **P-1**（涉及域含 config）；三个资源上限键已随 PENDING **P-1** 一并登记评审（2026-09-11；其语义由 `types.spec.md` §7 的 T-8 条款承载，配置→生效链路由 `core/snapshot.py::configure_limits` 承载）。

## 3. 宿主组件插槽（host_components，P-5 条件④，2026-09-11 归档）

宿主组件（host-component）是**接管宿主插槽**的异语言 backend（如 Rust 存储后端），与枝干扩展共用统一扩展目录树与 manifest（见 lifecycle 域 §3.2），不经钩子链。

```yaml
host_components:                       # 可选;缺省省略 = 内置 SQLite 行为完全不变
  - slot: storage                      # 必填,∈ SLOTS 白名单
    backend: stdio-proxy               # 必填,∈ BACKENDS 注册表
    options:                           # 可选,backend 自解释
      extension: storage_rust          # 引用 extensions_root 下 manifest(取其 command)
      args: [--db, data2/storage_rust/sessions.db]   # 追加启动参数
```

- **结构**：`[{slot, backend, options}]` 数组（确定性 = 启动期静态装配，无运行期热插拔）。
- **SLOTS 白名单**：`host_components.SLOTS`（当前 `storage` / `history`）——**未知插槽 = 启动失败**；同一 `slot` 重复接管 = 启动失败。
- **BACKENDS 注册表**：`backend` 名必须已注册（未注册 = 启动失败）。
- **工厂签名**：`factory(cfg, options, specs) -> {slot: obj}`；返回映射必须覆盖被请求的插槽，缺项 = 启动失败。
- **同实例覆盖多插槽**：一个 backend 实例可同时接管 `storage` + `history`（P-5 语义，如 stdio-proxy 单实例双插槽）；宿主按返回映射装配。
- **与扩展目录的关系**：`options.extension`（引用 extensions_root 下扩展的 manifest，取其 command；相对路径相对 manifest 目录解析）与 `options.command`（直接命令）**二选一，同时给出 = 启动失败**；被引用扩展必须是 `kind: host-component`（引用 `kind: branch` = 启动失败）。
- **条目级结构校验**（`required: [slot, backend]` + 闭合键集 + 值类型）**由外壳装载器 `server/host_components.py` 按本域 schema 执行**（2026-09-18 修正）：此前实现以 `entry.get("backend", "sqlite")` 使 `backend` 键缺失/拼错时**静默回落内置实现**，直接违反本节「必填」「未注册 = 启动失败」「不做静默降级、不回落内置实现」三处明文 —— 现改为**启动失败**并带 `CONFIG_MISSING_KEY`/`CONFIG_UNKNOWN_KEY`/`CONFIG_INVALID_VALUE` 前缀（见 errors 域 §5 面④）。
- **结构类与语义类的职责切分**：结构类（必填/未知键/类型/范围）由 schema 承载；语义类（`slot ∈ SLOTS`、`backend ∈ BACKENDS`、重复接管、`options` 互斥、扩展 `kind` 校验、插槽 ABC 满足）无法用 JSON Schema 表达，仍由装载器实现，文案由行为套件 `host-component-reject-24` 的 `expect_regex` 锚定。
- **空段写法（2026-09-18 声明）**：整段 `host_components:`（YAML null）与条目内 `options:`（null）**与省略同义**，均属**显式缺省**（整段 = 全部插槽用 core 默认实现；条目 = 该 backend 无额外选项），已在 `config.schema.json` 声明 `["array","null"]` / `["object","null"]`（此前是"实现容忍 `None`、schema 只写 `array`/`object`"的**漂移**，现契约与实现同源）；而**其它 falsy 非数组/非映射值**（`{}` / `0` / `""`）仍 = 启动失败 —— 那些是**类型错误**，不是缺省。
- **失败语义**：上述任一非法配置一律 = **启动失败（可读错误）**，不做静默降级、不回落内置实现。

## 4. 版本与演进

- **配置段的三类归属**（= C-1，2026-09-18 定）: ① core 段（core 手写校验）② **宿主段** `llm`·`storage`·`host_components`（键集由本域 `config.schema.json` 声明，**非扩展可决定**；由 core 与外壳校验）③ 扩展段（扩展自管 schema，不在 core 域 schema 内，由扩展 setup 自校验）。
- 新增 core 配置键 = minor 演进；删除/改名配置键 = major 演进。
- **宿主段配置键的演进级别**（2026-09-18 新定，此前为真空）：与 core 键**同规则** —— 新增键 = minor、删除/改名 = major。
- **校验语义收紧**（未增删/改名任何键，仅"原先静默通过 → 现在拒绝"）：**不升 VERSION**（冻结面未动），但须在 `PENDING.md` 登记（先例：G2 宿主段 `llm`/`storage` 校验、G3 扩展段未知键拒绝）；属"实现对齐契约"而非新能力。
- 扩展配置段由扩展自管 schema（不在 core 域 schema 内）。
