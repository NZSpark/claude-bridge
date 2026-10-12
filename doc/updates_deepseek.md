# ClaudeBridge 全项目分析与改进建议（DeepSeek 版 · 汇总与进度）

> 2026-10-12。本文件把三份独立评审（`doc/update_claude.md` / `doc/update_chatgpt.md` / `doc/update_deepseek.md`）的结论收敛成**一份可执行的改进清单 + 当前实现进度台账**。
>
> 与 `doc/update_deepseek.md` 的关系：那份是**评审正文**（问题论证）；本文件是**落地台账**（每条结论对应到任务、当前状态、验证方式）。任务编号沿用 `doc/tasks_deepseek.md`（D 系列），并标注与 `doc/tasks_claude.md`（T 系列）的对应。
>
> 基线：本文件撰写时 HEAD 为 `f8758a7`。静态检查 `.venv/bin/ruff check .` 全绿；`pytest -q` 全绿（2 skipped，e2e 未开启）。真实浏览器 / 真实客户端验证始终独立于离线测试。

## 0. 结论速览

分层干净，且近期每个真实事故都做了「归因 + 日志 + 回归测试」。**不建议大重构**。当前主要风险不是缺功能，而是「已写下的状态与生命周期契约没有闭合」，按故障影响排序为三类：

1. **契约漂移**：配置项、`tool_choice`、保活常量、若干超时 / 开关「定义了却从未接线」（见 §4 死代码复核）。
2. **并发 / 取消闭环**：驱动启动、页面池、管理命令、取消语义——**大部分已在本轮修复**（D2.1/D2.2/D2.3/D2.5 已落地），剩余 D2.4 / D2.6。
3. **复用网页会话之后的一致性**：增量模式解耦了「客户端历史」与「网页端历史」两个事实来源，指纹核对目前只**观测**（D1.5 步骤 A 已上线），尚未切换到「重播种」行为。

## 1. 应予保留的正确设计（三份评审一致）

- 结束 / 停滞 / 提交未生效判定用**时长**而非轮询次数，纯函数 + `IdleClock` 可穷举测试。
- 新回复识别用**两把独立钥匙**（条数 / 结构信号），对虚拟化回收保持保守兼容。
- 工具调用**默认缓冲后解析**，只有通过校验、属于声明集合的调用才外发；不执行模型生成的命令。
- 额度弹窗识别 / 关闭只点关闭类控件，绝不点升级 / 付费；错误归因 `usage_limit_reached`（429）。
- `/diagnostics` `/metrics` 不返回正文；绑定 URL 强制 HTTPS + 主机白名单 + 会话路径。
- 浏览器依赖惰性导入，离线测试用替身驱动真实 DOM 适配器与状态机。
- Responses API 默认关闭，未验证子集不伪装成完整支持。
- 配置契约由 `.env.example` / `config.py` / README 三处 + `test_config_drift.py` 约束。

## 2. 已落地（本轮及近期提交，已复核代码）

| 任务 | 内容 | 复核落点 | 提交 |
| --- | --- | --- | --- |
| D1.1 / T1.1 | 工具说明判重只在 system/user 行首 | `prompting.py` | `1fd4875` |
| D1.2 / T1.6 | 绝对超时优先延长，硬上限才截断并标 `length` | `end_detection.py:164-169` | `26c146d` |
| D1.3 / T1.2 | 管理操作统一取桶锁（`service.admin_*`） | `service.py:99-113` | `b75059a` |
| D1.4 / T1.3b | `reset` 保留 `linked_url` / `pending_reseed` | `store.py:120-135` | `b75059a` |
| D1.5(A) / T1.4a | 已同步历史指纹，**只观测**（`history_fingerprint`） | `lifecycle.py:88,190-206` | `3908a3c` |
| D2.1 / T2.1 | 驱动启动共享锁 + 页面池按桶锁 | `driver.py:169,188`; `page_pool.py:46` | `3861424` |
| D2.2 / T2.2 | `reap_idle` / `mark_busy` 接线（lifespan 周期任务） | `server.py:138,156-169`; `service.py` | `ba8ecf1` |
| D2.3 / T2.5 | `GenerationCancelledError`，取消不计超时 / 不写 `last_error`；`_fill` 只捕 `Exception` | `errors.py:163`; `generator.py:257`; `service.py:179` | `da78a10` |
| D2.5 / T2.7 | 提交前处理「页面仍在生成」（只点确认过的停止控件） | `dom_adapter.settle_before_submit` | `fbc9a7e` |
| D3.1 / T3.1 | `InternalError`(500)，内部 bug 不再伪装 503 | `errors.py:173`; `server.py:186-188,439` | `da78a10` |
| D3.3 / T3.3 | 额度归因收窄：仅在「现在仍有弹窗」或「本轮无正文」时报额度 | `generator.py` 失败归因段 | `f8758a7` |
| D3.2 / T3.2 | 去掉叠加重试：`_fill` 只对可恢复写入失败重试，不可恢复错误（输入区缺失 / 登录失效 / 提交已耗尽）快速冒泡 | `generator.py` `_UNRECOVERABLE_FILL_ERRORS` | 本次 |
| D2.4 / T2.4 | 非流式 / Responses 路径的断连检测：后台跑 `svc.run` + 轮询 `is_disconnected`，断开即取消、快速释放桶锁 | `server.py` `_run_with_disconnect_guard` | 本次 |
| D3.7 / T3.7 | 会话 URL 精确比较：解析 URL 按主机 + 路径精确比对，修掉「ID 只差末位被误判同一条」 | `lifecycle.py` `_conversation_key` | 本次 |

> **D3.3 落地细节（本次）**：条件收窄为 `if quota_now or (limit_modal_seen and not has_text_now):`。回归测试 `test_quota_modal_seen_at_submit_does_not_mask_a_real_stall` 构造「先自述在生成、随后停止信号且正文不再增长」的停滞；因默认 `STALL_S(30) > SETTLE_S(2.5)`，有正文的停滞分支不可达，测试显式把 `STALL_S` 降到 `1.0` 让停滞先于落定触发，断言抛 `GenerationStalledError` 且错误码 ≠ `usage_limit_reached`。

## 3. 待办（按依赖与故障影响排序）

### P0（收尾正确性）
- **D1.5(B) / T1.4b** — 指纹观测无误判后，切换为「对不上 → 换新对话并完整重播」。**风险最高**：先看日志确认零误判再切；切换须有回归测试保护。关注「相同提示词连续两轮 + 虚拟化」时纯文本指纹无法区分，优先找稳定 DOM 消息 ID（`data-testid` / `data-message-id`），找不到时**宁可重播也不要误认**。
- ~~**D2.4 / T2.4** — 非流式与 Responses 路径的客户端断连检测。~~ **已完成**：新增 `_run_with_disconnect_guard`——后台跑 `svc.run(cancel=...)`，并发轮询 `request.is_disconnected()`，断开即置取消标志，生成循环抛 `GenerationCancelledError`（499），桶锁在 `service.run` 的 `finally` 立即释放。**踩坑记录**：`finally` 里**不能** `await` 被取消的 watcher——它会卡在 `is_disconnected()` 内部不可取消的 `receive()`，把整条非流式路由拖死（实测该测试文件挂起 >8 分钟）；只发 `cancel()`、不 await。
- **D2.6 / T2.6** — 提交**之后**失败的桶标记「状态不确定」，交给 D1.5 决定是否重播，避免重复发同一增量。依赖 D1.5。

### P1（错误语义与配置）
- ~~**D3.2 / T3.2** — 去掉叠加重试边界。~~ **已完成**：`generator._fill` 新增 `_UNRECOVERABLE_FILL_ERRORS`（`ComposerNotFoundError` / `AuthenticationRequiredError` / `ChallengeRequiredError` / `PromptSubmissionError`），命中即冒泡不重试；其余瞬时失败仍按 `MAX_UPSTREAM_RETRIES` 重试。回归测试三条（不可恢复不重试、登录失效不重试、可恢复重试次数）。
- **D3.4 / T3.4** — 失败类别指标：为 `usage_limit_reached` / `prompt_submission_failed` / `reply_not_found` / `context_limit_reached` / 取消各加计数（`metrics.py` 目前只有 `request_cancelled_total`）。
- **D3.5 / T3.5** — 配置路径与列表解析：`SESSION_FILE` / `USER_DATA_DIR` / `TASK_FILE_DIR` / `OUTPUT_DIR` 相对路径以 `PROJECT_ROOT` 解析；选择器配置**只按 `||` 切分**（避免 `:is(a,b)` 被切坏）；`LINK_ALLOWED_HOSTS=*` 启动强告警。
- **D3.6 / T3.6** — 启动校验与摘要日志：旧键（`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS`）告警；校验 `BUCKET_LOCK_TIMEOUT_S >= CLAUDE_TIMEOUT`、`SETTLE_NO_SIGNAL_S >= SETTLE_S`、`STALL_S > SUBMIT_LOST_S`；生效值写摘要日志（不含密钥）。
- ~~**D3.7 / T3.7** — 会话比较与绑定。~~ **已完成（URL 精确比较部分）**：新增 `_conversation_key(url)`，`_same_conversation` 与 `lifecycle.link` 落点校验都改为解析 URL、按主机 + 规范化路径**精确**比对（忽略查询串 / 片段 / 末尾斜杠，其余逐字相同）。修掉了 `startswith(target[:len(target)-1])` 把「会话 ID 只差末位」误判成同一条的 bug。**未做**：绑定桶触顶后换新会话 / 明确报错（仍待办）。

### P2（API、提示词、日志、工程化）
- **D4.1 / T4.1** — Responses 真流式：复用 chat 路径的「队列 + 保活 + 断连 + 错误事件」骨架（见 §4 的 `_KEEPALIVE`）。
- **D4.2 / T4.2** — `store_thread` 保留 `tool_calls` / `tool_call_id`，保证 `previous_response_id` 续接工具循环不断链。
- **D4.3 / T4.3** — 补齐 Responses 事件集并用真实客户端（Codex CLI 等）探测，结果记入 `doc/tasks.md`。
- **D4.4 / T4.4** — 流式细节：网页重写回复时加 `stream_rewritten_total`；`on_delta` 仅在内容变化时回调；支持 `stream_options.include_usage`；已见回复后读取节点短暂失败时保留上一次文本。
- **D4.5 / T4.5** — 提示词体量：增量模式只在重播种 / 工具清单变化 / 每 N 轮重发工具说明；调大 `SYSTEM_MAX_CHARS`；`SEED_ENV_NOTE` 默认留空。依赖 D1.1。
- **D4.6** — `tool_choice` 生效（DeepSeek 版独有）：至少 `none` 时不注入工具说明；`required` / 指定函数在提示里点名。
- **D5.x / T5.x** — 日志上下文与脱敏、`X-Request-Id` 白名单、解析健壮性、落盘一致。
- **D6.x / T6.x** — 死代码清理（见 §4）。
- **D7.x / T7.x** — 工程化：CI、类型检查、README 校正等。

## 4. 「写了没用上」复核（当前 HEAD 实测）

> 复核方法：`grep -rln <符号> claude_web/`，排除 `__pycache__`。

| 符号 | 仅出现在 | 结论 |
| --- | --- | --- |
| `tool_choice` | `models.py`、`api/chat_adapter.py`（仅透传） | **未接线**：`prompting.py` / `toolcalls.py` 完全未用。D4.6 处理。 |
| `STREAM_KEEPALIVE_S` | `config.py:253` | **未接线**：`server.py:404` 写死 `_KEEPALIVE = 10.0`，`server.py:453` 用它。D4.1 处理。 |
| `READY_TIMEOUT_MS` | `config.py` | **未接线**：无任何调用点。 |
| `FILL_TIMEOUT_MS` | `config.py` | **未接线**。 |
| `NEW_SESSION_ON_START` | `config.py` | **未接线**。 |
| `SAVE_DEBUG_ARTIFACTS` | `config.py` | **未接线**。 |
| `reap_idle` / `mark_busy` | `driver.py` / `page_pool.py` / `server.py` / `service.py` | **已接线**（D2.2 本轮修复），评审旧结论**已失效**。 |
| `TaskStore.load` | `tasks.py:55`，被 `api/responses_adapter.py:147` 调用 | **纠正 DeepSeek 评审**：它**不是**死代码，`responses_adapter` 在用。 |

> 结论：D6 的死代码清单应剔除 `reap_idle` / `mark_busy` / `TaskStore.load`；保留 `tool_choice`、`STREAM_KEEPALIVE_S`（+`_KEEPALIVE` 重复定义）、`READY_TIMEOUT_MS`、`FILL_TIMEOUT_MS`、`NEW_SESSION_ON_START`、`SAVE_DEBUG_ARTIFACTS`。

## 5. 三份评审的取舍

- **工具说明判重（D1.1）**：DeepSeek 版主张「状态优先」（记录已发工具清单摘要 + 是否重播种），Claude 版是「短期兜底」（限定 system/user 行首）。**已按短期方案落地**；若后续仍有误判，再上状态方案。
- **超时截断（D1.2）**：三份一致「优先延长、硬上限才截断」。已落地，截断用 `finish_reason=length` 兼容。
- **死代码（§4）**：以**当前 HEAD 实测**为准，不照抄任何一份评审的 grep 结论（`TaskStore.load` 即为一例反例）。

## 6. 验证约定（持续有效）

- 每个任务单独提交；有行为变化的都配回归测试。
- 完成后 `.venv/bin/ruff check .` 与 `pytest -q` 必须全绿（e2e 需显式开启）。
- 真实浏览器 / 真实客户端验证独立记录，不以 FakePage 测试替代。
- 虚拟时钟测试断言「客户端等待了多少秒」，不依赖真实耗时。
