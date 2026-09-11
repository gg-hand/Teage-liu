# PENDING — 待实验并入协议追踪（演进面暂存区）

> **定位**:本文件是 PROTOCOL 演进面的**候选元素追踪文档**，不是契约——`PROTOCOL/` 内其余 spec/schema 才是唯一契约源（v1.0.0 稳定面冻结）。
> **流程（用户定案 2026-09-09）**：有新的协议候选（新钩子/新消息/新 schema/新规范）**先写入本文档** → 实验/实现并迭代 → **正式使用且迭代稳定后才更新进入协议**（spec + schema + 行为套件用例 + VERSION 升版）→ 同时在本文档"已并入记录"区登记去向。
> **准入红线**（evolution 域 RFC 条款）：入协议前必须回答"能否由现有 custom:* / 现有元素组合实现"——能则不升级，留在本表观察。

## 状态定义

| 状态 | 含义 |
|---|---|
| `proposed` | 仅提案，未实现 |
| `experimental` | 已实现/已接线，实验运行中（含生产数据/扩展在用） |
| `stable` | 实验通过，迭代稳定，具备入协议条件 |
| `merged` | 已正式进入 PROTOCOL（记录版本号与用例） |
| `rejected` | 评审否决（记录理由，防重复提案） |

## 条目模板

```
### P-<序号> <名称>
- 状态: proposed|experimental|stable|merged|rejected
- 提案日期 / 来源: YYYY-MM-DD /（谁、哪个任务提出）
- 涉及域: hooks|events|transport|lifecycle|storage|config|types|errors
- 动机: 一句话为什么需要
- 现状: 实现位置、使用方、迭代记录
- 入协议条件: 满足什么才转 stable / merged（含需新增的行为套件用例）
- 入协议记录: merged 时填写（协议版本 + 日期 + 用例编号 + 域文件改动清单）
```

---

## 候选清单

### P-1 manifest.yaml 扩展声明规范（统一扩展目录树）
- 状态: experimental
- 提案日期 / 来源: 2026-09-08 / 统一扩展目录树设计（`docs/plans/2026-09-08-统一扩展目录树-设计.md`，用户逐条定案）
- 涉及域: lifecycle（扩展声明与装载语义）+ config（extensions_root）
- 动机: 扩展统一目录树管理、不进 liu2 源码；manifest.yaml 为**安装态唯一事实源**（语言/入口/capabilities 声明 → 装载校验/stdio 字段合并/安全默认）
- 现状: 已落地——`core/extension_loader.py`（ExtensionSpec 严格解析 + discover + importlib 动态装载，模块名含 manifest_hash 支持热重载隔离）；registry 三通道解析优先级 factory > directory_loader > ValueError；`data2/extensions/{audit,guardrails}` 按 manifest 运行中；规范文本已迁入 `PROTOCOL/lifecycle/lifecycle.spec.md` §3 + `lifecycle.schema.json#/definitions/ExtensionManifest`（**2026-09-11 WP-D，条件④已满足**），SPI §13.2 起索引作用；三个资源上限键（`max_snapshot_bytes` / `max_message_bytes` / `max_messages_per_conversation`）语义由 `types.spec.md` §7 T-8 承载，其 config 键登记随本条一并评审（与 `config.spec.md` §2 补录说明同口径，**2026-09-11 登记**）。**入协议条件勾稽（2026-09-10 审计，逐条）**: ①扩展目录树再验证一轮（含热重载换 manifest + 异常 manifest 拒绝）——`tests_core/test_registry.py` 覆盖链级 rebuild/回滚，**基本满足**；②manifest 字段集冻结（name/version/language/entry/capabilities/transport/command + 2026-09-09 增补 kind/slots）——**满足**（已稳定运行，含 P-6 首个 host-component 用方）；③新增行为套件用例（manifest 解析 / 未声明不启用 / 声明未装启动失败）——**部分满足（2026-09-11 M2）**：`host-component-slot-23`（manifest 目录发现 + kind/slots 完整校验链）+ `host-component-reject-24`（5 条非法 manifest/配置快速失败矩阵）已锚定；**"已装未启用统计"勘误（2026-09-11 落地审查 E-1）**：该统计**已在 core 实现**（`core/extension_loader.py` 返回 `disabled_installed` + `server/app.py` 启动日志 + `tests_core/test_registry.py` 单测锚定），真缺 = ①套件用例 ②端点/热重载路径暴露（可选），**原记"属 server 层 M3/WP-E"不成立**；④明确归入 lifecycle 域文件（规范从 SPI §13.2 迁入 `PROTOCOL/lifecycle/`）——**已做（2026-09-11 WP-D：spec §3 + schema ExtensionManifest）**。**③已满足（2026-09-11 落地审查补齐）**：`host-component-slot-23`（manifest 解析/目录发现）+ `host-component-reject-24`（非法 manifest 快速失败）+ `host-component-disabled-40`（未声明不启用＝已装未启用统计）+ `host-component-uninstalled-43`（声明未装＝启动失败）四例齐备。结论：**四条条件均已满足（2026-09-11）**，具备转 `stable` 条件（转档与版本策略待单独评审，本计划不升 VERSION）；残余可选项（非条件项）= 端点/热重载路径暴露 `disabled_installed`，归后续 server 观测能力
- 入协议条件: ①扩展目录树实战再验证一轮（含热重载换 manifest 与异常 manifest 拒绝）②manifest 字段集冻结（name/version/language/entry/capabilities/transport/command 的必填与类型）③新增行为套件用例（manifest 解析 + 未声明不启用 + 声明未装启动失败）④明确归入 lifecycle 域文件
- 入协议记录: （未并入）

### P-2 异语言 stdio 扩展全链路实战验证
- 状态: experimental
- 提案日期 / 来源: 2026-08-21 / 阶段 3 验收报告遗留项（supervisor/remote_adapter/stdio/版本协商代码就绪但无真实异语言扩展）
- 涉及域: transport + lifecycle（握手协商/心跳僵死重建/进程监管）
- 动机: 异语言通道是 PROTOCOL"语言无关"定位的核心卖点，当前 0 实战案例；首个异语言扩展（如 Node/Rust 实现的 audit 变体）可同时验证版本协商 V-2 与 transport 消息全集
- 现状: 首个真实异语言扩展 storage_rust（Rust 存储后端）已落地，实战验证见任务 8 结论（`docs/plans/2026-09-09-rust存储后端扩展-执行计划.md`）；同语言链路已由 audit/guardrails 闭环。**入协议条件勾稽（2026-09-10 审计，逐条）**: ①至少 1 个真实异语言扩展跑通 spawn→握手→钩子→host 消息→shutdown 全链——**满足**（storage_rust e2e，含重启恢复）；②版本协商三态（major 拒 / minor 降 / 未上报降）各有用例——**已满足（2026-09-11 M2）**：用例 `evolution-negotiation-15` 覆盖 proceed(2)/downgrade(2)/reject(3) 七场景，**"未上报"（missing_version）→ downgrade**（2026-09-11 综合评审统一口径，删此前"归入非法版本拒绝路径"的过时括注；断言于同批 P0-2 修复中改为精确字面量）；③行为套件补异语言用例——**已满足（2026-09-11 M2）**：`stdio-proxy-roundtrip-22`（真实子进程 spawn→hello 握手→双通道读写→UTF-8 往返→bye 全链，被测进程 = P-4 参考后端）+ `storage-batch-atomic-26`（真实子进程批量原子性）。结论：**①②③ 均已满足（2026-09-11 M2）**，具备转 `stable` 条件（转档待单独评审）；残余可选项 = 条款②"未上报"曲线的独立用例，已由 `evolution-negotiation-15` 的 `missing_version → downgrade` 覆盖
- 入协议条件: ①至少 1 个真实异语言扩展跑通 spawn→握手→钩子→host 消息→shutdown 全链 ②版本协商三态（major 拒/minor 降/未上报降）各有用例 ③行为套件补异语言用例
- 入协议记录: （未并入；若验证全过可升级为"行为套件增补"直接回写 behavior-suite，不必然升协议版本）

### P-3 `custom:*` 自定义扩展点实战案例
- 状态: proposed
- 提案日期 / 来源: 2026-09-09 / 协议盘点（evolution 三阶演进第一阶已入 v1.0，但 core 无消费案例）
- 涉及域: evolution（横切）+ events/hooks
- 动机: 第一阶演进通道（Event.type/Hook.name/Action.op 前缀 `custom:*`）已在协议内，但从未被任何扩展实际消费——无案例则 RFC 评审缺乏"现有 custom 能否替代新提案"的依据
- 现状: 协议已定义（透传/记录绝不崩溃），零消费
- 入协议条件: 无需入协议（已在 v1.0）；本条为**观察项**——首个 custom:* 扩展出现后在此登记案例，作为后续候选元素的评审证据
- 入协议记录: 不适用（已是协议元素）

### P-4 storage-stdio 存储线协议（异语言存储后端）
- 状态: experimental
- 提案日期 / 来源: 2026-09-09 / 存储扩展接管落盘设计（`docs/plans/2026-09-09-存储扩展接管落盘-设计.md`，用户逐项定案）
- 涉及域: storage（SPI 承载）+ transport（stdio 帧语义，独立于 transport 12 消息）
- 动机: "扩展接管宿主落盘"需要语言无关的宿主↔后端进程契约；任意语言实现的存储后端满足本协议即可经宿主通用代理接管 `StorageProvider` + `HistoryStore`/`MessageStore` 双通道
- 现状: 宿主侧参考实现 `teage_liu2/server/storage_stdio_proxy.py`（方法调用机械映射为帧，无后端知识）。帧格式：JSON Lines over stdio，请求 `{"id","op","p"}`，响应 `{"id","ok","r"|"e"}`；握手 `hello`（major 版本不匹配 = 启动失败）、关闭 `bye`；op 集 = write/read/query/delete + ensure_session/log_message/get_session_messages/update_session_title/get_session_title/search_messages。**2026-09-10 增补登记**：`get_session_messages` 的 payload 新增 `before_id`（向上翻页游标，`id < before_id`，与 `limit` 组合实现"最近 N 条 → 更早 N 条"），Rust 后端与 stdio 代理同步实现 —— 深度审计 F-4 补登记（此前未入 PENDING）
- 语义条款（后端实现方义务）: ①kind 白名单 `^[a-z0-9_.]+$`（宿主先行校验，双重防线）②`query` 按写入序返回、filters 为 doc 顶层字段精确匹配 ③批量写 `docs[]` 原子提交 ④**write 响应 `r` 恒为 doc_id 数组**（单条与批量一致，数组顺序与 docs 对应；宿主单条写取 `[0]`——后端不得对单条返回字符串，否则 `s[0]` 静默截断）⑤`search_messages` = 子串匹配即可（FTS 是 SQLite 实现细节非契约）⑥单进程内串行应答（一请求一响应）⑦**帧编码必须 UTF-8**（stdout/stderr 均是；Python 后端在 Windows 默认 GBK，须 `sys.stdout/stderr.reconfigure(encoding="utf-8")`，宿主对非 UTF-8 数据快速失败）⑧`query` 的 filters 过滤先于 limit（过滤在前、限条在后）；后端启动参数经 manifest command + options.args 追加传递（--db 类参数通道）⑨**后端不声明也不启用 `FOREIGN KEY` 强制**（2026-09-10 定案，评估见 `docs/plans/2026-09-10-T5.2幽灵外键约束-评估.md`）：`messages.session_id → sessions(id)` 的存续性由**调用契约**保证——`ensure_session` 必须先于 `log_message`（core 侧由 `pipeline._load_history_sync` 保证）；SQLite 外键默认 OFF，声明而不启用属"声明不执行"的误导，故 DDL 中不声明。**注**：老系统 `teage_liu/storage/sqlite_log.py` 仍保留该声明 —— 两系统无需一致（2026-09-10 用户定案：liu2 不受老系统约束），存量共用库中残留的声明本就不生效、无害 ⑩**落盘档位语义（2026-09-10 定案）**：SQLite 实现一律 `journal_mode=WAL` + `busy_timeout=5000`（显式化，与 Rust 后端对齐）；`synchronous` 保持 **FULL**（每次 commit 逐次 fsync，进程崩溃与断电均不丢已提交事务）—— 用户裁决保持该档位（`synchronous=NORMAL` 在 WAL 下可省 fsync、实测单条写 1.23 ms→0.10 ms，代价是断电/OS 崩溃可能丢最近若干已提交事务；实测与评估见 `docs/plans/2026-09-10-core性能与健壮性完善-执行计划.md` §D-1）。Rust 后端与宿主侧 SQLite 实现同档位
- 入协议条件: ①至少 1 个真实异语言后端跑通 spawn→握手→双通道读写→重启恢复→bye 全链 ②语义条款逐条验证 ③行为套件补用例（P-4 专用 case）——**已满足（2026-09-11 M2）**：`stdio-proxy-roundtrip-22`（条款①③④⑥⑦⑧）+ `storage-batch-atomic-26`（条款③原子性）+ `types-doc-opaque-37`（T-7 doc 透明性）；条款②（filters 先于 limit）由 `tests_core/test_reference_backend.py` 单测锚定④与 P-2 异语言验证互相印证后一并评审
- 入协议记录: （未并入）

### P-5 宿主组件插槽契约（host_components 装载机制）
- 状态: experimental
- 提案日期 / 来源: 2026-09-09 / 存储扩展接管落盘设计（`docs/plans/2026-09-09-存储扩展接管落盘-设计.md`，用户逐项定案）
- 涉及域: config（host_components 段）+ lifecycle（宿主组件装配/关闭语义）
- 动机: 宿主装配点（composition root）需要语言无关的"可接管组件"声明——哪些宿主组件允许被扩展 backend 接管、接管者须满足什么 SPI、backend 形态如何命名，需要稳定契约供扩展生态参照
- 现状: 宿主侧参考实现 `teage_liu2/server/host_components.py`（插槽白名单 SLOTS + backend 注册表 BACKENDS + isinstance 快速失败）；storage 插槽已落地（backend: sqlite / stdio-proxy）；config 段 = `host_components: [{slot, backend, options}]`
- 入协议条件: ①storage 插槽经真实异语言后端实战验证一轮（含快速失败路径）②插槽清单与工厂签名冻结（factory(cfg, options) -> {slot: obj}，同实例可覆盖多插槽）③行为套件补用例（默认不接管 = SQLite、未知插槽/ABC 不满足 = 启动失败）——**部分满足（2026-09-11 M2）**：`host-component-slot-23`（同实例覆盖双插槽 + 三 ABC isinstance 快速失败）+ `host-component-reject-24`（未知插槽 / 未知 backend / 重复接管 / command 与 extension 互斥 / kind 非 host-component）；**"缺省省略 = 内置 SQLite" 勘误（2026-09-11 落地审查 E-2）**：缺省路径**实现已存在**（`server/host_components.py` 的 `_sqlite_backend` + 空 `entries` 返回 `{}` + `backend` 缺省 `sqlite`；`server/app.py` 装配兜底取 SQLite）但**零覆盖**；原记「属默认路径，M3/WP-E 覆盖」不成立（WP-E·E2 不含该路径）→ 归属改为「新增缺省路径用例」；**③已满足（2026-09-11 落地审查补齐）**：`host-component-default-41`（缺省省略＝全插槽用 core 默认实现，装载器返回空映射）+ `tests_core/test_server_host_components.py`（缺省/空数组/快速失败矩阵 11 条）④明确归入 config/lifecycle 域文件——**已做（2026-09-11 WP-D：`config.spec.md` §3 + `HostComponent` schema；归属裁决＝config 域，manifest 声明面归 lifecycle 由 P-1 承载）**。结论：①②③④均满足，具备转 `stable` 条件（转档待单独评审）
- 入协议记录: （未并入）

### P-6 宿主组件 backend 目录发现（manifest kind=host-component）
- 状态: experimental
- 提案日期 / 来源: 2026-09-09 / Rust 存储后端扩展设计（`docs/plans/2026-09-09-rust存储后端扩展-设计.md`，用户逐项定案）
- 涉及域: lifecycle（manifest 声明面）+ config（host_components options.extension）——扩展 P-1/P-5
- 动机: 宿主组件 backend 与枝干扩展统一纳入扩展目录树管理，manifest 为安装态唯一事实源（VSCode 式）
- 现状: 首个用方 = storage_rust（Rust 存储后端）；manifest 新增 kind（branch 缺省|host-component）+ slots 字段（host-component 必填、capabilities 必须为空、branch 禁 slots、强制 language: other）；host_components 工厂签名增补 specs 形参；stdio-proxy options.extension（与 command 互斥）+ options.args 追加启动参数；core.branches 声明 host-component = 启动失败
- 入协议条件: ①storage_rust 实战稳定运行一轮（含热重载扫描不受影响）②快速失败矩阵全过 ③行为套件补 host-component 用例——**已满足（2026-09-11 M2）**：`host-component-slot-23` + `host-component-reject-24` ④与 P-2/P-4/P-5 一并评审
- 入协议记录: （未并入）

### P-7 storage-stdio 批量消息写 op（log_messages）
- 状态: experimental
- 提案日期 / 来源: 2026-09-10 / P2 立项评估（`docs/plans/2026-09-10-P2批量写立项评估.md`，P0/P1 落地后基线数据的后续）
- 涉及域: storage（P-4 op 集增补：`log_messages` 数组版）+ lifecycle（宿主侧双档缓冲聚合器语义）
- 动机: 逐条写 = 一帧一事务,实测事务提交开销占单条写成本 98%（1.463/1.491ms,N=300）;合帧+合事务是写吞吐的结构性优化（每轮 5 帧 5 事务 → 2-3 帧 2-3 事务）,高频写入场景（多智能体/事件风暴）收益近线性放大
- 现状: **已实现(2026-09-10)**（提案态登记见评估文档 §5,本次实现后首次落表,状态 experimental）——Rust `log_messages` 单事务原子(任一元素非法整批回滚,insert_message 助手与 log_message 复用)+ 宿主双档聚合器(`log_message_buffered` 缓冲,窗口 25ms/32 条/flush 到达/直发与读路径前冲刷保 FIFO/close 先冲刷)+ pipeline._persist background 档 duck-typing。**执行后评审(plan-auditor)修复 5 项**:①关停期缓冲写入显式 error 日志(不静默丢弃);②失败分流——超时/连接已关闭不降级逐条重发(防重复消息与最坏 N×timeout 阻塞),仅后端 ok:false 类降级重发;③冲刷线程改在握手成功后启动(启动失败不留守护线程);④close join 冲刷线程(防在途批次被关连接打断);⑤指标口径(log_messages=帧数)与"append 单线程前提"写入 docstring。验证:仓库外冒烟(批量原子性/顺序/混合会话/错误路径)+ 双档驱动(缓冲合帧/保序/close 冲刷/flush 档不缓冲)+ 修复专项驱动(关停守卫/真挂起后端注入超时的"不降级"断言/close join)+ 行为套件 17/17 + e2e(`log_messages.count=2`、`log_message.count=2`、errors 全 0、重启恢复)。**行为套件 case 待 runner 支持双档场景后补(与 P-4 先例同口径,评审时确认)**。条款⑥流水线经评估**不立项**(收益上限为 RTT 重叠隐藏,后端串行执行时间不变,重构面大),降级观察。**宿主双档的 flush 档落盘档位语义同 P-4 条款⑩**（2026-09-10 定案：保持 `synchronous=FULL`，"断连不丢输入 = await commit 完成"语义不变）
- 入协议条件: ①真实异语言后端跑通批量写 + 批量失败降级逐条 ②缓冲窗口语义验证（崩溃丢失窗口 ≤ 窗口时长,flush 档永不缓冲）③行为套件补用例（批量原子性 + 双档分级行为）——**部分满足（2026-09-11 M2）**：批量原子性由 `storage-batch-atomic-26` 锚定（真注入非法元素 + 整批回滚 + 探针回读）；**双档分级**勘误（2026-09-11 落地审查 E-3）：双档**实现完整**，`storage_ops` 泛型分发 + `metrics_snapshot` **已具备观测能力**——真缺的是**套件用例与聚合字段**（非能力缺失）；**③已满足（2026-09-11 落地审查补齐）**：`stdio-proxy-dual-channel-42`（真实子进程双档：background 合帧经 `log_messages`、flush 直发保 FIFO 序、`metrics_snapshot` 区分两档）+ `tests_core/test_server_stdio_proxy.py`（5 条）④与 P-4 一并评审。结论：①②③满足，具备转 `stable` 条件（转档待单独评审）
- 入协议记录: （未并入）

### P-8 错误码可观测面定则（三面模型）+ transport 错误码子命名空间
- 状态: experimental
- 提案日期 / 来源: 2026-09-10 / 深度审计 F-3（18 码中仅 2 码有发射点，`CONFIG_*` 为死码；协议套件 0 锚定）
- 涉及域: errors（主）+ transport（③ 响应面子命名空间登记）
- 动机: 错误码此前处于"文档态"——责任矩阵定义 18 码，实现只在注释/常量里存在；套件枚举 18 码 **0 命中**，新宿主无法审计错误语义
- 现状: 已落地三面模型与发射点——①事件面：`error` 事件新增 `code`（step.py 四个 LLM_* + pipeline 的 HOOK_INVALID_ACTION）；②日志面：统一 `CODE: message` 前缀（hooks 的 HOOK_TIMEOUT/HOOK_EXCEPTION、loop 的 LOOP_MAX_REACHED/TOOL_NO_EXECUTOR、snapshot 的 HOOK_INVALID_ACTION、storage_writer/pipeline/transport 的 STORAGE_*、config 的 CONFIG_* 三码由死码接活）；③响应面：transport 10 个小写码登记进 errors.spec §5.1 与 errors.schema.json 的 TransportErrorCode。套件：新增用例 `error-codes-20`（logging 捕获 4 码）+ `config-domain-21`（配置码 2 个）；`hook-isolation-19` 断言 HOOK_EXCEPTION。**2026-09-10 执行后评审补**：事件面此前**零锚定**（删掉全部 `code` 键套件仍全绿），已由 `tests_core/test_error_codes_events.py` 补上（LLM_API_ERROR 事件 + `code ∈ ERROR_CODES`）；`errors.spec §5` 已把三个无发射点的 TOOL_* 码移入"⑤ 待落地"并指向本条条件 ④。**2026-09-11 WP-C 落地**：三码发射点已补（`core/hooks.py` 的 TOOL_MODIFY_INVALID / TOOL_EXEC_FAILED、`core/loop.py` 的 TOOL_REJECTED_BY_POLICY，均带 `CODE: ` 前缀），errors.spec §5 已把三者归入 ② 日志面，条件 ④ 已满足。**2026-09-11 落地审查补齐**：①套件 `error-codes-20` 扩 4 条 LLM_*/STORAGE_WRITE_FAILED 日志面场景（套件锚定 8 → 12 码，随后 +LLM_CANCELED → **13 码**）；②新增单测 `test_step_error_codes.py` / `test_storage_error_codes.py` / `test_config.py`（CONFIG_MISSING_KEY）/ `test_error_codes_schema.py` —— **并集 18/18 全码锚定（脚本实测，缺失 0）**，四条入协议条件全部满足，具备转 `stable` 条件
- 入协议条件: ①18 码逐条声明可观测面（已由 errors.spec §5 表格固化）②行为套件对拍覆盖 ≥ 12 码（**已满足 · 2026-09-11 落地审查补齐**，随后深化为 **13 码**：套件 `error_codes` 并集实测 **13 码**（`error-codes-20` 新增 LLM_TIMEOUT / LLM_STREAM_FAILED / LLM_API_ERROR / STORAGE_WRITE_FAILED 四场景 + **LLM_CANCELED** —— 后者经 `custom:error_matrix` 汇总各场景 `error` 事件 `code` 锚定**事件面①**，因其为 info 级日志不入 ②；同时修复了该用例此前事件面通道为空、仅靠日志面侥幸通过的隐患）；套件+单测并集 **18/18 全码**）③errors.schema.json 枚举与 `core/errors.py` 常量逐字一致（**已满足 + 机械校验 · 2026-09-11**：新增 `tests_core/test_error_codes_schema.py` 双向比对，此前仅人工事实、无回归防护） ④`TOOL_EXEC_FAILED` / `TOOL_REJECTED_BY_POLICY` / `TOOL_MODIFY_INVALID` 三码的日志发射点落地（**已满足 · 2026-09-11 WP-C**：发射点 = `core/hooks.py` 两码 + `core/loop.py` 一码；锚定 = `tests_core/test_tool_error_codes.py` + 套件用例 `error-responsibility-16`）
- 入协议记录: （未并入）

### P-9 tool_result 事件面错误码（事件面①补齐）
- 状态: experimental
- 提案日期 / 来源: 2026-09-11 / core 与协议交叉评审（`docs/plans/2026-09-11-core与协议-交叉评审与修正方案.md` §3.1）
- 涉及域: events（ToolResultEvent 可选 `code`）+ errors（① 事件面覆盖码）
- 动机: 工具未成功执行时，事件面此前只有 `is_error: true` 布尔——"被策略拒绝"与"执行异常"在事件面**不可区分**，第三方观测扩展（audit）拿不到机器可读原因；且原实现以越界键 `termination_reason`（done 专用枚举名）表达该语义，违反 ToolResultEvent schema（`additionalProperties:false`）
- 现状: 已落地——`events.schema.json` ToolResultEvent 增可选 `code`；`core/loop.py` 发射点 = reject → `TOOL_REJECTED_BY_POLICY`、执行异常 → `TOOL_EXEC_FAILED`；`errors.spec.md` §3/§5① 同步（两码由"仅日志面"升级为"事件面 + 日志面"双面）；旧越界键已删除
- 入协议条件: ①契约文本 + schema 落地（**已做**）②行为套件锚定（**已做**：用例 `hooks-reject-eventflow-32` 断言 `code=TOOL_REJECTED_BY_POLICY`）③`tool_result.code ∈ ERROR_CODES` 机械校验（**已做**：`tests_core/test_events_contract.py::test_tool_result_code_within_error_codes`）④稳定运行一轮后随 P-8 一并转 stable/merged
- 入协议记录: （未并入；VERSION 是否升 v1.1.0 待与 P-1..P-8 转档一批评审）

### P-10 usage 族字段可空语义统一（errata）
- 状态: experimental
- 提案日期 / 来源: 2026-09-11 / core 与协议交叉评审 §3.3
- 涉及域: events（StepEndEvent.usage / StepSummary.usage）
- 动机: 同一语义（LLM usage）在 4 处两种口径——`done` / `AfterResponse` 允许 `null`，`step_end` / `StepSummary` 却要求 `object`（required）。provider 未上报 usage 是真实且普遍场景，`null` = 未上报、`{}` = 上报但为空；强制 object 会丢失语义，并使 `DoneEvent.usage=null` 退化为不可达死分支
- 现状: 已落地——两处 schema 放宽为 `["object","null"]`；`events.spec.md` §1 补字段说明；core 代码零改动（`None` 即真实状态）
- 入协议条件: ①schema + spec 落地（**已做**）②行为套件锚定「provider 未上报 → usage 为 null 且事件合法」（待补）③稳定运行一轮后转 stable
- 入协议记录: （未并入）

---

## 已并入记录（merged 后从候选清单移入此处归档）

| 序号 | 名称 | 并入版本 | 日期 | 说明 |
|---|---|---|---|---|
| （暂无——v1.0.0 为首个冻结版本，其前身演进见 `docs/plans/2026-08-20-终极解耦架构-设计.md` 修订记录 v1.1-v1.15） | | | | |
