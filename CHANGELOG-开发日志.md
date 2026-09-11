开发日志 —— 本次变更摘要

对比基准：origin/dev @ 3420269 → 当前工作树
统计：80 文件修改（+9228/-3025），73 新增文件，1 删除文件

---

## 2026-09-11 综合评审修复（P0×2 + P1×6 + P2 群 + 套件假绿修复）

> 来源：`docs/plans/2026-09-11-liu2深度综合评审报告.md`（3 路子评审 + 主 agent 验证）→ `docs/plans/2026-09-11-综合评审修复计划.md`（plan-verifier 审查后执行）。**协议无新增元素，VERSION 仍 v1.0.0；套件 45/45、pytest 258 passed、lints 0；P0/P1 修复全部变异验证承重（退化必红 + 还原逐字节）。** 详情见 `teage_liu2/docs/plans/开发日志.md` 顶部条目。

- **P0-1**：pipeline 形态实例改**每对话新建**（原单例 `final_snapshot` 跨 session 并发互踩：after/on_error 拿错会话快照 + extra 跨会话串写）；新增跨 session 并发回归测试。
- **P0-2**：套件用例 14/15/10/13 弱断言改精确字面量 + runner negotiation 硬校验 —— **P-1/P-2 转档证据修复**（转档评审前置条件）。
- **P1 群**：①入口 user 消息校验移到落盘前（防超限输入污染历史库）②lifespan shutdown 重排（先排空写队列再关存储）+ 启动失败兜底清理 ③热重载回滚恢复旧扩展 bus 身份/L3 订阅 ④轮间收口 error 补 `code` ⑤H-20 schema 校验器 fail-open（畸形 schema 不炸对话）⑥`/reload` 重放 `core.max_*`。
- **套件/覆盖**：runner 守卫上移协议类用例 + op 白名单 + `read` 接线；新增用例 `45-resource-limits`（T-8 首个套件锚定）。
- **P2 群**：工具 duration 计时/`Union` import/日志常量化/decision 校验/tasks 时序/writer resolve/history 读锁/stdio limit/L3 deepcopy/docstring 收敛/routes 显式消费/空断言清理。
- **契约文本 9 处**：lifecycle `lang` 勘误、events step 1-based 契约化、PENDING P-2 口径、errors/config/types/transport 措辞、CORE/INTERFACES 同步。

## 2026-09-11 交叉评审剩余问题一并解决（P1-4/P1-5/P1-6/P1-8/P1-9 + P2 + 装载边界）

> 来源：`docs/plans/2026-09-11-core与协议-交叉评审与修正方案.md` §9/§10（4 路子智能体 + 主 agent 复核的全部剩余项）。**协议无新增元素，VERSION 仍 v1.0.0；PENDING P-9③ 勾稽补齐。**

- **P1-4 资源上限三键接线**（此前"配置静默失效"）：`core/snapshot.py` 新增 `configure_limits()` / `active_limits()`（`_ACTIVE_LIMITS` 注入，键未知/值非法保持协议默认）；`ChatPipeline(resource_limits=...)` + `server/app.py` 把 `core.max_*` 真正接入快照上限。
- **P1-5 H-20 落地**：新增 `core/schema_check.py`（极简 JSON Schema 子集校验，**零新依赖**、未知关键字宽容）；`hooks.pre_tool_call_all` 对 modify 后的入参**重过工具 `input_schema`**，非法 → 视同策略拒绝（`TOOL_MODIFY_INVALID` 记码 + tool_result is_error 回喂）。
- **P1-6 H-5 轮中短路**：`hooks.post_tool_call_all` / `after_step_all` 在 SetStop 后**短路后续扩展**（此前只对 before 短路）；`loop` 在 `post_tool_call` 置 stop 后**不再执行本批次剩余工具**，并以 `tool_use`+`tool_result`(is_error, `TOOL_REJECTED_BY_POLICY`) 补齐事件流与消息配对（H-19 不缺环 / 防下轮 400）。
- **P1-8 行为套件 `inputs` 假绿**：runner 新增 `_check_unknown_extension_inputs`（`<ext>_<suffix>` 后缀白名单守卫，写错即失败）；删除 case 05 `forged_revision` / case 14 `reload_cycles` 两个被静默忽略的键；case 09 改用真正被消费的 `round_injector_inject_round`；case 10 `limits` 落实为"与协议常量比对 + 真实驱动超限拒绝"；case 12 并发上限由宽区间改为与 `INVOKE_LLM_MAX_CONCURRENCY` 逐字一致的 `4`；case 18/25 文件名与 id 对齐；删除死代码 `_find_behavior_inputs`。
- **P1-9 文档一致性 14 处**：`INTERFACES.md`（`branches/guardrails.py` → `data2/extensions/guardrails/main.py`、17/17 → 44/44）、`docs/SUBSYSTEM-SPI.md`（`branches/audit.py` → `data2/extensions/audit/main.py`）、`PENDING.md` P-2 自相矛盾结论、`config.spec.md` 资源上限键登记口径、`PROTOCOL/README.md`（权威规格 v1.14→v1.15、将来时→完成时）、`CORE.md`（删除已消失的"装配点例外"、铁律 3、构造参数补 `session_store`/`resource_limits`）、`errors.spec.md`（6→7 常量）、`hooks.spec.md`（不绑定 `asyncio.wait_for`）、`lifecycle.spec.md`（`lang`→`language`）、`types.spec.md` T-4（扩展不提交 revision）。
- **P2 群**：SetExtra 值**必须 JSON 可序列化**（否则快照体积记账恒判 0、上限失效）；**入口 user 消息**与扩展 AppendMessage 同一套资源上限（`snapshot_with_user_message` 由死代码转为活路径，超限 → error 事件）；`storage.query` 改为 **filters 先于 limit**（SQL `json_extract` WHERE，与 P-4 条款⑧及 Rust 后端一致）；**终止原因常量消除双源**（单一事实源 = `core/errors.py`，`types` re-export）；**`ToolDecision.reason` 补齐**（协议已定义、实现此前丢弃；reject 文案改用 reason，case 16/32 同步）；**observe 扩展的 `ToolDecision` 一律忽略**（E-9 只读约束此前只管 action）；`TaskRegistry.cancel_all` 改 **async 并等待取消完成** + 任务异常回收（此前"never retrieved"噪声）；`step.py` 的流式总超时**只包裹每次 await**（不再把 `yield` 包进 `asyncio.timeout`，避免慢消费方被误取消/漏判）。
- **装载边界（lifecycle §3.3 机制化）**：`extension_loader.check_import_boundary` 在装载前 AST 扫描，禁止扩展入口 import `teage_liu2.server` / `teage_liu2.branches` / 老系统 `teage_liu`（该条款此前**只存在于文档**）。
- **验证**：行为套件 **44/44**；pytest **232 → 250 passed**；`audit_liu2.ps1` **exit=0**；lints 0；**变异验证 9/9 全承重**（H-5×2 / H-20 / observe / 入口上限 / 资源上限接线 / SetExtra / query filters / import 边界，退化必红 + 还原逐字节）。新增 `tests_core/test_cross_review_fixes.py`（16 条）。**未做 git 写操作，无临时文件残留。**

## 2026-09-11 core 与协议 交叉评审修正（P0-1 轮中拦截 + P1-1/2/3 事件契约 + P1-7 契约收口）

> 来源：`docs/plans/2026-09-11-core与协议-交叉评审与修正方案.md`（4 路只读子智能体并行评审 + 主 agent 行号级复核）。**协议侧新增 PENDING P-9/P-10，VERSION 仍 v1.0.0（升版待与 P-1..P-8 转档一批评审）。** teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。

- **P0-1（正确性）轮中 SetStop 拦截丢失**：`core/loop.py` 在 `after_step` 返回 SetStop 且本轮自然结束（end_turn）时，直接 `done(normal, is_complete=true)` —— 拦截被静默丢弃（`cur.stop` 检查原只在工具分支之后）。已补 guard → `done(intercepted)`，与 hooks H-5 一致；新增套件用例 `after-step-stop-44` 承重锚定。
- **P1-1 tool_result 越界键 → 协议定义的 `code`**：原实现发 done 专用枚举名 `termination_reason`（ToolResultEvent `additionalProperties:false`，属越界）。改为 errors 域可选 `code`：reject → `TOOL_REJECTED_BY_POLICY`、执行异常 → `TOOL_EXEC_FAILED`（两码由"仅日志面"升级为"事件面 + 日志面"双面，三面模型①更完整）。登记 PENDING P-9。
- **P1-2 `step_start.step` 恒 0 → 真实序号**：`StepExecutor.execute` 增 `step` 形参（默认 1），loop 传 `cur.round`、bare 传 1；schema `minimum:1` 不动（协议本来正确）。
- **P1-3 usage 口径分裂 → 协议统一**：`StepEndEvent.usage` / `StepSummary.usage` 放宽为 `["object","null"]`，与 `DoneEvent` / `AfterResponse` 一致（provider 未上报 = null，不以 `{}` 伪装）。登记 PENDING P-10；core 零改动。
- **P1-7 热重载 cancel_all 契约收口**：`TaskRegistry.cancel_all` 是**全局**且对扩展侧登记任务只清登记表（宿主无法终止扩展进程内任务）——L-4/T-4 原承诺的"热重载兜底取消"既会误杀新链任务、又语义不成立。按 liu2"扩展自持 / 主干最小"定案改为**改协议文本**：取消执行责任在扩展 teardown，`cancel_all` 兜底仅限宿主整体 shutdown；`core/tasks.py` docstring 与 `registry.rebuild` 注释同步。
- **验证**：行为套件 **43 → 44/44**；pytest **226 → 232 passed**；`audit_liu2.ps1` **exit=0**；lints 0；**变异验证 6 项全承重**（P0-1 guard / P1-1 code ×2 / P1-2 step ×2 / P1-3 schema，退化必红、还原逐字节完好）。**未做 git 写操作。**

## 2026-09-11 T-3 边界只读视图 + WP-E·E1/E4 routes 锚定（M3 收尾）

> 来源：`docs/plans/2026-09-11-三项待裁决分析.md` §6；用户裁决「T-3 采用边界只读，防止误用」「WP 按推荐」。**PROTOCOL v1.0.0 冻结面未动（仅实现 T-3 既有契约），不升 VERSION。** teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。

- **T-3 / H-17 边界只读视图（S2）**：`core/types.py` 新增 `readonly_view(snapshot)`（`messages`/`tools`/`history` → `tuple`、`extra` → `MappingProxyType`，元素共享引用、不 deepcopy）；`core/hooks.py` 对**全部面向扩展的 9 处钩子调用**（build_injections / inject_round / before / pre_tool_call / on_tool_call / post_tool_call / after_step / after / on_error）统一改传只读视图，core 自身仍用原快照。**H-17 由"缺口"转"已修复"**（`CORE-缺口记录.md`）；套件 `hooks-readonly-31` 由"如实锚定篡改生效"收紧为 `messages_roles=[user]`；`test_snapshot_identity.py` 新增 2 条。
- **WP-E·E1 routes 生产修复锚定**：新增 `tests_core/test_server_routes.py`（`server/` **首次有测试锚定**）——`/chat` 拦截取 `done.response`、`/chat/stream` 不提前关闭生成器（终态 `after` 仍触发）、`/sessions/{id}/messages` 的 `before_id` 游标分页、`/reload` 返回**分支名字符串**（Branch 实例 → 500）。夹具 = 假 `LLMClient` + 临时库 + 临时 `extensions_root`/`config.yaml`。
- **WP-E·E4 L-11 路由层真并发**：同一 session 两个并发 `/chat` 经路由层 session 锁串行化（确定性：第一个请求持锁阻塞，第二个必须等）；删掉 `async with lock` 即变红。
- **变异验证**：T-3 去边界包装 → 用例 31 + 单测红；E1/E4 批量退化 5 处 → 5 用例全红且报错信息准确；恢复后 `routes.py` 与 HEAD 逐字节一致（`git diff` 空）。
- **验证**：行为套件 **43/43**；pytest **221 → 226 passed**；`scripts/audit_liu2.ps1` **exit=0**；lints 0。**未做 git 写操作。**

## 2026-09-11 三项待裁决落地（CI 依赖放行 + L-11 边界修复 + LLM_CANCELED 事件面锚定）

> 来源：`docs/plans/2026-09-11-三项待裁决分析.md`。**PROTOCOL v1.0.0 冻结面未动，不升 VERSION。** teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。

- **CI 依赖放行（WP-E 步骤 0）**：`.github/workflows/liu2-audit.yml` 装机行追加 `fastapi httpx`（用户 2026-09-11 批准）。原方案 C"不经 FastAPI"被证**不成立** —— `server/routes.py:9-10` 与 `app.py:16-18` 均**模块级** import fastapi，import routes 即需要它；头注释同步写明"覆盖被测试模块的模块级 import 全集"原则（与 `python-dotenv` 同款）。
- **L-11 会话锁边界漏洞修复**：`server/session_locks.py` 超限淘汰时**跳过本次刚返回的锁**（此前会把刚拿到的锁淘汰 → 同 session 两次 `get` 拿到两把不同锁、互斥失效，`break`"临时超限"分支不可达）；`tests_core/test_session_locks.py` 更新受影响用例并新增 `test_just_returned_lock_not_evicted` 边界用例。
- **LLM_CANCELED 归位事件面**：`error-codes-20` 增 `LLM_CANCELED` 场景 + `must_include`；runner 的 `_run_error_code_matrix` 改为汇总各场景 `error` 事件 `code` 并经 `custom:error_matrix` 锚定**事件面①**（此前该用例事件面通道为空、LLM_* 仅靠日志面侥幸通过；LLM_CANCELED 为 info 级不入 ②，故只走事件面）。套件错误码并集 **12 → 13**。PENDING P-8 条件②、`PROTOCOL/README.md` 同步。
- **条款覆盖登记**：协议收口计划 §0/§13 回写 6 条（T-3/T-5/L-5/L-6/L-10/L-11）的锚定文件与**限定语**（T-3/L-11/L-10 为带限定的部分覆盖，不得洗白为全闭合）。
- **变异验证**：①事件面退化（`event_codes` 置空）→ `error-codes-20` 变红（LLM_CANCELED 未出现）；②去掉"刚返回锁不淘汰"防线 → 会话锁 2 条变红（复现"同 session 两把锁"）；恢复后全绿。
- **验证**：行为套件 **43/43**；pytest **219 passed**（218 + 1）；`scripts/audit_liu2.ps1` **exit=0**；lints 0。**未做 git 写操作。**

## 2026-09-11 「部分落实」项统一落地（协议债收口 + 测试锚定）

> 来源：`docs/plans/2026-09-11-部分落实项统一落地审查.md`。**PROTOCOL v1.0.0 冻结面未动，不升 VERSION。** teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。

- **套件 39 → 43**：新增 `host-component-disabled-40`（未声明不启用＝已装未启用统计）/ `host-component-default-41`（缺省省略 `host_components`＝全插槽用 core 默认实现）/ `stdio-proxy-dual-channel-42`（P-7 双档：background 合帧经 `log_messages`、flush 直发保 FIFO 序、`metrics_snapshot` 区分两档）/ `host-component-uninstalled-43`（声明未装＝启动失败）；`error-codes-20` 扩展 `llm_error` / `storage_write_fail` 两类场景。
- **错误码全码锚定**：套件 `error_codes` 并集 8 → **12 码**；新增单测 `test_step_error_codes.py`（4 个 LLM_*）、`test_storage_error_codes.py`（STORAGE_WRITE_FAILED / STORAGE_READ_FAILED）、`test_config.py` 补 CONFIG_MISSING_KEY、`test_error_codes_schema.py`（ERROR_CODES ↔ errors.schema.json 机械比对）→ **并集 18/18 全码（脚本实测，缺失 0）**；P-8 条件②③勾稽为已满足。
- **core/server 测试锚定（不引入 fastapi 依赖，CI 可跑）**：新增 `test_snapshot_identity.py`（T-3/T-5 结构共享 + 禁 deepcopy）、`test_assembler.py`、`test_modes.py`、`test_supervisor.py`（L-5/L-6 僵死重建）、`test_remote_adapter.py`（钩子往返 / `close()` 后可读错 / major 拒绝）、`test_server_host_components.py`（P-5 缺省路径 + 快速失败矩阵）、`test_server_stdio_proxy.py`（E3 + 双档）、`test_session_locks.py`（L-11）、`test_session_extra_continuity.py`（L-10）→ pytest 165 → **218 passed**。
- **协议登记勘误与勾稽**：PENDING P-1/P-5/P-7 条件③由「部分满足」改为**已满足**（E-1..E-3 勘误：能力已在 core 实现、仅缺锚定）；P-8 四条条件全部满足；T-8 移出「未覆盖」清单（未覆盖 20 → 6 条）；协议收口计划顶部 checkbox 免责覆盖 M1+M2（0/77 未勾）。
- **变异验证**：5 处退化（ERROR_CODES 去码 / STORAGE_WRITE_FAILED 去前缀 / `log_message` 免冲刷 / `disabled_installed` 恒空 / 缺省路径抛错）→ 单测 7 红 + 套件 3/3 红；恢复后全绿。
- **验证**：行为套件 **43/43**；pytest **218 passed**；`scripts/audit_liu2.ps1` **exit=0**。

## 2026-09-11 M2 行为套件场景与用例扩展（WP-A + WP-B，用例 21 → 39）

> 来源：`docs/plans/2026-09-10-协议收口与遗留债-解决计划.md` 的 M2；执行编排与事实修正见 `docs/plans/2026-09-11-M2行为套件场景能力-执行计划.md`。teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。**PROTOCOL v1.0.0 冻结面（11 钩子 / 12 消息 / 6 Action）未变，不升 VERSION。**

- **WP-A（场景与断言能力，5 任务全落地）**：
  - **A1 P-4 参考后端** `tools/reference_storage_backend.py`：**不 import `teage_liu2.core`**，仅标准库 sqlite3 独立实现 P-4 全部语义条款（kind 白名单 / filters 先于 limit / 批量单事务 / write 恒返数组 / 子串搜索 / 三流 UTF-8 / bye 退出 / WAL + busy_timeout）。执行中发现并修复**参考后端自身的原子性缺陷**：`_insert_message` 内层 `with self.conn:` 会提交外层未完成事务（sqlite3 嵌套 with 语义），致 `log_messages` 变逐条提交 → 去掉内层 with。
  - **A2 真实子进程 stdio 场景**（用例 22）：runner 首个依赖 `teage_liu2.server` 的分支；spawn 参考后端 → hello 握手 → 双通道读写 → UTF-8 往返 → bye。
  - **A3 host-component 插槽场景**（用例 23/24）：manifest 临时目录 + `wire_extensions` + `load_host_components`，覆盖"同实例覆盖 storage+history 双插槽 + 三 ABC isinstance"与 5 条快速失败矩阵。**修正原计划偏差**：manifest `command` 无占位符机制 → 直接写绝对路径。
  - **A4 断言能力**（用例 25）：新增 `expected.final.llm_assert`（4 子键，未识别子键显式失败）+ `inputs` 的 `storage_writer` / `session_store` / `core_overrides` / `mutate_snapshot`；`_assert_final` 增 `llm=None`（既有 2 处调用点零改动）。
  - **A5 批量原子性**（用例 26）：参考后端把元素校验**移入事务内**（否则 JSON 用例无法区分原子/非原子实现），runner 支持 `expect_error` + `storage_ops[].assert` + `final_probe` 回读。
- **WP-B（13 条黄金用例，编号 27–39）**：hooks 域 6 条（H-3 角色限制 / H-8 原子批次 / H-9 语义不变量 / H-16 inject_round 合并 / H-17 只读 / H-19 reject 事件完整）、events 域 4 条（E-2 未知类型宽容 / E-4 L2 唯一通道 / E-8 step_end 双呈现 / E-9 observe 只读）、types 域 2 条（T-1 预算整段丢弃 / T-7 doc 透明性）、config 域 1 条（C-2 敏感字段）。**13 条全部通过变异验证**。
- **执行中新发现并修复的 core 缺口（3 项）**：①`HOOK_TERMINAL_ACTION_IGNORED` 在 observe 只读路径**未发射**（E-9 契约漂移，`core/hooks.py` 补码前缀）②部分层 `injection_budget` 覆盖触发 **KeyError**（`core/injection.py` 改为默认层合并）③（见 A1）参考后端嵌套事务。
- **登记未修复缺口（2 项）**：H-17"只读视图"无内核级强制（`frozen` 只锁属性、`messages`/`extra` 容器仍可变，用例 31 如实锚定"篡改生效"，修复后须收紧断言）；H-9 语义级校验缺可构造负例路径（被 merge 前置保证，用例 29 改为正向不变量断言）。
- **协议条件勾稽（B5）**：PENDING 六条协议的条件③ —— **P-2 / P-4 / P-6 已满足**，**P-1 / P-5 / P-7 部分满足**（缺口分别为"已装未启用统计"（属 M3/WP-E）、"缺省省略 = 内置 SQLite"（M3/WP-E）、"双档分级观测"（M3/WP-E））。
- **验证**：行为套件 **39/39**；pytest **165 passed**（新增 6 条参考后端单测）；`scripts/audit_liu2.ps1` **exit=0**；每条新用例做变异验证（退化实现 → 变红 → 恢复）。

## 2026-09-11 协议收口 M1（WP-C + WP-D + WP-G + 两处同源小修）

> 来源：`docs/plans/2026-09-10-协议收口与遗留债-解决计划.md` 的里程碑 M1（另按 `docs/plans/2026-09-11-core与协议通用性局限-评估.md` §7 建议并入两处"契约信用"小修）。teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。**PROTOCOL v1.0.0 冻结面（11 钩子 / 12 消息 / 6 Action）未变，不升 VERSION。**

- **WP-C（P-8 三码发射点）**：`TOOL_MODIFY_INVALID`（`core/hooks.py` —— pre_tool_call 返回 modify 但 `input=None`，此前**静默忽略、无 else 分支**）、`TOOL_EXEC_FAILED`（同文件 dispatch 异常日志）、`TOOL_REJECTED_BY_POLICY`（`core/loop.py` reject 路径）三码补 `CODE: ` 前缀；`errors.spec.md §5` 把三者从"⑤ 待落地"归入"② 日志面"，PENDING P-8 条件④勾稽为**已满足**。锚定 = 新增 `tests_core/test_tool_error_codes.py`（2 条）+ 套件用例 16 增 `error_codes.must_include`（**变异验证**：去前缀 → 两处均变红）。
- **WP-D（协议域归档与契约同步）**：manifest 声明规范自 `SUBSYSTEM-SPI.md §13.2` 迁入 `PROTOCOL/lifecycle/lifecycle.spec.md §3` + 新增 `ExtensionManifest` schema（与 `extension_loader.parse_manifest` 逐条对应，含 language/kind 条件必填 if-then）；`host_components` 契约入 `config.spec.md §3` + 新增 `HostComponent` schema，并补录 schema 遗漏的 `extensions_root`；PENDING P-1 登记三个资源上限键 + 条件④勾稽；`MessageStore.get_session_messages` ABC 补 `before_id`（`core/storage.py`，与 HistoryStore/SQLiteHistoryStore 对齐，零行为变化）。
- **WP-G（文档一致性）**：`types` / `transport` 两域 `T-*` 编号歧义消解（各加域内限定 + README 新增"条款编号约定"，**既有编号不变**）；用例 17 description 删去未实现的"防重入/并发上限"表述；`teage_liu2/docs/INTERFACES.md` 任务章节修正两处漂移。
- **同源小修（契约信用）**：①`InProcessHostPort.register_task/cancel_task` 由**同步**改 `async` + `await` + 错误上抛（`core/transport.py`）—— 修复"调用 async `bus.handle` 未 await → 协程被丢弃、`task_register`/`task_cancel` 永不执行（+ RuntimeWarning）"的真实缺陷；②`INTERFACES.md` 关于"热重载重建 cancel_all 兜底"的表述与 `registry.rebuild`（**不调** cancel_all）对齐 —— 明确扩展必须在 teardown 自清理。两处均补锚定测试（`tests_core/test_host_port_tasks.py` 3 条，含"无未 await 协程"断言）。
- **验证**：行为套件 **21/21**；pytest **156 passed**（新增 5 条）；`scripts/audit_liu2.ps1` **exit=0**。

## 2026-09-10 core 性能与健壮性完善（7 任务落地，零契约变更）

> 来源：`docs/plans/2026-09-10-liu2-core性能审查报告.md`（本机实测口径）；执行计划：`docs/plans/2026-09-10-core性能与健壮性完善-执行计划.md`。teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。**PROTOCOL/ v1.0.0 冻结面一字未动**，不改配置键、不新增依赖。

- **落盘档位如实化（D-1 裁决 = 保持 FULL）**：SQLite 连接显式化 `busy_timeout=5000`；`synchronous` **保持 SQLite 默认 FULL**（逐次 fsync）——用户裁决不启用 WAL 下的 `NORMAL`；`core/storage_writer.py` docstring 从"NORMAL 下 WAL 已写"改为如实描述；PENDING P-4 增条款⑩、P-7 现状行注明 flush 档语义同上。
- **协议边界校验**：`json_depth` 递归改**迭代**（深嵌套不再 `RecursionError`，实测快 ≈2×）+ 拆出 `check_frame_depth`；入站帧体积一律按**线上字节**计（去掉每帧一次整帧 `json.dumps`）+ `RecursionError → ValueError` 兜底。
- **stdio 读循环单帧容错**：decode 与 dispatch 全包进逐帧 try/except —— 修复 `type` 字段不可哈希（`TypeError`）逃出 `except ValueError` 导致**整条通道死亡**的真实缺陷。
- **快照体积增量记账**：`_SizeCache`（messages 同一性失效 + revision 进位差分），`_check_snapshot_budget` 稳态零全量序列化 —— 实测 605 KiB 快照 `apply_action_batch` **3.156 ms → 0.016 ms（≈194×）**。
- **超时原语**：`asyncio.wait_for` → `asyncio.timeout`（llm 逐 chunk + hooks 逐钩子）—— 实测 8000 chunk **77.5 → 21.2 ms**、单钩子 **9.5 → 2.5 µs**。
- **L3 批处理修复**：恢复"64 条先到立即冲刷"（此前同 tick 500 条 0 次触发）、在途冲刷不被取消、缓冲 `list.pop(0)` → `deque.popleft()`、close 兜底投递。
- **验证**：`pytest teage_liu2/tests_core` **150 passed**（新增 26 条）；行为套件 **21/21**；关键改动均做变异验证（还原旧语义 → 对应用例变红）。
- **执行后严格审计（plan-auditor）修复 9 项**：①**[主要]** `json_depth` 迭代版在"宽而浅"帧上比递归慢 6× 且内存放大（70 万容器 378 ms / 42.9 MB）→ 改**迭代器栈 DFS** + 空容器不入栈 + `limit` 早退，复测宽浅 700k **50 ms**（快于递归 77 ms）、内存 0 MB；②`L3BatchSink.close()` 不 await 在途冲刷 → shutdown 期事件静默丢失 → 改为 await + 补红测试；③`_flush` 的 `while` 续送语义补用例；④`check_frame_limits` 零调用方零覆盖 → 补语义单测；⑤stdio 逐帧异常日志限速；⑥`_SizeCache` 失效契约补 `history`/元素原地改写；⑦超时原语等价性声明加 `timeout<=0` 限定；⑧dispatch_error 用例文案更正；⑨文档口径回写。审计独立复测：记账 16 000 步差分模糊 0 处不一致、`apply_action_batch` 0.0136 ms（≈237×）、L3 1000 并发投递守恒。

## 2026-09-09 Rust 存储后端扩展（首个异语言扩展 + 协议 P-6 登记）

> 设计/执行计划见 `docs/plans/2026-09-09-rust存储后端扩展-设计.md` 与同名执行计划。teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。

- **新 crate `storage-rust/`（仓库内正式入库）**：teage-storage-rust 以 Rust 实现 P-4 storage-stdio 存储线协议（rusqlite bundled + 单线程同步循环 + WAL，schema 与 core 兼容），编译产物 + manifest 安装到 `data2/extensions/storage_rust/`（gitignore），作为正式扩展纳入统一扩展目录树。
- **协议演进 P-6**：manifest 新增 `kind: host-component` + `slots` 字段，宿主组件 backend 与枝干扩展同样目录发现（VSCode 式）；host_components stdio-proxy 支持 `options.extension` 引用 + `options.args` 参数通道。P-4 语义条款增补⑧；P-2（异语言全链路实战验证）由本扩展兑现——e2e + 快速失败矩阵 6 项全过。
- **注意**：扩展名白名单 `^[a-z0-9_]+$` 禁连字符，故扩展名为 `storage_rust`（crate/二进制名仍为 teage-storage-rust）。

## 2026-09-08 liu2 统一扩展目录树：扩展装载运行期化（跨系统架构变更）

> 设计/实现计划见 `docs/plans/2026-09-08-统一扩展目录树-设计.md` 与同名实现计划（用户逐条定案）。teage_liu2 侧详情见 `teage_liu2/docs/plans/开发日志.md` 顶部。

- **架构**：扩展从"编译期硬编码工厂"（teage_liu2/branches/ + server/app.py import+register_factory）改造为"仓库外统一目录树 + manifest 发现 + 动态装载"——`teage_liu2/branches/` 包消亡，装配点例外消失，外壳纯 core 依赖。
- **新机制**：`core/extension_loader.py`（manifest 解析/目录扫描/importlib 动态装载/wire_extensions）；registry `set_directory_loader` 目录通道；config `core.extensions_root`（默认 data2/extensions，不入库）；/reload 重扫目录。
- **迁移**：audit / guardrails 外迁为 `data2/extensions/<name>/{manifest.yaml, main.py}`；register_factory 保留 = 测试/嵌入注入通道（设计 §2.1，8 处标注点全链路落实）。
- **验证**：tests 103 passed + 行为套件 runner 17/17 + 端到端（发现→装载→落盘→热重载→安全默认）。

---

## 2026-08-21 终极解耦架构三端审查问题收口（v1.15）

> 用户要求"按完美主义路线把三端审查发现的问题全部解决到位"。主 agent 亲自核代码 + plan-auditor（协议一致性）+ plan-verifier（落地可行性）三端交叉，发现 2 P1 + 3 P2 + 5 P3 全部落盘修复。设计文档升版 **v1.15（三端审查收口版）**。

**P1-1 会话态 extra 会话内延续接线落地（§5 L-10，原协议空头）**
- `core/pipeline.py`：ChatPipeline 增 `session_store` 参数；构建快照从 SessionStore 恢复 extra 基座（`extra_base`）、对话结束(done/error)写回最终快照 extra（`_persist_session_extra`：clear+update）
- `core/loop.py` / `core/modes.py`：ReactLoop/BareMode 增 `final_snapshot` 跟踪（各快照推进点更新），供 pipeline 结束时取终态 extra 写回
- `server/app.py`：session_store 注入 ChatPipeline
- `CORE.md` / `SUBSYSTEM-SPI.md`：会话态通道描述更新为"快照 extra 会话内延续"（消除旧的 `core.session_store.get` 直访描述）

**P1-2 PROTOCOL 版本号统一 v1.0.0**
- `VERSION`(v1.0.0) 与 README/spec/schema/cases(残 v0.1.0) 矛盾全量同步：README、9 域 spec.md 标题、9 域 schema.json `$id`、behavior-suite suite/matcher schema + 17 cases 的 `protocol_version` 统一为 v1.0.0
- `core/transport.py` `DEFAULT_PROTOCOL_VERSION` → v1.0.0；`core/supervisor.py` 默认版本引用该常量；`core/stdio.py` 注释同步

**P2-1 §18.1 扩展 storage_write 迁入 StorageWriter 队列**
- `core/transport.py`：TransportBus 增 `storage_writer`；`_handle_storage` write 分支走 `enqueue_flush`（与主对话消息落盘共享 FIFO 单写者），未配置时降级 `asyncio.to_thread`
- `server/app.py`：storage_writer 注入 TransportBus

**P2-2 §15-A7 帧长/深度阈值进协议 schema**
- `PROTOCOL/transport/transport.schema.json`：新增 `FrameLimits`（frame_max_bytes=4MiB / json_max_depth=64 const），与 transport.py 常量一致

**P3 修正**
- `events.schema.json`：DoneEvent.termination_reason 枚举 7 值（与 errors 域同步）
- `storage.schema.json`：StorageProviderResult 补 `doc` 单值字段（read 返回），doc_ids 明确为批量数组
- `server/routes.py`：补 `GET /health` 路由（与 index() endpoints 声明一致）
- 设计文档：§8 错误码前缀补 `LOOP_*`；§18.2 loop.py 行号漂移修正(→160-161/276-278)；§15-A2/A3/A6、§5 会话互斥与 invoke_llm 多角色路由的"现状"标注更新为已落地（低估现状的过时标注纠正）

---

## 2026-08-21 teage_liu2 阶段 4: 遗留项清零（完美主义收口）

> 用户要求"不要有任何遗留项,追求完美主义"。阶段 4 全部遗留项收口,验收报告更新至"遗留项清零"状态。

**进程僵死自动重建（§5 B1）**
- `StdioChannel.set_on_dead`（心跳连续失败 3 次判定僵死）+ `Supervisor._auto_rebuild`（spawn 新进程 → HookChain.replace 原位替换保注册序 → 新 adapter setup → 关旧通道;失败重试 + 指数退避防风暴,降级标记可观测;`process_restarts`/`degraded` 可观测）
- **修复 `StdioChannel.close` 自我取消缺陷**:close 内 `task.cancel()` 取消 heartbeat_task(其正在 await on_dead→_auto_rebuild→close),重建被 CancelledError 中断 → 跳过当前任务(`asyncio.current_task()`)
- **修复 close shutdown 5s 拖慢**:shutdown_timeout 参数化(默认 1s,重建场景 0.3s)
- 验证:僵尸进程触发重建 restarts=1,链替换完成

**同语言 host_port 行为套件用例**
- 新增 `17-host-port-inprocess.json`:同语言扩展经 host_port(InProcessHostPort)走 storage_*/invoke_llm/task_* 消息语义(前缀隔离/授权/防重入全生效)

**SessionLocks 严格 LRU**
- 超限时从最旧扫描淘汰**首个空闲锁**(不只看最旧一个);仅全部锁持有/等待中才临时超限(互斥必要边界)

**A8 配置端审计**
- 补配置读取端:mask_api_key 不泄漏明文 / is_masked_value / ${VAR} 占位符 / mask_sensitive_config 脱敏验证

**行为套件统一 runner CLI**
- 新增 `teage_liu2/PROTOCOL/behavior-suite/runner.py`:`python runner.py [--case N] [--verbose]`
- pipeline 类 + 协议层类用例统一执行,匹配器(§14.3:regex/length/range/type/子集/有序)内建,输出逐用例 PASS/FAIL + 通过率
- **行为套件 17/17 通过(100%)**;修正用例 01(messages length)/03(order 语义)与宿主对齐

**回归**:tests_core 85 passed;行为套件 runner 17/17;进程重建 + A8 配置端 8/8

## 2026-08-21 teage_liu2 阶段 4: 行为套件与冻结（稳定面 v1.0 冻结）

> 计划: `docs/plans/2026-08-21-宿主实现与稳定面冻结-阶段1-4-执行计划.md` 阶段 4 | 验收: `docs/plans/2026-08-21-稳定面冻结-阶段4-验收报告.md`（PASSED=46 + tests_core 85）

**evolution 版本协商（§evolution V-2）**
- `core/transport.py`: `parse_protocol_version`（semver）+ `negotiate_protocol_version`（major 拒绝 / minor 降级 / proceed / 缺失保守降级，对齐 evolution.schema.json）
- `core/stdio.py`: 握手接入协商 —— major 不匹配抛 StdioError（启动失败，可读错误）/ minor 降级记录；`channel.negotiation` 属性
- 验证: 语义对拍 11/11 + 握手集成 4/4（v2.0.0 拒绝启动 / v1.2.0 降级 / v1.0.0 proceed）

**行为套件增补至 16 用例**
- `15-evolution-negotiation.json`（版本协商黄金用例）+ `16-error-responsibility.json`（错误责任矩阵 7 终止原因全覆盖）
- Python 宿主实际路径跑 7 终止原因（normal/max_loops/user_cancel/no_tool_executor/llm_error/intercepted/tool_rejected）10/10

**性能预算复验（§18.6）**
- 首 token 前开销 N=50: **P50=0.029ms, P95=0.053ms, max=0.115ms**（预算 <10ms P95 大幅达标）

**会话并发互斥（B3 根治）+ A8 审计**
- 新增 `server/session_locks.py`: session 级 asyncio.Lock LRU 有界缓存，持有中的锁不可淘汰（保互斥）
- `server/routes.py`: /chat 与 /chat/stream 接线 session 锁（覆盖整个对话流/SSE 流）
- A8 无泄漏审计: 真实格式 API Key/内部路径样本扫描 /chat 响应、SSE 事件、错误响应、/reload、根端点、全部日志 → 无泄漏

**稳定面冻结 v1.0 + 文档回写（强制出口条件）**
- `PROTOCOL/VERSION` v0.1.0 → **v1.0.0**
- `teage_liu2/docs/CORE.md` + `docs/SUBSYSTEM-SPI.md` 回写至 11 钩子 + Snapshot+Action + 协议桥（transport/stdio/supervisor/热重载/版本协商/会话互斥/资源上限）时代

**回归**: tests_core 85 passed；阶段 4 综合验证 PASSED=46（错误矩阵 10 + 会话互斥/A8 6 + 版本协商 11 + 握手集成 4 + 最终回归 15）

**阶段 1-4 全链路完成**: 协议族 9 域 + 行为套件 16 用例 + 宿主实现全部落地，teage_liu2 core 从根基重构完成，稳定面 v1.0 冻结。

## 2026-08-21 teage_liu2 阶段 3: 协议桥与生命周期

> 计划: `docs/plans/2026-08-21-宿主实现与稳定面冻结-阶段1-4-执行计划.md` 阶段 3 | 验收: `docs/plans/2026-08-21-宿主实现-阶段3-验收报告.md`（PASSED=47）

**协议桥（transport/lifecycle 落地，§9/§18.3）**
- 新增 `core/transport.py`: TransportFrame（JSON 行协议，4 键齐整）+ 序列化边界校验（帧长 4MiB / JSON 深度 64 / 非法帧拒绝，§15-A7）+ delta 帧格式（base_revision/ops）+ TransportBus 宿主消息枢纽（扩展身份模型 + 统一 handle）
- 新增 `core/stdio.py`: stdio 跨进程通道（spawn/握手互报 protocol_version/请求响应/心跳/优雅关闭）
- 新增 `core/remote_adapter.py`: RemoteBranchAdapter（对 core 是普通扩展，11 钩子经 invoke_hook 转发 + invoke_tool 免快照轻量通道）
- 新增 `core/supervisor.py`: 扩展进程监管 + 热重载原子替换（spawn 新进程 → rebuild → 替换/回滚保旧链）

**storage_*/invoke_llm/task_* 消息**
- storage_* 消息通道 + kind 前缀隔离（§15-A3: 扩展只能读写 `{extension_name}.` 前缀；跨前缀/非法 kind 拒绝）；storage_provider 注入改走消息通道（storage S-2，同语言经 host_port 亦走消息）
- `LLMClient.chat_role` 多角色路由（main/consolidation，未配置降级 main）；invoke_llm 协议级防重入（直调不进钩子链，§15-A5）+ 并发信号量硬边界（§15-A6）
- TaskRegistry.register_task/cancel_task（宿主登记扩展侧任务，T-4）

**L3 观测通道 + 生命周期**
- L3BatchSink 批处理旁路（50ms/64 条先到触发 + 每观测扩展有界队列 1024 + 丢弃计数随心跳上报）；pipeline 事件流转处 route_l3 接线
- registry.setup_all 支持 host_builder（host 纯数据声明，kind 前缀按扩展名）；registry.rebuild 热重载（失败回滚保旧链）；POST /reload 端点
- 行为套件增补 5 个黄金用例（10-transport-frame / 11-storage-prefix-transport / 12-invoke-llm / 13-l3-observe / 14-lifecycle-reload），schema 校验通过

**回归**: tests_core 85 passed；综合验证 PASSED=47 FAILURES=0（含异语言 stdio 扩展全钩子/前缀隔离/防重入/并发上限/L3 投递/热重载回滚）

---

开发日志 —— 本次变更摘要（历史）

新功能

- 通用 Workflow 引擎：调度从单模板升级为通用多步引擎，支持 retry/fallback/skip/abort 四种错误策略、拓扑排序、条件跳过。6 个新模块：engine/spec/adapter/retry/step_executor/step_trace/validator
- 画像信号池：三层信号摘取统一入口，信号达阈值 7 次才写入画像。解决"一次提及即写入"导致的画像噪音。持久化到 data/profile_signal_pool.json
- 监控 + 调度独立页面：调度从 chat 内嵌面板拆为独立 /scheduler 页；新增实时监控页 /monitor（Canvas 直方图、健康检查、审计日志）
- Metrics 持久化：监控指标增量持久化到 SQLite，每日合并，支持趋势查询
- 统一工具错误处理：17 种结构化异常替代字符串错误，按 pre_execution/execution/protocol 三阶段分流处理
- B站 Skill：热门视频、搜索、视频详情、UP主信息、分区排行榜

优化

- ReactLoop 重构：弱引用防 GC 泄漏；下线过敏感卡死检测规则；中断提示跨轮注入
- 前端拆分：删除 chat-schedule.js（895 行），拆为独立调度页 + 轻量徽章轮询
- 审批卡片增强：按工具类别（文件/Skill/MCP/Shell/记忆）差异化渲染
- TodoList 持久化：从纯内存升级为磁盘原子写 + 懒加载恢复
- 会话标题：cron 会话标题取自 schedule.name，用户会话首轮异步生成
- 移动端适配：侧栏遮罩层、响应式 CSS
- 安全：新增 read_paths 黑白名单，保护 src/、config.yaml、.git/ 等敏感路径

Bug 修复

- 定时清理遗漏 todo/ 子目录（已补）
- 审批缺少拒绝原因字段（已加）
- WorkflowResult 异常路径返回 None 导致空指针（已修复）
- 调度 API 缺少 workflow 字段（已补）

测试

- 新增 24 个测试文件，修改 21 个
- 核心覆盖：Workflow 引擎全套、信号池、MetricsStore、ToolError、Todo 持久化、监控页面

---

## 2026-07-30 N-Worker 协作修复（阶段 0/1/2 + 阶段 5 验证 + 阶段 3 研究）

> 配套文档：
> - [docs/plans/2026-07-30-N-worker协作问题修复与可扩展架构探讨.md](docs/plans/2026-07-30-N-worker协作问题修复与可扩展架构探讨.md)
> - [docs/plans/2026-07-30-N-worker修复执行清单.md](docs/plans/2026-07-30-N-worker修复执行清单.md)
> - [docs/plans/2026-07-31-嵌入式Director设计.md](docs/plans/2026-07-31-嵌入式Director设计.md)
> - [docs/plans/2026-07-31-跨实例Director心跳查询设计.md](docs/plans/2026-07-31-跨实例Director心跳查询设计.md)
> - [docs/plans/2026-07-31-N-Worker扩展性验证方案.md](docs/plans/2026-07-31-N-Worker扩展性验证方案.md)

### 用户决策
1. 共享 Blackboard 方案：**Y A2A 同步**
2. Director 部署模式：**A 嵌入式 + 选举**
3. N 扩展性目标：**中规模 N≤10**
4. 实施优先级：**渐进**
5. 外部依赖：**暂不引入 Redis/etcd**
6. 部署形态：**同机/多机都可能**

### 阶段 0：现场止血（无代码改动）
- 杀掉 worker2 僵尸 director 子进程 PID 6420
- 删除 worker2 锁文件 `data/blackboard_a2a_2/locks/{director,audit}.lock`
- 重置 worker2 agent_card：`status: offline→active`、`trust_score: 89→100`
- 核查 worker1 audit.jsonl 尾部：无 trust_score_update 雪崩，判为健康（PID 12120 未处理；但 API 返回 state=degraded 待代码修复改善）
- 两实例 director/status 端点 200 OK

### 阶段 1：根因修复（任务 1.1–1.5）

#### 任务 1.1 Election 接入（类别 A1）
- 文件：`teage_liu/multiagent/worker_adapter.py`
- 改动：`_check_director_health` 检测到 `age > timeout` 时优先调 `Election.run()`，胜出则启动新 director（暂用 LocalDirectorManager.start()），失败则等待远程端点接管；旧"直接进入自治"逻辑保留为 fallback
- `__init__` 新增 `self._director_manager = None`（默认 None，由外部注入）
- 改动用 `if self._config.get("director_v2_enabled", True):` 包住便于回滚
- 测试：`tests/multiagent/test_election_integration.py`（3 个测试，全通过）

#### 任务 1.2 跨进程文件锁（类别 B4 + A3 扩展）
- 文件：`teage_liu/multiagent/file_lock.py`（新增 FileLock 类）
  - 基于 `portalocker.Lock`，async 上下文管理器，timeout 默认 5s
  - 锁文件路径 `{target}.lock`（与目标同目录，区别于现有 `locks/{name}.lock` 进程内 CAS 锁）
- 文件：`teage_liu/multiagent/agent_registry.py`
  - `update_heartbeat` / `update_agent_status` 用 `async with FileLock(agent_file):` 包住读-改-写
- 文件：`teage_liu/multiagent/director_engine.py`
  - `_update_trust_score` 用 `if self._config.get("director_v2_enabled", True):` 包住 FileLock 调用，旧路径作 fallback
  - 防死锁：FileLock 仅包住 trust_score 直接写，不嵌套 update_agent_status 调用
- 测试：`tests/multiagent/test_file_lock.py`（2 个测试，全通过）

#### 任务 1.3 director_engine._run_loop 异常容错（类别 B1）
- 文件：`teage_liu/multiagent/director_engine.py`
- 改动：`_run_loop` 每个子任务独立 try/except + 失败计数；连续失败 10 次触发 director 重启；成功一轮仅衰减本轮未失败任务的计数（修正原骨架"每轮衰减所有任务"的缺陷——否则连续失败计数永远不累积）
- `__init__` 新增 `self._task_failure_counts: dict[str, int] = {}`，keys 在 `_run_loop` 启动时按 tasks 列表初始化
- 测试：`tests/multiagent/test_director_engine.py`（2 个测试，全通过）

#### 任务 1.4 director_cli 僵尸修复（类别 B2）
- 文件：`teage_liu/multiagent/director_cli.py`
- 改动：`main_async` 新增 `_watch_loop_task` 协程监听 `director._loop_task` 退出，触发 `stop_event.set()`；watcher 在 `director.start()` 之后创建避免 AttributeError
- 用 `if config.get("director_v2_enabled", True):` 包住新逻辑
- 测试：`tests/multiagent/test_director_cli.py`（2 个测试，全通过）

#### 任务 1.5 LocalDirectorManager 自动重启（类别 B3）
- 文件：`teage_liu/multiagent/director_manager.py`
- `__init__` 新增 6 个 watchdog 字段：`_watchdog_task / _watchdog_running / _restart_count / _max_restarts(=3) / _restart_window(=300s) / _restart_times`
- 新增 `_watchdog` 协程：每 10s 检查子进程，崩溃自动重启；窗口内最多 3 次重启；崩溃时调 `_cleanup_stale_state` 清理锁文件
- 新增 `_cleanup_stale_state`：删除 `locks/director.lock` + `locks/audit.lock` + `director.pid`
- `stop()` 方法开头先关闭 watchdog（防止 stop 期间触发自动重启）
- 测试：`tests/multiagent/test_director_manager.py`（3 个测试，全通过）

### 阶段 2：性能优化（任务 2.1–2.3）

#### 任务 2.1 接入 WatchdogWatcher（类别 A2）
- 文件：`teage_liu/multiagent/worker_adapter.py`
  - `__init__` 新增 `self._collab_interrupt = asyncio.Event()`
  - 新增 `_on_collab_file_changed(event)`：watchdog 文件变更时 `set()`，仅对 collaboration.md / agent_card 触发避免噪声
  - `_collab_poll_loop` 改为 `await asyncio.wait_for(self._collab_interrupt.wait(), timeout=interval)`，事件触发或超时都执行 `_poll_collab_once`，延迟从 0-2s 降到 < 100ms
- 文件：`teage_liu/lifespan.py`
  - watchdog_watcher 注册用延迟绑定模式（`worker_adapter_holder: list = []`），在 multiagent_adapter 实例化后填充 holder，激活 callback
  - 用 `director_v2_enabled` 开关，关闭时保留旧 lambda
- 测试：`tests/multiagent/test_worker_adapter.py`（3 个新测试，全通过）

#### 任务 2.2 A2A 重试参数激进调整（类别 B5）
- 文件：`teage_liu/multiagent/a2a_client.py`
  - `__init__`：timeout 10→3，retry_count 2→1
  - `call_method` 重试 sleep 改为指数退避：`min(0.5 * (2 ** attempt), 2.0)`（0.5 → 1.0 → 2.0，上限 2.0s）
- 测试：`tests/multiagent/test_a2a_client.py`（2 个测试，全通过）

#### 任务 2.3 LLM 调用串行化（类别 B6）
- 文件：`teage_liu/multiagent/worker_adapter.py`
  - `__init__` 新增 `self._llm_lock = asyncio.Lock()`
  - `_trigger_urgent_llm` 用 `async with self._llm_lock:` 包住 `orchestrator.chat` 调用
  - 异常 catch 不阻塞 `_collab_poll_loop`
  - 用 `if self._config.get("director_v2_enabled", True):` 包住新逻辑
- 测试：`tests/multiagent/test_worker_adapter.py`（1 个新测试，通过）

### 阶段 5：统一验证

#### 单元测试
- 全量 `tests/multiagent/`：437 passed / 4 failed / 9 skipped
  - 4 failed 全部为**预先存在 WIP 失败**（untracked 测试文件依赖未完成的 `append_collab_message` 函数），经 git stash 验证与阶段 1+2 改动无关
  - 失败用例：`test_broadcast_dedup_on_duplicate_submission` / `test_worker_polls_request` / `test_already_processed_normal_request_skipped` / `test_normal_request_marked_processed_after_enqueue`
- 全量 `tests/api/test_multiagent_routes.py`：27 passed / 0 failed
- 新增测试总数：17 个（任务 1.1: 3 + 1.2: 2 + 1.3: 2 + 1.4: 2 + 1.5: 3 + 2.1: 3 + 2.2: 2）

#### 集成测试脚本（用户长跑验证用）
- 新增：`tests/integration/test_n_worker_collab.py`
  - 3 个测试：`test_director_auto_restart_on_kill` / `test_audit_growth_rate_under_threshold` / `test_e2e_collab_message_latency`
  - 默认 skip，需 `--run-integration` 启用
- 新增：`tests/conftest.py` 注册 `integration` marker + `--run-integration` 选项

#### 性能采集脚本
- 新增：`scripts/collect_perf_metrics.py`
  - 采集 a2a_p50/p95/p99/max、audit_growth_per_min、director_unreachable_count、sample_success_rate
  - 输出 JSON 含阈值判定 + `all_pass` 总判定
  - 用法：`python scripts/collect_perf_metrics.py --duration 1800 --output metrics.json`

#### 用户长跑验证步骤
```powershell
cd e:\Java\webser\web_app\webme\teage-liu
.\start_dual_workers.ps1
python scripts\collect_perf_metrics.py --duration 1800 --output metrics.json
python -m pytest tests\integration\test_n_worker_collab.py --run-integration -v
type metrics.json | findstr /C:"all_pass"
```

### 阶段 3：架构研究文档（待用户决策后启动实施）

#### 嵌入式 Director 设计（[docs/plans/2026-07-31-嵌入式Director设计.md](docs/plans/2026-07-31-嵌入式Director设计.md)）
- 设计 `EmbeddedDirector` 类（standby→active→standby 状态机 + asyncio.Lock 串行化 activate/deactivate）
- 与 `DirectorEngine` 组合（不继承，复用阶段 1+2 成果）
- 与 `WorkerAdapter` 协作（`_director_manager` 字段从 None 改为 EmbeddedDirector 实例）
- 新文件 `teage_liu/multiagent/embedded_director.py` + lifespan DI 改造 + multiagent_routes 改造
- 待用户决策 6 项（D1 默认启用 / D2 保留 LDM / D3 status 同步策略 / D4 组合关系 / D5 lease 续约 / D6 faulted 自愈）

#### 跨实例 Director 心跳查询设计（[docs/plans/2026-07-31-跨实例Director心跳查询设计.md](docs/plans/2026-07-31-跨实例Director心跳查询设计.md)）
- A2A 新增 JSON-RPC 方法 `read_director_md`（注册到 `A2AServer._handlers`）
- 三阶段查询算法：本地判定 → 远程并发查询 → Election 兜底
- 新增 `healthy_remote` 健康级别（远程有健康 director 时跟随，不进自治）
- 防风暴：`_remote_query_lock` 限每实例 1 个在飞查询；防无限递归：`_recheck_depth` 上限 2
- 待用户决策 4 项（D-1 查询频率 / D-2 端点列表来源 / D-3 重试策略 / D-4 healthy_remote 是否同步）

#### N-Worker 扩展性验证方案（[docs/plans/2026-07-31-N-Worker扩展性验证方案.md](docs/plans/2026-07-31-N-Worker扩展性验证方案.md)）
- 验证矩阵：N=2（已通过）/ N=3（P0）/ N=5（P1）/ N=10（P2）/ 跨机 N=3（P2）
- 5 个验证场景：选举 failover / 100 条广播无丢失 / LLM 限流 / A2A 调用风暴 / 跨机网络抖动
- 性能瓶颈预测表：LLM 风暴（N>3）/ A2A HTTP（N>5）/ 文件锁争用（N>3 同机）/ audit I/O（N>5）/ watchdog fd 耗尽（N>10）/ Director 主循环（N>8）
- 部署模板：3 份 config 模板 + `generate_n_configs.ps1` + `start_n_workers.ps1/.sh`
- 待用户决策 6 项

### 阶段 1 遗留潜在 bug（H2 研究发现，已纳入阶段 3 修复范围）
1. **GAP-1**：`a2a_server.py` 未注册 `read_director_md` handler → Election 远程候选收集实际失效（待 3.R2.1 修复）
2. **GAP-2**：Election 期望远程返回 `status.director` 子结构，但实际 `read_director_md` 应返回 director.md 格式 → 字段名不匹配（待 3.R2.5 修复）
3. **GAP-4**：`_check_director_health` 选举失败后递归调用自身无深度上限 → 远程持续超时时栈风险（待 3.R2.4 修复）

### 回滚预案
1. **代码级**：所有改动用 `if config.get("director_v2_enabled", True):` 包住，配置一键回退
2. **配置级**：保留旧 config 模板，可恢复 10s timeout / 3 retry
3. **进程级**：`auto_restart_enabled` 配置开关，可关闭回归手动管理
4. **架构级**：保留 `LocalDirectorManager` 作为永久 fallback（嵌入式 Director 失败时降级）

### 改动文件清单
- 代码改动（8 个文件）：
  - `teage_liu/multiagent/worker_adapter.py`（__init__ + _check_director_health + _on_collab_file_changed + _collab_poll_loop + _trigger_urgent_llm）
  - `teage_liu/multiagent/file_lock.py`（新增 FileLock 类）
  - `teage_liu/multiagent/agent_registry.py`（update_heartbeat + update_agent_status 加锁）
  - `teage_liu/multiagent/director_engine.py`（__init__ + _run_loop + _update_trust_score）
  - `teage_liu/multiagent/director_cli.py`（main_async watcher）
  - `teage_liu/multiagent/director_manager.py`（__init__ + _watchdog + _cleanup_stale_state + stop）
  - `teage_liu/lifespan.py`（watchdog callback 延迟绑定）
  - `teage_liu/multiagent/a2a_client.py`（__init__ + call_method 重试参数）
- 数据文件改动（1 个文件）：
  - `data/blackboard_a2a_2/agents/teagent-liu-2.md`（status + trust_score）
- 测试文件新增/扩展（7 个文件）：
  - `tests/multiagent/test_election_integration.py`（新建）
  - `tests/multiagent/test_file_lock.py`（扩展）
  - `tests/multiagent/test_director_engine.py`（扩展）
  - `tests/multiagent/test_director_cli.py`（新建）
  - `tests/multiagent/test_director_manager.py`（新建）
  - `tests/multiagent/test_worker_adapter.py`（扩展）
  - `tests/multiagent/test_a2a_client.py`（扩展）
  - `tests/conftest.py`（追加 integration marker）
- 集成测试（1 个文件）：
  - `tests/integration/__init__.py` + `tests/integration/test_n_worker_collab.py`（新建）
- 性能采集脚本（1 个文件）：
  - `scripts/collect_perf_metrics.py`（新建）
- 研究文档（3 份，共 3415 行）：
  - `docs/plans/2026-07-31-嵌入式Director设计.md`（1394 行）
  - `docs/plans/2026-07-31-跨实例Director心跳查询设计.md`（1044 行）
  - `docs/plans/2026-07-31-N-Worker扩展性验证方案.md`（977 行）
