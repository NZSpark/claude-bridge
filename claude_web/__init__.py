"""ClaudeBridge 实现包。

顶层刻意**不**导入 Playwright：离线单元测试与文档工具只需导入纯逻辑模块
（config / models / prompting / toolcalls / end_detection），不应因为缺少浏览器
依赖而失败。浏览器相关能力在 :mod:`claude_web.driver` 内部惰性导入。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
