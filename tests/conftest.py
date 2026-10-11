"""共享测试夹具：把配置调快，并把状态文件 / 任务目录指向临时目录。

默认测试套件**不联网、不启动浏览器**（doc/design.md 11.1）。
"""

from __future__ import annotations

import pytest

from claude_web import config


@pytest.fixture(autouse=True)
def fast_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(config, "STABLE_POLLS", 1)
    monkeypatch.setattr(config, "QUIET_POLLS", 1)
    monkeypatch.setattr(config, "STALL_POLLS", 5)
    monkeypatch.setattr(config, "RESPONSE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(config, "RESPONSE_TIMEOUT_EXTEND_S", 0.0)
    monkeypatch.setattr(config, "FILL_RETRIES", 1)
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    monkeypatch.setattr(config, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "TASK_FILE_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "BRIDGE_TOKEN", "")
    monkeypatch.setattr(config, "RESET_TOKEN", "")
    monkeypatch.setattr(config, "SESSION_SCOPING", True)
    monkeypatch.setattr(config, "SESSION_SCOPING_BY_UA", False)
    monkeypatch.setattr(config, "MAX_SESSION_BUCKETS", 4)
    monkeypatch.setattr(config, "ENABLE_RESPONSES_API", False)
    yield


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"
