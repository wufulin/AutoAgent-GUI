"""Regression coverage for layered grounding, retry context, and replay evidence."""

import asyncio
import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import RunContextWrapper

from AutoGLM_GUI import subtasks
from AutoGLM_GUI.agents.gemini.async_agent import AsyncGeminiAgent
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.layered_agent_service import chat, execute_subtask
from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
from AutoGLM_GUI.phone_agent_trace import phone_agent_replay_active
from AutoGLM_GUI.subtasks import SubtaskResult, SubtaskRunContext, Verification


GOAL = "点击城市名"
CRITERIA = ["目标城市页已打开"]


def context_for(task_id="task-a", trace_id="trace-a"):
    return SubtaskRunContext(
        task_id, "session-a", "fake-phone", 100, "config", trace_id=trace_id
    )


def verification(verdict="met", reason="目标城市页可见"):
    return Verification(
        criteria=[{"criterion": CRITERIA[0], "verdict": verdict, "reason": reason}],
        facts=[reason],
        observed_at="2026-01-01T00:00:00+00:00",
        screenshot_sha256="fresh-frame-digest",
    )


class Device:
    device_id = "fake-phone"

    def __init__(self):
        self.frames = []
        self.taps = []
        self.width = 1080
        self.height = 2412
        self.fail_after_tap = False

    def get_screenshot(self, timeout=10):
        if self.taps and self.fail_after_tap:
            raise RuntimeError("screenshot unavailable")
        frame = Screenshot(
            base64.b64encode(f"frame-{len(self.frames)}".encode()).decode(),
            self.width,
            self.height,
        )
        self.frames.append(frame)
        return frame

    def get_current_app(self):
        return "test-app"

    def tap(self, x, y, delay=None):
        self.taps.append((x, y))


class Agent(AsyncGeminiAgent):
    def __init__(self, device):
        super().__init__(
            model_config=ModelConfig(),
            agent_config=AgentConfig(max_steps=100, verbose=False),
            device=device,
        )
        self.calls = []
        self.responses = []
        self.before_response = None

    async def _call_llm_with_tools(self):
        self.calls.append(copy.deepcopy(self.context))
        if self.before_response:
            await self.before_response()
        name, arguments = self.responses.pop(0)
        return "", None, name, arguments


@pytest.fixture
def setup_phone(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOGLM_TRACE_FILE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("AUTOGLM_TRACE_ENABLED", "1")
    monkeypatch.setenv("AUTOGLM_TRACE_REPLAY_ENABLED", "1")
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", "artifact")
    device = Device()
    agent = Agent(device)
    manager = SimpleNamespace(
        acquire_device_lease=AsyncMock(),
        validate_device_lease=AsyncMock(),
        dispose_device_lease=AsyncMock(return_value=True),
        quarantine_device=AsyncMock(),
        acquire_device_async=AsyncMock(return_value=True),
        get_agent_with_context_async=AsyncMock(return_value=agent),
        release_device_async=AsyncMock(),
    )
    monkeypatch.setattr(PhoneAgentManager, "get_instance", lambda: manager)
    monkeypatch.setattr(
        SubtaskRunContext, "fingerprint", staticmethod(lambda: "config")
    )
    monkeypatch.setattr(
        subtasks, "verify_completion", AsyncMock(return_value=verification())
    )
    context = context_for()
    return agent, device, manager, context


async def invoke(
    context, message=GOAL, device_id="fake-phone", *, max_steps=5, checkpoint=None
):
    result = await execute_subtask.on_invoke_tool(
        RunContextWrapper(context=context),
        json.dumps(
            {
                "device_id": device_id,
                "goal": message,
                "success_criteria": CRITERIA,
                "constraints": [],
                "max_steps": max_steps,
                "checkpoint": checkpoint,
            }
        ),
    )
    return json.loads(result)


def records(tmp_path, trace_id="trace-a"):
    path = tmp_path / "runs" / trace_id / "replay.jsonl"
    return [json.loads(line)["phone_agent"] for line in path.read_text().splitlines()]


def test_layered_prompt_preserves_tool_format_until_task_closes(setup_phone):
    agent, _, manager, context = setup_phone
    original = agent.get_system_prompt()
    agent.responses = [("finish", {"message": "ELEMENT_NOT_FOUND: 当前页面没有城市名"})]

    async def run():
        result = await invoke(context)
        manager.dispose_device_lease.assert_not_awaited()
        await context.close()
        return result

    result = asyncio.run(run())
    actual = agent.calls[0][0]["content"]
    assert original in actual
    assert "WRONG_TARGET" in actual and "不要猜测坐标" in actual
    assert "<answer>" not in actual
    assert result["success"] is False
    assert result["result"]["status"] == "blocked"
    manager.dispose_device_lease.assert_awaited_once()
    subtasks.verify_completion.assert_not_awaited()


def test_failure_coordinates_survive_continuation_but_not_other_tasks(
    setup_phone, monkeypatch
):
    agent, _, _, context = setup_phone
    verifier = AsyncMock(
        return_value=verification(
            "unmet", "WRONG_TARGET: 当前为景点游玩页，目标城市页未打开"
        )
    )
    monkeypatch.setattr(subtasks, "verify_completion", verifier)

    async def run():
        agent.responses = [
            ("tap", {"x": 245, "y": 165}),
            ("finish", {"message": "已进入景点游玩"}),
        ]
        first = await invoke(context)
        previous = first
        for report in ("当前为景点游玩页", "已回首页", "重试前重新定位"):
            agent.responses = [("finish", {"message": report})]
            previous = await invoke(
                context, checkpoint=previous["result"]["checkpoint"]
            )
            assert previous["result"]["subtask_id"] == first["result"]["subtask_id"]
        retry_input = json.dumps(agent.calls[-1], ensure_ascii=False)
        attempt_count = len(context.recent_attempts["fake-phone"])
        await context.close()
        verifier.return_value = verification()
        agent.responses = [("finish", {"message": "完成"})]
        other_context = context_for(task_id="task-b", trace_id="trace-b")
        await invoke(other_context, "其他任务")
        other_input = json.dumps(agent.calls[-1][1:], ensure_ascii=False)
        await other_context.close()
        return first, retry_input, other_input, attempt_count

    first, retry_input, other_input, attempt_count = asyncio.run(run())
    assert first["success"] is False
    assert first["result"]["status"] == "partial"
    assert first["result"]["stop_reason"] == "verification_unmet"
    assert "245" in retry_input and "165" in retry_input
    assert "WRONG_TARGET" in retry_input and "景点游玩" in retry_input
    assert "245" not in other_input and "近期执行记录" not in other_input
    assert attempt_count == 3
    assert context.recent_attempts == {}
    assert context.instruction("different-phone", "点击") == "点击"


def test_step_budget_resumes_without_losing_final_frame(setup_phone, tmp_path):
    agent, device, _, context = setup_phone
    agent.responses = [("tap", {"x": n * 10, "y": 165}) for n in range(1, 6)]

    async def run():
        first = await invoke(context)
        agent.responses = [("finish", {"message": "当前页面说明"})]
        second = await invoke(
            context, max_steps=1, checkpoint=first["result"]["checkpoint"]
        )
        await context.close()
        return first, second

    first, second = asyncio.run(run())
    assert first["steps"] == 5 and first["success"] is False
    assert first["result"]["status"] == "partial"
    assert first["result"]["stop_reason"] == "step_budget_reached"
    assert len(device.taps) == 5
    assert second["steps"] == 1 and second["success"] is True
    assert second["result"]["total_steps"] == 6
    assert first["result"]["subtask_id"] == second["result"]["subtask_id"]
    assert first["phone_call_id"] != second["phone_call_id"]
    evidence = records(tmp_path)
    final_frame = next(
        r
        for r in evidence
        if r["call_id"] == first["phone_call_id"]
        and r["index"] == 5
        and r["phase"] == "post_action"
    )
    assert final_frame["artifacts"]
    assert any(
        r["call_id"] == first["phone_call_id"]
        and r["phase"] == "done"
        and r["result"]["success"] is False
        for r in evidence
    )
    assert any(
        r["call_id"] == second["phone_call_id"]
        and r["phase"] == "observation"
        and r["index"] == 6
        for r in evidence
    )
    paths = [r["artifacts"]["screenshot"]["path"] for r in evidence if r["artifacts"]]
    assert len(paths) == len(set(paths))


def test_replay_preserves_actual_model_input_and_pixel_mapping(setup_phone, tmp_path):
    agent, device, _, context = setup_phone
    agent.responses = [("tap", {"x": 245, "y": 165}), ("finish", {"message": "完成"})]
    asyncio.run(invoke(context))
    evidence = records(tmp_path)
    observation = next(r for r in evidence if r["phase"] == "observation")
    assert observation["screen_width"] == 1080
    assert observation["screen_height"] == 2412
    image = agent.calls[0][1]["content"][0]["image_url"]["url"].split(",", 1)[1]
    path = tmp_path / "runs/trace-a" / observation["artifacts"]["screenshot"]["path"]
    assert path.read_bytes() == base64.b64decode(image)
    assert device.taps == [(264, 397)]
    assert next(r for r in evidence if r["phase"] == "action_selected")["action"] == {
        "_metadata": "do",
        "action": "Tap",
        "element": [245, 165],
    }
    assert any(r["phase"] == "post_action" and r["artifacts"] for r in evidence)


@pytest.mark.parametrize(
    "mode,failed,expected",
    [
        ("off", True, False),
        ("on_error", True, True),
        ("on_error", False, False),
    ],
)
def test_capture_policy_applies_to_inner_frames(
    setup_phone, tmp_path, monkeypatch, mode, failed, expected
):
    agent, _, _, context = setup_phone
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", mode)
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_ACTION", "0")
    agent.responses = [
        ("tap", {"x": 245, "y": 165}),
        ("finish", {"message": "WRONG_TARGET: 误入其他页" if failed else "完成"}),
    ]
    asyncio.run(invoke(context))
    evidence = records(tmp_path)
    assert any(r["artifacts"] for r in evidence) is expected
    assert all(r["action"] is None for r in evidence)
    assert "base64" not in (tmp_path / "runs/trace-a/replay.jsonl").read_text()


@pytest.mark.parametrize(
    "verdict,status", [("unmet", "partial"), ("unknown", "blocked")]
)
def test_on_error_capture_uses_verified_outcome(
    setup_phone, tmp_path, monkeypatch, verdict, status
):
    agent, _, _, context = setup_phone
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", "on_error")
    monkeypatch.setattr(
        subtasks,
        "verify_completion",
        AsyncMock(return_value=verification(verdict)),
    )
    agent.responses = [("finish", {"message": "模型声称完成"})]
    result = asyncio.run(invoke(context))
    assert result["result"]["status"] == status
    evidence = records(tmp_path)
    assert any(item["artifacts"] for item in evidence)
    terminal = next(item for item in evidence if item["phase"] == "done")
    assert terminal["result"]["success"] is False


def test_on_error_capture_keeps_final_frame_with_larger_budget(
    setup_phone, tmp_path, monkeypatch
):
    agent, device, _, context = setup_phone
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", "on_error")
    agent.responses = [("tap", {"x": step * 10, "y": 165}) for step in range(1, 9)]
    result = asyncio.run(invoke(context, max_steps=8))
    assert result["result"]["status"] == "partial"
    assert len(device.taps) == 8
    final_frame = next(
        item
        for item in records(tmp_path)
        if item["call_id"] == result["phone_call_id"]
        and item["phase"] == "post_action"
        and item["index"] == 8
    )
    assert final_frame["artifacts"]


@pytest.mark.parametrize("capture_mode", ["artifact", "on_error"])
def test_cancel_preserves_observation_and_disposes_task_agent(
    setup_phone, tmp_path, monkeypatch, capture_mode
):
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", capture_mode)
    agent, _, manager, context = setup_phone

    async def cancel():
        raise asyncio.CancelledError()

    agent.before_response = cancel

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await invoke(context)
        await context.close()

    asyncio.run(run())
    assert context.cancelled is True
    assert context.agents == {}
    assert not phone_agent_replay_active()
    manager.dispose_device_lease.assert_awaited_once()
    frame = next(r for r in records(tmp_path) if r["phase"] == "observation")
    assert frame["artifacts"]
    assert any(r["phase"] == "cancelled" for r in records(tmp_path))


def test_cancel_during_verification_flushes_failure_frames(
    setup_phone, tmp_path, monkeypatch
):
    agent, _, manager, context = setup_phone
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", "on_error")
    monkeypatch.setattr(
        subtasks, "verify_completion", AsyncMock(side_effect=asyncio.CancelledError())
    )
    agent.responses = [("finish", {"message": "模型声称完成"})]

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await invoke(context)
        await context.close()

    asyncio.run(run())
    evidence = records(tmp_path)
    assert any(item["phase"] == "cancelled" for item in evidence)
    assert any(item["artifacts"] for item in evidence)
    assert not phone_agent_replay_active()
    assert context.final_result() == (False, "cancelled", "user_stopped")
    manager.dispose_device_lease.assert_awaited_once()


def test_rotation_during_inference_stops_without_click(setup_phone):
    agent, device, _, context = setup_phone

    async def rotate():
        device.width, device.height = 2412, 1080

    agent.before_response = rotate
    agent.responses = [("tap", {"x": 245, "y": 165})]
    result = asyncio.run(invoke(context))
    assert result["success"] is False
    assert "SCREEN_CHANGED" in result["result"]["message"]
    assert device.taps == []


def test_post_action_capture_failure_does_not_replay_click(setup_phone, tmp_path):
    agent, device, _, context = setup_phone
    device.fail_after_tap = True
    agent.responses = [("tap", {"x": 245, "y": 165})]
    result = asyncio.run(invoke(context))
    assert result["success"] is False
    assert device.taps == [(264, 397)]
    evidence = records(tmp_path)
    assert any(r["phase"] == "post_action_unavailable" for r in evidence)
    action_result = next(r for r in evidence if r["phase"] == "action_result")
    assert action_result["result"]["success"] is True


def test_reset_resolves_config_changes_and_restores_default(setup_phone):
    agent, _, _, _ = setup_phone
    default = agent.get_system_prompt()
    agent.agent_config.system_prompt = "custom protocol"
    agent.reset()
    assert agent.context[0]["content"] == "custom protocol"
    agent.agent_config.system_prompt = None
    agent.reset()
    assert agent.context[0]["content"] == default


def test_legacy_chat_returns_verified_result_and_replay_id(setup_phone, tmp_path):
    agent, _, _, context = setup_phone
    agent.responses = [("finish", {"message": "完成"})]

    async def run():
        response = await chat.on_invoke_tool(
            RunContextWrapper(context=context),
            json.dumps({"device_id": "fake-phone", "message": GOAL}),
        )
        await context.close()
        return json.loads(response)

    result = asyncio.run(run())
    assert result["success"] is True
    assert result["result"]["status"] == "completed"
    assert result["result"]["success_criteria"] == [GOAL]
    assert result["phone_call_id"] == result["result"]["phone_call_id"]
    assert any(
        item["phase"] == "done"
        and item["call_id"] == result["phone_call_id"]
        and item["result"]["success"] is True
        for item in records(tmp_path)
    )


def test_mcp_sibling_override_preserves_function_calling_format(setup_phone):
    from AutoGLM_GUI.api.mcp import chat as mcp_chat

    agent, _, manager, _ = setup_phone
    original = agent.get_system_prompt()
    agent.responses = [("finish", {"message": "ELEMENT_NOT_FOUND: 没有目标"})]
    result = asyncio.run(mcp_chat("fake-phone", "点击城市"))
    assert result["success"] is False
    assert original in agent.calls[0][0]["content"]
    assert "<answer>" not in agent.calls[0][0]["content"]
    assert agent.context[0]["content"] == original
    assert agent.agent_config.max_steps == 100
    manager.release_device_async.assert_awaited_once()


def test_planner_run_allocates_fresh_context_for_same_session(monkeypatch):
    import AutoGLM_GUI.layered_agent_service as service

    contexts = []
    monkeypatch.setattr(service, "_ensure_agent", lambda: object())
    monkeypatch.setattr(service, "_get_or_create_session", lambda _: object())
    monkeypatch.setattr(
        service.config_manager,
        "get_effective_config",
        lambda: SimpleNamespace(layered_max_turns=50, default_max_steps=100),
    )
    monkeypatch.setattr(
        SubtaskRunContext, "fingerprint", staticmethod(lambda: "config")
    )

    def run_streamed(*args, **kwargs):
        contexts.append(kwargs["context"])
        return SimpleNamespace()

    monkeypatch.setattr(service.Runner, "run_streamed", run_streamed)
    monkeypatch.setattr(service, "_active_runs", {})
    service.start_run(task_id="one", session_id="shared", message="task one")
    contexts[0].remember(
        "fake-phone",
        SubtaskResult(
            subtask_id="previous",
            goal=GOAL,
            status="partial",
            message="failure",
            stop_reason="verification_unmet",
        ),
    )
    service.start_run(task_id="two", session_id="shared", message="task two")
    assert contexts[1].task_id == "two"
    assert contexts[1].recent_attempts == {}
    assert contexts[1].checkpoints == {}
    assert contexts[1].agents == {}
    assert contexts[1].total_steps == 0


def test_recording_disabled_and_sensitive_frames_are_not_persisted(
    setup_phone, tmp_path, monkeypatch
):
    from AutoGLM_GUI.phone_agent_trace import (
        PhoneAgentReplay,
        phone_agent_replay,
        record_phone_agent_event,
    )

    replay = PhoneAgentReplay(task_id="task-a", trace_id="trace-a", device_id="fake")

    async def run():
        with phone_agent_replay(replay):
            await record_phone_agent_event(
                "observation",
                1,
                screenshot=Screenshot("c2VjcmV0", 1080, 2412, is_sensitive=True),
            )
        replay.finish(True)

    asyncio.run(run())
    assert records(tmp_path)[0]["artifacts"] == {}
    assert "c2VjcmV0" not in (tmp_path / "runs/trace-a/replay.jsonl").read_text()
    monkeypatch.setenv("AUTOGLM_TRACE_REPLAY_ENABLED", "0")
    replay.trace_id = "disabled"
    asyncio.run(run())
    assert not (tmp_path / "runs/disabled").exists()
