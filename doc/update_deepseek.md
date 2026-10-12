# ClaudeBridge 全项目分析与改进建议（DeepSeek 版）

> 审查基线：`5a24797`（Docs: explain reply detection after row pruning），含实现提交 `d9bb75d`（虚拟化行回收后的回复识别）。审查日期：2026-10-12。
>
> 本文是**第三份**独立评审（此前已有 `doc/update_claude.md`、`doc/update_chatgpt.md`）。为免与它们重复堆砌，本文：
> * 对已确认结论只做**收敛性确认**（标注「与 Claude/ChatGPT 版一致」），不重复论证；
> * 补充我这次通读代码**新发现或需要纠正**的点；
> * 对三份评审可能出现分歧的地方给出明确取舍。
>
> 已通读：`server`、`service`、`driver`、`page_pool`、`completion/{generator,lifecycle,extractor}`、`end_detection`、`browser/dom_adapter`、`prompting`、`toolcalls`、`bridge_commands`、`config`、`models`、`tasks`、`metrics`、`errors`、`session/{store,lock,schema,migration}`、`api/{chat_adapter,responses_adapter,streaming_adapter}`、入口文件、`pyproject.toml`、`requirements.txt`、README。
> 未逐行通读：`browser/selectors.py` 的 JS 片段、`tests/*`、`tools/bench_pipeline.py`。
> 静态检查基线：本轮 `.venv/bin/ruff check .` 全绿；未跑 pytest、未跑真实浏览器 E2E。

## 1. 总体判断

分层是干净的，且**近期所有真实事故都做了归因 + 日志 + 回归测试**，这在个人工具类项目里属于少见的高标准。`end_detection` 的纯函数状态机 + 虚拟时钟、`reply` 的「两把钥匙」判定、`REPLY_CHROME_SELECTORS` / `CONTEXT_LIMIT_PATTERNS` 的配置化，都应当保留，不要为了重构而动它们。

结论与另两份评审一致：**当前最大的风险不是缺功能，而是「已经写下的状态与生命周期契约没有闭合」**——具体表现为三类：

1. **契约漂移**：配置、指标、状态字段、错误码里有一批「定义了但从未接线」的成员（第 5 节，我做了全仓库 grep 复核）；
2. **并发 / 取消闭环缺失**：驱动启动、页面池创建、管理命令、取消语义都没有统一入口（P0/P1）；
3. **复用网页会话之后的一致性**：增量模式把「客户端历史」与「网页端历史」这两个事实来源解耦了，却没有任何指纹核对（P0）。

**不建议大重构。** 下面按「故障影响」而不是「代码整洁度」排序；每项改动都应单独提交、配回归测试，真实浏览器验证始终独立于离线单测。

## 2. 应予保留的正确设计（三份评审一致）

- 结束 / 停滞 / 提交未生效判定用**时长**而非轮询次数，纯函数 + `IdleClock` 可穷举测试。
- 新回复识别用**两把独立钥匙**（条数 / 结构信号），对虚拟化回收保持保守兼容。
- 工具调用**默认缓冲后解析**，且只有通过校验、属于声明集合的调用才外发；不执行模型生成的命令。
- 额度弹窗识别 / 关闭只点关闭类控件，绝不点升级 / 付费，且错误归因为 `usage_limit_reached`（429）。
- `/diagnostics` `/metrics` 不返回正文；绑定 URL 强制 HTTPS + 主机白名单 + 会话路径。
- 浏览器依赖惰性导入，离线测试用替身驱动真实 DOM 适配器与状态机。
- Responses API 默认关闭，未验证的子集不伪装成完整支持。
- 配置契约由 `.env.example` / `config.py` / README 三处 + `test_config_drift.py` 约束。

## 3. P0：正确性与安全

### 3.1 工具说明判重会被历史正文误抑制（与 Claude/ChatGPT 版一致，**已在本次会话实测触发**）

`prompting._mentions_tool_instructions` 在**所有非 system 消息**（含 tool 结果与 assistant 历史）里搜 `[Tool Calling Instructions]`。开发 agent 只要读过本仓库的 `toolcalls.py` / 测试 / 文档（都含这个字面量），此后每轮以及任何重播种都不再注入工具说明。

**本次评审会话中确实复现**：`cat toolcalls.py` 之后的每一轮请求都不再带工具说明；一旦换新网页会话就会失效（直接凭知识作答或退回 XML/DSL）。用本项目开发本项目**必然**触发。

**建议（采纳 ChatGPT 版的「状态优先」方案，优于 Claude 版的「只扫 system/user」）**：
- 长期方案：记录「上一轮已发送的工具清单摘要」，清单未变且网页会话未重播种就不重发；用协议状态而非任意文本推断。
- 短期兜底：判重只在 system / user 角色、且要求标题在行首。
- 回归测试：tool 结果 / assistant 历史 / 代码块里含该标题时**仍须注入**。

### 3.2 绝对超时会截断长回复，页面仍在生成（与 Claude 版一致）

`end_detection.evaluate_poll` 的 `past_deadline and has_text` 分支排在 `extend` 之前，直接 `finish` 并以 `finish_reason=stop` 返回。于是：生成超过 `CLAUDE_TIMEOUT`（默认 300s）且已有部分正文时，回复被截断，页面还在继续生成；`RESPONSE_TIMEOUT_EXTEND_S` 只在「到期时一个字都没有」时才有机会生效，与配置注释相反。

**建议**：到期时若 `generating is True` 且仍有延长额度，先 `extend`；真到硬上限才结束，并用兼容的截断语义（可用时 `finish_reason=length`）+ notes 标注「被截断」。补虚拟时钟测试：到期仍在生成且有正文 / 延长额度耗尽 / 到期前已静止三种场景。

### 3.3 管理操作不取桶锁，会打断在途生成（与另两版一致）

> 说明：**鉴权不在本项目考虑范围**（默认只监听回环、单用户本机使用），本节及全文不再涉及 `BRIDGE_TOKEN` / `RESET_TOKEN` / 端点访问控制。

reset / link / unlink / modal 等管理操作都不经 `BucketLockManager`：对正在生成的桶 reset 会 `pool.drop` 关掉页面，link 会 `goto` 打断在途请求（`handle_command` 直接调用 `lifecycle.reset/link/unlink`，HTTP 端点与 `/bridge` 命令、`/v1/responses` 路径三条入口各自为政）。

**建议**：统一走 `service.admin(bucket)`——先取桶锁（短超时，失败回 `session_busy`），再执行；HTTP 端点、聊天命令、Responses 路径复用同一实现。补并发测试：桶忙碌时 reset/link 不得破坏在途任务，桶空闲时行为保持兼容。

### 3.4 复用网页会话后的上下文串线（与另两版一致；本文给出最小落地方案）

默认 `SESSION_SCOPING_BY_UA=true`：同种客户端的不同任务共用一个桶。旧行为每轮新开对话所以安全；改为只发增量后，换任务会把新任务消息发进上一个任务的网页会话。`SessionLifecycle` 只比较系统提示摘要，且系统提示变了也只是「重新带上系统指令」，**不换会话**。`delta_messages` 取最后一条 assistant 之后的消息，没有任何一致性核对。

**建议（分两步，先观测后切换）**：
1. 成功后记录「已同步历史」指纹：首条 user 摘要 + 消息条数 + 末条 assistant 摘要 + 系统提示摘要 + 工具清单摘要。
2. 下一轮校验「客户端历史是否以已同步历史为前缀」：对不上或系统提示 / 工具清单变化 → 换新会话重新播种。
3. **先只记录诊断并打日志**，跑一段真实任务确认无误判，再切换为触发重播。
4. README 明确：多任务 / 多项目 / 需隔离时显式用 `X-Claude-Session`，不要把 UA 桶当隔离保证。

> 另两版都提到了「相同提示词连续两轮 + 虚拟化」时文本指纹无法区分。这是真实边界，但**不适合用纯文本解决**。建议优先寻找稳定的 DOM 消息 ID / 节点身份（`data-testid`、`data-message-id`）；找不到时，在指纹相同的场景下**宁可重新播种也不要误认**（保守优先）。

## 4. P1：可靠性、并发、可维护性

### 4.1 驱动与页面池的竞态（与另两版一致，本文确认调用点缺失）

- `ClaudeWebDriver.start()` 无锁：两个桶首次并发请求会各启动一次 Playwright / Chrome。
- `PagePool.acquire` 无锁：`await factory()` 期间另一个协程可为同一桶再建页面，先建的被覆盖并泄漏；并发 `evict_if_needed` 可能超 `MAX_PAGE_POOL`。
- `readiness()` 不取桶锁，会和默认桶生成请求撞上。

**建议**：`start()` 与 `acquire()` 各加 `asyncio.Lock`（按桶合并创建），`readiness` 对忙碌桶返回缓存。

### 4.2 空闲回收与忙碌保护是死代码（**我 grep 复核确认**）

全仓库对 `reap_idle` 的引用只有 `page_pool.py` 定义与 `driver.py:385` 的包装；**没有任何调用点**。`mark_busy` 同样**零调用**。于是：`BUCKET_IDLE_TTL_S` 完全不生效；`MAX_PAGE_POOL` 与 `MAX_SESSION_BUCKETS` 默认都是 4，第 5 个桶进来会 LRU 关掉某个**正在生成**的页面（因为 `busy` 永远是 False）。

**建议**：`lifespan` 起周期回收任务（关闭时取消并等待）；`service._run_locked` 在生成前后 `mark_busy(True/False)`（`finally` 复位，覆盖取消 / 异常 / 成功路径）；池满且候选全忙时返回 busy，而不是驱逐在用页面；对客户端可控的桶名做新建速率限制。补并发测试：同桶只建一个页面、池不超限、忙碌页不可淘汰、空闲 TTL 真的回收。

### 4.3 取消 / 断连 / 「页面上一轮仍在生成」（与另两版一致）

- 非流式与 Responses 路径无断连检测：客户端走了，生成仍占桶锁跑完（最长 300s + 延长额度）。
- 取消时 `generator` 抛 `GenerationStalledError`，被 `_record_failure` 计成失败、写进 `last_error`、污染超时统计。
- `generate._fill` 里 `except BaseException` 会吞掉 `CancelledError`。

**建议**：新增 `GenerationCancelledError`，`_fill` 只捕 `Exception`；取消不计入超时统计、不写 `last_error`；各响应路径统一取消令牌并检测断连；提交前若页面仍在生成，先短暂等待，必要时只点经确认的停止控件，然后重新确认可提交；提交后取消的桶标记「状态不确定」，不假设浏览器没收到提示词。

### 4.4 Responses API 兼容缺口（与另两版一致）

- `_stream_responses` 先 `await svc.run` 再一次性吐事件：没有保活、不处理断开 / 取消，长任务易触发客户端空闲超时。
- `StreamingResponse` 已开始后抛出的 `BridgeError` 既不能变 HTTP 错误，也不会变 SSE 错误事件（chat 路径有 `sse_error`，这里没有）。
- `store_thread` 只存 `role` / `content`，**丢掉 `tool_calls` 与 `tool_call_id`**，`previous_response_id` 续接工具循环时调用关系断裂。
- 事件集只有 `created` / `output_text.delta` / `function_call_arguments.delta` / `completed`，缺 `output_item.added|done`、`output_text.done` 等；严格客户端（如 Codex CLI）可能拒绝。

**建议**：复用 chat 路径的「队列 + 保活 + 断连 + 错误事件」骨架，再补齐事件集；`store_thread` 保留工具关联；用真实客户端做一次兼容探测。

### 4.5 提示词体量与默认值（与另两版一致）

- 增量模式每轮都重新插入完整工具说明（约 3KB，判重只看入站消息、不看网页会话是否已有），50 轮约 150KB 重复内容，加速触顶。建议只在重播种 / 工具清单变化 / 每 N 轮重发（与 3.1 同一处修复）。
- `SYSTEM_MAX_CHARS` 默认 8000：Codex / Pi 系统提示常更长，尾部规则被静默截断。建议调大默认值，或把截断写进 notes 与响应头。
- `SEED_ENV_NOTE` 默认写死 git 命令与「项目就在本机」，对非编码场景是噪声；建议默认留空由部署方配置。
- **`tool_choice` 被解析进 `ChatTask` 却从未使用**（我 grep 确认：仅 `models.py` 定义、`chat_adapter.py:46` 赋值，无消费点）。至少 `tool_choice="none"` 应当不注入工具说明。

### 4.6 错误语义（与另两版一致）

- `_unhandled_handler` 把内部 bug 包成 `NotReadyError`（503），客户端会当成登录 / 页面问题去重试。应新增 `InternalError`（500）。
- `fill_composer` 已内部重试 `FILL_RETRIES`（3），`generator._fill` 外面再重试 `MAX_UPSTREAM_RETRIES + 1`，叠加成六次，且连 `ComposerNotFound` 这类不可恢复错误也在重试。只重试可恢复的写入失败。
- 归因：`generate` 里只要提交前见过额度弹窗（`limit_modal_seen`），任何失败（含已有正文后的停滞）都报 `UsageLimitError`。应只在「没有新回复」或「弹窗仍在页面上」时归因额度。
- `_record_failure` 只给 `generation_timeout` / `generation_stalled` 计 `request_timeout_total`；`usage_limit_reached` / `prompt_submission_failed` / `reply_not_found` / `context_limit_reached` 无各自计数。

### 4.7 会话与绑定（与另两版一致）

- `_same_conversation` 用 `startswith(target[:max(10, len-1)])`：会话 ID 只差最后一位会被当成同一条；`lifecycle.link` 的落点校验（`lifecycle.py:266`）也是同样前缀写法。应解析 URL、精确比较 `/chat/<ID>`。
- `store.reset()` 新建空 `SessionState`，**同时清掉 `linked_url`**；`/session/reset` 与 `/bridge reset` 的回复都没说明这点。绑定桶 reset 后会**悄悄变成未绑定**。
- 绑定桶触顶后 `has_history=False`，下一轮仍 `goto` 同一已满会话，重复失败。
- 未绑定桶新建网页会话但不保存 URL；重启 / 页面回收后只能新开并重播完整历史。

### 4.8 配置与启动（与另两版一致）

- `SESSION_FILE`、`USER_DATA_DIR`、`TASK_FILE_DIR`、`OUTPUT_DIR` 默认相对路径，按 CWD 解析：换目录启动会换一份浏览器 profile，登录态丢失。应以 `PROJECT_ROOT` 为基准。
- `env_list` 同时按 `||` 与逗号切分：含逗号的选择器（`:is(a,b)`、选择器组）会被切坏。选择器类配置只用 `||`。
- `.env` 解析器不支持行内注释；`LINK_ALLOWED_HOSTS=*` 可让登录态浏览器被导航到任意 https 站点。建议去掉 `*` 或启动时强告警。
- 无启动期校验：旧键（`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS`）静默失效；`BUCKET_LOCK_TIMEOUT_S`（120s）< `CLAUDE_TIMEOUT`（300s），同桶排队请求会先超时；`SETTLE_NO_SIGNAL_S >= SETTLE_S`、`STALL_S > SUBMIT_LOST_S` 无人检查。
- 不支持多 worker（状态全在进程内），文档应写明；浏览器惰性启动，首个请求承担启动成本，可加 `WARM_START`。

### 4.9 日志与可观测性（与另两版一致）

- **流式请求日志上下文很可能丢失**：`log_context` 在路由返回 `StreamingResponse` 时就退出，而生成发生在之后迭代响应体的阶段，所以最长那段日志没有 `request_id` / `session_key`。（推断自 contextvars + Starlette 执行顺序，未在真实请求验证，见第 7 节。）
- `X-Request-Id` 未清洗就写日志与响应头；需白名单字符 + 长度上限。
- `bridge_commands` 直接打印会话 URL、桶键与命令首行，未走 `redact`；`handle_command` 的 `exc_info=True` 会把完整堆栈写进日志。
- 多个指标从未被增加（见第 5 节），`/metrics` 上的零值会误导排障。

### 4.10 流式细节（与另两版一致）

- 检测到「重写回复」后不再发送，却仍以 `finish_reason=stop` 收尾，客户端拿到截断文本且无提示。至少加 `stream_rewritten_total` 指标，并在最终块或 notes 里说明。
- `generate` 每轮轮询都调用 `on_delta(normalized)`，即使内容没变；只在内容变化时回调。
- 流式最后一块没有 `usage`，忽略 `stream_options.include_usage`。
- 已见回复时若节点短暂消失，`read_new` 抛的 `ReplyExtractionError` 会让整个请求失败；窗口很小，但已看到正文时应保留上次文本继续判定。

### 4.11 回复外壳剔除与工具解析边界（与另两版一致）

- 文本兜底按前缀 `Claude said:` 剥离，正文以此开头（引用）会被误剥；末行恰为时间戳的日志 / 代码类回复会丢最后一行。建议只在 DOM 侧确认存在 `sr-only` / `<time>` 时才启用兜底。
- `strip_tool_call_blocks` 只处理围栏与无围栏两种形态；DSML / `json` 围栏 / `tool_uses` 路径的原始标记会残留在正文。
- 工具参数只检查「是 JSON 对象」，不核对 `required` 与类型，而文档字符串写的是「参数形状校验」。可加轻量 schema 校验，失败作为诊断返回。
- `toolcalls.py` 逾 700 行，建议拆「提取」与「校验 / 强转」两层，补随机化测试（嵌套代码块、重复调用、半截 JSON）。

### 4.12 状态与快照落盘（与另两版一致）

- 每次 `save_state` 都 `load_document()` 再整体写回；同步 IO 在 asyncio 里持线程锁，会阻塞事件循环。建议内存持文档 + 变更时原子写盘，必要时 `asyncio.to_thread`。
- `TaskSnapshot.last_reply` 把完整回复明文写进 `user_data/.claude_tasks`，`goal` 含用户消息前 2000 字；应可配置为不落正文，并在 README 说明。

## 5. 写了但没用上的东西（**我做了全仓库 grep 复核**）

> 处理原则：**要么接上，要么删掉并同步 `design.md`**，不要保留「看起来有保护」的空壳。

**从未被增加的指标**（`metrics.py` 声明，`claude_web` 内无 `inc` / `observe` 调用点）：
`request_retry_total`、`browser_unavailable_total`、`browser_selector_miss_total`、`reply_extraction_failure_total`、`session_recovery_total`、`tool_parse_failure_total`，以及耗时 `request_latency`、`reply_extraction_latency`。
（实际只在 `service.py` 增加：`request_total` `session_busy_total` `tool_call_total` `request_success_total` `request_error_total` `request_context_limit_total` `request_timeout_total`，以及 `browser_generation_latency` 计时。）

**未被消费的配置**（`config.py` 定义，`claude_web` 内无读取点）：
`STREAM_KEEPALIVE_S`（`server.py:374` 用写死的 `_KEEPALIVE = 10.0`）、`TASK_KEEP_MESSAGES`、`READY_TIMEOUT_MS`、`FILL_TIMEOUT_MS`、`NEW_SESSION_ON_START`、`SAVE_DEBUG_ARTIFACTS`、`OUTPUT_DIR`、`SUPPORTED_MODELS`（仅兼容导出）、`CLAUDE_WEBSITE`（`HOME_URL` 用的是它，实际有读，不算死）、`ENV_FILE` / `PROJECT_ROOT`（模块内部用）。
（`CHALLENGE_EXCLUDE_SELECTORS` 经 `conversation_exclude_selectors()` 间接使用，不算死。）

**状态字段**：
`SessionState.cap_failures` 只写不读（注释说会换更小的播种预算，未实现）。
`TaskSnapshot.messages` / `recoveries` 与状态 `queued` / `generating` / `recovering` 从未被生产代码设置；`TaskStore.load` 在 `claude_web` 内**无调用点**（只有 `tests` 用），即生产代码没有任何「按任务快照恢复」的路径。

**函数 / 常量**：
`models.model_is_supported`（仅测试用）、`session/lock.WARNED_BAD_STATE`（`store` 自己有 `_warned_bad_state`）、`errors.is_page_lost_error` / `is_timeout_error` 在 `claude_web` 内无调用方；`errors.page_usable_reason` / `page_responds` 只在 `tests/e2e/probe.py` 与测试用——**页面判活其实应该用在 `ensure_usable` 与 readiness 里**，现在没用上。

**其它**：
`prompting.py` 里 `_OMISSION_NOTE` 的注释重复了两遍；`/bridge modal` 注册为「改状态」实际不改会话状态；`session_store.py` 是纯委托门面；`main.py` 与 `claude_api_server.py` 并存且都启动服务（`main.py` 只有 docstring + `main()`，无 `__main__` 外的额外逻辑，属重复入口）。

## 6. P2：工程化与体验

- **CI**：无 `.github/workflows`。最小方案：`ruff check` + `pytest`（跳过 e2e）+ 版本矩阵；e2e 手动触发，并在 `doc/tasks.md` 记录最近一次真实探测日期。
- **类型**：`pyproject.toml` 提到 mypy 但无 `[tool.mypy]`。先对 `errors` / `session/*` / `models` / `end_detection` 开启，或删掉注释。
- **依赖**：`requirements.txt` 无版本约束且与 `pyproject.toml` 重复（以后者为准）；`requires-python >=3.13` 偏严，代码看起来不依赖 3.13 特性，放宽可让更多人用上。
- **ruff**：目前只选 `E4/E7/E9/F/I`，可逐步加 `B`、`UP`、`SIM`。
- **目录整洁**：根目录有 `user_data_chrome.bak-*`、`pre-refresh-*` 等含登录 Cookie 的 profile 备份（已被忽略），建议统一放到用户缓存目录；`.gitignore` 里 `.freebuff/` 重复。
- **测试补强**：`generate` 端到端时序（重写 / 取消 / 停滞 / 超时延长）；复用会话回归（页面被换、系统提示变化、`link --seed`、URL 精确比较）；同桶串行 / 跨桶并行 / 管理操作取锁；tool 结果里含工具说明标题；`tests/e2e` 增「会话里含特征文案但可正常对话」「条数停在基线但页面已回答」两条真实用例。
- **指标**：每桶排队等待、播种与增量次数、重写次数、取消次数、选择器回退命中、`structural=True` 认回复次数；`/metrics` 可提供 Prometheus 文本格式。
- **文档**：`design.md` 已近 700 行，建议把「结束判定与回复识别」拆成独立文档；README 加「现象 -> 日志关键字 -> 处理」对照表（`prompt_submission_failed` / `generation_stalled` / `context_limit_reached` / `reply_not_found` / `challenge_required` / `usage_limit_reached`）。
- **提交习惯**：按主题拆分提交，提交前看 `git status` 与 `git diff --stat`。

## 7. 未覆盖 / 不确定

- `browser/selectors.py` 的 JS 片段（`GENERATING_STATE_JS` / `MODAL_PROBE_JS` / `REPLY_STATE_JS` / `BODY_TEXT_JS`）未逐行读全；4.3 中「页面仍在生成」的判定依赖它们。
- 4.9 中「流式请求日志上下文丢失」是依据 contextvars 与 Starlette 执行顺序**推断**，未在真实请求上验证。建议加一条断言式测试或真实抓一次日志确认。
- 4.4 中 Responses 事件集是否满足 Codex CLI，需真实客户端验证，不能从代码断定。
- `tests/*`、`tools/bench_pipeline.py` 未通读；未重跑 pytest；未跑真实浏览器 E2E。
- 第 5 节的「无调用点」结论基于 `claude_web/` 包内 grep；若有包外（如 `tools/`、`claude_api_server.py`）间接使用，请以全仓库为准复核。

## 8. 建议实施顺序

1. **3.1 工具说明判重（状态优先）+ 3.2 超时延长**：改动小、影响面最大，各配回归测试。
2. **3.3 管理操作取桶锁 + 4.9 请求 ID 清洗 + 4.6 内部错误码**。
3. **4.1 驱动 / 页面池加锁 + 4.2 回收与 `mark_busy` 接线**（这一步同时消掉「驱逐在用页面」的真实风险）。
4. **3.4 历史指纹**（先只记录并打日志，确认无误判后再切换为触发重播）+ **4.3 取消与页面忙碌处理**。
5. **4.4 Responses 流式 / 事件集 / 线程续接**，配真实客户端探测。
6. **4.5 提示词体量 + 4.8 配置路径与启动校验 + 4.7 会话比较与 reset 语义**。
7. **第 5 节清理**（接上或删除）+ 4.10 / 4.11 / 4.12。
8. **第 6 节工程化**。

## 9. 与前两份评审的关系

- 与 `doc/update_claude.md`、`doc/update_chatgpt.md` 的 P0 判断一致（工具说明误抑制、超时截断、管理操作取锁、增量一致性）。两者均含鉴权整改建议，但**本项目不考虑鉴权**，故本文已移除相关章节。
- 本文新增：对「死配置 / 死指标 / 死字段」做了**全仓库 grep 复核**（第 5 节），并明确 `tool_choice`、`_KEEPALIVE`、`TaskStore.load` 三处未被消费。
- 取舍：工具说明判重**优先采纳 ChatGPT 版的「记录已发送工具清单摘要」状态方案**，Claude 版的「只扫 system/user」仅作短期兜底。
- 未在本文重复的细节论证，请直接参阅上述两份文档；三份文档如出现冲突，以**本次 grep 复核过的第 5 节**与各节标注的「实测 / 推断」为准。
