"""配置漂移检查（doc/design.md 8、13）。

``.env.example`` 与 ``claude_web/config.py`` 必须互相覆盖：文档里写了开关、
运行代码却读不到（或反之）都算漂移。
"""

from __future__ import annotations

import re
from pathlib import Path

from claude_web import config

PROJECT_ROOT = Path(config.__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
CONFIG_PY = PROJECT_ROOT / "claude_web" / "config.py"

_ENV_CALL = re.compile(r"env_(?:str|int|float|bool|list)\(\s*[\"']([A-Z0-9_]+)[\"']")


def _keys_in_env_example() -> set:
    keys = set()
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key.isupper():
            keys.add(key)
    return keys


def _keys_in_config() -> set:
    return set(_ENV_CALL.findall(CONFIG_PY.read_text(encoding="utf-8")))


def test_env_example_covers_every_config_key():
    missing = _keys_in_config() - _keys_in_env_example()
    assert not missing, f".env.example 缺少配置项：{sorted(missing)}"


def test_config_reads_every_env_example_key():
    unread = _keys_in_env_example() - _keys_in_config()
    assert not unread, f"config.py 未读取 .env.example 中的配置项：{sorted(unread)}"
