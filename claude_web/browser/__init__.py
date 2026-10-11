"""Claude 网页 UI 定位与交互（doc/design.md 4.3、7）。

本包只负责「页面怎么读、怎么点」；不依赖 HTTP 协议细节，也不感知会话策略。
Playwright 相关的类型不在导入期求值（页面对象按鸭子类型使用），
因此离线单元测试可以用轻量测试替身驱动整个包。
"""

from .dom_adapter import ClaudeDOMAdapter
from .selectors import SELECTOR_VERSION

__all__ = ["ClaudeDOMAdapter", "SELECTOR_VERSION"]
