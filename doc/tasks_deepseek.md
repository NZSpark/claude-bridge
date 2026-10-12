# ClaudeBridge 任务分解（来源：doc/update_deepseek.md）

> 2026-10-12。每个任务标注：来源章节、涉及文件、做法、验收标准。
> **范围排除**：鉴权不在本项目范围，不拆任务，也不含任何 `BRIDGE_TOKEN` / `RESET_TOKEN` / 端点访问控制内容。3.3 只保留「管理操作取桶锁」这一并发正确性问题。
> 约定：每个任务单独提交；有行为变化的都配回归测试；完成后 `.venv/bin/ruff check .` 与 `pytest -q` 必须全绿（e2e 除外，需显式开启）。
>
> 与 `doc/tasks_claude.md` 的关系：来源文档不同（本文来自 DeepSeek 版评审），但很多任务重叠。**重叠任务以先落地者为准，不要重复实现**；本文额外覆盖了第 5 节的「死代码全仓库 grep 复核」与 `tool_choice` / `_KEEPALIVE` / `TaskStore.load` 三处具体死点。

## 阶段 0：基线确认

- [ ] **D0.1** 复核评审基线与现状是否一致（0.3h）
  - 对照 `doc/update_deepseek.md` 第 5 节的 grep 结论，在**当前 HEAD** 上重跑一遍相关 grep（`reap_idle` / `mark_busy` / `tool_choice` / `STREAM_KEEPALIVE_S` / `TaskStore.load`），确认没有因新提交而失效。
  - 验收：每条「死代码」结论在当前 HEAD 上仍成立，或已在文档中标注其变化。

## 阶段 1：P0 正确性（先做，改动小、影响最大）

- [x] **D1.1** 工具说明判重不被历史正文误抑制（来源 3.1）
  - 文件：`claude_web/prompting.py`、`claude_web/toolcalls.py`、`tests/test_prompting.py`
  - 长期做法（状态优先，采纳 ChatGPT 版方案）：记录「上一轮已发送的工具清单摘要 + 是否已重播种」，清单未变且网页会话未重播种就不重发；用协议状态而非任意文本推断。
  - 短期兜底（若状态方案未就绪）：`_mentions_tool_instructions` 只在 system / user 角色、且标题位于行首时判重；tool 结果与 assistant 历史不参与。
  - 验收：新增用例——tool 结果 / assistant 历史 / 代码块里含 `[Tool Calling Instructions]` 时**仍然注入**；system 里已内联时**不重复注入**。

- [x] **D1.2** 绝对超时优先延长，而不是截断（来源 3.2）
  - 文件：`claude_web/end_detection.py`、`claude_web/completion/generator.py`、`tests/test_end_detection.py`、`tests/test_generation_latency.py`
  - 做法：`past_deadline` 时若 `generating is True` 且仍有延长额度，**先 `extend`**（`extend` 分支排在 `has_text → finish` 之前）；真正达到硬上限才结束，用兼容的截断语义（可用时 `finish_reason=length`）并在 notes 标注「被截断」。
  - 验收：虚拟时钟用例覆盖三种情形——① 到期时正在生成且已有正文（应延长，不截断）；② 延长额度耗尽仍在生成（结束且标注截断）；③ 到期前已静止（正常收尾）。

- [x] **D1.3** 管理操作统一取桶锁（来源 3.3）
  - 文件：`claude_web/service.py`、`claude_web/bridge_commands.py`、`claude_web/server.py`、`tests/test_bridge_commands.py`、`tests/test_sessions.py`
  - 做法：新增 `service.admin(bucket)`——先取桶锁（短超时，失败回 `session_busy`），再执行 `reset / link / unlink / modal`；HTTP 的 `/session/*`、聊天命令（chat 与 responses 两条路径）全部走它，消除三条入口各自为政。
  - 验收：桶正在生成时 reset / link 返回 busy、**不会关掉页面 / 打断在途请求**；桶空闲时行为与现在一致。

- [x] **D1.4** `store.reset()` 不再悄悄清掉 `linked_url`（来源 4.7）
  - 文件：`claude_web/session/store.py`、`claude_web/completion/lifecycle.py`、`claude_web/bridge_commands.py`、`tests/test_sessions.py`
  - 做法：reset 只清对话状态，保留绑定；要解绑用 unlink。`/session/reset` 与 `/bridge reset` 的回复文案如实说明保留还是清除绑定。
  - 验收：绑定桶 reset 后 `lifecycle.status(bucket)` 仍显示 `linked_url`。

- [ ] **D1.5** 复用会话的「已同步历史」指纹（来源 3.4，**分两步**）
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/session/schema.py`、`claude_web/service.py`、`tests/test_conversation_reuse.py`、`README.md`
  - 步骤 A（先上线，只观测）：成功后记录指纹——首条 user 摘要 + 消息条数 + 末条 assistant 摘要 + 系统提示摘要 + 工具清单摘要；下一轮「客户端历史是否以已同步历史为前缀」对不上时，**只打日志 + notes**，不改行为。
  - 步骤 B（观察无误判后切换）：对不上或系统提示 / 工具清单变化 → 换新对话并完整重播。
  - 验收：回滚、重新生成、换任务、压缩历史四种场景各一条用例；README 写明多任务 / 多项目 / 需隔离时显式用 `X-Claude-Session`，不要把 UA 桶当隔离保证。
  - 备注：三份评审都指出「相同提示词连续两轮 + 虚拟化」时纯文本指纹无法区分。优先寻找稳定 DOM 消息 ID（`data-testid` / `data-message-id`）；找不到时，指纹相同场景**宁可重新播种也不要误认**（保守优先）。

## 阶段 2：并发与资源生命周期

- [x] **D2.1** 驱动启动与页面池创建加锁（来源 4.1）
  - 文件：`claude_web/driver.py`、`claude_web/page_pool.py`、`tests/test_browser.py`
  - 做法：`ClaudeWebDriver.start()` 加共享 `asyncio.Lock`（多个桶首次并发只初始化一次）；`PagePool.acquire` 按桶加锁并把驱逐放进同一临界区；`readiness()` 对忙碌桶返回缓存结果，避免与默认桶生成请求并发操纵同一页面。
  - 验收：并发两桶首次请求只启动一次 Playwright / Chrome；同桶并发 acquire 只建一个页面，页面数不超过 `MAX_PAGE_POOL`。

- [x] **D2.2** 接上空闲回收与忙碌标记（来源 4.2，**grep 确认当前为零调用**）
  - 文件：`claude_web/server.py`（lifespan）、`claude_web/service.py`、`claude_web/page_pool.py`、`tests/test_browser.py`
  - 做法：lifespan 起周期 `reap_idle` 任务，关闭时取消并等待；`service._run_locked` 在生成前后 `mark_busy(True/False)`（`finally` 复位，覆盖取消 / 异常 / 成功）；满池且候选全忙时返回 busy，而不是驱逐在用页面；对客户端可控的桶名做新建速率限制。
  - 验收：空闲超过 `BUCKET_IDLE_TTL_S` 的页面被关；**生成中的页面永不被 LRU 驱逐**；第 5 个桶进来不会误关正在生成的页面。

- [x] **D2.3** 取消语义（来源 4.3）
  - 文件：`claude_web/errors.py`、`claude_web/completion/generator.py`、`claude_web/service.py`、`tests/test_generation_latency.py`
  - 做法：新增 `GenerationCancelledError`；`_record_failure` 对它单独计数、**不写会话 `last_error`、不计入超时统计**；`generate._fill` 的 `except BaseException` 改为 `except Exception`，让 `CancelledError` 冒泡。
  - 验收：取消不计入超时 / 失败统计，不污染 `last_error`；桶锁在取消后正常释放。

- [x] **D2.4** 非流式与 Responses 路径的断连检测（来源 4.3）
  - 文件：`claude_web/server.py`
  - 做法：非流式请求在后台任务里跑，检测到客户端断连就置取消标志（与流式路径同一机制）。
  - 验收：客户端断开后桶锁在一个轮询周期内释放，不再空跑到绝对超时。

- [x] **D2.5** 提交前处理「页面仍在生成」（来源 4.3）
  - 文件：`claude_web/completion/generator.py`、`claude_web/browser/dom_adapter.py`
  - 做法：提交前若 `generating_state` 为真，先等一小段；仍未结束就**只点经确认的停止控件**（沿用 `design.md` 4.3 的安全约束，绝不点其它控件），再重新确认可提交后提交。
  - 验收：上一轮取消 / 失败后立即发下一轮，不再误报「提交未生效」。

- [ ] **D2.6** 失败后标记桶状态不确定（来源 4.3）
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/service.py`
  - 做法：提交**之后**失败的桶标记为「状态不确定」，下一轮交给 D1.5 的指纹决定是否重播，避免网页端出现重复提示词。
  - 验收：提交后失败再重试，不会把同一条增量重复发进网页会话。
  - 依赖：D1.5。

## 阶段 3：错误语义与配置

- [x] **D3.1** 内部错误码（来源 4.6）：新增 `InternalError`（500），`_unhandled_handler` 与流式兜底不再把内部 bug 包成 `NotReadyError`。文件：`errors.py`、`server.py`、`tests/`。
- [x] **D3.2** 去掉叠加重试（来源 4.6）：`fill_composer` 已重试 `FILL_RETRIES`；`generator._fill` 只对**可恢复的写入失败**再重试，`ComposerNotFound` 这类不可恢复错误不重试。文件：`generator.py`、`dom_adapter.py`、`tests/test_generation_latency.py`。
- [x] **D3.3** 额度归因收窄（来源 4.6）：只在「没有新回复」或「弹窗仍在页面上」时报 `UsageLimitError`；已有正文后的停滞仍报停滞。文件：`generator.py`、`tests/test_generation_latency.py`。
- [x] **D3.4** 失败类别指标（来源 4.6、第 5 节）：为 `usage_limit_reached`、`prompt_submission_failed`、`reply_not_found`、`context_limit_reached`、取消各加计数。文件：`metrics.py`、`service.py`。
  - **落地**：`metrics.COUNTERS` 增加四个 `*_total` 分类计数器；`service._FAILURE_COUNTERS` 用「错误码 → 计数器」映射表驱动（而非 if 链），在 `_record_failure` 末尾查表 `inc()`。取消沿用既有的 `request_cancelled_total`（它在更早分支就 `raise`，不进映射表），避免造出「写了没用上」的新死键。回归测试 `tests/test_failure_metrics.py` 五条：四类错误码各一条 + 成功路径对照，逐条断言「只增对应计数、不误增其它」。
- [x] **D3.5** 配置路径与列表解析（来源 4.8）
  - `SESSION_FILE` / `USER_DATA_DIR` / `TASK_FILE_DIR` / `OUTPUT_DIR` 相对路径以 `PROJECT_ROOT` 为基准解析（换目录启动不再丢登录态）。
  - 选择器类配置**只按 `||` 切分**，不按逗号（避免 `:is(a,b)` 被切坏）。
  - `LINK_ALLOWED_HOSTS=*` 时启动强告警。
  - 文件：`config.py`、`.env.example`、`tests/test_config.py`、`tests/test_config_drift.py`。
  - **落地**：新增 `env_path()`（相对路径以 `PROJECT_ROOT` 解析，绝对路径原样）；`env_list()` 只按 `||` 切分、逐项 strip、过滤空串，不再按逗号切；`LINK_ALLOWED_HOSTS` 含 `*` 时启动 `warnings.warn` 强告警。`test_config_drift.py` 的 `_ENV_CALL` 正则扩到 `env_path`，保证四处路径键被计入契约检查。
- [x] **D3.6** 启动校验与摘要日志（来源 4.8）
  - 旧键（`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS`）告警；校验 `BUCKET_LOCK_TIMEOUT_S >= CLAUDE_TIMEOUT`、`SETTLE_NO_SIGNAL_S >= SETTLE_S`、`STALL_S > SUBMIT_LOST_S`；把生效值写进摘要日志（不含密钥）。
  - 文件：`config.py`、`server.py`、`tests/test_config.py`。
  - **落地**：`config.config_warnings()` 返回问题清单（**不抛异常**——默认值本身命中「锁超时 120s < 单轮超时 300s」，抛异常会让默认配置启动失败）；`config.summary_lines()` 输出生效值（`_SUMMARY_KEYS` 不含密钥）。`server.lifespan` 逐条 `logger.warning("[启动配置] …")` 并打一行摘要。回归 `tests/test_startup_config.py` 七条：默认值冲突、旧键告警、一致时零告警、两条跨键约束、摘要不泄漏密钥。
- [x] **D3.7** 会话比较与绑定（来源 4.7）
  - `_same_conversation` 与 `lifecycle.link` 落点校验改为解析 URL、**精确比较 `/chat/<ID>`**（现在用 `startswith(target[:len-1])`，会话 ID 只差最后一位会误判同一条）。
  - 绑定桶触顶后的处理：换新会话或明确报错，不再反复 `goto` 同一个已满会话。
  - 可选：保存未绑定桶新建会话的 URL，页面回收 / 重启后恢复。
  - 文件：`lifecycle.py`、`tests/test_conversation_reuse.py`。

## 阶段 4：API 与提示词

- [x] **D4.1** Responses 真流式（来源 4.4）：复用 chat 路径的「队列 + 保活 + 断连 + 错误事件」骨架；`STREAM_KEEPALIVE_S` 在两条路径统一使用（替换写死的 `_KEEPALIVE`）。文件：`server.py`、`api/responses_adapter.py`、`tests/test_responses.py`。
  - **落地**：`_stream_responses` 由伪流式（先 `await svc.run` 再一次性吐事件）改为真流式——后台 `produce()` 把增量投入队列，主循环逐段发 `response.output_text.delta`、空闲超 `STREAM_KEEPALIVE_S` 发 SSE 注释、断连置取消标志、失败发 `sse_error`。`responses_adapter` 新增 `response_created_event` / `response_text_delta_event` / `response_completed_event`，`response_events` 改为复用它。写死的 `_KEEPALIVE = 10.0` 换成 `_keepalive_s()`（读 `config.STREAM_KEEPALIVE_S`，带 0.1s 下限防忙轮询）。回归 `tests/test_responses.py` 新增六条：增量事件存在、无重写时增量拼接 == 最终文本、重写时 completed 为准且不再追加、路由传 `request`、keepalive 读配置、事件构造函数一致。
  - **注意**：重写（网页改写回复）语义与 chat 路径对齐——已发出的增量无法撤回，停止再发，最终文本以 `response.completed` 为准。
- [x] **D4.2** `store_thread` 保留 `tool_calls` 与 `tool_call_id`（来源 4.4）：保证 `previous_response_id` 续接工具循环不断链。文件：`api/responses_adapter.py`、`tests/test_responses.py`。
  - **落地**：新增 `_message_to_payload()`，序列化 `NormalizedMessage` 时带上非空的 `tool_calls` / `tool_call_id`；`store_thread` 增加可选 `tool_calls` 参数——回复含工具调用时记成带 `tool_calls` 的 assistant 消息（`call.model_dump()` 嵌套形态）而非纯文本。`server.py` 两处调用（非流式 + 流式）都传入 `outcome.result.tool_calls`。
  - **关键坑**：存储必须用 `ToolCall` 的**嵌套**形态（`{id,type,function:{name,arguments}}`），因为回灌走 `ChatMessage(**item)`，其 `tool_calls` 类型是 `List[ToolCall]`；若存内部扁平形态（`{id,name,arguments}`）会在 pydantic 校验处直接 `ValidationError: tool_calls.0.function Field required`。之后 `normalize_messages` → `_tool_calls_to_payload` 会把它转回内部扁平表示。
  - 回归 `tests/test_responses.py` 新增两条：工具循环续接（两条 assistant 调用 + tool 结果归属都在、名字/参数完整）、无工具调用时仍存纯文本。
- [ ] **D4.3** 补齐 Responses 事件集并做真实客户端探测（来源 4.4）：补 `output_item.added|done`、`output_text.done` 等；用 Codex CLI 之类真实客户端验证，结果记入 `doc/tasks.md`。
- [x] **D4.4** 流式细节（来源 4.10）
  - 网页重写回复时增加 `stream_rewritten_total` 指标，并在最终块或 `bridge_notes` 说明「已截断」。
  - `on_delta` 只在内容变化时回调（现在每轮都回调，队列堆重复项）。
  - 支持 `stream_options.include_usage`。
  - 已见回复后读取节点短暂失败时，保留上一次文本继续判定。
  - 文件：`server.py`、`generator.py`、`chat_adapter.py`、`metrics.py`。
  - **落地（三项）**：① `generator` 的 `on_delta` 加 `normalized != emitted` 判定，只在内容变化时回调（此前每轮轮询都回调，队列堆重复项）；② `metrics.COUNTERS` 新增 `stream_rewritten_total`，chat 与 Responses 两条路径在首次检测到重写时 `inc`（用 `if not rewritten` 防重复计数）；③ `StreamEncoder.usage()` 发 choices 为空、usage 非空的收尾块，`server._include_usage` 读 `task.stream_options.include_usage`，`chat_adapter.to_task` 透传 `stream_options`，`ChatTask` 新增该字段。
  - 回归 `tests/test_stream_details.py` 三条：usage 块结构、`_include_usage` 判定、指标名已注册。
- [ ] **D4.5** 提示词体量（来源 4.5）
  - 增量模式只在重播种、工具清单变化或每 N 轮重发工具说明（与 D1.1 的状态方案同一处）。
  - 调大 `SYSTEM_MAX_CHARS` 默认值，截断时写进 notes。
  - `SEED_ENV_NOTE` 默认留空，由部署方配置。
  - 文件：`prompting.py`、`config.py`、`.env.example`。
  - 依赖：D1.1。
- [x] **D4.6** `tool_choice` 生效（来源 4.5，**grep 确认当前无消费点**）：至少 `none` 时不注入工具说明；`required` 与指定函数在提示里点名。文件：`prompting.py`、`api/chat_adapter.py`、`models.py`、`tests/test_prompting.py`。
  - **落地**：新增纯函数 `prompting.tool_choice_effect(tool_choice, tools) -> (是否注入, 附加指令)`；`build_prompt` 增加 `tool_choice` 参数，`none` 时不注入工具说明，`required` / 指定函数在工具说明后追加点名指令；`SessionLifecycle.prepare` 与 `service.run` 透传 `task.tool_choice`。点名未声明的工具时仍注入说明（不静默丢弃）但不给误导指令。
  - 回归 `tests/test_tool_choice.py` 六条：none 抑制、auto/None 维持、required 点名、指定函数点名、未声明函数不误导、无工具时不注入。
  - 关闭 `doc/updates_deepseek.md` §4 死键表里的 `tool_choice` 未接线项。

## 阶段 5：日志、解析与落盘

- [ ] **D5.1** 日志上下文与脱敏（来源 4.9）
  - 流式请求的 `request_id` / `session_key` 绑定到生成阶段（现在 `log_context` 在返回 `StreamingResponse` 时就退出）。先加断言式测试或真实抓一次日志确认问题存在，再修。
  - `X-Request-Id` 白名单字符 + 长度上限（建议 64），非法时重新生成。
  - `bridge_commands` 日志走 `redact`；`handle_command` 的 `exc_info=True` 改为只记异常类型。
  - 文件：`server.py`、`logging_setup.py`、`bridge_commands.py`、`tests/`。
- [ ] **D5.2** 回复外壳剔除的边界（来源 4.11）：文本兜底只在 DOM 侧确认存在 `sr-only` / `<time>` 外壳时才启用；补含公式、表格、代码块、以 `Claude said:` 开头引用、末行为时间戳样式的用例。文件：`extractor.py`、`tests/test_extraction_chrome.py`。
- [ ] **D5.3** 工具调用解析（来源 4.11）
  - `strip_tool_call_blocks` 覆盖 DSML、`json` 围栏、`tool_uses` 形态，避免标记残留在正文。
  - 增加轻量 schema 校验（`required` 与类型），失败只作诊断返回。
  - 拆成「提取」与「校验 / 强转」两层，补随机化测试（嵌套代码块、重复调用、半截 JSON）。
  - 文件：`toolcalls.py`、`tests/test_toolcalls.py`。
- [ ] **D5.4** 状态与快照落盘（来源 4.12）
  - `SessionStore` 内存持有文档，变更时原子写盘；同步 IO 用 `asyncio.to_thread`，避免阻塞事件循环。
  - `TaskSnapshot` 增加「不落正文」开关（`last_reply`、`goal`），README 说明默认行为。
  - 文件：`session/store.py`、`tasks.py`、`config.py`、`README.md`。

## 阶段 6：清理「写了没用上」（来源第 5 节）

> 每项二选一：**接上，或删除并同步 `design.md`**。不要保留「看起来有保护」的空壳。

- [ ] **D6.1** 指标：`request_retry_total`、`browser_unavailable_total`、`browser_selector_miss_total`、`reply_extraction_failure_total`、`session_recovery_total`、`tool_parse_failure_total`、`request_latency`、`reply_extraction_latency`。
- [x] **D6.2** 配置与字段：`STREAM_KEEPALIVE_S`（见 D4.1）、`TASK_KEEP_MESSAGES`、`READY_TIMEOUT_MS`、`FILL_TIMEOUT_MS`、`NEW_SESSION_ON_START`、`SAVE_DEBUG_ARTIFACTS`、`SessionState.cap_failures`、`TaskSnapshot.messages / recoveries`、状态 `queued / generating / recovering`。
  - **接线（3 项）**：`FILL_TIMEOUT_MS` 包住 `fill_composer` 重试循环（超时抛 `PromptSubmissionError`）；`READY_TIMEOUT_MS` 包住 `readiness` 的 `page_status` 探测（超时回 `browser_unavailable`）；`TASK_KEEP_MESSAGES` 在 `TaskStore.new` 截断快照消息（0 = 不限制）。
  - **删除（2 项）**：`NEW_SESSION_ON_START`（无消费点 + 不在 .env.example）、`SAVE_DEBUG_ARTIFACTS`（声明了却无落盘实现）。同步删掉 `.env.example` 的键与 `design.md` 的表格行。
  - **已接线无需改**：`SessionState.cap_failures`（lifecycle 读写）、`TaskSnapshot.recoveries`（tasks 序列化）。
  - 回归 `tests/test_config_wiring.py` 六条：删除的键彻底不在 config / .env.example、`TASK_KEEP_MESSAGES` 截断与 0 不限、`FILL_TIMEOUT_MS` / `READY_TIMEOUT_MS` 出现在对应源码中。
  - **踩坑**：删 `NEW_SESSION_ON_START` 时漏删 `.env.example` 的 `CLAUDE_NEW_SESSION` 行，被 `test_config_drift.py` 当场逮住——正是这套契约检查的价值。
- [ ] **D6.3** 函数：`models.model_is_supported`、`session/lock.WARNED_BAD_STATE`、`errors.page_usable_reason` / `page_responds`（应接进 `ensure_usable` 与 readiness 判活）、`errors.is_page_lost_error` / `is_timeout_error`。
- [ ] **D6.4** 其它：`prompting.py` 里 `_OMISSION_NOTE` 的重复注释；`/bridge modal` 的「改状态」标注（实际不改会话状态）；确认 `session_store.py`（纯委托门面）、`main.py` 与 `claude_api_server.py`（重复入口）是否都要保留；先 grep 确认 `TaskStore.load / clear` 有无生产调用方。

## 阶段 7：工程化（来源第 6 节）

- [ ] **D7.1** 最小 CI：`.github/workflows` 跑 `ruff check` + `pytest`（跳过 e2e）；e2e 手动触发，最近一次真实探测日期记入 `doc/tasks.md`。
- [ ] **D7.2** 类型：补 `[tool.mypy]`，先对 `errors`、`session/*`、`models`、`end_detection` 开启，或删掉 `pyproject.toml` 里的相关注释。
- [ ] **D7.3** 依赖：以 `pyproject.toml` 为准，`requirements.txt` 去重并加版本约束，生成锁文件；评估放宽 `requires-python`。
- [ ] **D7.4** ruff 逐步加 `B`、`UP`、`SIM`。
- [ ] **D7.5** 目录整洁：profile 备份（`user_data_chrome.bak-*` / `pre-refresh-*`）迁出项目根目录；`.gitignore` 去掉重复的 `.freebuff/`。
- [ ] **D7.6** 测试补强：`generate` 端到端时序（重写 / 取消 / 停滞 / 超时延长）；复用会话回归（页面被换、系统提示变化、`link --seed`、URL 精确比较）；同桶串行 / 跨桶并行 / 管理操作取锁；tool 结果里含工具说明标题；真实 e2e 增加「会话里含特征文案但可正常对话」「条数停在基线但页面已回答」两条。
- [ ] **D7.7** 指标扩充：每桶排队等待、播种与增量次数、重写次数、取消次数、选择器回退命中、`structural=True` 次数；可选 Prometheus 文本格式。
- [ ] **D7.8** 文档：把「结束判定与回复识别」从 `design.md` 拆成独立文档；README 加「现象 -> 日志关键字 -> 处理」对照表（`prompt_submission_failed` / `generation_stalled` / `context_limit_reached` / `reply_not_found` / `challenge_required` / `usage_limit_reached`）；写明不支持多 worker、浏览器惰性启动。

## 依赖与顺序

```
D0.1
  └─ D1.1, D1.2                 （可并行，P0 优先）
       └─ D1.3, D1.4
            └─ D2.1, D2.2       （并发与资源）
                 └─ D1.5(A)      （历史指纹，先只观测）
                      └─ D2.3 ~ D2.5
                           └─ D1.5(B), D2.6   （切换重播 + 状态不确定）
                                └─ 阶段 3
                                     └─ 阶段 4（D4.5 依赖 D1.1）
                                          └─ 阶段 5
                                               └─ 阶段 6
                                                    └─ 阶段 7
```

## 风险提示

- **D1.5(B)** 会改变复用语义：先上线步骤 A 观察日志，确认无误判后再切换；切换行为必须由回归测试保护。
- **D2.5** 点击停止控件属于新增页面操作：只点停止，沿用 `design.md` 4.3 的安全约束，失败不抛异常。
- **D2.2** 会给 `lifespan` 增加后台任务：关闭时必须取消并等待，避免进程退出时挂起。
- **D4.3** 需要真实客户端与登录态，不能放进默认测试套件。
- **D5.1** 的「流式日志上下文丢失」在评审中属**推断**，先验证再改，避免误修。

## 与 doc/tasks_claude.md 的对应

| 本文 | tasks_claude.md | 备注 |
| --- | --- | --- |
| D1.1 | T1.1 | 本文采用状态优先方案，T1.1 是短期兜底；二者可合并实现 |
| D1.2 | T1.2 | 一致 |
| D1.3 | T1.3 | 一致（均排除鉴权） |
| D1.4 | T1.3b | 一致 |
| D1.5 | T1.4 | 一致（分步） |
| D2.1 / D2.2 | T2.1 / T2.2 | 一致 |
| D2.3 ~ D2.6 | T2.3 ~ T2.6 | 一致 |
| D3.x | T3.x | 一致 |
| D4.x | T4.x | 本文额外含 `tool_choice`（D4.6） |
| D5.x | T5.x | 一致 |
| D6.x | T6.x | 本文含 `READY_TIMEOUT_MS` / `FILL_TIMEOUT_MS` / `NEW_SESSION_ON_START` / `SAVE_DEBUG_ARTIFACTS` |
| D7.x | T7.x | 一致 |

> 重叠任务**以先落地者为准，不要重复实现**。
