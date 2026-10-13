# Token 速度统计设计方案（`/bridge token`）

> 目标：为 ClaudeBridge 增加 **prefill 速度** 与 **decode 速度** 统计，通过 `/bridge token` 命令查看。
>
> 核心难点：本桥不是直接访问大模型，而是 `客户端 → bridge → 浏览器 → Claude 网页 → 浏览器 → bridge → 客户端`。页面上**没有 token 级事件**，只有「整段回复快照」的轮询。因此必须先把「测什么」定义清楚，再谈公式。

---

## 1. 问题陈述

### 1.1 为什么不能照搬 API 的 token/s

客户端直接调用大模型 API（如 OpenAI / Anthropic）时，`usage` 字段给出精确的 `prompt_tokens` / `completion_tokens`，配合流式事件的到达时间，可以算出：

- **prefill 速度** = `prompt_tokens / 首 token 延迟(TTFT)`
- **decode 速度** = `completion_tokens / (末 token 时间 − 首 token 时间)`

但本桥面临三个差异：

1. **没有精确 token 数**：只有字符数，token 数靠 `estimate_tokens ≈ len//3` 估算（`prompting.estimate_tokens`）。
2. **没有 token 级流**：网页只渲染整段文本；桥用轮询「回复快照」的增量来合成 SSE（见 `doc/design.md` 3.5「合成流」）。
3. **存在四段额外时延**：客户端网络、bridge 进程内调度、Playwright/DOM 往返、网页版推理+渲染，全部叠在同一条时间线上。

### 1.2 我们真正要回答的问题

用户/开发者看 `/bridge token`，想知道的是：

- 这次（或平均）**网页版模型**的 prefill / decode 有多快？（→ 与「直接 API」可比的那部分）
- 我的 bridge 引入的额外时延占了多少？（→ 桥接效率）
- 是不是网络 / 页面渲染拖慢了体感？（→ 归因）

因此设计必须**区分「模型速度」与「端到端速度」**，不能只报一个大数。

---

## 2. 端到端时间线分解

一次非流式请求，从桥的视角看有这些时刻（都用 `time.perf_counter()`，单调、不受系统时钟调整影响）：

```
t0  bridge 收到请求（server 中间件进入，或 _run_locked 开始）
t1  lifecycle.prepare 完成：页面就绪、提示词构造完成
t2  fill_composer 完成：提示词写进网页输入区
t3  submit（Enter）返回
t4  页面首次出现「新回复 / 正在生成」信号（首字节可观测）
t5  页面首次读到**非空回复文本**（TTFT 的可观测代理）
t6  页面自述不再生成 + 内容静默落定（最后一轮 read）
t7  桥完成回复读取与收尾，构造好响应
t8  客户端收到完整响应（仅流式路径可观测：SSE `[DONE]` 写出）
```

对应的时延段：

| 段 | 定义 | 包含什么 |
| --- | --- | --- |
| **队列/调度** | `t1 − t0` | 桶锁等待、任务快照落盘、页面获取 |
| **prepare** | `t2 − t1` | `build_prompt`、`ensure_usable`、开新对话/导航 |
| **fill** | `t3 − t2` | 写输入框（含 `_read_back` 校验与重试） |
| **submit** | `t4 − t3` | Enter 到页面开始响应的观测延迟 |
| **prefill（可观测）** | `t5 − t3` | 提交后到**首个非空回复文本**出现 = TTFT 代理 |
| **decode（可观测）** | `t6 − t5` | 首个文本到「落定」；期间文本持续增长 |
| **收尾** | `t7 − t6` | 最后一轮 read、tool 解析、响应构造 |
| **回传** | `t8 − t7` | 流式 SSE 写出到客户端（非流式只在客户端侧可见） |

> **关键区分**：`t5` 是「页面渲染出第一个字」，它**晚于**模型真正的 TTFT——中间还有网页前端把流式 token 合成段落、DOM 更新、我们的轮询间隔。因此 `prefill(可观测)` 是**上界**（偏大）。

---

## 3. 公式设计

### 3.1 记号

- `P_chars` / `C_chars`：提示词 / 回复正文的**字符数**（`len(prompt)` / `len(result.text)`）。
- `P_tok = estimate_tokens(prompt)`，`C_tok = estimate_tokens(reply)`（≈ 字符数 / 3）。
- `TTFT = t5 − t3`：首字延迟（time-to-first-token 的可观测代理）。
- `T_decode = t6 − t5`：从首字到落定的时长。
- `T_gen = t6 − t3`：提交到落定（prefill + decode 的可观测总和）。

### 3.2 观测速度（与「直接 API」口径尽量对齐）

**prefill 速度**（tokens/s）：

```
prefill_tps = P_tok / max(TTFT, ε)
```

- 分子用**输入 token**（提示词），分母用首字延迟。这与 API 场景「prompt_tokens / TTFT」同构。
- 但要注意：API 的 TTFT 是**模型产出首 token** 的时刻；本桥的 TTFT 是**页面渲染出首字**的时刻，二者差一个「网页合成 + 轮询粒度」。因此本值会**系统性偏小**，标注为「观测口径（偏保守）」。

**decode 速度**（tokens/s）：

```
decode_tps = C_tok / max(T_decode, ε)
```

- 分子用**输出 token**（回复正文），分母用首字到落定的时长。
- 同样偏小：`t5` 偏晚、`t6` 是「静默 SETTLE_S 秒后才落定」，会把解码时长**多算** `≈ SETTLE_S`（默认 2.5s）。

### 3.3 修正项：扣掉可归因的桥接 / 落定开销

为了让「观测速度」更接近「模型速度」，引入**保守修正**（只减能明确归因的部分，绝不过度修正）：

**decode 时长修正**：落定判定要求内容静默 `SETTLE_S` 秒（或探不到信号时的 `SETTLE_NO_SIGNAL_S`）。这 `SETTLE_S` 不属于解码时间。

```
T_decode_adj = max(T_decode − settle_credit, ε)
settle_credit = SETTLE_S  (若本轮 saw_generating=True)
              = SETTLE_NO_SIGNAL_S (若无生成信号，实际未采用)
```

> 说明：`_log_end`/`verdict.note` 已记录 `saw_generating`，可直接复用。仅在「确实观测到生成中」时才扣 `SETTLE_S`——否则没有依据。

**prefill 时长修正**：桥在提交前可能做了 `settle_before_submit` 等待（最多 `PRE_SUBMIT_WAIT_S`）与 `fill` 重试，但这些发生在 `t3` 之前，已不计入 `TTFT`。真正污染 `TTFT` 的是**轮询粒度**：首字出现后，最多要等一个 `POLL_INTERVAL_ACTIVE_S`（默认 0.4s）才被读到。

```
TTFT_adj = max(TTFT − poll_credit, ε)
poll_credit = POLL_INTERVAL_ACTIVE_S / 2   # 期望半周期
```

### 3.4 面向客户端的速度（端到端口径）

**仅流式路径**有意义（非流式客户端只在最后一次性拿到全文，无「增量到达」的概念）：

```
e2e_decode_tps = C_tok / max(t8 − t_first_sse_content, ε)
```

- `t_first_sse_content`：桥写出**第一个含正文的 SSE chunk** 的时刻（`on_delta` 首次触发、或首个 `encoder.content()` 写出）。
- 这对应客户端直接调 API 时的「decode 观测速度」——即用户在流式界面上**看到文字冒出来的速度**。
- 与非流式的 `decode_tps` 不同：这里包含**回传**（桥→客户端）。

**端到端 TTFT**：

```
e2e_ttft = t_first_sse_content − t0
```

这对应客户端直接调 API 时的「首 token 延迟」体感。

### 3.5 桥接开销占比

```
bridge_overhead_ratio = 1 − (T_gen_adj / (t8 − t0))
```

其中 `T_gen_adj = TTFT_adj + T_decode_adj`。它回答「端到端时间里，有多少不是模型生成」。**只作为诊断**，不追求精确。

---

## 4. 与「直接 API」的对应表

| 直接 API 指标 | 本桥对应 | 口径差异 |
| --- | --- | --- |
| `prompt_tokens` | `P_tok`（估算） | 字符数/3，非精确 |
| `completion_tokens` | `C_tok`（估算） | 同上 |
| TTFT | `TTFT_adj` | 含网页合成 + 半轮询周期，**偏大** |
| decode tokens/s | `decode_tps`（修正后） | 含渲染与轮询，**偏小** |
| 流式 decode 速度（客户端观测） | `e2e_decode_tps` | 含回传，**偏小** |
| — | `bridge_overhead_ratio` | 本桥特有诊断 |

**结论**：本桥给出的是「**网页版在真实桥接链路下的有效速度**」，不是「模型裸速度」。文档与命令输出都必须**明确标注口径**，避免误导。

---

## 5. 数据采集点（落地设计）

### 5.1 埋点位置

| 时刻 | 采集点 | 现状 |
| --- | --- | --- |
| `t0` | `server.chat_completions` 进入 / `_run_locked` 开始 | 需新增（`time.perf_counter()`） |
| `t1` | `service._run_locked` 中 `lifecycle.prepare` 返回后 | 需新增 |
| `t2` | `generator.generate` 中 `self._fill` 返回后 | 需新增 |
| `t3` | `generator.generate` 中 `self.dom.submit` 返回后 | 需新增 |
| `t5` | 生成循环里 `reply_seen` 首次为真且 `normalized` 非空时 | 需新增 |
| `t6` | 循环 `break`（`verdict.action == 'finish'`）时 | 需新增 |
| `t8` | 流式：SSE 生成器 `finally` 前 / `_stream_chat_completion` 结束 | 需新增 |

### 5.2 承载对象

新增一个纯数据类 `TokenSpeed`（放 `claude_web/completion/token_speed.py`）：

```python
@dataclass
class TokenSpeed:
    # 输入
    prompt_chars: int = 0
    reply_chars: int = 0
    prompt_tokens: int = 0
    reply_tokens: int = 0
    # 时长（秒）
    ttft_s: float = 0.0            # t5 - t3
    decode_s: float = 0.0          # t6 - t5
    total_s: float = 0.0           # t6 - t3
    # 修正后
    ttft_adj_s: float = 0.0
    decode_adj_s: float = 0.0
    # 速度
    prefill_tps: float = 0.0
    decode_tps: float = 0.0
    # 端到端（仅流式）
    e2e_ttft_s: Optional[float] = None
    e2e_decode_tps: Optional[float] = None
    # 归因
    settle_credit_s: float = 0.0
    poll_credit_s: float = 0.0
    saw_generating: bool = False
    streamed: bool = False
```

- `GenerationResult` 增加字段 `speed: Optional[TokenSpeed]`。
- 计算逻辑放 `token_speed.py` 的纯函数 `compute_speed(...)`，便于单测穷举（与 `end_detection.py` 的纯函数风格一致）。

### 5.3 会话级聚合

`SessionState` 已有 `turns` / `est_tokens`。新增**滚动平均**（避免状态文件膨胀）：

```python
# SessionState 新字段（schema v1 内向后兼容，缺失时默认 0）
speed_samples: int = 0          # 已累计样本数
speed_prefill_tps_sum: float = 0.0
speed_decode_tps_sum: float = 0.0
speed_ttft_s_sum: float = 0.0
```

- 每轮成功后 `update(...)` 累加（已在 `lifecycle.note_success` 附近）。
- `/bridge token` 显示**本轮**（最近一次）与**该桶平均**两组数字。
- 只存**数值和**，不存正文、不存每次明细——符合「状态文件最小化 + 不落盘正文」的既有约束（`session/schema.py`）。

---

## 6. `/bridge token` 命令设计

### 6.1 命令注册

在 `bridge_commands.py` 的 `REGISTRY` 新增：

```python
CommandSpec(f"{BRIDGE_COMMAND} token", "最近一轮与本桶平均的 token 速度（prefill/decode）"),
```

解析：`parse_command` 的 `head == "token"` 分支（不接受参数，与 `status` 同级）。`action = "token"`。

### 6.2 输出示例

```
[Bridge] token 速度（桶 default；本轮 = 最近一次成功生成）：
  本轮：
    prefill  1 234 tok/s   （输入 3 700 tok / 首字 3.0s）
    decode      52 tok/s   （输出   420 tok / 解码 8.1s）
    TTFT 3.0s（修正后 2.8s）；decode 8.1s（修正后 5.6s，已扣落定 2.5s）
    端到端：TTFT 3.4s，流式 decode 48 tok/s
    桥接开销占比 ≈ 18%
  本桶平均（近 12 轮）：
    prefill  1 180 tok/s
    decode      55 tok/s
    TTFT 3.1s
  口径：token 数为字符数/3 的估算；速度为「网页版真实链路下的有效速度」，
        非模型裸速度。仅统计成功的生成。
```

### 6.3 边界与降级

- **无样本**（本进程启动后还没有成功生成）：显示「暂无样本；先发一轮普通请求」。
- **非流式请求**：不显示 `e2e_*`（那是流式专属），其余照常。
- **工具调用轮**：`C_tok` 用**被剔除围栏后的正文**估算（`result.text`），避免把工具 JSON 算进 decode。
- **重写（rewritten）轮**：`on_delta` 曾回退，`t5` 用首个非空 `normalized` 即可；decode 仍按最终文本算，但输出标注「本轮有重写，速度仅供参考」。
- **取消 / 失败轮**：**不计入**（与 `request_cancelled_total` / 失败统计一致）。
- **精度**：速度保留到整数 tok/s；时长保留 1 位小数。数字带千位分隔符便于阅读。

### 6.4 隐私

- 只输出**计数与时长**，不回显提示词 / 回复正文，不区分具体内容。
- 不泄露其它桶的信息（与 `status` 同一约束）。

---

## 7. 为什么这样设计是合理的

1. **口径诚实**：明确区分「观测速度（修正前）」「修正速度」「端到端速度」，并写清各自的偏向（prefill 偏大、decode 偏小）。用户不会把桥的数当成模型裸速度。
2. **与 API 可对应**：`prefill_tps = P_tok/TTFT`、`decode_tps = C_tok/T_decode` 与直接 API 的定义同构，可直接横向比较（在理解口径差异的前提下）。
3. **只做保守修正**：只扣**可明确归因**的 `SETTLE_S`（落定等待）与**半轮询周期**；不臆造其它修正项。修正量与依据都随结果一起输出（`settle_credit_s` / `poll_credit_s`），可审计。
4. **纯函数 + 可穷举测试**：`compute_speed` 与 `end_detection.evaluate_poll` 同风格，可用虚拟时钟测试断言「算了多少秒、得出多少 tok/s」，不依赖真实耗时。
5. **复用既有骨架**：埋点是给 `generator.generate` 的既有阶段（fill/submit/首字/落定）加时间戳，不改判定逻辑；聚合复用 `SessionState` 的落盘与缓存；命令复用 `/bridge` 的解析与本地应答机制。
6. **符合既有约束**：不落盘正文、状态文件只加数值和、只读命令不导航页面、不泄露。

---

## 8. 已知局限（必须在文档与命令输出里承认）

- **token 数是估算**：`len//3` 对中英文混排、代码、JSON 都会偏；绝对值不可当作精确计费依据。
- **TTFT 偏大**：包含网页前端合成 + 半轮询周期（≈0.2–0.4s）+ 渲染时间；修正只扣了轮询半周期，网页合成时间无法从外部观测。
- **decode 偏小**：`t6` 是「静默 SETTLE_S 后才落定」，已扣 `SETTLE_S`，但首字偏晚的误差无法完全消除；长回复受影响更小（误差被摊薄）。
- **轮询粒度限制分辨率**：`POLL_INTERVAL_ACTIVE_S=0.4s` 意味着短回复（<2s）的速度估计误差较大。命令应对**短回复**（如 `C_tok < 50`）加一句「样本过短，仅供参考」。
- **无真实模型对照**：无法知道「模型裸速度」真值，只能给有效速度。若将来能从网页 DOM 读到精确 token 计数（如 `data-token-count`），应优先采用并废弃估算。

---

## 9. 实施步骤（供后续落地）

1. `claude_web/completion/token_speed.py`：`TokenSpeed` 数据类 + `compute_speed()` 纯函数 + 单测（虚拟时钟穷举）。
2. `generator.generate`：加 `t2/t3/t5/t6` 时间戳，构造 `TokenSpeed`，挂到 `GenerationResult.speed`。
3. `service._run_locked`：加 `t0/t1`，成功后把数值累加进 `SessionState`（`speed_*` 字段）。
4. 流式路径（`server._stream_chat_completion` / `_stream_responses`）：记录首个正文 chunk 时刻与 `[DONE]` 时刻，补 `e2e_*`。
5. `session/schema.py`：`SessionState` 加 `speed_*` 字段（向后兼容，缺失默认 0）；`session_payload_only` 白名单同步。
6. `bridge_commands.py`：注册 `token` 命令 + `_token_reply()`；`parse_command` 加分支；帮助文本加一行。
7. 测试：`tests/test_token_speed.py`（纯函数）、`tests/test_bridge_commands.py`（命令输出）、`tests/test_generation_latency.py`（埋点接入）。
8. 文档：本文件 + README 的命令表加一行 + `doc/design.md` 4.4 附近提一句。
