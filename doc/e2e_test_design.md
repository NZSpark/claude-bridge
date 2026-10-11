# ClaudeBridge E2E 测试设计

对应 [doc/design.md](design.md) 11.2。真实网页测试**必须显式开启**，默认套件绝不
触发外网请求或消费用户网页额度。

## 运行方式与前置条件

```bash
playwright install chromium
cp .env.example .env          # 确认 USER_DATA_DIR / CLAUDE_WEBSITE
# 先有头启动一次并手动登录，登录态落在 USER_DATA_DIR
CLAUDE_E2E=1 HEADLESS=false .venv/bin/python -m pytest tests/e2e -q
```

判定矩阵：

| 条件 | 行为 |
| --- | --- |
| `CLAUDE_E2E != 1` | 全部 `tests/e2e` 用例 skip（`pytestmark`） |
| `CLAUDE_E2E=1` 且浏览器/登录未就绪 | 用例 `skip`，并打印 `readiness.error_code` |
| `CLAUDE_E2E=1` 且就绪 | 执行真实链路 |
| 命中站点安全挑战 | 报告 `challenge_required`，**不尝试绕过**，由用户在普通浏览器处理 |

频率约束：真实用例只用**短提示词**；不要用长回复反复压测状态机（那属于单元测试范畴）。

## 场景矩阵

已实现（`tests/e2e/test_smoke_e2e.py`）：

1. **readiness 区分状态**：浏览器不可用 / 未登录 / 挑战 / 输入区缺失必须给出可区分
   的 `error_code`，而不是裸异常。
2. **单轮新回复**：一条短提示词能提交并返回**确定的新回复**，不把历史页面内容误识别
   为当前答案。

后续按阶段补（优先级从高到低）：

3. 多轮历史播种后，模型能基于前文回答。
4. 流式最终合并文本与非流式结果结构一致（Markdown / 长文本 / 换行 / 代码块不截断不重复）。
5. 两个桶并发请求消息不串线；同一桶连续请求顺序确定。
6. 生成超时 / 页面丢失 / 未登录时返回正确错误分类。
7. 工具调用能转成合法 `tool_calls`，且工具结果轮次不污染其他桶。
8. 选择器变化时 `/diagnostics` 能指出具体缺失的交互元素。
9. 服务重启后安全恢复配置与会话元数据，不泄漏认证资料。

## 与单元测试的边界

| 维度 | 单元测试（默认套件） | E2E（显式开启） |
| --- | --- | --- |
| 网络 | 无 | 访问 claude.ai |
| 浏览器 | Fake Page / Fake Driver | 真实 Playwright Chromium |
| 覆盖 | 协议、状态机、解析器、会话、安全 | 选择器有效性、真实生成时序、登录态 |
| 失败定位 | 直接断言 | 依赖 `/diagnostics` 的选择器命中与 `selector_version` |

选择器一旦在 E2E 中失效，应把**实测确认过**的新选择器写回 `claude_web/config.py`
默认值与 `.env.example`，并在 [doc/tasks.md](tasks.md) 的待验证表中更新状态。
