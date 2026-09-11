# PROTOCOL — teage_liu2 协议族（唯一契约源）

> **协议版本**: v1.0.0（semver；稳定面冻结，见 `VERSION`）
> **权威规格**: `docs/plans/2026-08-20-终极解耦架构-设计.md`（v1.15）
> **定位**: 本目录是 core 与扩展之间一切交互的**唯一契约源**——语言无关，任何宿主语言（Python/Rust/…）按此实现。
> `teage_liu2/docs/CORE.md` 与 `docs/SUBSYSTEM-SPI.md` 为实现视图，阶段 2 起逐步对齐本目录。

## 9 协议域

| 域 | 内容 | 关键文件 |
|---|---|---|
| `types/` | 值对象定义（Message/ContentBlock/ToolSchema/Usage/Injection/Snapshot）+ 命名约束 | `types.spec.md` / `types.schema.json` |
| `events/` | 事件流（8+ 事件 / done 9 键 / L1-L2-L3 三层） | `events.spec.md` / `events.schema.json` |
| `hooks/` | 11 钩子 + 6 Action + 应用时序（收口四连 / 覆盖规则 / SetStop 短路） | `hooks.spec.md` / `hooks.schema.json` |
| `lifecycle/` | 生命周期（setup/teardown/热重载/进程重建）+ 扩展声明 | `lifecycle.spec.md` / `lifecycle.schema.json` |
| `storage/` | 存储 SPI + 三层能力模型 + kind 前缀隔离 | `storage.spec.md` / `storage.schema.json` |
| `config/` | 配置协议（YAML + ${VAR} + schema 校验） | `config.spec.md` / `config.schema.json` |
| `transport/` | 传输协议（双绑定形态 + 消息全集 + 帧编码） | `transport.spec.md` / `transport.schema.json` |
| `errors/` | 错误协议（终止原因 + 错误码全集 + 责任矩阵） | `errors.spec.md` / `errors.schema.json` |
| `evolution/` | 演进机制（稳定面冻结 vs 演进面开放 + 三阶演进） | `evolution.spec.md` / `evolution.schema.json` |

## 演进追踪

候选/实验性协议元素（未入冻结面）统一记录在 [`PENDING.md`](./PENDING.md)：新扩展协议**先写 PENDING.md**，正式使用且迭代稳定后才更新进本目录契约（spec + schema + 行为套件用例 + VERSION），并在 PENDING.md"已并入记录"区登记。

## 行为套件（黄金用例集）

`behavior-suite/` 是协议级语言无关黄金用例集（JSON），每个宿主实现运行同一套用例并报告通过率——v1.0 冻结前提 + 新宿主验收门槛 + 协议-实现漂移审计工具。

**交付物边界（2026-09-10 明确）**：语言无关交付物 = `cases/*.json` + `suite.schema.json` + `matcher.schema.json`；`runner.py` 是**本实现的 Python 参考执行器**，允许依赖宿主实现（`teage_liu2/core/`）与其测试基础设施（`teage_liu2/tests_core/fake_llm.py`）——该依赖经用户 2026-09-10 授权（测试套件允许入库）显式化，非隐性耦合。

**断言能力纪律（2026-09-10 起）**：runner 对**未实现的断言键一律显式失败**（`_check_supported`），禁止静默忽略——此前 `final.persisted` / `invocations[].isolation` 曾被静默跳过，形成"假绿"。

**依赖边界（2026-09-11 补充）**：runner 依赖 `teage_liu2/core/` 与 `teage_liu2/tests_core/fake_llm.py`；WP-A（M2）起**额外允许**依赖 `teage_liu2/server/` —— 用例 22（`stdio-proxy-roundtrip`）已引用 `server.storage_stdio_proxy.StdioStorageProxy`，用例 23/24（`host-component`）已引用 `server.host_components.load_host_components`。该依赖的定位始终是**本实现的 Python 参考执行器**（语言无关交付物 = `cases/*.json` + `suite.schema.json`，保持零依赖）。

**场景能力补充（2026-09-11 落地审查）**：新增用例 `host-component-disabled-40`（未声明不启用＝已装未启用统计）、`host-component-default-41`（缺省省略 `host_components`＝全插槽用 core 默认实现）、`stdio-proxy-dual-channel-42`（P-7 双档：`custom:stdio_result` 增 `buffered_frames` / `flush_direct_calls` 字段）、`host-component-uninstalled-43`（声明未装＝启动失败）；`error-codes-20` 扩展 `llm_error` / `storage_write_fail` 两类场景并增 `LLM_CANCELED`（经 `custom:error_matrix` 走**事件面①**），套件错误码锚定 8 → **13 码**（runner 已补事件面通道：汇总各场景 `error` 事件 `code`）。

**事件契约修正补充（2026-09-11 交叉评审）**：新增用例 `after-step-stop-44`（hooks H-5 轮中 SetStop → `done(intercepted)`，P0-1 承重锚定）；用例 32 断言改用 `tool_result.code=TOOL_REJECTED_BY_POLICY`（越界键 `termination_reason` 已弃用，见 PENDING **P-9**）；用例 09 增 `step_start.step=1` 断言（P1-2）。套件规模 **43 → 44 条**。

**WP-A 场景与断言能力（2026-09-11，M2；用例集 21 → 39 条）**：

- **新增协议层分支**：`stdio-proxy` / `storage-batch-atomic` / `types-doc-opaque`（真实子进程：用例 22 全链往返、26 批量原子性、37 doc 透明性）、`host-component`（用例 23 插槽装载、24 快速失败矩阵）、`events-`（用例 33 未知事件类型宽容）；
- **`expected.final.llm_assert`**：`system_contains` / `system_regex` / `messages_roles` / `message_count`（对 `FakeLLMClient.last_system` / `last_messages` 断言；**未识别子键显式失败**，防"假绿"）；
- **`inputs` 新键**：`storage_ops`（`{op,args?,kwargs?,assert?,expect_error?}`）、`final_probe`、`core_overrides`（**仅**覆盖 `ChatPipeline` 构造参数）、`storage_writer`、`session_store`、`mutate_snapshot`、`route_events`、`env`/`yaml`/`probe`；
- **聚合观测事件**：`custom:stdio_result` / `custom:host_component_result` / `custom:host_component_reject` / `custom:pipeline_l3_result` / `custom:events_result` / `custom:config_secret_result`；
- **P-4 参考后端** `tools/reference_storage_backend.py`：独立于 `teage_liu2.core` 的第二个 P-4 实现（标准库 sqlite3），作为用例 22/26/37 的被测子进程，用以印证"任意语言可按 P-4 实现"。

## 条款编号约定（2026-09-11）

各域条款编号**域内唯一**：`types T-*` / `transport T-*` / `hooks H-*` / `events E-*` / `lifecycle L-*` / `storage S-*` / `config C-*` / `errors R-*`。**跨域引用必须带域名前缀**（如 `transport T-5`、`types T-8`）—— types 与 transport 两域历史上都使用 `T-*`，无前缀即歧义；既有编号一律不改（避免破坏 PENDING 与既有引用）。

## 核心约束（速查）

- `extension_name`: `^[a-z0-9_]+$`（禁点）
- `kind`: `^[a-z0-9_.]+$`
- `SetExtra` key: `^[a-z0-9_]+\.[a-z0-9_.]+$`
- done 事件统一 9 键；事件流必以 done/error 收尾
- core 对未知协议元素一律"透传/记录，绝不崩溃"
