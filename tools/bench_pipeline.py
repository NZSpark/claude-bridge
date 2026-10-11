"""内容处理链路离线基准（页面回复 -> 客户端）。

量化「一轮请求里桥自己在处理内容上花掉的时间 / 轮询次数」，用于改动前后的对比：

* ``build_prompt``：提交前的提示词构造（含去重检查、预算裁剪、渲染次数）；
* 轮询循环：每轮触碰页面的次数（``COMPLETE_TEXT_JS`` / ``GENERATING_STATE_JS`` /
  ``BODY_TEXT_JS``）与**虚拟耗时**（把 ``asyncio.sleep`` 换成记录器，累加轮询间隔，
  即客户端实际等待的秒数）；
* 收尾解析：``parse_tool_calls`` / ``strip_tool_call_blocks``（大回复）。

用法::

    .venv/bin/python tools/bench_pipeline.py [--repeat 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from claude_web import config  # noqa: E402
from claude_web.browser import selectors  # noqa: E402
from claude_web.completion import generator as generator_module  # noqa: E402
from claude_web.completion.generator import ReplyGenerator  # noqa: E402
from claude_web.models import NormalizedMessage, ToolSpec  # noqa: E402
from claude_web.prompting import build_prompt  # noqa: E402
from claude_web.toolcalls import parse_tool_calls, strip_tool_call_blocks  # noqa: E402
from tests.fakes import FakePage  # noqa: E402

TOOLS = [
    ToolSpec(
        name="bash",
        description="Run a shell command in the user's working directory.",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    ),
    ToolSpec(
        name="read_file",
        description="Read a file from disk.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
]

#: 一段 ~20k 字符的工具结果（贴近真实 read_file 输出）。
BIG_TOOL_RESULT = "\n".join(f"{i:6d}\tconst someIdentifier{i} = doSomething({i});" for i in range(300))


def make_history(turns: int = 12, tool_result: str = BIG_TOOL_RESULT) -> List[NormalizedMessage]:
    """构造一段贴近真实 agent 循环的历史（每轮带一次工具调用与工具结果）。"""
    messages: List[NormalizedMessage] = [
        NormalizedMessage(role="system", content="You are a coding agent." * 50),
        NormalizedMessage(role="user", content="把项目里的桥接层内容处理逻辑优化一遍。"),
    ]
    for i in range(turns):
        messages.append(
            NormalizedMessage(
                role="assistant",
                content=f"第 {i} 步：先看一下相关文件。",
                tool_calls=[
                    {
                        "id": f"call_{i}",
                        "name": "bash",
                        "arguments": json.dumps({"command": f"sed -n '1,200p' file_{i}.py"}, ensure_ascii=False),
                    }
                ],
            )
        )
        messages.append(
            NormalizedMessage(role="tool", content=tool_result, tool_call_id=f"call_{i}")
        )
    messages.append(NormalizedMessage(role="user", content="继续，把结论给我。"))
    return messages


def bench_build_prompt(messages: List[NormalizedMessage], repeat: int) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for seed in (True, False):
        samples = []
        size = 0
        for _ in range(repeat):
            start = time.perf_counter()
            build = build_prompt(messages, tools=TOOLS, seed=seed)
            samples.append((time.perf_counter() - start) * 1000)
            size = len(build.text)
        out[f"build_prompt(seed={seed})"] = statistics.median(samples)
        out[f"prompt_chars(seed={seed})"] = size
    return out


def bench_parse(reply: str, repeat: int) -> Dict[str, float]:
    names = [spec.name for spec in TOOLS]
    parse_samples = []
    strip_samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        parsed = parse_tool_calls(reply, allowed_names=names)
        parse_samples.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        strip_tool_call_blocks(reply, fence=config.TOOL_CALL_FENCE)
        strip_samples.append((time.perf_counter() - start) * 1000)
    assert parsed.calls, "基准用回复必须能解析出工具调用"
    return {
        "parse_tool_calls": statistics.median(parse_samples),
        "strip_tool_call_blocks": statistics.median(strip_samples),
        "reply_chars": len(reply),
    }


class CountingPage(FakePage):
    """记录每种 DOM 探针被调用次数的假页面（同时记录首次读到正文的虚拟时刻）。"""

    def __init__(self, *, clock: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.evaluate_calls: Dict[str, int] = {}
        self.clock = clock
        self.first_content_s: float | None = None

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        label = {
            selectors.COMPLETE_TEXT_JS: "complete_text",
            selectors.GENERATING_STATE_JS: "generating_state",
            selectors.BODY_TEXT_JS: "body_text(上下文到顶扫描)",
            selectors.PAGE_PROBE_JS: "page_probe",
        }.get(expression, "other")
        self.evaluate_calls[label] = self.evaluate_calls.get(label, 0) + 1
        result = await super().evaluate(expression, arg)
        if (
            expression == selectors.COMPLETE_TEXT_JS
            and self.clock is not None
            and self.first_content_s is None
            and str(result or "").strip()
        ):
            self.first_content_s = round(self.clock.monotonic(), 3)
        return result


class SleepRecorder:
    """把生成循环里的 ``asyncio.sleep`` 换成记录器，并充当虚拟时钟。

    * ``total`` 累加每一次轮询间隔 = 客户端实际等待的秒数；
    * 同时用它驱动 ``time.monotonic``，于是「静止时长」类的阈值全部在虚拟时间轴上判定，
      基准运行不需要真的睡几秒（否则一次基准要跑好几分钟）。
    """

    def __init__(self) -> None:
        self.total = 0.0
        self.count = 0
        self.intervals: List[float] = []

    async def __call__(self, seconds: float) -> None:
        self.total += float(seconds)
        self.count += 1
        self.intervals.append(float(seconds))

    def monotonic(self) -> float:
        return self.total


async def bench_poll_loop(
    *,
    tools: bool,
    snapshots: List[str],
    generating_plan: List[bool],
    repeat: int = 1,
) -> Dict[str, Any]:
    """跑一轮真实生成（假页面），统计轮询次数 / DOM 探针次数 / 虚拟等待秒数。"""
    import types

    original_asyncio = generator_module.asyncio
    original_time = generator_module.time
    runs: List[Dict[str, Any]] = []
    for _ in range(repeat):
        page = CountingPage(
            snapshots=snapshots,
            generating_plan=generating_plan,
            submit_lands=True,
        )
        recorder = SleepRecorder()
        page.clock = recorder
        # 生成循环只用 asyncio.sleep 与 time.monotonic；两者都指向虚拟时间轴。
        generator_module.asyncio = types.SimpleNamespace(sleep=recorder)  # type: ignore[assignment]
        generator_module.time = types.SimpleNamespace(monotonic=recorder.monotonic)  # type: ignore[assignment]
        try:
            gen = ReplyGenerator()
            await gen.generate(page, "把结论给我。", tools=TOOLS if tools else None)
        finally:
            generator_module.asyncio = original_asyncio  # type: ignore[assignment]
            generator_module.time = original_time  # type: ignore[assignment]
        runs.append(
            {
                "polls": page.poll,
                "首段正文到达(s)": page.first_content_s,
                "收尾(s)": round(recorder.total, 3),
                "轮询间隔序列": [round(v, 3) for v in recorder.intervals],
                "DOM 调用": dict(sorted(page.evaluate_calls.items())),
            }
        )
    return runs[0]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="内容处理链路离线基准")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args(argv)

    history = make_history()
    reply = (
        "先看看这个文件。\n\n```tool_call\n"
        + json.dumps(
            {"name": "bash", "arguments": {"command": "sed -n '1,400p' claude_web/prompting.py"}},
            ensure_ascii=False,
        )
        + "\n```\n"
    )

    report: Dict[str, Any] = {
        "config": {
            "POLL_INTERVAL_S": config.POLL_INTERVAL_S,
            "POLL_INTERVAL_ACTIVE_S": config.POLL_INTERVAL_ACTIVE_S,
            "SETTLE_S": config.SETTLE_S,
            "SETTLE_NO_SIGNAL_S": config.SETTLE_NO_SIGNAL_S,
            "STALL_S": config.STALL_S,
        },
        "history": {"messages": len(history), "chars": sum(len(m.content or "") for m in history)},
    }
    report.update(bench_build_prompt(history, args.repeat))
    report.update(bench_parse(reply, args.repeat))

    # 短回复：页面两轮就写完了，随后落定 —— 尾部等待由此暴露。
    settled = asyncio.run(
        bench_poll_loop(
            tools=True,
            snapshots=["正在执行。", "已经执行完了，结论如下：一切正常。"],
            generating_plan=[True, False],
        )
    )
    no_tools = asyncio.run(
        bench_poll_loop(
            tools=False,
            snapshots=["正在执行。", "已经执行完了，结论如下：一切正常。"],
            generating_plan=[True, False],
        )
    )
    report["poll_loop(工具模式)"] = settled
    report["poll_loop(纯文本模式)"] = no_tools

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
