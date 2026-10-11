"""共享测试夹具：把配置调快，并把状态文件 / 任务目录指向临时目录。

默认测试套件**不联网、不启动浏览器**（doc/design.md 11.1）。
"""

from __future__ import annotations

import pytest

from claude_web import config


@pytest.fixture(autouse=True)
def fast_config(tmp_path, monkeypatch):
    # 结束 / 停滞阈值都是**时长**（秒），测试里压缩到毫秒级即可（轮询间隔也只有 1ms）。
    monkeypatch.setattr(config, "POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(config, "POLL_INTERVAL_ACTIVE_S", 0.001)
    monkeypatch.setattr(config, "SETTLE_S", 0.01)
    monkeypatch.setattr(config, "SETTLE_NO_SIGNAL_S", 0.02)
    monkeypatch.setattr(config, "SUBMIT_LOST_S", 0.01)
    monkeypatch.setattr(config, "STALL_S", 0.05)
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


@pytest.fixture
def virtual_time(monkeypatch):
    """把生成循环换成虚拟时间轴（毫秒级跑完真实秒数的判定）。

    见 :class:`tests.fakes.VirtualClock`：断言的是「客户端等待了多少秒」而不是真实耗时。
    """
    from tests.fakes import VirtualClock

    return VirtualClock().install(monkeypatch)


@pytest.fixture
def default_timing(monkeypatch):
    """把结束 / 停滞 / 轮询参数恢复成**默认值**（不受 fast_config 的毫秒级覆盖影响）。"""
    for name, value in (
        ("POLL_INTERVAL_S", 1.5),
        ("POLL_INTERVAL_ACTIVE_S", 0.4),
        ("SETTLE_S", 2.5),
        ("SETTLE_NO_SIGNAL_S", 6.0),
        ("SUBMIT_LOST_S", 6.0),
        ("STALL_S", 30.0),
        ("RESPONSE_TIMEOUT_S", 300.0),
        ("RESPONSE_TIMEOUT_EXTEND_S", 0.0),
    ):
        monkeypatch.setattr(config, name, value)
