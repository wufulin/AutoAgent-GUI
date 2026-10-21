"""Completion handoff, observation freshness, and bounded modal transition guards."""

import asyncio
import base64
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.agents.gemini.action_mapper import (
    InvalidToolCallError,
    tool_call_to_action,
)
from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.agents.gemini.screen_guard import compare_modal_transition
from AutoGLM_GUI.agents.gemini.tools import (
    DEVICE_TOOLS,
    PHYSICAL_TOOL_NAMES,
    get_device_tools,
)
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot


def _shot(image: Image.Image | None = None, *, sensitive: bool = False) -> Screenshot:
    image = image or Image.new("RGB", (240, 400), "white")
    data = BytesIO()
    image.save(data, "PNG")
    return Screenshot(
        base64.b64encode(data.getvalue()).decode(), *image.size, sensitive
    )


def _modal(*, loaded: bool = False) -> Screenshot:
    image = Image.new("RGB", (240, 400), (100, 100, 100))
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 120, 226, 290), fill="white")
    if loaded:
        draw.rectangle((30, 140, 210, 260), fill=(80, 120, 180))
    return _shot(image)


class _Device:
    device_id = "fake-optimization"

    def __init__(self, frames: list[Screenshot]):
        self.frames = frames
        self.captures = 0

    def get_screenshot(self, timeout: int = 10) -> Screenshot:
        frame = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        return frame

    def get_current_app(self) -> str:
        return "test-app"


class _Agent(AsyncGeminiAgent):
    verification_settle_seconds = 0.0

    def __init__(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        frames: list[Screenshot] | None = None,
        *,
        independent: bool = True,
        result: ActionResult | None = None,
    ):
        self.fake_device = _Device(frames or [_shot()])
        super().__init__(
            ModelConfig(), AgentConfig(max_steps=5, verbose=False), self.fake_device
        )
        self.calls = calls.copy()
        self.independent_verification = independent
        self.action_handler.execute = AsyncMock(
            return_value=result or ActionResult(True, False)
        )

    async def _call_llm_with_tools(self):
        name, arguments = (
            self.calls.pop(0) if self.calls else ("finish", {"message": "done"})
        )
        return "", None, name, arguments


async def _single_step(agent: _Agent):
    agent._initial_screenshot = agent.fake_device.frames[0]
    agent._prepare_initial_context(
        "bounded task", agent._initial_screenshot.base64_data, "test-app"
    )
    return [event async for event in agent._execute_step()][-1]["data"]


def test_completion_handoff_is_only_declared_in_independent_physical_tools():
    tools = get_device_tools(independent_verification=True)
    for tool in tools:
        function = tool["function"]
        assert ("verify_after" in function["parameters"]["properties"]) == (
            function["name"] in PHYSICAL_TOOL_NAMES
        )
    assert all(
        "verify_after" not in t["function"]["parameters"]["properties"]
        for t in DEVICE_TOOLS
    )


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_verify_after_requires_an_actual_boolean(value):
    with pytest.raises(InvalidToolCallError, match="Expected boolean"):
        tool_call_to_action(
            "back", {"verify_after": value}, independent_verification=True
        )


@pytest.mark.parametrize(
    "name, independent", [("back", False), ("finish", True), ("wait", True)]
)
def test_verify_after_rejected_without_supported_handoff(name, independent):
    with pytest.raises(InvalidToolCallError, match="independently verified physical"):
        tool_call_to_action(
            name, {"verify_after": True}, independent_verification=independent
        )


def test_successful_final_action_requests_verification_without_finish_turn():
    async def run():
        agent = _Agent(
            [("back", {"verify_after": True}), ("finish", {"message": "unused"})]
        )
        events = [event async for event in agent.stream("back then verify")]
        return agent, events

    agent, events = asyncio.run(run())
    assert len(agent.calls) == 1
    assert agent.action_handler.execute.await_count == 1
    assert events[-1]["data"]["status"] == "completion_requested"
    assert events[-1]["data"]["stop_reason"] == "verification_requested"
    assert events[-1]["data"]["message"] == "已执行最后一步。"
    assert agent._cached_observation is None


@pytest.mark.parametrize(
    "result",
    [
        ActionResult(False, False),
        ActionResult(False, True),
        ActionResult(True, True),
        ActionResult(True, False, requires_confirmation=True),
    ],
)
def test_failed_or_terminal_actions_never_promote_verify_after(result):
    agent = _Agent([("back", {"verify_after": True})], result=result)
    step = asyncio.run(_single_step(agent))
    assert step.get("stop_reason") != "verification_requested"
    assert step["finished"] is result.should_finish


def test_unknown_result_cannot_request_verification():
    agent = _Agent([("back", {"verify_after": True})])

    async def execute(*args):
        agent.device._outcome_unknown = True
        return ActionResult(True, False)

    agent.action_handler.execute = AsyncMock(side_effect=execute)
    step = asyncio.run(_single_step(agent))
    assert step.get("stop_reason") != "verification_requested"
    assert agent._cached_observation is None


@pytest.mark.parametrize("independent", [False, True])
def test_finish_does_not_capture_pre_action_frame(independent):
    async def run():
        agent = _Agent(
            [("finish", {"message": "already complete"})], independent=independent
        )
        events = [event async for event in agent.stream("inspect only")]
        return agent, events

    agent, events = asyncio.run(run())
    assert agent.fake_device.captures == 1
    assert events[-1]["data"]["status"] == "completion_requested"


def test_post_action_reused_once_but_pre_action_stays_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.gemini.async_agent.phone_agent_replay_active", lambda: True
    )
    monkeypatch.setenv("AUTOGLM_TRACE_ENABLED", "1")
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AUTOGLM_TRACE_FILE", str(trace_file))

    async def run():
        agent = _Agent([("back", {}), ("back", {"verify_after": True})])
        events = [event async for event in agent.stream("two actions")]
        return agent, events

    agent, events = asyncio.run(run())
    # Initial + two fresh pre-actions + two replay post-actions, no second observation capture.
    assert agent.fake_device.captures == 5
    assert agent.action_handler.execute.await_count == 2
    assert events[-1]["data"]["stop_reason"] == "verification_requested"
    import json

    spans = [json.loads(line) for line in trace_file.read_text().splitlines()]
    reuse = [s for s in spans if s["name"] == "step.reuse_observation"]
    assert len(reuse) == 1
    assert reuse[0]["attrs"]["fresh_capture"] is False
    assert reuse[0]["attrs"]["source_step"] == 1


def test_cache_expires_and_is_single_use(monkeypatch):
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.gemini.async_agent.time.monotonic", lambda: 10.0
    )
    agent = _Agent([])
    agent._step_count = 2
    agent._cached_observation = (_shot(), 8.9, 1)
    assert agent._take_cached_observation() is None
    agent._cached_observation = (_shot(), 9.9, 1)
    assert agent._take_cached_observation() is not None
    assert agent._take_cached_observation() is None


@pytest.mark.parametrize("frame", [_shot(sensitive=True), Screenshot("", 0, 400)])
def test_sensitive_and_invalid_frames_are_not_reused(frame):
    import time

    agent = _Agent([])
    agent._step_count = 2
    agent._cached_observation = (frame, time.monotonic(), 1)
    assert agent._take_cached_observation() is None


def test_reset_cancel_and_continuation_discard_cached_frame():
    import time

    agent = _Agent([])
    agent._cached_observation = (_shot(), time.monotonic(), 1)
    agent.reset()
    assert agent._cached_observation is None
    assert agent.independent_verification is False
    assert "Independent verification handoff" not in agent.get_system_prompt()
    agent.independent_verification = True
    agent._cached_observation = (_shot(), time.monotonic(), 1)
    asyncio.run(agent.cancel())
    assert agent._cached_observation is None

    async def resume():
        agent._step_count = 1
        agent._watchdog_started_at = time.monotonic()
        agent._cached_observation = (_shot(), time.monotonic(), 1)
        return [
            event async for event in agent.stream("resume", continue_with="continue")
        ]

    asyncio.run(resume())
    assert (
        agent.fake_device.captures == 1
    )  # Fresh continuation observation, no finish pre-frame.
    assert agent._cached_observation is None


def test_cached_observation_does_not_replace_geometry_guard(monkeypatch):
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.gemini.async_agent.phone_agent_replay_active", lambda: True
    )
    normal, rotated = _shot(), _shot(Image.new("RGB", (400, 240), "white"))
    agent = _Agent(
        [("back", {}), ("back", {"verify_after": True})],
        [normal, normal, normal, rotated],
    )

    async def run():
        return [event async for event in agent.stream("two actions")]

    events = asyncio.run(run())
    assert agent.action_handler.execute.await_count == 1
    assert events[-1]["data"]["success"] is False
    assert "dimensions changed" in events[-1]["data"]["message"]


def test_new_modal_and_loading_modal_reject_stale_action():
    assert compare_modal_transition(_shot(), _modal()).reason == "new_dimmed_modal"
    assert (
        compare_modal_transition(_modal(), _modal(loaded=True)).reason
        == "loading_modal_changed"
    )
    assert (
        compare_modal_transition(_modal(loaded=True), _modal(loaded=True)).reason
        is None
    )


def test_status_bars_and_small_animation_do_not_trigger_modal_guard():
    before = Image.new("RGB", (240, 400), "white")
    after = before.copy()
    draw = ImageDraw.Draw(after)
    draw.rectangle((0, 0, 240, 20), fill="black")
    draw.rectangle((0, 380, 240, 400), fill="black")
    draw.rectangle((110, 180, 130, 200), fill="red")
    assert compare_modal_transition(_shot(before), _shot(after)).reason is None


def test_modal_guard_prevents_device_write_and_never_promotes_completion():
    agent = _Agent([("back", {"verify_after": True})], [_modal(), _modal(loaded=True)])
    # _single_step supplies the observation without consuming a device capture.
    agent.fake_device.captures = 1
    step = asyncio.run(_single_step(agent))
    assert agent.action_handler.execute.await_count == 0
    assert step["success"] is False
    assert "SCREEN_CHANGED: loading_modal_changed" in step["message"]
    assert step.get("stop_reason") != "verification_requested"


def test_classic_mode_does_not_apply_modal_heuristic():
    agent = _Agent([("back", {})], [_modal(), _modal(loaded=True)], independent=False)
    agent.fake_device.captures = 1
    step = asyncio.run(_single_step(agent))
    assert agent.action_handler.execute.await_count == 1
    assert step["success"] is True


def test_cancellation_during_inference_prevents_device_write():
    agent = _Agent([("back", {"verify_after": True})])

    async def cancelled_inference():
        await agent.cancel()
        return "", None, "back", {"verify_after": True}

    agent._call_llm_with_tools = cancelled_inference
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_single_step(agent))
    assert agent.action_handler.execute.await_count == 0
    assert agent._cached_observation is None


def test_invalid_pre_action_image_fails_closed():
    normal = _shot()
    malformed = Screenshot("not-a-png", normal.width, normal.height)
    agent = _Agent([("back", {"verify_after": True})], [normal, malformed])
    agent.fake_device.captures = 1
    step = asyncio.run(_single_step(agent))
    assert agent.action_handler.execute.await_count == 0
    assert step["success"] is False
    assert "SCREEN_CHANGED: invalid screenshot" in step["message"]


def test_failed_action_does_not_cache_post_action_frame(monkeypatch):
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.gemini.async_agent.phone_agent_replay_active", lambda: True
    )
    agent = _Agent([("back", {})], result=ActionResult(False, False))
    step = asyncio.run(_single_step(agent))
    assert step["success"] is False
    assert agent.fake_device.captures == 2  # pre-action and replay evidence retained
    assert agent._cached_observation is None


def test_closing_stream_early_releases_agent_before_next_stream():
    async def run():
        agent = _Agent([("back", {}), ("finish", {"message": "next run"})])
        stream = agent.stream("first run")
        first = await anext(stream)
        assert first["type"] == "step"
        assert agent.is_running
        await stream.aclose()
        # No event-loop tick or deferred async-generator finalization is needed.
        assert not agent.is_running
        assert agent._cached_observation is None
        next_events = [event async for event in agent.stream("second run")]
        assert next_events[-1]["type"] == "done"
        assert next_events[-1]["data"]["message"] == "next run"

    asyncio.run(run())


def test_verification_handoff_waits_in_separate_span(monkeypatch, tmp_path):
    import json

    assert AsyncGeminiAgent.verification_settle_seconds == 1.0
    trace_file = tmp_path / "settle.jsonl"
    monkeypatch.setenv("AUTOGLM_TRACE_ENABLED", "1")
    monkeypatch.setenv("AUTOGLM_TRACE_FILE", str(trace_file))
    wait = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr(asyncio, "wait_for", wait)
    agent = _Agent([("back", {"verify_after": True})])
    agent.verification_settle_seconds = 0.01
    step = asyncio.run(_single_step(agent))
    assert step["stop_reason"] == "verification_requested"
    wait.assert_awaited_once()
    assert wait.await_args.kwargs["timeout"] == 0.01
    spans = [json.loads(line) for line in trace_file.read_text().splitlines()]
    settle = [s for s in spans if s["name"] == "step.verification_settle"]
    assert len(settle) == 1
    assert settle[0]["attrs"]["seconds"] == 0.01


def test_finish_does_not_add_verification_settle(monkeypatch):
    wait = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr(asyncio, "wait_for", wait)
    agent = _Agent([("finish", {"message": "already done"})])
    asyncio.run(_single_step(agent))
    wait.assert_not_awaited()


@pytest.mark.parametrize("interruption", ["cancel", "unknown", "pending"])
def test_settle_interruption_never_requests_completion(interruption):
    async def run():
        import threading

        agent = _Agent([("back", {"verify_after": True})])
        agent.verification_settle_seconds = 1.0
        entered = asyncio.Event()
        proceed = asyncio.Event()

        async def controlled_wait():
            entered.set()
            await proceed.wait()

        agent._cancel_event.wait = controlled_wait
        pending = asyncio.create_task(_single_step(agent))
        await entered.wait()
        if interruption == "cancel":
            await agent.cancel()
        elif interruption == "unknown":
            agent.device._outcome_unknown = True
        else:
            agent.device._writes.add(threading.Event())
        proceed.set()
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            step = await pending
            assert step["finished"] is True
            assert step["success"] is False
            assert step.get("stop_reason") != "verification_requested"
        assert agent._cached_observation is None

    asyncio.run(run())
