# config 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `config.schema.json` 是 config 域的语言无关规范。

## 1. 配置格式

- 配置文件 YAML（语言无关）；`${ENV_VAR}` 占位注入，敏感字段分离（.env 不入库）；
- 配置 schema（JSON Schema）: core 段 + 各扩展段；严格校验——未知键拒绝、类型/范围校验，失败 = 启动失败（可读错误列未知键）。**严格校验限 core 段**（根级 `additionalProperties: true`：扩展段键名由扩展声明面决定，根级放行，2026-09-11 注明）。

**行为条款 C-1（配置段归属）**: core 段由 core 校验，扩展段由扩展在 setup 自校验（收到自己的配置段）。
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
- **失败语义**：上述任一非法配置一律 = **启动失败（可读错误）**，不做静默降级、不回落内置实现。

## 4. 版本与演进

- 新增 core 配置键 = minor 演进；删除/改名配置键 = major 演进。
- 扩展配置段由扩展自管 schema（不在 core 域 schema 内）。
