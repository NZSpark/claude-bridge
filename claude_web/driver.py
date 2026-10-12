"""浏览器生命周期与 Driver 组装（doc/design.md 4.1、7）。

职责：启动 / 关闭 Playwright 持久化上下文、按桶提供页面、汇总 readiness。

设计约束：

* **惰性导入 Playwright**：这样离线单元测试与纯逻辑模块不需要安装浏览器依赖；
* **浏览器初始化失败不必然让 HTTP 服务退出**：健康检查与诊断端点照常可用，
  生成类请求通过 503 类响应报告尚未就绪；
* 登录资料保存在 ``USER_DATA_DIR`` 的持久化 profile 中，绝不复制进普通会话 JSON。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config
from .browser.dom_adapter import ClaudeDOMAdapter, PageStatus
from .errors import ErrorCode, NotReadyError
from .page_pool import PagePool

logger = logging.getLogger(__name__)

# 惰性依赖：不在导入期求值。测试可注入替身。
async_playwright = None  # type: ignore[assignment]


def _find_chrome_executable() -> str:
    """定位本机正式浏览器，供 CDP 模式启动；不使用 Playwright 自带的测试浏览器。"""
    if sys.platform == "darwin":
        if config.BROWSER_CHANNEL == "msedge":
            candidates = [
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        else:
            candidates = [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
    elif os.name == "nt":
        base = Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files"))
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        if config.BROWSER_CHANNEL == "msedge":
            candidates = [base / "Microsoft/Edge/Application/msedge.exe", local / "Microsoft/Edge/Application/msedge.exe"]
        else:
            candidates = [base / "Google/Chrome/Application/chrome.exe", local / "Google/Chrome/Application/chrome.exe"]
    else:
        names = ("microsoft-edge", "microsoft-edge-stable", "msedge") if config.BROWSER_CHANNEL == "msedge" else ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        candidates = [Path(found) for name in names if (found := shutil.which(name))]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("找不到本机 Chrome/Edge，请安装浏览器或使用 BROWSER_LAUNCH_MODE=playwright")


async def _cdp_port_open(port: int) -> bool:
    """仅检查回环地址的 CDP 端口是否正在监听。"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=0.25
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def browser_launch_args() -> list:
    """浏览器启动参数（纯函数，便于单测）。

    只叠加显式配置的 profile 目录；刻意**不**加任何 UA / 反检测定制：本项目依靠
    「复用已登录 profile」通过站点校验，而不是伪装浏览器 —— 伪装只会产生更可疑的
    指纹，也属于设计文档 1.2 不允许的绕过手段。
    """
    args = ["--no-first-run", "--no-default-browser-check"]
    if config.BROWSER_PROFILE_DIRECTORY:
        args.append(f"--profile-directory={config.BROWSER_PROFILE_DIRECTORY}")
    return args


def browser_ignore_default_args():
    """要从 Playwright 默认参数里移除的项（纯函数，便于单测）。

    仅在 ``BROWSER_USE_OS_KEYCHAIN`` 开启时移除 mock keychain 相关参数：
    macOS 上它们会让 Chromium 用 mock key 解密 cookie，导致复用已有 profile 时
    会话被站点判定为无效。返回 ``None`` 表示不改动默认参数。
    """
    if not config.BROWSER_USE_OS_KEYCHAIN:
        return None
    return ["--use-mock-keychain", "--password-store=basic"]


def browser_launch_options() -> dict:
    """集中构造 ``launch_persistent_context`` 的关键参数（纯函数，便于单测）。

    ``chromium_sandbox`` 默认开启：Playwright 默认会传 ``--no-sandbox``，既不安全
    又会在 Chrome 顶部留下 "unsupported command-line flag" 提示。开启沙箱只是去掉
    那个不需要的参数，属于安全加固，不是反检测定制。
    """
    return {
        "headless": bool(config.HEADLESS),
        "channel": config.BROWSER_CHANNEL or None,
        "args": browser_launch_args(),
        "ignore_default_args": browser_ignore_default_args(),
        "chromium_sandbox": bool(config.BROWSER_SANDBOX),
        "viewport": {"width": 1280, "height": 900},
    }


@dataclass
class Readiness:
    """readiness 报告：进程存活 ≠ 浏览器能处理请求（doc/design.md 9.2）。"""

    ok: bool = False
    browser: bool = False
    page: bool = False
    authenticated: bool = False
    composer: bool = False
    url: str = ""
    error: Optional[str] = None
    error_code: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "browser": self.browser,
            "page": self.page,
            "authenticated": self.authenticated,
            "composer": self.composer,
            "error": self.error,
            "error_code": self.error_code,
            "detail": self.detail,
        }


class ClaudeWebDriver:
    """持久化 Chromium 上下文 + 页面池的薄封装。"""

    def __init__(
        self,
        *,
        dom: Optional[ClaudeDOMAdapter] = None,
        pool: Optional[PagePool] = None,
    ) -> None:
        self.dom = dom or ClaudeDOMAdapter()
        self.pool = pool or PagePool(closer=self._close_page)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._chrome_process: Optional[subprocess.Popen] = None
        self._owns_context = True
        self._started = False
        self._start_error: Optional[str] = None
        #: 保护浏览器初始化：两个桶首次并发请求时，幂等检查本身挡不住重复启动。
        self._start_lock = asyncio.Lock()

    # ---- 生命周期 ----
    @property
    def started(self) -> bool:
        return self._started and self._context is not None

    @property
    def start_error(self) -> Optional[str]:
        """浏览器初始化失败的摘要（异常类型名）；未失败时为 ``None``。

        供 ``/bridge status`` 这类只读诊断读取：它们不该为了报状态而真的打开页面。
        """
        return self._start_error

    async def start(self) -> None:
        """启动浏览器（幂等且并发安全）：同一进程内只初始化一次（T2.1）。"""
        if self.started:
            return
        async with self._start_lock:
            if self.started:
                return
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        """实际的启动逻辑；调用方必须已持有 ``_start_lock``。"""
        global async_playwright
        try:
            if async_playwright is None:  # pragma: no cover - 真实依赖
                from playwright.async_api import async_playwright as _ap

                async_playwright = _ap
            self._playwright = await async_playwright().start()
            mode = config.BROWSER_LAUNCH_MODE
            if mode == "chrome-cdp":
                # 优先连接现成的本机 CDP Chrome；否则启动正式浏览器，而非 Playwright 的测试版。
                # 调试端口只绑定 127.0.0.1，避免将登录态和浏览器控制权暴露到网络。
                port = int(config.BROWSER_CDP_PORT)
                endpoint = f"http://127.0.0.1:{port}"
                if not await _cdp_port_open(port):
                    executable = _find_chrome_executable()
                    profile_dir = Path(config.USER_DATA_DIR).expanduser().resolve()
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    args = [
                        executable,
                        f"--remote-debugging-port={port}",
                        "--remote-debugging-address=127.0.0.1",
                        f"--user-data-dir={profile_dir}",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ]
                    if config.BROWSER_PROFILE_DIRECTORY:
                        args.append(f"--profile-directory={config.BROWSER_PROFILE_DIRECTORY}")
                    self._chrome_process = subprocess.Popen(
                        args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=(os.name != "nt"),
                    )
                    deadline = asyncio.get_running_loop().time() + 20
                    while not await _cdp_port_open(port):
                        if self._chrome_process.poll() is not None:
                            raise RuntimeError("Chrome 提前退出；请检查浏览器 profile 是否正被其他 Chrome 实例占用")
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError("等待 Chrome 的本机 CDP 端口启动超时")
                        await asyncio.sleep(0.2)
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint, timeout=10000
                )
                contexts = self._browser.contexts
                if not contexts:
                    raise RuntimeError("CDP Chrome 没有可用的默认浏览器上下文")
                self._context = contexts[0]
                # 连接到外部 Chrome 时不关闭其 context，避免退出服务时关闭用户浏览器。
                self._owns_context = False
            elif mode in ("playwright", ""):
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(config.USER_DATA_DIR),
                    **browser_launch_options(),
                )
                self._owns_context = True
            else:
                raise ValueError(f"不支持的 BROWSER_LAUNCH_MODE：{mode}")
            self._context.set_default_timeout(int(config.CLAUDE_BROWSER_TIMEOUT * 1000))
            self._started = True
            self._start_error = None
        except Exception as exc:  # noqa: BLE001
            self._start_error = type(exc).__name__
            logger.warning("[浏览器] 初始化失败：%s", type(exc).__name__)
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
            self._browser = None
            self._context = None
            await self._stop_owned_chrome()
            self._owns_context = True
            raise NotReadyError(
                "浏览器初始化失败。playwright 模式请确认已安装浏览器；chrome-cdp 模式请确认 Chrome 已以远程调试端口启动。",
                detail={"error": type(exc).__name__, "launch_mode": config.BROWSER_LAUNCH_MODE},
            ) from exc

    async def close(self) -> None:
        await self.pool.close_all()
        try:
            if self._context is not None and self._owns_context:
                await self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        await self._stop_owned_chrome()
        self._context = None
        self._browser = None
        self._playwright = None
        self._owns_context = True
        self._started = False

    async def _stop_owned_chrome(self) -> None:
        """只停止本 driver 启动的 Chrome；绝不关闭外部已运行的浏览器。"""
        process, self._chrome_process = self._chrome_process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        except Exception:  # noqa: BLE001
            pass

    async def _close_page(self, page: Any) -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 页面 ----
    async def _new_page(self) -> Any:
        if not self.started:
            raise NotReadyError("浏览器尚未启动", detail={"error": self._start_error})
        page = await self._context.new_page()
        try:
            await page.goto(config.HOME_URL, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[浏览器] 打开首页失败：%s", type(exc).__name__)
        return page

    async def page_for(self, bucket: str) -> Any:
        """取（必要时创建）某个会话桶的页面。"""
        if not self.started:
            await self.start()
        return await self.pool.acquire(bucket, self._new_page)

    async def open_new_conversation(self, page: Any) -> None:
        """把页面切到干净的新对话：先试「新对话」入口，退化为回到首页。"""
        from .errors import page_alive

        if await self.dom.open_new_chat(page):
            return
        if not page_alive(page):
            raise NotReadyError("页面已失效，无法新开对话")
        try:
            await page.goto(config.HOME_URL, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise NotReadyError("导航到 Claude 首页失败", detail={"error": type(exc).__name__}) from exc

    # ---- 状态 ----
    async def page_status(self, page: Any) -> PageStatus:
        return await self.dom.page_status(page)

    async def readiness(self) -> Readiness:
        """检查浏览器、登录状态与输入区是否满足处理请求的条件。"""
        report = Readiness()
        if not self.started:
            try:
                await self.start()
            except NotReadyError as exc:
                report.error = exc.message
                report.error_code = ErrorCode.BROWSER_UNAVAILABLE.value
                return report
        report.browser = True
        try:
            page = await self.page_for(config.DEFAULT_SESSION_KEY)
        except Exception as exc:  # noqa: BLE001
            report.error = "无法创建会话页面"
            report.error_code = ErrorCode.BROWSER_UNAVAILABLE.value
            report.detail = {"error": type(exc).__name__}
            return report
        report.page = True
        status = await self.dom.page_status(page)
        report.authenticated = status.authenticated and not status.challenged
        report.composer = status.composer
        report.url = status.url
        if status.challenge_suspect:
            # 命中了挑战特征但页面能用：只如实上报为诊断信息，不让它变成 503。
            report.detail["challenge_suspect"] = {
                "reason": status.challenge_reason,
                "evidence": status.challenge_evidence,
            }
        if status.challenged:
            report.error = "检测到站点安全挑战"
            report.error_code = ErrorCode.CHALLENGE_REQUIRED.value
            report.detail["challenge"] = {
                "reason": status.challenge_reason,
                "evidence": status.challenge_evidence,
                "composer_hits": status.composer_hits,
            }
        elif not status.authenticated:
            report.error = "尚未登录 Claude 网页"
            report.error_code = ErrorCode.AUTHENTICATION_REQUIRED.value
        elif not status.composer:
            report.error = "未找到输入区（页面可能改版或仍在加载）"
            report.error_code = ErrorCode.COMPOSER_NOT_FOUND.value
            report.detail = {"composer_hits": status.composer_hits}
        report.ok = report.authenticated and report.composer
        return report

    async def reap_idle(self) -> list:
        return await self.pool.reap_idle()

    def mark_busy(self, bucket: str, busy: bool) -> None:
        """标记某桶页面「正在被使用」：空闲回收与 LRU 驱逐都跳过它。"""
        self.pool.mark_busy(bucket, busy)


#: 进程级默认 driver（server 使用；测试可替换注入）。
driver = ClaudeWebDriver()


__all__ = ["ClaudeWebDriver", "Readiness", "driver"]
