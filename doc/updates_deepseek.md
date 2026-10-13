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
- ~~**D3.4 / T3.4** — 失败类别指标。~~ **已完成**：`metrics.COUNTERS` 新增 `usage_limit_reached_total` / `prompt_submission_failed_total` / `reply_not_found_total` / `context_limit_reached_total`；`service._FAILURE_COUNTERS` 用「错误码 → 计数器」映射表驱动（新增类别只改一处，也便于测试穷举），在 `_record_failure` 末尾查表 `inc()`。取消**不进**映射表——它在 `_record_failure` 更早分支就已 `inc("request_cancelled_total")` 并 `raise`，沿用既有计数，避免造出「写了没用上」的新死键。回归 `tests/test_failure_metrics.py` 五条：四类错误码各一条 + 成功路径对照，断言「只增对应计数、不误增其它」，且 `request_error_total` / `request_context_limit_total` 等既有计数不丢。**踩坑记录**：`claude_web.metrics.metrics` 是进程级单例，`make_service` 未注入时默认共用它，导致第一个测试的计数泄漏到后续测试（表现为「成功路径竟然增了 usage_limit 计数」）；修法是每个测试注入独立 `MetricsRegistry()`。
- ~~**D3.5 / T3.5** — 配置路径与列表解析。~~ **已完成**：新增 `config.env_path()`——相对路径一律以 `PROJECT_ROOT` 解析、显式绝对路径原样保留，四处路径键（`SESSION_FILE` / `USER_DATA_DIR` / `TASK_FILE_DIR` / `OUTPUT_DIR`）全部走它，换 cwd 启动不再丢登录态；`env_list()` 改为**只按 `||` 切分**（逐项 strip、丢空串），不再按逗号切，`:is(a,b)` 这类合法选择器原样保留；`LINK_ALLOWED_HOSTS` 含 `*` 时启动 `warnings.warn` 强告警。**踩坑记录**：`tests/test_config_drift.py` 的 `_ENV_CALL` 正则原先只认 `env_(str|int|float|bool|list)`，路径键改用 `env_path` 后会漏出契约检查，已把 `path` 加进正则；`test_config.py` 原有一条断言「逗号也切分」与本次语义冲突，按新契约改为「逗号不切分、`||` 才切分」。
- ~~**D3.6 / T3.6** — 启动校验与摘要日志。~~ **已完成**：`config.config_warnings()` 返回人类可读问题清单，`config.summary_lines()` 输出生效值；`server.lifespan` 逐条告警 + 打一行摘要（`_SUMMARY_KEYS` 显式不含 `BRIDGE_TOKEN` / `RESET_TOKEN`）。三条跨键约束（锁超时 vs 单轮超时、`SETTLE_NO_SIGNAL_S` vs `SETTLE_S`、`STALL_S` vs `SUBMIT_LOST_S`）+ 三个废弃键都覆盖。**设计取舍**：校验**返回**问题而不抛异常——默认值本身就命中第一条（`BUCKET_LOCK_TIMEOUT_S`=120s < `RESPONSE_TIMEOUT_S`=300s），抛异常等于默认配置都跑不起来；改为日志告警，既暴露问题又不破坏既有部署。回归 `tests/test_startup_config.py` 七条。**踩坑记录**：`conftest.py` 的 autouse `fast_config` 把 `RESPONSE_TIMEOUT_S` 降到 5s，导致「默认值冲突」在测试里测不到；改用既有的 `default_timing` fixture 恢复真实默认值。**遗留**：`BUCKET_LOCK_TIMEOUT_S` 默认值 120s 与单轮超时 300s 的冲突仍是现状（仅告警，未改默认值），是否调整默认值待单独评估。
- ~~**D3.7 / T3.7** — 会话比较与绑定。~~ **已完成（URL 精确比较部分）**：新增 `_conversation_key(url)`，`_same_conversation` 与 `lifecycle.link` 落点校验都改为解析 URL、按主机 + 规范化路径**精确**比对（忽略查询串 / 片段 / 末尾斜杠，其余逐字相同）。修掉了 `startswith(target[:len(target)-1])` 把「会话 ID 只差末位」误判成同一条的 bug。**未做**：绑定桶触顶后换新会话 / 明确报错（仍待办）。

### P2（API、提示词、日志、工程化）
- ~~**D4.1 / T4.1** — Responses 真流式。~~ **已完成**：`_stream_responses` 改为与 chat 路径同一骨架（后台 produce + 队列 + 保活 + 断连取消 + 错误事件），生成期间逐段发 `response.output_text.delta`。`responses_adapter` 新增三个增量事件构造函数，`response_events` 复用它们。**顺带消除死键**：写死的 `_KEEPALIVE = 10.0` 换成 `_keepalive_s()` 读 `config.STREAM_KEEPALIVE_S`（§4 表格里的 `STREAM_KEEPALIVE_S` 未接线问题由此关闭）。回归六条见 `tests/test_responses.py`。**发现的真实语义问题**：网页重写回复时纯增量会与最终文本不一致（测试用 `snapshots=["答复一","最终完整答复"]` 复现）；处理方式与 chat 路径对齐——重写后停止发增量，最终文本以 `response.completed` 为准，并在测试里显式锁定该契约。
- ~~**D4.2 / T4.2** — `store_thread` 保留 `tool_calls` / `tool_call_id`。~~ **已完成**：新增 `_message_to_payload()` 保真序列化（带非空 `tool_calls` / `tool_call_id`）；`store_thread` 增加 `tool_calls` 参数，有工具调用时记成带 `tool_calls` 的 assistant 消息；`server.py` 非流式与流式两处调用都传 `outcome.result.tool_calls`。**关键坑**：存储必须用 `ToolCall` 的**嵌套**形态（`{id,type,function:{...}}`）——回灌走 `ChatMessage(**item)`，其 `tool_calls` 是 `List[ToolCall]`，存内部扁平形态会在 pydantic 校验处 `ValidationError: tool_calls.0.function Field required`。这是测试逼出来的（我先按「与内部表示统一」写成扁平，pydantic 直接拒绝）。回归两条：工具循环续接、无工具调用时纯文本。
- **D4.3 / T4.3** — 补齐 Responses 事件集并用真实客户端（Codex CLI 等）探测，结果记入 `doc/tasks.md`。
- ~~**D4.4 / T4.4** — 流式细节。~~ **已完成（三项）**：① `generator` 的 `on_delta` 加 `not normalized != emitted` 判定，只在内容变化时回调（此前每轮轮询都回调、队列堆重复项）；② 新增 `stream_rewritten_total` 指标，chat / Responses 两条路径首次检测到重写时 `inc`（`if not rewritten` 防重复）；③ `StreamEncoder.usage()` + `server._include_usage` + `chat_adapter.to_task` 透传 `stream_options` + `ChatTask.stream_options` 字段，支持 `stream_options.include_usage`（usage 块在 finish_reason 之后、[DONE] 之前）。回归 `tests/test_stream_details.py` 三条。**踩坑**：`ChatTask` 起初没有 `stream_options` 字段（该字段只在 `ChatCompletionRequest` 上），`to_task` 传参时 `TypeError`，已补。**未做**：已见回复后读取节点短暂失败时保留上一次文本——评审中属推断，需先验证再改。
- **D4.5 / T4.5** — 提示词体量：增量模式只在重播种 / 工具清单变化 / 每 N 轮重发工具说明；调大 `SYSTEM_MAX_CHARS`；`SEED_ENV_NOTE` 默认留空。依赖 D1.1。
- ~~**D4.6** — `tool_choice` 生效（DeepSeek 版独有）。~~ **已完成**：新增 `prompting.tool_choice_effect()` 纯函数 + `build_prompt(tool_choice=...)`；`none` 不注入工具说明，`required` / 指定函数在说明后点名；`SessionLifecycle.prepare` / `service.run` 透传。回归 `tests/test_tool_choice.py` 六条。**关闭 §4 死键表里的 `tool_choice` 未接线项**。
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
| `NEW_SESSION_ON_START` | ~~`config.py`~~ | **已删除（D6.2）**：无消费点且不在 .env.example 契约里。 |
| `SAVE_DEBUG_ARTIFACTS` | ~~`config.py`~~ | **已删除（D6.2）**：声明了却无落盘实现，属空壳。 |
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

---

# 附录 A：全项目复评（2026-10-13 · HEAD `cbf5af5`）

> 本文是上面台账之后的**又一次独立全项目复评**，不是重写。上面的 §0–§6 保留（撰写于 HEAD `f8758a7`）；本附录以**当前 HEAD `cbf5af5` + 一个未提交的工作区改动**为准重新核对。
>
> 方法：`grep -rln <符号> claude_web/` 逐一核对接线点（排除 `__pycache__`）；`ruff check .` 与 `pytest -q` 实跑；逐个读关键模块（`server.py` / `service.py` / `config.py` / `driver.py` / `dom_adapter.py` / `generator.py` / `lifecycle.py` / `toolcalls.py`）。所有结论都标了**实测落点**，不照抄任何一份旧评审。

## A0. 复评结论（一句话）

台账 §3 的 P0/P1/P2 大部分已落地，代码质量仍然很高；本轮复评发现的问题**已全部收口**（1 个必须修的破窗 + 2 个接线缺口 + D6.3 函数清单 + A3.3 默认冲突 + D7.1/D7.2 工程化），不构成重构理由。

**最重要发现（必须先修）**：工作区里有一处**未完成的改动**——`claude_web/browser/dom_adapter.py` 的 `_find_visible` 用了 `is_page_lost_error` / `page_lost_reason` / `NotReadyError` 三个名字，但**该文件没有 import 它们**。`ruff check .` 直接报 3 条 `F821 Undefined name`；这是「一运行就 `NameError`」级别的破窗，必须先补 import 才能谈别的。**本会话已就地修复**（见 A1）。

## A1. 本轮实际改动：补全 D6.3 的 page-lost 快速失败

### 问题

未提交的工作区把 D6.3（「页面已死时不继续试下一个选择器」）写了一半：

```python
# claude_web/browser/dom_adapter.py:212
if is_page_lost_error(exc):        # ← 未 import
    raise NotReadyError(           # ← 未 import
        page_lost_reason(page),    # ← 未 import
        detail={"selector": selector, "error": type(exc).__name__},
    ) from exc
```

三个名字都在 `claude_web/errors.py`（`NotReadyError` 见 `:123`，`is_page_lost_error` 见 `:247`，`page_lost_reason` 见 `:311`），但 `dom_adapter.py` 顶部的 `from ..errors import (...)` 只导了 5 个错误类，没导它们。

### 修复（已完成）

```python
from ..errors import (
    AuthenticationRequiredError,
    ChallengeRequiredError,
    ComposerNotFoundError,
    NotReadyError,          # 新增
    PromptSubmissionError,
    ReplyExtractionError,
    is_page_lost_error,     # 新增
    page_lost_reason,       # 新增
)
```

### 验证

- 改前：`.venv/bin/ruff check .` → `Found 3 errors`（3× `F821`）。
- 改后：`All checks passed!`；`pytest -q` → `292 passed, 2 skipped`。

### 语义评估（为什么这个改动方向是对的）

`_find_visible` 遍历 `INPUT_SELECTORS` 候选，逐个 `query_selector_all`。旧行为：**任一**选择器抛异常都只 `hits.append(...)` 然后 `continue` 下一个。当页面已死（标签关闭 / 渲染进程崩溃）时，每个候选都会各自抛一次、各自白等一次超时，最终错误退化成「输入区没找到」（`composer_not_found`），掩盖了「页面根本没了」这个更准确、更可操作的原因。

`is_page_lost_error` 的存在意义正是区分「页面没了」与「选择器没命中 / 普通交互失败」（见 `errors.py:247` 的注释与 `_PAGE_LOST_TOKENS`）。**页面已死时快速失败、抛 `NotReadyError(page_lost_reason(page))`**，与 `service.py` / `generator.py` 里既有的「页面失效 → 不可恢复、快速冒泡」路径一致（`generator._UNRECOVERABLE_FILL_ERRORS` 不包含 `NotReadyError`，但 `NotReadyError` 是 `BrowserError`，会走 `service._record_failure` 如实上报）。

### 遗留（本轮未做，留给后续）

1. ~~没有回归测试~~ **已补**：`tests/test_browser.py` 新增两条——`test_find_visible_fails_fast_when_page_is_lost`（页面已死 → 抛 `NotReadyError`，`detail.error` 是页面失效类异常名，消息含「标签已失效」）与 `test_find_visible_keeps_trying_on_ordinary_selector_error`（普通异常仍逐个试候选）。`tests/fakes.py` 的 `FakePage` 新增 `query_raise` 钩子以模拟死页面。
2. **`is_timeout_error` 仍无生产用点**：D6.3 同时列了 `errors.is_timeout_error`，但当前 `claude_web/` 里除定义外**零调用**（`grep -rn is_timeout_error claude_web/` 只命中 `errors.py:253` 定义）。要么接进 `_find_visible` 的「超时 = 没命中、其它异常 = 记诊断」判定，要么按 D6 原则删除并同步 `design.md`。（本轮未做，仍待决。）

## A2. 「写了没用上」再复核（当前 HEAD `cbf5af5` 实测）

> 复核方法：`grep -rln <符号> claude_web/ --include=*.py | grep -v __pycache__`。与台账 §4 的旧结论逐项对齐。

| 符号 | 当前实测落点 | 结论 |
| --- | --- | --- |
| `READY_TIMEOUT_MS` | `config.py:206`、`driver.py:366,370,375` | **已接线**（D6.2）：包住 `readiness` 的 `page_status`，超时回 `browser_unavailable`。旧结论失效。 |
| `FILL_TIMEOUT_MS` | `config.py:207`、`dom_adapter.py:519,523,528` | **已接线**（D6.2）：包住 `fill_composer` 重试循环，超时抛 `PromptSubmissionError`。旧结论失效。 |
| `MAX_PAGE_POOL` | `config.py`、`page_pool.py`、`server.py` | **已接线**。 |
| `BROWSER_SANDBOX` / `BROWSER_USE_OS_KEYCHAIN` | `config.py`、`driver.py` | **已接线**（`browser_launch_options` / `browser_ignore_default_args` 纯函数）。 |
| `TOOL_CALL_BUFFER` / `TOOL_CALL_FENCE` | `config.py`、`generator.py`、`toolcalls.py` | **已接线**。 |
| `TOOLS_INSTRUCTION_VERBOSE` / `TOOLS_DESC_MAX_CHARS` | `config.py`、`toolcalls.py` | **已接线**。 |
| `ENABLE_DEBUG_DOM` | `config.py`、`server.py` | **已接线**（`/debug/dom` 条件注册）。 |
| `TASK_SNAPSHOT_ENABLED` / `TASK_KEEP_MESSAGES` | `config.py`、`tasks.py` | **已接线**。 |
| `TASK_GOAL_MAX_CHARS` / `TOOL_RESULT_MAX_CHARS` / `SEED_ENV_NOTE` | `config.py`、`prompting.py` | **已接线**。 |
| `MAX_SESSION_STATE_CACHE` | `config.py`、`session/store.py` | **已接线**。 |
| `PRE_SUBMIT_WAIT_S` | `config.py`、`generator.py` | **已接线**（`settle_before_submit`）。 |
| `model_is_supported` | `models.py:126` 定义 + **本会话新增** `api/chat_adapter.py` `to_task` | **已接线**（D6.3 / 本会话）：未知别名记一条 INFO 诊断日志（宽松兼容不变），有回归测试。 |
| `WARNED_BAD_STATE` | ~~`session/lock.py`~~ / ~~`session/__init__.py`~~ | **已删除**（D6.3 / 本会话）：真正的「损坏只 warning 一次」在 `SessionStore._warned_bad_state` 实例属性里，模块级同名标志是影子副本，删掉（连同 `__init__` 导出）。有回归测试锁定不再出现。 |
| `page_usable_reason` / `page_responds` | `errors.py` 定义；`page_alive` 被 `lifecycle.py:20,185`、`driver.py:332,336` 用；**本会话新增** `driver.py` 的 `readiness` | **已接线**（A3.1 / 本会话）：`page_usable_reason` 已接进 `readiness` 的判活慢路径；`page_responds` 由 `page_usable_reason` 内部调用。`ensure_usable` 仍只走 `page_status`（见 A3.1 说明，不构成缺口）。 |
| `is_timeout_error` | `errors.py:253` 定义 + **本会话新增** `dom_adapter.py` `_find_visible` | **已接线**（A3.2）：超时 → 记 `:timeout` 并继续试候选。 |
| `is_page_lost_error` | `errors.py:247` 定义 + **本会话新增** `dom_adapter.py` `_find_visible` | **已接线**（A1）：页面已死 → 抛 `NotReadyError`。 |

> **D6 死代码清单更新（本会话收尾）**：台账 §4 里标「未接线」的 `READY_TIMEOUT_MS` / `FILL_TIMEOUT_MS` 现已接线；`page_usable_reason` / `page_responds` / `is_timeout_error` / `is_page_lost_error` 已接线；`model_is_supported` 已接线；`WARNED_BAD_STATE` 已删除。**D6.3 的函数清单至此全部收口**。

## A3. 新发现的分层/一致性问题（按影响排序）

### A3.1（中，**已修复**）`page_usable_reason` 的保护没有真正用上——「误报成功」缺口

`errors.page_usable_reason` 的 docstring 自陈用途是**防止误报成功**：页面死掉后 `PageStatus` 退回默认值（`authenticated=True`、`composer` 陈旧），只看那些字段会把一次失败探测报成 `ok: true`。

但实测这条慢路径**没有接进任何生产判定**：

- `driver.readiness()`（`driver.py:352-395`）只调 `dom.page_status(page)` 并给它套了 `READY_TIMEOUT_MS` 超时；没有在 `page_status` 返回后、或在其抛异常时调 `page_usable_reason(page)` 复核。
- `dom_adapter.ensure_usable()`（`dom_adapter.py`）同样只走 `page_status`。

后果：渲染进程崩溃 / 挂起但 `is_closed()` 仍为 `False` 时，`page_status` 里的 `page.evaluate(PAGE_PROBE_JS)` 会抛异常 → 被 `except` 吞掉、`info = {}` → `status` 保持默认（`authenticated=True`、`composer=False`）。`readiness` 会据此报 `authenticated=True` 但 `composer=False`，`ok=False`（这一档**恰好**没误报成功，因为 composer 默认 False）。**真正的风险在 `page_status` 里已有的 composer 探测**：`_find_visible` 对死页面若走「元素仍在但不可见」的路径，`status.composer` 可能被置 True，从而 `report.ok = authenticated and composer = True` —— 这正是 docstring 警告的场景。

**建议**：在 `driver.readiness()` 的 `page_status` 之前补一次 `await page_usable_reason(page)`；非 `None` 时把 `report.ok=False`、`error_code=BROWSER_UNAVAILABLE`、`detail={"reason": ...}`。这是**接线既有函数**，不是新逻辑，风险低。

**已落地（本会话）**：`driver.py` 导入 `page_usable_reason`，在 `report.page = True` 之后、`page_status` 之前插入慢路径判活；不可用即 `return report`（不再拿陈旧 / 默认的 `PageStatus` 做判断）。回归 `tests/test_browser.py` 三条：`test_readiness_flags_dead_but_unclosed_page`（`evaluate` 抛错 + `is_closed()` 仍 False → `browser_unavailable`）、`test_readiness_flags_closed_page`（快路径）、`test_readiness_ok_for_live_usable_page`（活页面不被误伤，仍 `ok=True`）。

### A3.2（中，**已修复**）`is_timeout_error` 与 `_find_visible` 的语义缺口

`_find_visible` 现在把「任一选择器抛异常」一律记为 `hits.append(f"{selector}:{type(exc).__name__}")` 然后 `continue`。`is_timeout_error` 的 docstring 明确说「定位元素时只有超时算『没命中』」——但目前没有任何地方用这个区分。

**建议**：与 A1 的 page-lost 快速失败合并处理——`_find_visible` 里 `is_page_lost_error(exc)` → 快速失败（已做）；否则若 `is_timeout_error(exc)` → 记 `:timeout` 并继续；其它异常 → 记诊断并继续（保持现状）。这样 `is_timeout_error` 有了用点，语义也更准。

**已落地（本会话）**：`_find_visible` 的 `except` 分支改为——`is_page_lost_error` → 抛 `NotReadyError`；否则 `reason = "timeout" if is_timeout_error(exc) else type(exc).__name__`，记进 `hits` 后 `continue`。回归 `tests/test_browser.py` 两条：`test_find_visible_labels_timeout_as_not_found`（超时 → 每条命中以 `:timeout` 结尾）、`test_find_visible_keeps_trying_on_ordinary_selector_error`（普通异常 → 以 `:RuntimeError` 结尾）。`is_timeout_error` 由此有了生产用点。

### A3.3（低，**已修复**）`BUCKET_LOCK_TIMEOUT_S` 默认值与单轮超时冲突

`config.config_warnings()` 默认就会对默认配置报警：`BUCKET_LOCK_TIMEOUT_S`=120s < `RESPONSE_TIMEOUT_S`=300s。台账 §3 P1 标注「是否调整默认值待单独评估」。

**语义分析**：`BUCKET_LOCK_TIMEOUT_S` 是**同桶排队等待**超时，`RESPONSE_TIMEOUT_S` 是**单轮生成**超时。同桶的第二个请求必须等第一个**完整跑完一轮**（最多 300s）才轮得到它；默认 120s < 300s 意味着排队请求会在前一个请求仍**合法生成**时就先超时（503），表现为「同桶并发请求莫名失败」。这是真实缺陷，不是可接受的告警。

**决策（本会话）**：把默认提到 `300s`（= `RESPONSE_TIMEOUT_S`，最小满足不变量的值；`MAX_SESSION_BUCKETS=4` 保证排队不会无限堆积）。改动点：`config.py` 默认值、`.env.example`、`README.md` 配置表。`config_warnings()` 的跨键检查保留（用户手动调低仍会告警）。

**测试调整**：`tests/test_startup_config.py` 原用例「默认值本身命中冲突」改为「默认值**不**命中冲突」+ 新增「手动降到 `RESPONSE_TIMEOUT_S` 以下必须被检出」；`tests/conftest.py` 的 `default_timing` 补钉 `BUCKET_LOCK_TIMEOUT_S=300`，避免本机 `.env` 的旧值泄漏进「默认值」契约测试。

> **注意（本机 `.env`）**：仓库里的 `.env`（未跟踪）仍写着 `BUCKET_LOCK_TIMEOUT_S=120`，它会覆盖新默认值。本机部署若要消除冲突，请把 `.env` 里这行改成 `300`（或删除该行用默认值）。

### A3.4（低）工程化缺口（D7 大部分未动）

- ~~**无 CI**：`.github/` 不存在。~~ **已补（本会话，D7.1）**：`.github/workflows/ci.yml` 跑 `ruff check .` + `mypy` + `pytest -q`（e2e 默认 skip）。
- ~~**无 mypy 配置**~~ **已补（本会话，D7.2）**：`pyproject.toml` 新增 `[tool.mypy]`，`files` 只覆盖已全绿的 `errors` / `models` / `end_detection` / `session`（8 个源文件 `Success: no issues found`）；其余 5 处历史遗留（`toolcalls` / `page_pool` / `prompting` / `responses_adapter` / `server`）待逐步修完再扩清单。`mypy` 已加入 `dev` extras。
- **`requirements.txt` 与 `pyproject.toml` 并存且不同步**：`requirements.txt` 无版本约束，`pyproject.toml` 有上下界。D7.3 待办。
- **ruff 规则集仍很小**（`E4,E7,E9,F,I`）：D7.4 建议逐步加 `B`/`UP`/`SIM`。
- **项目根目录有 profile 备份**：`user_data_chrome.bak-20261011-191138`、`user_data_chrome.pre-refresh-20261011-193653` 两个目录在项目根，D7.5 建议迁出。

### A3.5（低）`_OMISSION_NOTE` 的「重复注释」（D6.4 待办）

台账 D6.4 提到 `prompting.py` 里 `_OMISSION_NOTE` 有重复注释。实测 `prompting.py:60` 定义、`:514` 使用，两处**没有重复定义**，但注释确实有冗余（同一说明在两处附近各写一遍）。属清理项，无行为影响。

## A4. 值得保留的设计（复评再次确认）

以下设计在复评中**再次确认正确**，不应在后续「清理」中被误删：

- **时长驱动结束判定**（`end_detection.evaluate_poll` + `IdleClock`），轮询间隔只影响「多快看到变化」。
- **两把独立钥匙识别新回复**（条数 / 结构信号，`ReplyState.trailing_reply`），对虚拟化回收保守。
- **工具调用默认缓冲 + 校验后才外发**，绝不执行模型生成命令（`generator.generate` 末尾的 `parse_tool_calls` 分支）。
- **额度弹窗只点关闭类控件**，绝不点升级 / 付费（`MODAL_CLOSE_TEXT_PATTERNS` 的措辞约束）。
- **挑战 / 会话到顶扫描剔除会话正文**（`conversation_exclude_selectors()`），避免「会话里引用特征文案」的假阳性（doc/cloudfare.md 3.4 事故）。
- **`/diagnostics` `/metrics` 不返回正文**；绑定 URL 强制 HTTPS + 主机白名单 + 会话路径（`validate_link_url`）。
- **浏览器惰性导入**，离线测试用替身驱动真实适配器。
- **非流式断连保护**（`_run_with_disconnect_guard`），且 `finally` 里只发取消不 await watcher（避免挂住整条路由）。

## A5. 本轮建议的落地顺序（增量，不打断现有节奏）

1. ~~**立即**：补 `dom_adapter.py` 的 import~~ **已完成**（A1）——否则 `ruff` / 运行都不通过。
2. ~~**D6.3 收尾**（P1）~~ **大部分已完成**：
   - ~~给 A1 的 page-lost 快速失败加回归测试~~ **已完成**（`test_find_visible_fails_fast_when_page_is_lost` 等）。
   - ~~决定 `is_timeout_error` 的去留~~ **已完成**：接进 `_find_visible`（A3.2，记 `:timeout` 并继续试候选），有回归测试。
   - ~~**仍待决**：`model_is_supported`、`WARNED_BAD_STATE` 的去留~~ **已完成**：`model_is_supported` 接进 `to_task` 诊断日志；`WARNED_BAD_STATE` 删除（影子副本）。
3. ~~**A3.1**（P1，接线既有保护）~~ **已完成**：`page_usable_reason` 接进 `driver.readiness()`，关闭「死页面误报成功」缺口，配三条回归测试。
4. ~~**A3.3**（P1）：就 `BUCKET_LOCK_TIMEOUT_S` 默认值做一次明确决策~~ **已完成**：默认提到 300s（= `RESPONSE_TIMEOUT_S`），同步 `.env.example` / README；测试从「默认命中冲突」改为「默认不冲突 + 手动调低才告警」。
5. ~~**D7.1 / D7.2**（P2，工程化）：补最小 CI 与 mypy 配置~~ **已完成**：`.github/workflows/ci.yml`（ruff + mypy + pytest）；`[tool.mypy]` 覆盖四个已全绿模块，`dev` extras 加 `mypy`。
6. **D7.3 / D7.4 / D7.5**（P2，工程化）：`requirements.txt` 去重加约束、ruff 加 `B`/`UP`/`SIM`、profile 备份迁出项目根。**（未做）**

## A5b. tasks 文档状态核对（2026-10-13）

按「代码实测」而非「文档自称」核对 `doc/tasks_deepseek.md` / `doc/tasks_claude.md` 的勾选状态，更新如下：

| 任务 | 原状态 | 新状态 | 依据 |
| --- | --- | --- | --- |
| D6.3 / T6.3 函数接线 | `[ ]` | **`[x]`** | 本会话完成（`readiness` / `_find_visible` / `to_task` / 删 `WARNED_BAD_STATE`），6 条回归 |
| D7.1 / T7.1 最小 CI | `[ ]` | **`[x]`** | `.github/workflows/ci.yml`（ruff + mypy + pytest） |
| D7.2 / T7.2 mypy | `[ ]` | **`[x]`** | `[tool.mypy]` 四模块零错误，接进 CI |

**核对中发现的真实缺口（未标记完成，反而补注）**：

- **D6.1 / T6.1（指标）——未完成**：`metrics.COUNTERS` / `LATENCIES` **声明了** `request_retry_total` / `browser_unavailable_total` / `browser_selector_miss_total` / `reply_extraction_failure_total` / `session_recovery_total` / `tool_parse_failure_total` / `request_latency` / `reply_extraction_latency`，但 `grep -rn '\.inc('` 显示**只有 9 个计数器有调用点**，这 6 个计数器与 2 个延迟**零调用**（`browser_generation_latency` 是唯一被 `timer()` 用到的）。属「声明了没接」的空壳，按 D6 原则须接上或删除。**这正是 A2 表里我此前的疏漏**：只看了 `metrics.snapshot()` 能列出键，没验证 `inc()` 调用点。已在 `tasks_deepseek.md` D6.1 补注。
- **D6.4 / T6.4（其它）——部分**：`_OMISSION_NOTE` 实测**只有一处定义 + 一处使用，没有重复注释**（该子项可划掉）；`TaskStore.load / clear` 经 `grep` 仍**无生产调用方**（`responses_adapter` 用的是另一个 `response_state.load`）；`/bridge modal` 标注、`session_store.py` / `main.py` 去留未处理。
- **D1.5(B) / T1.4(B)——未完成**：`lifecycle.py` 的历史指纹核对仍**只观测**（代码注释明确写「等真实任务验证无误判后再切换为触发重播种」），未切换为「对不上 → 换新对话重播」。

> **方法学教训**：`metrics.COUNTERS` 是**声明**清单，`snapshot()` 会把声明原样列出——「键存在」不等于「被使用」。判断死代码必须 grep **调用点**（`inc(` / `timer(`），不能只看定义 / 输出。这条已回填到 D6.1 的注记里。

## A6. 复评验证记录

- HEAD：`cbf5af5`（工作区含本会话的改动）。
- `ruff check .`：修复前 `Found 3 errors`（3× `F821`）→ 修复后 `All checks passed!`。
- `pytest`：修复前 `292 passed, 2 skipped` → **本会话收尾后 `309 passed, 2 skipped`**（新增 10 条回归测试：A1 两条、A3.1 三条、A3.2 一条、D6.3 收口三条、A3.3 一条）。
- **本会话实际改动文件**：
  - `claude_web/browser/dom_adapter.py` — 补 import（A1）+ `_find_visible` 区分 page-lost / timeout / 普通异常（A3.2）。
  - `claude_web/driver.py` — `readiness` 接入 `page_usable_reason` 慢路径判活（A3.1）。
  - `claude_web/session/lock.py` + `session/__init__.py` — 删除影子标志 `WARNED_BAD_STATE`（D6.3）。
  - `claude_web/api/chat_adapter.py` — `to_task` 接入 `model_is_supported` 诊断日志（D6.3）。
  - `claude_web/config.py` + `.env.example` + `README.md` — `BUCKET_LOCK_TIMEOUT_S` 默认 120 → 300（A3.3）。
  - `.github/workflows/ci.yml`（新增）+ `pyproject.toml` — 最小 CI（D7.1）+ `[tool.mypy]` 与 dev extra（D7.2）。
  - `tests/fakes.py` — `FakePage` 新增 `query_raise` 钩子；`tests/conftest.py` — `default_timing` 补钉 `BUCKET_LOCK_TIMEOUT_S`。
  - `tests/test_browser.py` / `tests/test_config_wiring.py` / `tests/test_startup_config.py` — 共 10 条回归测试。
  - `doc/updates_deepseek.md` — 本附录。
- 代码规模：`claude_web/` 约 13.9k 行（含测试 `tests/`），最大单文件 `dom_adapter.py` 778 行、`toolcalls.py` 714 行、`server.py` 696 行。

