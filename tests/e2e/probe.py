"""真实页面探测脚本（doc/design.md 4.3）。

**手动运行**，不属于 pytest 套件（默认套件不联网）。用途：

* 确认选择器候选是否命中（输入区 / 回复节点 / 生成状态 / 新对话入口）；
* 确认登录态、是否命中挑战页、导航落点是否符合预期。

只读：**不提交任何提示词**，也不打印页面正文或会话标题（只回布尔 / 计数 / 状态串）。

用法：

    .venv/bin/python -m tests.e2e.probe https://claude.ai/chat/<id>
    .venv/bin/python -m tests.e2e.probe            # 探测 CLAUDE_WEBSITE 默认入口
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from claude_web import config
from claude_web.browser.dom_adapter import ClaudeDOMAdapter
from claude_web.driver import ClaudeWebDriver
from claude_web.errors import page_usable_reason


async def probe(
    url: str | None,
    *,
    settle_ms: int = 15000,
    verbose_selectors: bool = False,
    wait_human_s: int = 0,
) -> dict:
    target = url or config.HOME_URL
    dom = ClaudeDOMAdapter()
    report: dict = {
        "target_path_segments": max(0, target.count("/") - 2),
        "headless": bool(config.HEADLESS),
        "channel": config.BROWSER_CHANNEL or "chromium",
        "profile_directory": config.BROWSER_PROFILE_DIRECTORY or None,
        "os_keychain": bool(config.BROWSER_USE_OS_KEYCHAIN),
        "sandbox": bool(config.BROWSER_SANDBOX),
    }
    report["launch_mode"] = config.BROWSER_LAUNCH_MODE
    report["cdp_port"] = config.BROWSER_CDP_PORT if config.BROWSER_LAUNCH_MODE == "chrome-cdp" else None
    driver = ClaudeWebDriver()
    context = None
    page = None
    try:
        await driver.start()
        context = driver._context
        if context is None:
            return {**report, "ok": False, "error": "浏览器上下文未就绪"}
        try:
            context.set_default_timeout(int(config.CLAUDE_BROWSER_TIMEOUT * 1000))
            # 不操作 Chrome 恢复的既有标签：CDP 模式可能连接用户正在使用的浏览器，
            # 关闭既有页面会造成数据丢失。探测始终使用新建页面。
            existing_pages = len(context.pages)
            page = await context.new_page()
            report["existing_pages_preserved"] = existing_pages
            try:
                response = await page.goto(target, wait_until="domcontentloaded")
                report["nav_status"] = getattr(response, "status", None)
            except Exception as exc:  # noqa: BLE001
                report["nav_error"] = type(exc).__name__
            # 只有 DOM 可见并不代表导航成功：HTTP 403 等错误响应也可能保留部分页面壳，
            # 不能仅凭输入框命中就报告 ok=true。
            nav_status = report.get("nav_status")
            report["navigation_ok"] = (
                isinstance(nav_status, int) and 200 <= nav_status < 400
            )
            # 挑战页常常在 JS 跑完后自行跳转，多等一会再判定。
            try:
                await page.wait_for_load_state("networkidle", timeout=settle_ms)
            except Exception:  # noqa: BLE001
                report["networkidle"] = "timeout"
            await asyncio.sleep(3)

            status = await dom.page_status(page)
            if wait_human_s and not status.usable:
                # 安全校验与登录都必须由**人**完成：这里只等待用户在已打开的有头窗口里
                # 自行通过验证 / 登录，然后重新探测；不代勾选、不绕过、不代填凭据。
                if status.challenged:
                    need = "站点安全校验（请在窗口里手动完成验证）"
                elif not status.authenticated:
                    need = "登录（请在窗口里登录你的 Claude 账号）"
                else:
                    need = "页面就绪（等页面加载完，或按需登录）"
                print(
                    f"[探测] 需要人工处理：{need}。"
                    f"本脚本最多等待 {wait_human_s}s，期间每 5s 重新探测一次…",
                    file=sys.stderr,
                    flush=True,
                )
                loop = asyncio.get_event_loop()
                deadline = loop.time() + wait_human_s
                while loop.time() < deadline:
                    await asyncio.sleep(5)
                    status = await dom.page_status(page)
                    if status.usable:
                        print("[探测] 页面已就绪，继续完成本次探测。", file=sys.stderr, flush=True)
                        break
                await asyncio.sleep(2)
                status = await dom.page_status(page)
            groups = {
                "input": config.INPUT_SELECTORS,
                "send": config.SEND_BUTTON_SELECTORS,
                "response": config.RESPONSE_SELECTORS,
                "generating": config.GENERATING_SELECTORS,
                "new_chat": config.NEW_CHAT_SELECTORS,
            }
            _, healthy = await dom.selector_diagnostics(page, groups)
            report.update(
                {
                    # URL 可能含会话 id：只保留主机与路径段数。
                    "final_url_host": (status.url.split("/", 3)[2] if "//" in status.url else ""),
                    "final_url_path_segments": max(0, status.url.count("/") - 2),
                    "authenticated": status.authenticated,
                    "challenged": status.challenged,
                    "challenge_reason": status.challenge_reason,
                    "composer": status.composer,
                    "composer_hits": status.composer_hits,
                    "reply_nodes": await dom.reply_count(page),
                    "generating": await dom.is_generating(page),
                    "context_limit": await dom.detect_context_limit(page),
                    "selector_healthy": healthy,
                    "page_probe": await dom.page_probe(page, config),
                }
            )
            if verbose_selectors:
                detail, _ = await dom.selector_diagnostics(page, groups)
                report["selector_detail"] = detail
            # 页面必须在探测的**最后**仍然活着：否则 PageStatus 会退回默认值
            # （authenticated=True / composer 取值陈旧），把一次失败误报成 ok:true。
            # 「不能确认成功就不报告成功」在探测工具里同样适用。
            unusable = await page_usable_reason(page)
            if unusable:
                report["page_unusable_reason"] = unusable
            report["ok"] = bool(
                report["navigation_ok"]
                and unusable is None
                and status.composer
                and status.authenticated
                and not status.challenged
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        # chrome-cdp 模式由 Driver 负责断开 CDP 并清理其自行启动的 Chrome。
        await driver.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaudeBridge 页面探测（只读）")
    parser.add_argument("url", nargs="?", default=None, help="要探测的 URL（默认 CLAUDE_WEBSITE）")
    parser.add_argument("--settle-ms", type=int, default=15000, help="等待网络空闲/挑战跳转的毫秒数")
    parser.add_argument("--verbose-selectors", action="store_true", help="附带每条选择器的命中数")
    parser.add_argument(
        "--wait-human",
        type=int,
        default=0,
        help="命中安全校验时，等待人工在有头窗口中完成验证的秒数（默认 0=不等待）",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(
        probe(
            args.url,
            settle_ms=args.settle_ms,
            verbose_selectors=args.verbose_selectors,
            wait_human_s=args.wait_human,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
