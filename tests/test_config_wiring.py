"""配置与字段的接线（T6.2 / D6.2）。

「声明了却从未被读」的键要么接上、要么删掉——不留「看起来有保护」的空壳。
本文件锁定这一契约，防止未来再引入未接线的配置。
"""

from __future__ import annotations

from claude_web import config
from claude_web.tasks import TaskStore


def test_removed_dead_keys_are_gone():
    """无消费点的键必须彻底删除（而不是留在 config 里）。"""
    assert not hasattr(config, "NEW_SESSION_ON_START")
    assert not hasattr(config, "SAVE_DEBUG_ARTIFACTS")


def test_warned_bad_state_shadow_flag_is_gone():
    """`lock.WARNED_BAD_STATE` 是 `SessionStore._warned_bad_state` 的影子副本，必须删除。

    真正的「状态文件损坏只 warning 一次」逻辑在 `SessionStore` 的实例属性里；模块级
    的 `WARNED_BAD_STATE` 从未被读写，属「看起来有保护」的空壳（D6.3）。
    """
    from claude_web import session
    from claude_web.session import lock

    assert not hasattr(lock, "WARNED_BAD_STATE")
    assert "WARNED_BAD_STATE" not in session.__all__


def test_removed_keys_not_in_env_example():
    """删键必须同步 .env.example，否则漂移检查名不副实。"""
    text = (config.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "NEW_SESSION_ON_START" not in text
    assert "CLAUDE_NEW_SESSION" not in text
    assert "SAVE_DEBUG_ARTIFACTS" not in text


def test_task_keep_messages_truncates_snapshot(tmp_path, monkeypatch):
    """TASK_KEEP_MESSAGES 生效：快照只保留最近 N 条（此前从未被读）。"""
    monkeypatch.setattr(config, "TASK_KEEP_MESSAGES", 3)
    store = TaskStore(tmp_path / "tasks")
    messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    snap = store.new(bucket="b", goal="g", messages=messages)
    assert [m["content"] for m in snap.messages] == ["m7", "m8", "m9"]


def test_task_keep_messages_zero_means_unlimited(tmp_path, monkeypatch):
    """0 = 不限制，保留全部。"""
    monkeypatch.setattr(config, "TASK_KEEP_MESSAGES", 0)
    store = TaskStore(tmp_path / "tasks")
    messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    snap = store.new(bucket="b", goal="g", messages=messages)
    assert len(snap.messages) == 10


def test_fill_timeout_is_wired():
    """FILL_TIMEOUT_MS 必须在 fill_composer 路径上被读（此前从未被读）。"""
    import inspect

    from claude_web.browser import dom_adapter

    src = inspect.getsource(dom_adapter.ClaudeDOMAdapter.fill_composer)
    assert "FILL_TIMEOUT_MS" in src


def test_ready_timeout_is_wired():
    """READY_TIMEOUT_MS 必须在 readiness 路径上被读。"""
    import inspect

    from claude_web.driver import ClaudeWebDriver

    src = inspect.getsource(ClaudeWebDriver.readiness)
    assert "READY_TIMEOUT_MS" in src


def test_model_is_supported_is_wired(caplog):
    """未知模型别名要有一条诊断日志（`model_is_supported` 由此有了生产用点，D6.3）。"""
    import logging

    from claude_web.api.chat_adapter import to_task
    from claude_web.models import ChatCompletionRequest

    req = ChatCompletionRequest(
        model="claude-web-does-not-exist",
        messages=[{"role": "user", "content": "hi"}],
    )
    with caplog.at_level(logging.INFO, logger="claude_web.api.chat_adapter"):
        task = to_task(req, "b")
    # 宽松兼容：未知别名不报错，任务照常构造（沿用默认别名语义）。
    assert task.model == "claude-web-does-not-exist"
    assert any("未知模型别名" in rec.message for rec in caplog.records)


def test_known_model_does_not_log_unknown(caplog):
    """已知别名不该触发未知模型日志。"""
    import logging

    from claude_web.api.chat_adapter import to_task
    from claude_web.models import ChatCompletionRequest

    req = ChatCompletionRequest(
        model=config.DEFAULT_MODEL_ALIAS,
        messages=[{"role": "user", "content": "hi"}],
    )
    with caplog.at_level(logging.INFO, logger="claude_web.api.chat_adapter"):
        to_task(req, "b")
    assert not any("未知模型别名" in rec.message for rec in caplog.records)
