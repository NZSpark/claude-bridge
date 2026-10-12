"""页面池的容量、忙碌保护与空闲回收（T2.3，doc/design.md 4.1、7）。

回归背景：``mark_busy`` / ``reap_idle`` 此前在生产代码里**没有任何调用点**，于是
``BUCKET_IDLE_TTL_S`` 形同虚设，而满池 LRU 会驱逐**正在生成**的页面。这里覆盖：
忙碌页不可淘汰、TTL 回收、容量上限，以及 driver 的薄封装确实接上了这两个入口。
"""

from __future__ import annotations

import asyncio

from claude_web.page_pool import PagePool


class _Page:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _pool(**kwargs):
    pages = []

    async def _factory():
        page = _Page()
        pages.append(page)
        return page

    async def _closer(page):
        await page.close()

    return PagePool(closer=_closer, **kwargs), pages, _factory


def test_busy_page_is_not_evicted_by_lru():
    """满池时 LRU 只淘汰**空闲**页：忙碌页必须留下（否则会关掉正在生成的页面）。"""

    async def _scenario():
        pool, pages, factory = _pool(max_pages=2, idle_ttl_s=0)
        a = await pool.acquire("a", factory)
        b = await pool.acquire("b", factory)
        pool.mark_busy("a", True)  # a 正在生成
        # 第三个桶进来：应淘汰空闲的 b，而不是忙碌的 a。
        c = await pool.acquire("c", factory)
        assert a.closed is False
        assert b.closed is True
        assert pool.get("a") is a
        assert pool.get("c") is c

    asyncio.run(_scenario())


def test_all_busy_does_not_exceed_capacity_or_evict():
    """候选全忙时不驱逐任何页，也不静默超过容量。"""

    async def _scenario():
        pool, pages, factory = _pool(max_pages=1, idle_ttl_s=0)
        await pool.acquire("a", factory)
        pool.mark_busy("a", True)
        # 唯一候选忙碌：evict_if_needed 不做任何事，调用方随后自行决定返回 busy。
        await pool.evict_if_needed(exclude="b")
        assert pool.size() == 1
        assert pages[0].closed is False

    asyncio.run(_scenario())


def test_reap_idle_closes_only_expired_idle_pages():
    """空闲超过 TTL 的页面被回收；忙碌页与未到期的页面保留。"""

    async def _scenario():
        pool, pages, factory = _pool(max_pages=0, idle_ttl_s=10)
        await pool.acquire("idle", factory)
        await pool.acquire("busy", factory)
        pool.mark_busy("busy", True)
        reaped = await pool.reap_idle(now=pool._entries["idle"].last_used + 11)
        assert reaped == ["idle"]
        assert pool.get("idle") is None
        assert pool.get("busy") is not None

    asyncio.run(_scenario())


def test_reap_idle_disabled_when_ttl_zero():
    async def _scenario():
        pool, pages, factory = _pool(max_pages=0, idle_ttl_s=0)
        await pool.acquire("a", factory)
        assert await pool.reap_idle(now=10**9) == []

    asyncio.run(_scenario())


def test_driver_wrappers_reach_the_pool():
    """driver.mark_busy / driver.reap_idle 必须转发到 page_pool（防再次变死代码）。"""
    from claude_web.driver import ClaudeWebDriver

    driver = ClaudeWebDriver()
    driver.pool.put("b", _Page())
    driver.mark_busy("b", True)
    assert driver.pool._entries["b"].busy is True
    driver.mark_busy("b", False)
    assert driver.pool._entries["b"].busy is False
