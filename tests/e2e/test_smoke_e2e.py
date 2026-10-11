"""真实网页 E2E 冒烟测试（doc/e2e_test_design.md）。

默认**全部 skip**；只有显式设置 ``CLAUDE_E2E=1`` 且已在本机持久化浏览器中
完成 Claude 登录后才会运行。默认测试套件绝不触发外网请求。

运行：

    CLAUDE_E2E=1 HEADLESS=false .venv/bin/python -m pytest tests/e2e -q

前置条件：
* ``playwright install chromium`` 已执行；
* ``USER_DATA_DIR`` 下的 profile 已登录 claude.ai；
* 遵守站点正常使用限制，不要用它反复跑高成本长回复。
"""

from __future__ import annotations

import asyncio
import os

import pytest

E2E_ENABLED = os.environ.get("CLAUDE_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason="真实 E2E 需显式开启：CLAUDE_E2E=1（见 doc/e2e_test_design.md）",
)


def test_readiness_reports_browser_state():
    from claude_web.driver import ClaudeWebDriver

    async def scenario():
        driver = ClaudeWebDriver()
        try:
            return await driver.readiness()
        finally:
            await driver.close()

    report = asyncio.run(scenario())
    # 无论成功与否都必须给出可区分的状态，而不是抛裸异常。
    assert report.ok in (True, False)
    if not report.ok:
        assert report.error_code is not None


def test_single_turn_returns_new_reply():
    """最小真实链路：一条短提示词必须能返回确定的新回复。"""
    from claude_web.browser.dom_adapter import ClaudeDOMAdapter
    from claude_web.completion.generator import ReplyGenerator
    from claude_web.completion.lifecycle import SessionLifecycle
    from claude_web.driver import ClaudeWebDriver
    from claude_web.models import NormalizedMessage

    async def scenario():
        driver = ClaudeWebDriver()
        try:
            report = await driver.readiness()
            if not report.ok:
                pytest.skip(f"浏览器未就绪：{report.error_code} {report.error}")
            lifecycle = SessionLifecycle(driver)
            conversation = await lifecycle.prepare(
                "e2e", [NormalizedMessage(role="user", content="Reply with the single word: pong")]
            )
            generator = ReplyGenerator(ClaudeDOMAdapter(), poll_interval=1.0)
            result = await generator.generate(conversation.page, conversation.prompt)
            return result.text
        finally:
            await driver.close()

    assert asyncio.run(scenario()).strip()
