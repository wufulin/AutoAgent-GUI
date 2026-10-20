"""Coverage for Qwen and Gemini executors without external model calls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.agents.qwen.async_agent import AsyncQwenAgent
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot


PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


async def _collect(agen: AsyncGenerator[dict[str, Any], None]) -> list[dict[str, Any]]:
    return [event async for event in agen]


class FakeDevice:
    device_id = "device-1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def get_screenshot(self, timeout: int = 10) -> Screenshot:
        if self.fail:
            raise RuntimeError("screen failed")
        return Screenshot(base64_data=PNG_1X1_BASE64, width=100, height=200)

    def get_current_app(self) -> str:
        if self.fail:
            raise RuntimeError("app failed")
        return "com.example"


def _make_qwen_agent(
    device: FakeDevice | None = None, *, verbose: bool = True
) -> AsyncQwenAgent:
    return AsyncQwenAgent(
        ModelConfig(model_name="qwen-model"),
        AgentConfig(max_steps=3, verbose=verbose),
        device or FakeDevice(),
    )


def _make_gemini_agent(
    device: FakeDevice | None = None, *, verbose: bool = True
) -> AsyncGeminiAgent:
    return AsyncGeminiAgent(
        ModelConfig(model_name="gemini-model"),
        AgentConfig(max_steps=3, verbose=verbose),
        device or FakeDevice(),
    )


def test_qwen_agent_execute_step_error_debug_and_action_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _make_qwen_agent()
    monkeypatch.chdir(tmp_path)

    debug_path = agent._draw_tap_debug(PNG_1X1_BASE64, 1, 1, "Tap", 1)
    assert debug_path is not None
    assert Path(debug_path).exists()
    assert agent._draw_tap_debug("not-base64", 1, 1, "Tap", 2) is None

    device_error_agent = _make_qwen_agent(FakeDevice(fail=True))
    device_events = asyncio.run(_collect(device_error_agent._execute_step()))
    assert [event["type"] for event in device_events] == ["error", "step"]
    assert device_events[-1]["data"]["message"] == "Device error: screen failed"

    model_error_agent = _make_qwen_agent()

    async def broken_stream(messages):
        raise RuntimeError("model failed")
        yield {}

    monkeypatch.setattr(model_error_agent, "_stream_openai", broken_stream)
    model_events = asyncio.run(_collect(model_error_agent._execute_step()))
    assert model_events[-2]["data"]["message"] == "Model error: model failed"
    assert model_events[-1]["data"]["finished"] is True

    parse_fallback_agent = _make_qwen_agent()

    async def invalid_action_stream(messages):
        yield {"type": "thinking", "content": "thinking"}
        yield {"type": "raw", "content": "not a qwen action"}

    async def raise_action(*args, **kwargs):
        raise RuntimeError("action failed")

    monkeypatch.setattr(parse_fallback_agent, "_stream_openai", invalid_action_stream)
    monkeypatch.setattr(parse_fallback_agent.action_handler, "execute", raise_action)
    fallback_events = asyncio.run(_collect(parse_fallback_agent._execute_step()))
    assert [event["type"] for event in fallback_events] == ["thinking", "step"]
    assert fallback_events[-1]["data"]["action"] is None
    assert fallback_events[-1]["data"]["success"] is False
    assert fallback_events[-1]["data"]["stop_reason"] == "parse_error"

    tap_agent = _make_qwen_agent()

    async def tap_stream(messages):
        yield {
            "type": "raw",
            "content": '<answer>do(action="Tap", element=[500, 500])</answer>',
        }

    monkeypatch.setattr(tap_agent, "_stream_openai", tap_stream)

    async def fake_tap_execute(*a, **k):
        return ActionResult(success=True, should_finish=True, message="tapped")

    monkeypatch.setattr(tap_agent.action_handler, "execute", fake_tap_execute)
    tap_events = asyncio.run(_collect(tap_agent._execute_step()))
    assert tap_events[-1]["data"]["action"]["action"] == "Tap"
    assert tap_events[-1]["data"]["message"] == "tapped"


def test_qwen_stream_openai_streaming_and_cancel_paths() -> None:
    agent = _make_qwen_agent(verbose=False)

    class FakeDelta:
        def __init__(self, content: str | None) -> None:
            self.content = content

    class FakeChoice:
        def __init__(self, content: str | None) -> None:
            self.delta = FakeDelta(content)

    class FakeChunk:
        def __init__(self, content: str | None = None, *, empty: bool = False) -> None:
            self.choices = [] if empty else [FakeChoice(content)]

    class FakeStream:
        def __init__(self, chunks: list[FakeChunk]) -> None:
            self.chunks = chunks
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.chunks:
                raise StopAsyncIteration
            return self.chunks.pop(0)

        async def close(self) -> None:
            self.closed = True

    fake_stream = FakeStream(
        [
            FakeChunk("thinking "),
            FakeChunk(empty=True),
            FakeChunk('<answer>finish(message="done")</answer>'),
            FakeChunk("ignored action text"),
        ]
    )

    class FakeCompletions:
        async def create(self, **kwargs):
            return fake_stream

    agent.openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )

    events = asyncio.run(_collect(agent._stream_openai([{"role": "user"}])))
    assert events[0] == {"type": "raw", "content": "thinking "}
    assert {"type": "thinking", "content": "thinking "} in events
    assert events[-1] == {"type": "raw", "content": "ignored action text"}
    assert fake_stream.closed is True

    cancelled_stream = FakeStream([FakeChunk("cancel")])

    class CancelCompletions:
        async def create(self, **kwargs):
            return cancelled_stream

    agent.openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=CancelCompletions())
    )
    agent._cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_collect(agent._stream_openai([{"role": "user"}])))
    assert cancelled_stream.closed is True


def test_gemini_agent_execute_step_and_llm_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_gemini_agent()
    agent._prepare_initial_context("tap", PNG_1X1_BASE64, "app")

    async def tap_tool():
        return "think", None, "tap", {"x": 1, "y": 2}

    monkeypatch.setattr(agent, "_call_llm_with_tools", tap_tool)

    async def fake_execute(*a, **k):
        return ActionResult(success=True, should_finish=False, message="ok")

    monkeypatch.setattr(agent.action_handler, "execute", fake_execute)
    success_events = asyncio.run(_collect(agent._execute_step()))
    assert [event["type"] for event in success_events] == ["thinking", "step"]
    assert success_events[-1]["data"]["action"]["action"] == "Tap"
    assert success_events[-1]["data"]["success"] is True

    action_error_agent = _make_gemini_agent()
    action_error_agent._prepare_initial_context("tap", PNG_1X1_BASE64, "app")

    async def back_tool():
        return "", None, "back", {}

    async def raise_action(*args, **kwargs):
        raise RuntimeError("tap failed")

    monkeypatch.setattr(action_error_agent, "_call_llm_with_tools", back_tool)
    monkeypatch.setattr(action_error_agent.action_handler, "execute", raise_action)
    action_error_events = asyncio.run(_collect(action_error_agent._execute_step()))
    assert action_error_events[-1]["data"]["success"] is False
    assert action_error_events[-1]["data"]["finished"] is True
    assert action_error_events[-1]["data"]["message"] == "tap failed"

    model_error_agent = _make_gemini_agent()

    async def broken_tool():
        raise RuntimeError("model failed")

    monkeypatch.setattr(model_error_agent, "_call_llm_with_tools", broken_tool)
    model_events = asyncio.run(_collect(model_error_agent._execute_step()))
    assert [event["type"] for event in model_events] == ["error", "step"]
    assert model_events[-1]["data"]["message"] == "Model error: model failed"

    device_error_agent = _make_gemini_agent(FakeDevice(fail=True))
    device_error_agent._step_count = 1
    device_events = asyncio.run(_collect(device_error_agent._execute_step()))
    assert [event["type"] for event in device_events] == ["error", "step"]
    assert device_events[-1]["data"]["message"] == "Device error: screen failed"


def test_gemini_call_llm_with_tools_parses_tool_no_tool_bad_json_and_cancel() -> None:
    agent = _make_gemini_agent()
    agent._prepare_initial_context("tap", PNG_1X1_BASE64, "app")

    class FakeCompletions:
        def __init__(self, message: Any) -> None:
            self.message = message

        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])

    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="tap", arguments='{"x": 1, "y": 2}')
    )
    agent.openai_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=FakeCompletions(
                SimpleNamespace(content="thinking", tool_calls=[tool_call])
            )
        )
    )
    assert asyncio.run(agent._call_llm_with_tools()) == (
        "thinking",
        None,
        "tap",
        {"x": 1, "y": 2},
    )

    no_tool_agent = _make_gemini_agent()
    no_tool_agent.openai_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=FakeCompletions(SimpleNamespace(content="done", tool_calls=[]))
        )
    )
    assert asyncio.run(no_tool_agent._call_llm_with_tools()) == (
        "done",
        None,
        "finish",
        {"message": "done"},
    )

    bad_json_agent = _make_gemini_agent()
    bad_tool_call = SimpleNamespace(
        function=SimpleNamespace(name="tap", arguments="{bad")
    )
    bad_json_agent.openai_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=FakeCompletions(
                SimpleNamespace(content="", tool_calls=[bad_tool_call])
            )
        )
    )
    assert asyncio.run(bad_json_agent._call_llm_with_tools()) == ("", None, "tap", {})

    cancelled_agent = _make_gemini_agent()
    cancelled_agent._cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_agent._call_llm_with_tools())
