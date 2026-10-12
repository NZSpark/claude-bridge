"""配置解析、默认值与工具函数（doc/design.md 11.1）。"""

from __future__ import annotations

from claude_web import config


def test_env_helpers_read_and_fallback(monkeypatch):
    monkeypatch.setenv("CB_TEST_STR", "value")
    monkeypatch.setenv("CB_TEST_INT", "not-an-int")
    monkeypatch.setenv("CB_TEST_BOOL", "YES")
    monkeypatch.setenv("CB_TEST_FLOAT", "2.5")
    assert config.env_str("CB_TEST_STR", "d") == "value"
    assert config.env_int("CB_TEST_INT", 7) == 7  # 非法值回退默认
    assert config.env_int("CB_TEST_MISSING", 7) == 7
    assert config.env_bool("CB_TEST_BOOL") is True
    assert config.env_bool("CB_TEST_MISSING", True) is True
    assert config.env_float("CB_TEST_FLOAT", 0.0) == 2.5


def test_env_list_splits_on_pipe_only(monkeypatch):
    """只按 ``||`` 切分（T3.5）：逗号是选择器 / 文案里的合法字符，不能被当分隔符。"""
    monkeypatch.setenv("CB_TEST_LIST", "a||b, c || d")
    assert config.env_list("CB_TEST_LIST", "") == ["a", "b, c", "d"]


def test_env_list_preserves_comma_in_css_is(monkeypatch):
    """:is(a,b) 这类选择器必须原样保留，否则 query_selector_all 静默命中不到。"""
    monkeypatch.setenv("CB_TEST_SEL", ':is(button, a[role="button"])||div.x')
    assert config.env_list("CB_TEST_SEL", "") == [
        ':is(button, a[role="button"])',
        "div.x",
    ]


def test_env_path_resolves_relative_to_project_root(monkeypatch):
    """相对路径以 PROJECT_ROOT 为基准（T3.5）：换 cwd 启动不再丢登录态。"""
    monkeypatch.setenv("CB_TEST_PATH", "./somewhere/state.json")
    resolved = config.env_path("CB_TEST_PATH", "./fallback")
    assert resolved == (config.PROJECT_ROOT / "somewhere/state.json").resolve()


def test_env_path_keeps_absolute(monkeypatch, tmp_path):
    target = tmp_path / "abs_state.json"
    monkeypatch.setenv("CB_TEST_PATH", str(target))
    assert config.env_path("CB_TEST_PATH", "./x") == target.resolve()


def test_is_loopback_host():
    assert config.is_loopback_host("127.0.0.1")
    assert config.is_loopback_host("localhost")
    assert config.is_loopback_host("::1") is False or config.is_loopback_host("[::1]")
    assert not config.is_loopback_host("0.0.0.0")
    assert not config.is_loopback_host("192.168.1.10")
    assert not config.is_loopback_host("example.com")


def test_defaults_are_loopback_and_documented(monkeypatch):
    # HOST 默认回环（环境可能覆盖，但绝不该默认对外）。
    assert config.is_loopback_host(config.HOST)
    # 端口默认刻意与同族 Bridge 区分（ChatGPTBridge=8002）。
    monkeypatch.delenv("PORT", raising=False)
    assert config.env_int("PORT", 8003) == 8003
    assert config.DEFAULT_MODEL_ALIAS in config.MODEL_ALIASES


def test_model_aliases_parsing():
    aliases = config._parse_model_aliases("a=Sonnet,b =,c")
    assert aliases == {"a": "Sonnet", "b": "", "c": ""}
