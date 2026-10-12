# ClaudeBridge 任务分解（来源：doc/update_claude.md）

> 2026-10-12。每个任务都标了来源章节、涉及文件、验收标准。
> **范围排除**：鉴权相关不在本次范围，不拆任务。包括 3.3（端点鉴权）、3.4 中 `RESET_TOKEN` / `ALLOW_CHAT_ADMIN_COMMANDS` 的部分，以及「鉴权矩阵测试」。3.4 只保留「管理操作取桶锁」。
> 约定：每个任务单独提交；有行为变化的都要配回归测试；完成后 `pytest` 与 `ruff check` 必须全绿。

## 阶段 0：文档校正

- [ ] **T0.1** 校正 `doc/update_claude.md`（0.5h）
  - 删除 3.1 里「本次会话里确实不再带这段说明」那句（未验证，大概率不准确）。
  - 在文档开头注明鉴权类条目（3.3、3.4 的令牌部分、第 6 节鉴权矩阵）已划出范围，不做。
  - 验收：文档不再含未验证的断言，范围声明清楚。

## 阶段 1：P0 正确性（先做，改动小、影响大）

- [x] **T1.1** 工具说明判重不被内容抑制（来源 3.1）
  - 文件：`claude_web/prompting.py`、`tests/test_prompting.py`
  - 做法：`_mentions_tool_instructions` 只在 system 与 user 角色里判重，且标题必须位于行首；tool、assistant 消息不参与。
  - 验收：新增用例，tool 结果里含 `[Tool Calling Instructions]` 时仍然注入说明；system 里已内联时仍不重复注入。

- [x] **T1.2** 绝对超时优先延长而不是截断（来源 3.2）
  - 文件：`claude_web/end_detection.py`、`claude_web/completion/generator.py`、`tests/test_end_detection.py`、`tests/test_generation_latency.py`
  - 做法：`past_deadline` 时若 `generating is True` 且还有延长额度，先 `extend`；延长额度用尽仍在生成，则返回已有正文并在 notes 标明被截断，`finish_reason` 用 `length`。
  - 验收：虚拟时钟用例覆盖「到期时正在生成且已有正文」「延长额度用尽」「到期时已静止」三种情形。

- [x] **T1.3** 管理操作取桶锁（来源 3.4 的锁部分）
  - 文件：`claude_web/service.py`、`claude_web/bridge_commands.py`、`claude_web/server.py`、`tests/test_bridge_commands.py`、`tests/test_sessions.py`
  - 做法：新增 `service.admin(bucket)`：取桶锁（短超时，失败回 `session_busy`）后再执行 `reset / link / unlink / modal`；HTTP 的 `/session/*` 与聊天命令（chat 与 responses 两条路径）都走它。
  - 验收：桶正在生成时 reset / link 返回 busy，不会关掉页面；空闲时行为不变。

- [x] **T1.3b** `store.reset()` 不再悄悄清掉 `linked_url`（来源 4.7）
  - 文件：`claude_web/session/store.py`、`claude_web/completion/lifecycle.py`、`claude_web/bridge_commands.py`
  - 做法：reset 保留绑定；要解绑用 unlink。回复文案如实说明。
  - 验收：绑定桶 reset 后 `status` 仍显示绑定。

- [ ] **T1.4** 复用会话的历史指纹（来源 3.5，分两步）
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/session/schema.py`、`claude_web/service.py`、`tests/test_conversation_reuse.py`
  - 步骤 A：成功后记录「已同步历史」指纹（首条 user 摘要、消息条数、末条 assistant 摘要），下一轮前缀对不上时只打日志与 notes，不改行为。
  - 步骤 B：对不上或系统提示变化时，改为新对话并完整重播。
  - 验收：回滚、重新生成、换任务、压缩历史四种场景各一条用例；README 说明多任务应用 `X-Claude-Session` 区分桶。

## 阶段 2：并发与资源

- [x] **T2.1** 驱动启动与页面池创建加锁（来源 4.1）
  - 文件：`claude_web/driver.py`、`claude_web/page_pool.py`、`tests/test_browser.py`
  - 做法：`start()` 加 `asyncio.Lock`；`PagePool.acquire` 按桶加锁，驱逐也在锁内；`readiness()` 对忙碌桶返回缓存结果。
  - 验收：并发两个桶同时首次请求只启动一次浏览器；同一桶并发 acquire 只建一个页面，页面数不超过 `MAX_PAGE_POOL`。

- [x] **T2.2** 接上空闲回收与忙碌标记（来源 4.2）
  - 文件：`claude_web/server.py`（lifespan）、`claude_web/service.py`、`claude_web/page_pool.py`
  - 做法：lifespan 起周期回收任务并在关闭时取消；`_run_locked` 前后 `mark_busy(True/False)`（finally 里复位）；满池且全部忙碌时返回 busy，而不是驱逐他人页面。
  - 验收：空闲页面超过 `BUCKET_IDLE_TTL_S` 被关；生成中的页面永不被 LRU 驱逐。

- [x] **T2.3** 取消语义（来源 4.3）
  - 文件：`claude_web/errors.py`、`claude_web/completion/generator.py`、`claude_web/service.py`、`tests/test_generation_latency.py`
  - 做法：新增 `GenerationCancelledError`；`_record_failure` 对它单独计数、不写会话 `last_error`；`_fill` 只捕获 `Exception`，让 `CancelledError` 冒泡。
  - 验收：取消不计入超时与失败统计，不污染 `last_error`。

- [x] **T2.4** 非流式与 Responses 路径的断连检测（来源 4.3）
  - 文件：`claude_web/server.py`
  - 做法：非流式请求在后台任务里跑，断连时置取消标志（与流式路径同一机制）。
  - 验收：客户端断开后桶锁在一个轮询周期内释放。

- [x] **T2.5** 提交前处理「页面仍在生成」（来源 4.3）
  - 文件：`claude_web/completion/generator.py`、`claude_web/browser/dom_adapter.py`
  - 做法：提交前若 `generating_state` 为真，先等一小段；仍未结束就只点停止控件，再提交。
  - 验收：上一轮取消后立即发下一轮，不再误报「提交未生效」。

- [ ] **T2.6** 失败后桶状态不确定（来源 4.3）
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/service.py`
  - 做法：提交之后失败的桶标记为「不确定」，下一轮交给 T1.4 的指纹决定是否重播，避免网页端出现重复提示词。
  - 验收：提交后失败再重试，不会把同一条增量重复发进网页会话。
  - 依赖：T1.4。

## 阶段 3：错误语义与配置

- [x] **T3.1** 内部错误码（来源 4.6）：新增 `InternalError`（500），`_unhandled_handler` 与流式兜底不再包成 `NotReadyError`。文件：`errors.py`、`server.py`。
- [x] **T3.2** 去掉叠加重试（来源 4.6）：`fill_composer` 已重试 `FILL_RETRIES`，`generator._fill` 只对可恢复的写入失败再重试，`ComposerNotFound` 与登录类错误不重试。文件：`generator.py`、`dom_adapter.py`。
- [x] **T3.3** 额度归因收窄（来源 4.6）：只在「没有新回复」或「弹窗仍在页面上」时报 `UsageLimitError`；已有正文后的停滞仍报停滞。文件：`generator.py`、`tests/test_generation_latency.py`。
- [x] **T3.4** 失败类别指标（来源 4.6、第 5 节）：为 `usage_limit_reached`、`prompt_submission_failed`、`reply_not_found`、`context_limit_reached`、取消各加计数。文件：`metrics.py`、`service.py`。（与 D3.4 同一处落地，见 `doc/updates_deepseek.md`）
- [x] **T3.5** 配置路径与列表解析（来源 4.8）（与 D3.5 同一处落地，见 `doc/updates_deepseek.md`）
  - `SESSION_FILE` / `USER_DATA_DIR` / `TASK_FILE_DIR` / `OUTPUT_DIR` 相对路径以 `PROJECT_ROOT` 为基准解析。
  - 选择器类配置只按 `||` 切分，不按逗号。
  - `LINK_ALLOWED_HOSTS=*` 时启动强告警。
  - 文件：`config.py`、`.env.example`、`tests/test_config.py`、`tests/test_config_drift.py`。
- [x] **T3.6** 启动校验与摘要日志（来源 4.8）：旧键（`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS`）告警；校验 `BUCKET_LOCK_TIMEOUT_S >= CLAUDE_TIMEOUT`、`SETTLE_NO_SIGNAL_S >= SETTLE_S`、`STALL_S > SUBMIT_LOST_S`；把生效值写进摘要日志（不含密钥）。文件：`config.py`、`server.py`。（与 D3.6 同一处落地，见 `doc/updates_deepseek.md`）
- [x] **T3.7** 会话比较与绑定（来源 4.7）
  - `_same_conversation` 与 `link` 落点校验改为解析 URL，精确比较 `/chat/<ID>`。
  - 绑定桶触顶后的处理：换新会话或明确报错，不再反复 goto 同一个已满会话。
  - 可选：保存未绑定桶新建会话的 URL，页面回收或重启后恢复。
  - 文件：`lifecycle.py`、`tests/test_conversation_reuse.py`。

## 阶段 4：API 与提示词

- [ ] **T4.1** Responses 真流式（来源 4.4）：复用 chat 路径的队列 + 保活 + 断连 + 错误事件；`STREAM_KEEPALIVE_S` 在两条路径统一使用。文件：`server.py`、`api/responses_adapter.py`、`tests/test_responses.py`。
- [ ] **T4.2** `store_thread` 保留 `tool_calls` 与 `tool_call_id`（来源 4.4）：保证 `previous_response_id` 续接工具循环不断链。
- [ ] **T4.3** 补齐 Responses 事件集并做真实客户端探测（来源 4.4）：`output_item.added|done`、`output_text.done` 等；用 Codex CLI 之类真实客户端验证，结果记入 `doc/tasks.md`。
- [ ] **T4.4** 流式细节（来源 4.10）
  - 网页重写回复时增加 `stream_rewritten_total`，并在最终块或 `bridge_notes` 说明。
  - `on_delta` 只在内容变化时回调。
  - 支持 `stream_options.include_usage`。
  - 已见回复后读取节点短暂失败时，保留上一次文本继续判定。
  - 文件：`server.py`、`generator.py`、`chat_adapter.py`。
- [ ] **T4.5** 提示词体量（来源 4.5）
  - 增量模式只在重播种、工具清单变化或每 N 轮重发工具说明（记录已发清单摘要）。
  - 调大 `SYSTEM_MAX_CHARS` 默认值，截断时写进 notes。
  - `SEED_ENV_NOTE` 默认留空。
  - 文件：`prompting.py`、`config.py`、`.env.example`。
  - 依赖：T1.1。
- [ ] **T4.6** `tool_choice` 生效（来源 4.5）：至少 `none` 时不注入工具说明；`required` 与指定函数在提示里点名。文件：`prompting.py`、`chat_adapter.py`。

## 阶段 5：日志、解析与落盘

- [ ] **T5.1** 日志上下文与脱敏（来源 4.9）
  - 流式请求的 `request_id` / `session_key` 绑定到生成阶段。
  - `X-Request-Id` 白名单字符 + 长度上限（64），非法时重新生成。
  - `bridge_commands` 日志走 `redact`，去掉 `exc_info=True` 的完整堆栈（改为异常类型）。
  - 文件：`server.py`、`logging_setup.py`、`bridge_commands.py`。
- [ ] **T5.2** 回复外壳剔除的边界（来源 4.11）：文本兜底只在 DOM 侧确认存在 `sr-only` / `<time>` 外壳时启用；补含公式、表格、代码块、以 `Claude said:` 开头引用、末行为时间戳样式的用例。文件：`extractor.py`、`tests/test_extraction_chrome.py`。
- [ ] **T5.3** 工具调用解析（来源 4.11）
  - `strip_tool_call_blocks` 覆盖 DSML、json 围栏、`tool_uses` 形态，避免标记残留在正文。
  - 增加轻量 schema 校验（`required` 与类型），失败只作诊断返回。
  - 拆成「提取」与「校验 / 强转」两层，补随机化测试（嵌套代码块、重复调用、半截 JSON）。
  - 文件：`toolcalls.py`、`tests/test_toolcalls.py`。
- [ ] **T5.4** 状态与快照落盘（来源 4.12）
  - `SessionStore` 内存持有文档，变更时原子写盘，对外只暴露 `update(**fields)`；同步 IO 用 `asyncio.to_thread`。
  - `TaskSnapshot` 增加「不落正文」开关（`last_reply`、`goal`），README 说明默认行为。
  - 文件：`session/store.py`、`tasks.py`、`config.py`。

## 阶段 6：清理「写了没用上」（来源第 5 节）

每项二选一：接上，或删除并同步 `design.md`。不要保留空壳。

- [ ] **T6.1** 指标：`request_retry_total`、`browser_unavailable_total`、`browser_selector_miss_total`、`reply_extraction_failure_total`、`session_recovery_total`、`tool_parse_failure_total`、`request_latency`、`reply_extraction_latency`。
- [ ] **T6.2** 配置与字段：`STREAM_KEEPALIVE_S`（见 T4.1）、`TASK_KEEP_MESSAGES`、`SessionState.cap_failures`、`TaskSnapshot.messages / recoveries`、状态 `queued / generating / recovering`。
- [ ] **T6.3** 函数：`model_is_supported`、`lock.WARNED_BAD_STATE`、`page_usable_reason` / `page_responds`（应接进 `ensure_usable` 与 readiness 判活）、`is_page_lost_error`、`is_timeout_error`。
- [ ] **T6.4** 其它：`_OMISSION_NOTE` 的重复注释；`/bridge modal` 的「改状态」标注；确认 `session_store.py`、`main.py` 与 `claude_api_server.py` 是否都要保留；先 grep 确认 `TaskStore.load / clear` 有无生产调用方。

## 阶段 7：工程化（来源第 6 节）

- [ ] **T7.1** 最小 CI：`.github/workflows` 跑 `ruff check` + `pytest`（跳过 e2e）；e2e 手动触发。
- [ ] **T7.2** 类型：补 `[tool.mypy]`，先对 `errors`、`session/*`、`models`、`end_detection` 开启，或删掉相关注释。
- [ ] **T7.3** 依赖：以 `pyproject.toml` 为准，`requirements.txt` 去重并加版本约束，生成锁文件；评估放宽 `requires-python`。
- [ ] **T7.4** ruff 逐步加 `B`、`UP`、`SIM`。
- [ ] **T7.5** 目录整洁：profile 备份迁出项目根目录；`.gitignore` 去掉重复的 `.freebuff/`。
- [ ] **T7.6** 测试补强：`generate` 端到端时序（重写、取消、停滞、超时延长）；复用会话回归；同桶串行、跨桶并行、管理操作取锁；真实 e2e 增加「会话里含特征文案但可正常对话」「条数停在基线但页面已回答」两条。
- [ ] **T7.7** 指标扩充：每桶排队等待、播种与增量次数、重写次数、取消次数、选择器回退命中、`structural=True` 次数；可选 Prometheus 文本格式。
- [ ] **T7.8** 文档：把「结束判定与回复识别」从 `design.md` 拆成独立文档；README 加「现象 -> 日志关键字 -> 处理」对照表；写明不支持多 worker。

## 依赖与顺序

`T0.1` -> `T1.1`、`T1.2`（可并行）-> `T1.3 / T1.3b` -> `T2.1`、`T2.2` -> `T1.4(A)` -> `T2.3 ~ T2.5` -> `T1.4(B)`、`T2.6` -> 阶段 3 -> 阶段 4（`T4.5` 依赖 `T1.1`）-> 阶段 5 -> 阶段 6 -> 阶段 7。

## 风险提示

- `T1.4(B)` 会改变复用语义，先上线步骤 A 观察日志，再切换。
- `T2.5` 点击停止控件属于新增页面操作，只点停止，沿用 `design.md` 4.3 的安全约束。
- `T4.3` 需要真实客户端与登录态，不能放进默认测试套件。
