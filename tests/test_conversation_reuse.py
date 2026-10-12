"""会话复用：同一条网页会话只开一次，之后只发增量（回归：此前每轮都新开对话）。

全部使用 FakeDriver，不联网、不启动浏览器。
"""

from __future__ import annotations

import asyncio

import pytest

from claude_web.models import ChatTask, NormalizedMessage
from claude_web.prompting import delta_messages
from tests.fakes import FakePage, make_service

BUCKET = "reuse"


def _task(pairs, bucket=BUCKET) -> ChatTask:
    return ChatTask(
        messages=[NormalizedMessage(role=role, content=text) for role, text in pairs],
        model="claude-web",
        bucket=bucket,
        stream=False,
    )


def _next_turn(page: FakePage, reply: str) -> None:
    """模拟客户端下一轮：页面上出现一条新回复（假页面需要手动翻页）。"""
    page.snapshots = [reply]
    page.submitted = False
    page.poll = 0


TURN_1 = [("system", "SYS"), ("user", "第一轮任务")]
TURN_2 = [("system", "SYS"), ("user", "第一轮任务"), ("assistant", "答复一"), ("user", "第二轮任务")]


def test_first_turn_opens_new_conversation_and_seeds(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    outcome = asyncio.run(service.run(_task(TURN_1)))
    conversation = outcome.conversation
    assert conversation.seeded is True
    assert conversation.created_new is True
    assert page.new_chat_opened is True
    assert "第一轮任务" in conversation.prompt
    assert "SYS" in conversation.prompt


def test_second_turn_reuses_conversation_and_sends_only_delta(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    first = asyncio.run(service.run(_task(TURN_1)))
    _next_turn(page, "答复二")
    second = asyncio.run(service.run(_task(TURN_2)))
    conversation = second.conversation
    assert conversation.seeded is False
    assert conversation.created_new is False
    # 关键：不再新开对话，且本轮只发新增内容（不带旧历史、不重发系统提示）
    assert "复用当前网页会话 + 只发增量" in conversation.notes
    assert conversation.prompt.strip() == "第二轮任务"
    assert "第一轮任务" not in conversation.prompt
    assert "SYS" not in conversation.prompt
    assert len(conversation.prompt) < len(first.conversation.prompt)


def test_delta_turn_sends_only_the_new_tool_result(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    _next_turn(page, "答复二")
    tool_turn = [
        ("system", "SYS"),
        ("user", "第一轮任务"),
        ("assistant", "答复一"),
        ("assistant", "发起调用"),
        ("tool", "$ git status --short\n(no output)\nTook 0.0s"),
    ]
    outcome = asyncio.run(service.run(_task(tool_turn)))
    prompt = outcome.conversation.prompt
    assert outcome.conversation.seeded is False
    assert "第一轮任务" not in prompt
    assert "[工具执行结果" in prompt
    # 空输出仍要被显式说明（复用会话也不会退化成原样透传）
    assert "没有任何输出" in prompt


def test_replaced_page_forces_reseed(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    # 页面被换掉（进程内重建 / 回收）：网页端上下文已丢，必须重新播种
    fresh = FakePage(snapshots=["答复二"])
    service.driver._factory = lambda: fresh
    asyncio.run(service.driver.pool.drop(BUCKET))
    outcome = asyncio.run(service.run(_task(TURN_2)))
    assert outcome.conversation.seeded is True
    assert outcome.conversation.created_new is True
    assert "第一轮任务" in outcome.conversation.prompt


def test_reset_forces_new_conversation(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    asyncio.run(service.lifecycle.reset(BUCKET))
    _next_turn(page, "答复二")
    outcome = asyncio.run(service.run(_task(TURN_2)))
    assert outcome.conversation.seeded is True
    assert outcome.conversation.created_new is True


def test_changed_system_prompt_is_resent_without_new_conversation(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    _next_turn(page, "答复二")
    changed = [*TURN_2]
    changed[0] = ("system", "SYS-改了")
    outcome = asyncio.run(service.run(_task(changed)))
    conversation = outcome.conversation
    assert conversation.seeded is False
    assert conversation.created_new is False
    assert "SYS-改了" in conversation.prompt
    assert "系统提示已变化：本轮重新带上系统指令" in conversation.notes


def test_linked_bucket_navigates_then_reuses(tmp_path):
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    url = "https://claude.ai/chat/b646548c-d27c-4dc7-9205-3134ac5d9c8d"
    asyncio.run(service.lifecycle.link(BUCKET, url))
    asyncio.run(service.run(_task(TURN_1)))
    navigations = len(page.goto_calls)
    assert navigations >= 1
    _next_turn(page, "答复二")
    outcome = asyncio.run(service.run(_task(TURN_2)))
    # 已在目标会话上：不再重复导航，只发增量
    assert len(page.goto_calls) == navigations
    assert outcome.conversation.seeded is False
    assert outcome.conversation.linked_url == url


def test_http_client_messages_share_one_web_conversation(tmp_path, monkeypatch):
    """客户端视角（真实 HTTP 路由）：连发两条消息只开一条网页会话。

    这是用户报的现象对应的验收路径——走 ``/v1/chat/completions``，而不是直接调 service。
    """
    from fastapi.testclient import TestClient

    from claude_web import server

    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    monkeypatch.setattr(server, "service", service)

    opened = []
    original = service.driver.open_new_conversation

    async def _count_open(target):
        opened.append(target)
        await original(target)

    service.driver.open_new_conversation = _count_open  # 统计「新开对话」的次数
    client = TestClient(server.app)

    first = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "第一轮任务"}]},
    )
    assert first.status_code == 200, first.text
    assert len(opened) == 1

    _next_turn(page, "答复二")
    second = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "第一轮任务"},
                {"role": "assistant", "content": "答复一"},
                {"role": "user", "content": "第二轮任务"},
            ]
        },
    )
    assert second.status_code == 200, second.text
    # 关键验收：第二条消息没有新开网页会话，且只把增量发进输入框。
    assert len(opened) == 1
    assert page.filled_texts[-1].strip() == "第二轮任务"
    # 提交成功后输入区被站点清空（试身已按真实页面模拟），否则就会被判成「提交没生效」。
    assert page.composer.text == ""


def test_delta_messages_helper():
    """单元级别：只看「最后一条 assistant 之后」、丢弃 system，没新消息时退回最后一条 user。"""
    messages = [
        NormalizedMessage(role="system", content="S"),
        NormalizedMessage(role="user", content="一"),
        NormalizedMessage(role="assistant", content="答复"),
        NormalizedMessage(role="user", content="二"),
    ]
    assert [m.content for m in delta_messages(messages)] == ["二"]
    # 只有一个 user 在最后一条 assistant 之前：退回它
    assert [m.content for m in delta_messages(messages[:2])] == ["一"]
    # 纯 tool 结果（工具循环）也要原样带出
    tool_only = [
        NormalizedMessage(role="user", content="一"),
        NormalizedMessage(role="assistant", content="答复"),
        NormalizedMessage(role="tool", content="结果", tool_call_id="c1"),
    ]
    assert [m.role for m in delta_messages(tool_only)] == ["tool"]
    assert delta_messages([]) == []


# ==================== 管理操作取桶锁（T1.2）====================


def test_admin_reset_returns_busy_when_bucket_generating(tmp_path, monkeypatch):
    """桶正忙时 reset 快速返回 session_busy，不关掉在途任务的页面（T1.2）。"""
    from claude_web.errors import SessionBusyError
    from claude_web.session.lock import BucketLockManager

    monkeypatch.setattr("claude_web.config.ADMIN_LOCK_TIMEOUT_S", 0.05)
    page = FakePage(snapshots=["答复"])
    service = make_service(page, tmp_path, locks=BucketLockManager(timeout_s=1))

    async def _scenario() -> None:
        # 模拟一个在途生成：桶锁被占用。
        async with service.locks.acquire(BUCKET):
            try:
                await service.admin_reset(BUCKET)
            except SessionBusyError:
                return
        raise AssertionError("桶忙时 admin_reset 应该抛 SessionBusyError")

    asyncio.run(_scenario())
    # 页面没被关掉（reset 未执行）。
    assert page.closed is False


def test_admin_reset_runs_when_bucket_idle(tmp_path):
    """桶空闲时 admin_reset 正常执行。"""
    page = FakePage(snapshots=["答复"])
    service = make_service(page, tmp_path)

    async def _scenario() -> None:
        # 先把页面放进池子（模拟该桶已有活动），再 reset。
        await service.driver.page_for(BUCKET)
        await service.admin_reset(BUCKET)

    asyncio.run(_scenario())
    # 页面被关闭（下一轮会重建 / 重新播种）。
    assert page.closed is True


# ==================== 历史一致性指纹（T1.4a，只观测）====================


def test_history_fingerprint_recorded_after_success(tmp_path):
    """成功后把「已同步历史」指纹写进会话状态（T1.4a）。"""
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    state = service.lifecycle.store.state(BUCKET)
    assert state.history_len == len(TURN_1)
    assert state.history_digest


def test_divergent_history_is_logged_but_does_not_reseed(tmp_path):
    """客户端历史前缀对不上：只打日志 + notes，本轮仍复用会话（观察模式）。"""
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    _next_turn(page, "答复二")

    # 客户端「回滚」了历史：前缀里第一轮任务被改写了。
    divergent = [
        ("system", "SYS"),
        ("user", "被改写的第一轮任务"),
        ("assistant", "答复一"),
        ("user", "第二轮任务"),
    ]
    outcome = asyncio.run(service.run(_task(divergent)))
    conversation = outcome.conversation
    # 观察模式：不重播种，仍复用；只是加了 notes 提示。
    assert conversation.seeded is False
    assert any("分叉" in note for note in conversation.notes)


def test_consistent_history_prefix_is_not_flagged(tmp_path):
    """对照：正常追加的历史（前缀一致）不应被标记。"""
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(TURN_1)))
    _next_turn(page, "答复二")
    outcome = asyncio.run(service.run(_task(TURN_2)))
    assert not any("分叉" in note for note in outcome.conversation.notes)


# ==================== 取消语义（T2.5）====================


def test_cancel_is_not_recorded_as_failure(tmp_path):
    """取消不计入失败 / 超时统计，也不写会话 last_error（T2.5）。"""
    from claude_web.errors import GenerationCancelledError

    page = FakePage(snapshots=["部分回复"], generating_plan=[True])
    service = make_service(page, tmp_path)
    with pytest.raises(GenerationCancelledError):
        asyncio.run(service.run(_task(TURN_1), cancel=lambda: True))
    state = service.lifecycle.store.state(BUCKET)
    assert state.last_error is None
    snap = service.metrics.snapshot()["counters"]
    assert snap["request_cancelled_total"] == 1
    assert snap["request_error_total"] == 0
    assert snap["request_timeout_total"] == 0


# ==================== 会话 URL 精确比较（T3.7 / D3.7）====================


def test_same_conversation_requires_exact_id():
    """会话 ID 只差最后一位必须判**不同**（旧前缀比对会误判成同一条）。"""
    from claude_web.completion.lifecycle import _same_conversation

    a = "https://claude.ai/chat/b646548c-d27c-4dc7-9205-3134ac5d9c8d"
    b = "https://claude.ai/chat/b646548c-d27c-4dc7-9205-3134ac5d9c8e"
    assert _same_conversation(a, b) is False
    assert _same_conversation(a, a) is True


def test_same_conversation_tolerates_query_and_trailing_slash():
    """站点自身的查询串 / 末尾斜杠差异不算换会话，其余一律逐字比较。"""
    from claude_web.completion.lifecycle import _same_conversation

    base = "https://claude.ai/chat/abc123"
    assert _same_conversation(base + "?foo=bar", base) is True
    assert _same_conversation(base + "/", base) is True
    assert _same_conversation(base + "#frag", base) is True
    # 主机不同 / 路径不同 → 不同会话。
    assert _same_conversation("https://evil.example.com/chat/abc123", base) is False
    assert _same_conversation("https://claude.ai/chat/abc124", base) is False
    # 空 / 无法解析 → 保守判否。
    assert _same_conversation("", base) is False
    assert _same_conversation(base, "") is False


def test_link_rejects_navigation_landing_on_other_conversation(tmp_path):
    """落点校验：页面停在「ID 只差最后一位」的别的会话上 → 报落点不一致（T3.7）。

    旧实现的前缀比对会放行这种落点，于是桶绑定到了错误的会话。
    """
    from claude_web.errors import NotReadyError

    target = "https://claude.ai/chat/b646548c-d27c-4dc7-9205-3134ac5d9c8d"
    wrong = "https://claude.ai/chat/b646548c-d27c-4dc7-9205-3134ac5d9c8e"

    class _RedirectingPage(FakePage):
        async def goto(self, url: str, **kwargs):
            # 站点把我们导到了**另一条**会话：绑定必须失败，而不是默默接受。
            self.goto_calls.append(url)
            self.url = wrong

    page = _RedirectingPage()
    service = make_service(page, tmp_path)
    with pytest.raises(NotReadyError):
        asyncio.run(service.lifecycle.link(BUCKET, target))
