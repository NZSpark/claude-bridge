"""DOM 诊断（doc/design.md 4.3、10）。

这些方法不参与业务判定，只用于「网页改版导致选择器失效」时定位问题。

约定：诊断方法**不抛异常**，失败时返回可读的错误字符串 / 占位；
输出必须**只含布尔、计数与类别**，不得回显页面正文或凭据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Tuple

from . import selectors

logger = logging.getLogger(__name__)


class DiagnosticsMixin:
    async def selector_diagnostics(
        self, page: Any, groups: Dict[str, Iterable[str]]
    ) -> Tuple[Dict[str, List[dict]], Dict[str, bool]]:
        """统计每组选择器的命中数，返回 ``(明细, 是否至少有一条命中)``。"""
        result: Dict[str, List[dict]] = {}
        healthy: Dict[str, bool] = {}
        for name, selectors_list in groups.items():
            entries: List[dict] = []
            for selector in selectors_list:
                selector = str(selector).strip()
                if not selector:
                    continue
                try:
                    count = len(await page.query_selector_all(selector))
                    entries.append({"selector": selector, "matches": count})
                except Exception as exc:  # noqa: BLE001
                    entries.append({"selector": selector, "error": type(exc).__name__})
            result[name] = entries
            healthy[name] = any((entry.get("matches") or 0) > 0 for entry in entries)
        return result, healthy

    async def page_probe(self, page: Any, config_module) -> Dict[str, Any]:
        """返回页面状态探测（布尔 / 状态串 / 选择器命中数）。"""
        probe: Dict[str, Any] = {"selector_version": selectors.SELECTOR_VERSION}
        try:
            info = await page.evaluate(
                selectors.PAGE_PROBE_JS,
                {
                    "input": list(config_module.INPUT_SELECTORS),
                    "auth": list(config_module.AUTH_INDICATORS),
                    "challenge": list(config_module.CHALLENGE_INDICATORS),
                },
            )
            if isinstance(info, dict):
                # title 可能含用户信息（会话标题）；只保留长度与是否为空。
                title = str(info.pop("title", "") or "")
                info["title_length"] = len(title)
                probe.update(info)
        except Exception as exc:  # noqa: BLE001
            probe["probe_error"] = type(exc).__name__
        return probe


__all__ = ["DiagnosticsMixin"]
