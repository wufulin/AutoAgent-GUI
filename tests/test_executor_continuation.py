"""Execution budgets and terminal signals preserve state across subtasks."""

import asyncio
import copy
from typing import Any

import pytest

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.agents.base.async_agent_base import AsyncAgentBase
from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.agents.glm.async_agent import AsyncGLMAgent
from AutoGLM_GUI.agents.glm.parser import GLMParser
from AutoGLM_GUI.agents.qwen.async_agent import AsyncQwenAgent
from AutoGLM_GUI.agents.qwen.parser import QwenParser
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot


class Device:
    device_id = "phone"

    def get_screenshot(self, timeout=10):
        return Screenshot(base64_data="image", width=100, height=200)

    def get_current_app(self):
        return "app"


class CountingAgent(AsyncAgentBase):
    def _get_default_system_prompt(self, lang):
        return "system"

    def _prepare_initial_context(self, task, screenshot, app, reference_images=None):
        self._context.append({"role": "user", "content": task})

    async def _execute_step(self):
        self._step_count += 1
        yield {
            "type": "step",
            "data": {
                "step": self._step_count,
                "action": {"_metadata": "do", "action": "Wait"},
                "success": True,
                "finished": False,
            },
        }


def agent(max_steps=10):
    return CountingAgent(ModelConfig(), AgentConfig(max_steps=max_steps), Device())


async def collect(stream):
    return [event async for event in stream]


def test_budget_continuation_preserves_context_and_total_limit():
    executor = agent(max_steps=3)
    first = asyncio.run(collect(executor.stream("goal", step_budget=2)))
    assert first[-1]["data"]["status"] == "partial"
    assert first[-1]["data"]["stop_reason"] == "step_budget_reached"
    assert executor.step_count == 2
    second = asyncio.run(
        collect(executor.stream("goal", continue_with="continue", step_budget=2))
    )
    assert [e["data"]["step"] for e in second if e["type"] == "step"] == [3]
    assert second[-1]["data"]["stop_reason"] == "max_steps_reached"
    assert executor.agent_config.max_steps == 3
    assert executor.context[1]["content"] == "goal"
    assert "continue" in str(executor.context[-1]["content"])


def test_budget_continuation_cannot_reset_repetition_watchdog(monkeypatch):
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.base.async_agent_base.WATCHDOG_REPEATED_ACTION_LIMIT", 3
    )
    executor = agent()
    asyncio.run(collect(executor.stream("goal", step_budget=2)))
    result = asyncio.run(
        collect(executor.stream("goal", continue_with="continue", step_budget=2))
    )
    assert executor.step_count == 3
    assert result[-1]["data"]["status"] == "blocked"
    assert result[-1]["data"]["stop_reason"] == "watchdog_repeated_actions"
    executor.reset()
    result = asyncio.run(collect(executor.stream("new goal", step_budget=2)))
    assert result[-1]["data"]["stop_reason"] == "step_budget_reached"


def test_budget_continuation_cannot_reset_runtime_watchdog(monkeypatch):
    executor = agent()
    asyncio.run(collect(executor.stream("goal", step_budget=1)))
    executor._watchdog_started_at -= 4000
    result = asyncio.run(
        collect(executor.stream("goal", continue_with="continue", step_budget=1))
    )
    assert result[-1]["data"]["stop_reason"] == "watchdog_timeout"
    assert executor.step_count == 1


def test_repeated_completion_requests_cannot_evade_watchdog(monkeypatch):
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.base.async_agent_base.WATCHDOG_REPEATED_ACTION_LIMIT", 3
    )

    class FinishAgent(CountingAgent):
        async def _execute_step(self):
            self._step_count += 1
            yield {
                "type": "step",
                "data": {
                    "action": {"_metadata": "finish", "message": "done"},
                    "success": True,
                    "finished": True,
                },
            }

    executor = FinishAgent(ModelConfig(), AgentConfig(max_steps=None), Device())
    result = asyncio.run(collect(executor.stream("goal", step_budget=1)))
    assert result[-1]["data"]["status"] == "completion_requested"
    for _ in range(2):
        result = asyncio.run(
            collect(
                executor.stream(
                    "goal", continue_with="verification unmet", step_budget=1
                )
            )
        )
    assert executor.step_count == 3
    assert result[-1]["data"]["status"] == "blocked"
    assert result[-1]["data"]["stop_reason"] == "watchdog_repeated_actions"


def test_cancel_flag_emits_cancelled_and_does_not_become_budget_exhaustion():
    executor = agent()

    async def run():
        events = []
        with pytest.raises(asyncio.CancelledError):
            async for event in executor.stream("goal", step_budget=1):
                events.append(event)
                if event["type"] == "step":
                    await executor.cancel()
        return events

    result = asyncio.run(run())
    assert result[-1]["type"] == "cancelled"
    assert result[-1]["data"]["status"] == "cancelled"
    assert not executor.is_running


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_per_call_budget_does_not_start_executor(budget):
    executor = agent()
    with pytest.raises(ValueError):
        asyncio.run(collect(executor.stream("goal", step_budget=budget)))
    assert executor.step_count == 0
    assert not executor.is_running


@pytest.mark.parametrize("parser", [GLMParser(), QwenParser()])
@pytest.mark.parametrize(
    "text",
    [
        'finish(message="unterminated)',
        'finish(message="missing close"',
        'finish(message="a", message="b")',
        'finish_other(message="wrong function")',
        'finish("positional")',
        'finish(**{"message": "expanded"})',
        'finish(message=str("executable"))',
        "finish(message=3)",
        'finish(message="ok"); do(action="Tap")',
    ],
)
def test_malformed_completion_is_rejected(parser, text):
    with pytest.raises(ValueError):
        parser.parse(text)


@pytest.mark.parametrize("agent_class", [AsyncGLMAgent, AsyncQwenAgent])
def test_model_parse_failure_cannot_execute_or_claim_completion(
    agent_class, monkeypatch
):
    executor = agent_class(ModelConfig(), AgentConfig(verbose=False), Device())
    executed: list[Any] = []

    async def invalid_model(messages):
        yield {"type": "raw", "content": 'finish(message="broken'}

    async def execute(*args):
        executed.append(args)
        raise AssertionError("A malformed action must not reach the device handler")

    monkeypatch.setattr(executor, "_stream_openai", invalid_model)
    monkeypatch.setattr(executor.action_handler, "execute", execute)
    result = asyncio.run(collect(executor.stream("goal")))
    assert result[-1]["data"]["status"] == "failed"
    assert result[-1]["data"]["stop_reason"] == "parse_error"
    assert executed == []


def test_initial_device_failure_has_structured_terminal_reason(monkeypatch):
    executor = agent()

    async def fail():
        raise RuntimeError("disconnected")

    monkeypatch.setattr(executor.device, "get_screenshot", fail)
    result = asyncio.run(collect(executor.stream("goal")))
    assert result[-1]["data"]["status"] == "failed"
    assert result[-1]["data"]["stop_reason"] == "device_error"


@pytest.mark.parametrize("rotate_on_resume", [False, True])
def test_gemini_continuation_uses_fresh_grounding_and_preserves_replay(
    monkeypatch, rotate_on_resume
):
    class FrameDevice(Device):
        def __init__(self):
            self.frames = []
            self.width, self.height = 100, 200

        def get_screenshot(self, timeout=10):
            frame = Screenshot(
                base64_data=f"frame-{len(self.frames) + 1}",
                width=self.width,
                height=self.height,
            )
            self.frames.append(frame)
            return frame

    device = FrameDevice()
    executor = AsyncGeminiAgent(ModelConfig(), AgentConfig(verbose=False), device)
    model_inputs = []
    executed = []
    replay = []

    async def model():
        model_inputs.append(copy.deepcopy(executor.context))
        if rotate_on_resume and len(model_inputs) == 2:
            device.width, device.height = 200, 100
        return "", None, "tap", {"x": 100 * len(model_inputs), "y": 500}

    async def execute(action, width, height):
        executed.append((action, width, height))
        return ActionResult(success=True, should_finish=False)

    async def record(phase, step, **kwargs):
        replay.append((phase, step, kwargs))

    monkeypatch.setattr(executor, "_call_llm_with_tools", model)
    monkeypatch.setattr(executor.action_handler, "execute", execute)
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.gemini.async_agent.phone_agent_replay_active",
        lambda: True,
    )
    monkeypatch.setattr(
        "AutoGLM_GUI.agents.gemini.async_agent.record_phone_agent_event", record
    )
    first = asyncio.run(collect(executor.stream("tap", step_budget=1)))
    second = asyncio.run(
        collect(executor.stream("tap", continue_with="continue", step_budget=1))
    )

    assert first[-1]["data"]["stop_reason"] == "step_budget_reached"
    assert executor.step_count == 2
    observations = [
        (step, data["screenshot"])
        for phase, step, data in replay
        if phase == "observation"
    ]
    assert observations == [(1, device.frames[0]), (2, device.frames[3])]
    for model_input, (_, observation) in zip(model_inputs, observations):
        image_urls = [
            part["image_url"]["url"]
            for message in model_input
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        ]
        assert image_urls == [f"data:image/png;base64,{observation.base64_data}"]
    for step in (1, 2):
        assert [phase for phase, number, _ in replay if number == step] == [
            "observation",
            "action_selected",
            "pre_action",
            "action_result",
            "post_action",
        ]
    if rotate_on_resume:
        assert len(executed) == 1
        assert second[-1]["data"]["status"] == "failed"
        assert "SCREEN_CHANGED" in second[-1]["data"]["message"]
    else:
        assert len(executed) == 2
        assert [dimensions[1:] for dimensions in executed] == [(100, 200)] * 2
        assert second[-1]["data"]["stop_reason"] == "step_budget_reached"


def test_qwen_info_pauses_without_action_and_continues_only_with_user_input(
    monkeypatch,
):
    executor = AsyncQwenAgent(ModelConfig(), AgentConfig(verbose=False), Device())
    model_inputs = []
    executed = []
    original_execute = executor.action_handler.execute

    async def model(messages):
        model_inputs.append(copy.deepcopy(messages))
        response = (
            '<answer>info(question="请选择第一个还是第二个联系人？")</answer>'
            if len(model_inputs) == 1
            else '<answer>finish(message="已确认联系人")</answer>'
        )
        yield {"type": "raw", "content": response}

    async def execute(action, *args):
        executed.append(action)
        return await original_execute(action, *args)

    monkeypatch.setattr(executor, "_stream_openai", model)
    monkeypatch.setattr(executor.action_handler, "execute", execute)
    first = asyncio.run(collect(executor.stream("选择联系人", step_budget=5)))
    assert [event["type"] for event in first] == ["step", "takeover"]
    assert first[0]["data"]["waiting_for_input"] is True
    assert first[-1]["data"]["status"] == "needs_user"
    assert first[-1]["data"]["message"] == "请选择第一个还是第二个联系人？"
    assert len(model_inputs) == 1
    assert executed == []
    assert executor.step_count == 1

    second = asyncio.run(
        collect(
            executor.stream(
                "选择联系人", continue_with="选择第二个联系人", step_budget=5
            )
        )
    )
    assert executor.step_count == 2
    assert len(model_inputs) == 2
    assert "选择第二个联系人" in str(model_inputs[-1])
    assert [action["_metadata"] for action in executed] == ["finish"]
    assert second[-1]["data"]["status"] == "completion_requested"
