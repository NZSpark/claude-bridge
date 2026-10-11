# Claude 网页 Cloudflare 拦截问题排查记录

## 1. 现象与处理结果

本次排查 ClaudeBridge 访问 `https://claude.ai/new` 时遇到的 Cloudflare 安全拦截及探测结果误报问题。

首次真实页面探测出现以下现象：

- `page.goto()` 返回 HTTP `403`。
- 页面仍然存在部分可交互的应用界面，输入框选择器可以命中。
- 状态探测结果出现 `page_probe.auth = false`，但适配器的 `authenticated` 为 `true`，存在状态信号不一致。
- 输入区和发送按钮可以命中，回复节点、生成状态选择器均未命中。
- 旧版探测脚本只检查页面是否存活、输入区是否存在、登录状态及挑战状态，没有检查导航 HTTP 状态，因此最后错误地报告 `ok: true`。

随后修正探测脚本的成功条件，并重新运行了真实页面探测。第二次结果为 HTTP `200`、`navigation_ok: true`、`authenticated: true`、`challenged: false`、`composer: true`、`ok: true`，进程退出码为 `0`。

**注意：** 第二次成功说明当时页面已能正常打开且探测条件通过；不能据此断言首次 403 的具体服务端原因，也不能保证 Cloudflare 不会再次拦截。

## 2. 关键修复：HTTP 403 不得被误报成功

修改文件：`tests/e2e/probe.py`。

### 2.1 显式记录导航状态

在 `page.goto()` 之后记录 HTTP 状态，并增加 `navigation_ok`：

```python
nav_status = report.get("nav_status")
report["navigation_ok"] = (
    isinstance(nav_status, int) and 200 <= nav_status < 400
)
```

导航抛出异常时，脚本记录 `nav_error`。没有获得有效的 HTTP 响应状态时，`navigation_ok` 不会被视为成功。

### 2.2 把导航结果纳入最终成功条件

最终 `ok` 必须同时满足：

- `navigation_ok` 为真；
- 页面在探测结束时仍然可用，即 `page_usable_reason(page)` 返回 `None`；
- 输入区存在；
- 页面状态判断为已登录；
- 未检测到安全挑战。

关键判定为：

```python
report["ok"] = bool(
    report["navigation_ok"]
    and unusable is None
    and status.composer
    and status.authenticated
    and not status.challenged
)
```

这样，即使 HTTP 403 的响应还残留部分 HTML 或可命中的输入框，也不能仅凭 DOM 命中就报告探测成功。HTTP 状态是页面可用性的必要条件之一，而不是唯一条件。

## 3. 安全挑战页的识别与人工处理

相关代码：

- `claude_web/browser/selectors.py`：`PAGE_PROBE_JS`、`SELECTOR_VERSION`。
- `claude_web/browser/dom_adapter.py`：`ClaudeDOMAdapter.page_status()`、`ensure_usable()`。
- `claude_web/config.py`：`CHALLENGE_INDICATORS`、`CHALLENGE_TEXT_PATTERNS`。
- `tests/e2e/probe.py`：等待、重新探测和报告逻辑。

### 3.1 不要只依赖挑战页选择器

Cloudflare / Turnstile 的安全验证页面可能没有常见的挑战 iframe 或特定 CSS 类，标题也未必包含明显的验证字样。因此项目同时检查：

- 挑战页候选选择器；
- 页面标题中的挑战文案；
- 页面可见文本中的挑战文案。

文案候选集中在 `CHALLENGE_TEXT_PATTERNS`，例如 `performing security verification`、`security verification`、`verify you are human`、`checking your browser`、`just a moment` 和 `attention required`，还包括现有中文候选。

这些规则是启发式探测：既要避免漏检，也要留意普通页面误匹配。新增或调整候选时，应该配套测试正常页面和挑战页面。

### 3.2 等待页面稳定后重新检查

挑战页可能在页面初始加载之后执行 JavaScript 跳转。探测脚本会：

1. 导航并等待 `domcontentloaded`；
2. 尝试等待 `networkidle`，默认等待窗口为 15000 毫秒；
3. 再等待 3 秒；
4. 重新检查页面的登录状态、安全挑战状态和输入区。

等待 `networkidle` 超时不一定意味着失败，因为某些网页会持续产生网络请求；应该综合最终 HTTP 状态、挑战检测和 DOM 状态进行判断。

### 3.3 由用户手动完成验证或登录

脚本支持 `--wait-human`。页面尚不可用时，可在有头浏览器窗口中由用户本人完成站点要求的验证或登录，脚本定期重新探测状态。

该机制只等待人工操作，不自动点击验证框、不绕过安全挑战、不代填账号或密码。若验证没有完成，探测应返回失败状态，并根据报告继续诊断。

## 4. 复用现有 Chrome 登录状态时的注意事项

本次探测使用的浏览器报告包括：

- `launch_mode: chrome-cdp`；
- `channel: chrome`；
- `cdp_port: 9222`；
- `profile_directory: Default`；
- `headless: false`；
- `os_keychain: true`。

这些是本次运行报告中的配置，不代表所有环境都应使用相同设置。需要复用已有 Chrome 登录状态时，应确认 Chrome 的远程调试端口、配置目录、浏览器启动方式和钥匙串配置彼此匹配，避免误用另一个空白或未登录的浏览器环境。

CDP 模式可能连接到用户正在使用的 Chrome，因此探测脚本采用以下安全方式：

- 记录开始前已有的标签页数量；
- 使用 `context.new_page()` 新建专用探测页；
- 不关闭或导航用户已有的标签页；
- 探测结束时只关闭本次新建的页面，再由 Driver 按其生命周期约定处理连接清理。

不要为了排查而直接清空 Chrome 用户配置目录、删除 Cookie 或关闭用户已有标签页。若怀疑登录状态失效，应先在普通 Chrome 窗口中确认账号状态。

## 5. 复现与验证命令

在项目根目录执行。

### 5.1 运行只读页面探测

```bash
.venv/bin/python -m tests.e2e.probe --wait-human 30 --verbose-selectors
```

脚本默认使用配置中的网站入口。可以把 URL 作为第一个参数传入以探测指定页面；不要把包含敏感会话信息的完整 URL 随意粘贴到公开日志或工单。

探测脚本不提交提示词，也不输出页面正文或会话标题；报告中的 URL 仅用于状态分析，并对路径作有限度的摘要处理。

重点检查：

- `nav_status`：实际导航响应状态；
- `navigation_ok`：导航响应是否满足 HTTP 状态判定；
- `final_url_host`、`final_url_path_segments`：最终落点摘要；
- `authenticated`、`challenged`、`challenge_reason`：登录和挑战状态；
- `composer`、`composer_hits`：输入区是否命中；
- `selector_healthy`、`selector_detail`：各组候选的命中情况；
- `page_unusable_reason`：页面是否已关闭或无响应；
- `ok`：综合判定结果。

### 5.2 运行自动测试和代码检查

```bash
.venv/bin/python -m py_compile tests/e2e/probe.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

本次修正后的验证结果：

- Python 语法检查通过；
- 完整默认测试集通过，2 项跳过；
- Ruff 检查通过；
- 随后真实页面探测返回 HTTP `200`、`navigation_ok: true` 和 `ok: true`，退出码为 `0`。

真实页面探测是手动运行的联网检查，不属于默认 pytest 套件。

## 6. 回复提取选择器的后续验证

成功打开 `/new` 只能说明新对话页面的基本状态满足探测条件。该页面没有历史助手回复，因此 `reply_nodes: 0`，回复选择器全部未命中并不足以证明它们已失效。

同样，`generating` 选择器没有命中可能只是因为当时没有生成任务。

后续应在一个已正常登录、包含历史助手回复的真实会话页面中，检查：

1. `RESPONSE_SELECTORS` 是否至少命中实际的助手回复节点；
2. `reply_count()` 是否能正确统计回复；
3. 生成状态选择器在非生成和生成状态下是否符合预期；
4. 是否仍存在导航状态或登录状态信号不一致的情况。

只有获得这些结果后，才能决定是否需要调整回复提取选择器。不要为了验证选择器而自动提交无关提示词。

## 7. 再次遇到拦截时的排查顺序

1. 先重新运行只读探测，记录 `nav_status`、`navigation_ok`、`challenged`、`authenticated` 和最终 URL 摘要。
2. 若返回 403 或检测到挑战页，先在普通有头 Chrome 中查看页面实际状态，并由用户手动完成必要验证。
3. 如果状态仍不一致，使用 `--verbose-selectors` 检查选择器命中情况，并核对 `page_probe` 与适配器的 `page_status`；不要仅根据输入框存在就断言页面可用。
4. 确认 Chrome CDP、登录配置和页面落点无误后，再重试一次。
5. 如果持续出现 403，应保留必要的状态码和非敏感诊断信息，检查站点访问限制或服务端情况；不要通过伪装指纹、自动绕过验证或删除用户浏览器数据来处理。

## 8. 结论

本次已修复的是**探测结果误报成功**的问题：最终成功条件现在明确包含导航 HTTP 状态。重试时网页返回 HTTP 200，登录状态与输入区检查通过，探测退出码为 0。

本次并未查明首次 403 的服务端根因，也没有把 Cloudflare 验证当作可以永久消除的问题。回复提取和生成状态选择器仍需在适合的真实会话页面中验证。
