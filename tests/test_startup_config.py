"""启动期配置校验与摘要（T3.6 / D3.6）。

设计上刻意「返回问题清单」而不是抛异常：默认值本身就命中一条跨键约束
（锁超时 < 单轮超时），若拒绝启动则默认配置都跑不起来。这些测试锁定该契约。
"""

from __future__ import annotations

from claude_web import config


def test_default_config_has_no_lock_vs_timeout_conflict(default_timing):
    """默认值本身不得命中「锁超时 < 单轮超时」（A3.3：默认值已从 120s 提到 300s）。

    用 ``default_timing`` fixture 把超时恢复成真实默认值：``conftest`` 的 autouse
    ``fast_config`` 会把超时降到毫秒级（跑测试用），不恢复就测不准默认值。
    """
    assert config.BUCKET_LOCK_TIMEOUT_S >= config.RESPONSE_TIMEOUT_S
    problems = config.config_warnings()
    assert not any("BUCKET_LOCK_TIMEOUT_S" in p for p in problems)


def test_lock_below_timeout_is_reported(monkeypatch, default_timing):
    """把锁超时降到单轮超时以下时必须被检出，否则这条跨键约束形同虚设。"""
    monkeypatch.setattr(config, "BUCKET_LOCK_TIMEOUT_S", config.RESPONSE_TIMEOUT_S - 1)
    problems = config.config_warnings()
    assert any("BUCKET_LOCK_TIMEOUT_S" in p for p in problems)


def test_deprecated_keys_are_reported(monkeypatch):
    """旧的「轮询次数」键仍在 .env 里时必须告警，而不是静默失效。"""
    monkeypatch.setenv("QUIET_POLLS", "4")
    problems = config.config_warnings()
    assert any("QUIET_POLLS" in p for p in problems)


def test_no_warning_when_values_are_consistent(monkeypatch, default_timing):
    """把跨键约束都调一致后，不应再有任何告警（防误报）。"""
    monkeypatch.setattr(config, "BUCKET_LOCK_TIMEOUT_S", config.RESPONSE_TIMEOUT_S)
    monkeypatch.setattr(config, "SETTLE_NO_SIGNAL_S", config.SETTLE_S + 1)
    monkeypatch.setattr(config, "STALL_S", config.SUBMIT_LOST_S + 1)
    for key in config.DEPRECATED_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert config.config_warnings() == []


def test_settle_no_signal_below_settle_is_reported(monkeypatch, default_timing):
    monkeypatch.setattr(config, "SETTLE_NO_SIGNAL_S", config.SETTLE_S - 1)
    assert any("SETTLE_NO_SIGNAL_S" in p for p in config.config_warnings())


def test_stall_not_greater_than_submit_lost_is_reported(monkeypatch, default_timing):
    """STALL_S <= SUBMIT_LOST_S 会让停滞判定抢在「提交没生效」之前，归因错报。"""
    monkeypatch.setattr(config, "STALL_S", config.SUBMIT_LOST_S)
    assert any("STALL_S" in p for p in config.config_warnings())


def test_summary_never_leaks_secrets(monkeypatch):
    """摘要日志绝不能带出 BRIDGE_TOKEN / RESET_TOKEN。"""
    monkeypatch.setattr(config, "BRIDGE_TOKEN", "super-secret-token")
    monkeypatch.setattr(config, "RESET_TOKEN", "another-secret")
    joined = "\n".join(config.summary_lines())
    assert "super-secret-token" not in joined
    assert "another-secret" not in joined
    assert "BRIDGE_TOKEN" not in joined
    assert "RESET_TOKEN" not in joined


def test_summary_includes_key_runtime_values():
    """摘要要覆盖排障最常看的几项，且逐行可读。"""
    joined = "\n".join(config.summary_lines())
    for key in ("HOST", "PORT", "RESPONSE_TIMEOUT_S", "STALL_S", "SETTLE_S"):
        assert f"{key}=" in joined
    assert all("=" in line for line in config.summary_lines())
