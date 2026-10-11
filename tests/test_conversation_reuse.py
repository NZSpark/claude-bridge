"""会话复用：同一条网页会话只开一次，之后只发增量（回归：此前每轮都新开对话）。

全部使用 FakeDriver，不联网、不启动浏览器。
"""

from __future__ import annotations

import asyncio

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
