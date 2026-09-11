# lifecycle 域规范（v1.0.0）

> 唯一契约源声明：本文档 + `lifecycle.schema.json` 是 lifecycle 域的语言无关规范。lifecycle 域覆盖扩展进程级生命周期与扩展声明模型。

## 1. 生命周期协议

**行为条款 L-1（setup 幂等可重入）**: `setup` 幂等可重入（热重载反复调用）；任一失败 → 逆序 teardown 已成功者 → 启动失败（可读错误）。
**行为条款 L-2（teardown 幂等）**: `teardown` 幂等；异常仅告警，逆序继续。
**行为条款 L-3（热重载原子替换）**: build 新链 → 全部 setup 成功 → 原子替换；失败 → 回滚保持旧链。
**行为条款 L-4（扩展进程重建时序，"接近原子"）**: ① spawn 并握手新进程 → ② 新扩展全部 setup 成功 → ③ 才 teardown + 优雅关闭旧进程（含旧扩展后台任务取消：扩展自取消 + 宿主 cancel_all 兜底）→ ④ 原子替换引用。② 失败 → 杀新进程、保留旧进程与旧链（回滚保持可用；回滚路径不取消旧链后台任务）。重建窗口期心跳/监管仍归旧链，替换完成后再移交新链。
**行为条款 L-5（进程归属粒度）**: 进程归属粒度 = **每扩展一进程**（非链级进程池）——重建链只影响被替换扩展的进程，无级联重建。
**行为条款 L-6（崩溃隔离）**: 扩展进程崩溃不影响 core 主对话（隔离语义）；崩溃自动重启属部署/外壳层运维职责（§15-B1，非 core 范围）。
**行为条款 L-7（终态钩子 action 一律忽略）**: `after` / `on_error` 返回的 Action[] 协议强制**忽略并记录**（error 级日志，记 `HOOK_TERMINAL_ACTION_IGNORED`），不区分 observe/策略扩展。终态钩子的数据写入一律经 storage 消息通道（§storage/transport）。
**行为条款 L-8（宿主能力声明与 storage 通道）**: `setup(config, host)` 的 `host` 为**纯数据声明**（宿主→扩展的能力面）：含宿主提供的协议域列表、storage 域声明（通道 = transport `storage_*` 消息）、该扩展的 kind 命名空间前缀（`{extension_name}.`）。扩展**不得**以对象引用方式访问宿主存储——宿主存储的唯一访问通道 = transport storage 消息。术语澄清: `host`（宿主能力声明，宿主→扩展）与 §extension 的 `capabilities`（扩展→宿主）方向相反。
**行为条款 L-9（扩展调 LLM 通道）**: 扩展调用 LLM 的唯一协议通道 = transport `invoke_llm` 消息——payload `{role, messages, system?, max_tokens?, ...}`；走宿主 LLMAdapter 直调（多角色路由），**协议级防重入**（绝不进入 pipeline/钩子链）；调用前提 = 扩展声明 `capabilities: [..., "llm"]`；失败 → 响应 `{error: {code, message}}`，core 只透传、扩展自降级。
**行为条款 L-10（会话态协议化）**: 快照 extra 会话内延续——构建时从 SessionStore 恢复同 session_id 的 extra 命名空间作为快照 extra 基座；对话结束（done/error 路径）时快照最终 extra 写回 SessionStore。扩展经 SetExtra 写入的数据跨同 session 的多次对话延续。
**行为条款 L-11（会话并发互斥）**: 同 session_id 的并发对话由外壳串行化（会话级互斥是外壳责任，非 core 能力）——core 快照单对话无共享；core 不检测、不防御并发，若外壳未串行化，extra 恢复/写回表现为 last-writer-wins 或丢失更新（语义未定义）。

## 2. 扩展声明模型

```
Extension = { name, lang, transport, protocol_version, hooks_implemented[], capabilities[] }
name 匹配 ^[a-z0-9_]+$（禁点, §types 前缀隔离的可判定前提）
```

- **声明式钩子**: 扩展声明实现哪些钩子，core 只调用已声明的（减少无效交互）。
- **能力声明（集合）**: `capabilities` 为集合（可组合，自由声明），v1.0 枚举：
  - `observe`: 观测只读——钩子返回的 action 被 core 忽略并记录；
  - `tool_executor`: 工具执行者——启用 transport `invoke_tool` 专用消息；
  - `llm`: 可调用 `invoke_llm` 消息；
  - `self_hosted_storage`: 自持存储——扩展自管文件/向量库/外部存储（沙箱归扩展+部署层）；
  - 组合示例: `observe + tool_executor` = 只读执行者；`observe + llm` = 观测型 LLM 消费者；
  - 缺省（空集）= 策略/工具类，可正常返回 action。
- **能力是授权面而非行为面**: core 只强制声明面（未声明 llm 却调 invoke_llm → 拒绝等，§15-A4）；细粒度 action 权限 / invoke_llm 配额 / 自持沙箱均为非 core 范围（§15-B）。
- **版本协商**: core 与扩展互验 `protocol_version`，降级兼容或拒绝（可读错误）。

## 3. 扩展 manifest 声明规范（P-1 条件④，2026-09-11 自 `docs/SUBSYSTEM-SPI.md §13.2` 迁入）

扩展目录 = 安装单位，`manifest.yaml` = **安装态唯一事实源**（本节为字段集唯一权威定义，SPI §13.2 起索引作用）。启动/热重载时严格校验：未知字段拒绝、类型/取值校验；坏 manifest = 该扩展装配失败（已在 config 声明启用的扩展上 = 启动失败）。校验实现 = `core/extension_loader.py:parse_manifest`。

### 3.1 字段集

| 字段 | 必填 | 类型 | 约束 |
|---|---|---|---|
| `name` | 是 | string | 匹配 `^[a-z0-9_]+$`（与 types 域 extension_name 同源）**且必须与目录名一致** |
| `version` | 是 | string | 非空；扩展自身版本（与宿主/协议版本无关）。**只改 entry 代码而不 bump version → 模块 hash 不变 → 热重载仍命中旧模块** |
| `language` | 是 | enum | `python`（进程内）\| `other`（必须配 transport） |
| `entry` | 条件 | string | `language: python` 必填；相对 manifest 目录、必须落在扩展目录内；`other` 不支持 |
| `transport` | 条件 | enum | `language: other` 必填且必须 `stdio`；`python` 不支持 |
| `command` | 条件 | string[] | `language: other` 必填、非空字符串列表；相对路径相对 manifest 目录解析（越出目录则不解析） |
| `protocol_version` | 否 | string | 非空；`language: other` 参与握手版本协商（evolution V-2） |
| `capabilities` | 是 | string[] | 可空；值域 = `observe` / `tool_executor` / `llm` / `self_hosted_storage`（§2）。**代码类声明的 capabilities ⊆ manifest 授权面，超出 = 启动失败**（manifest 为授权声明面唯一事实源） |
| `description` | 否 | string | 默认 `""` |
| `requirements` | 否 | string[] | 仅文档声明，宿主不自动安装 |
| `kind` | 否 | enum | `branch`（缺省）\| `host-component`（P-6） |
| `slots` | 条件 | string[] | `host-component` 必填且非空（⊆ 宿主 `SLOTS` 白名单）；`branch` 禁止声明 |

**未知字段一律拒绝**（防 typo 静默失效）。

### 3.2 kind 分支约束（P-6）

- `kind: branch`（缺省）：`language: python` → importlib 进程内装载；`language: other` → supervisor stdio 进程（transport/command 合并进 effective config）。
- `kind: host-component`：强制 `language: other` + `transport: stdio`；`slots` 必填；**`capabilities` 必须为空**（钩子授权面不适用）；不作为枝干装载、不计入"已装未启用"统计；**在 `core.branches` 声明 = 启动失败**；引用方式 = `host_components[].options.extension`（与 `options.command` 二选一，见 config 域）。

### 3.3 装载语义

- python：`importlib` 动态装载，模块名 `teage_liu2_ext_<name>_<manifest_hash>`（manifest 变更 = 全新模块对象，热重载隔离；未变 = 命中 `sys.modules` 缓存）。
- `main.py` 契约：导出 `create_branch(config) -> Branch`；只允许 import `teage_liu2.core` 的公开契约（hooks/actions/types 等），禁止 import core 实现内部模块与 `teage_liu2.server`；子模块/资源经 `__file__` 相对定位，装载器不污染 `sys.path`。
- 解析优先级：`register_factory`（测试/嵌入注入，**非生产装载方式**）> 目录装载器 > `ValueError`。

## 4. 会话级生命周期边界

会话/对话级生命周期由外壳管理，不在扩展生命周期协议范围内（lifecycle 域只覆盖扩展进程级 setup/teardown/热重载）。

## 5. 版本与演进

- 新增 capability = minor 演进；删除/改名 capability = major 演进。
- Extension 声明结构变更走协议版本化（见 evolution 域）。
