"""Behavior contracts for task-local execution and completion evidence."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from AutoGLM_GUI import subtasks
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
from AutoGLM_GUI.subtasks import (
    SubtaskResult,
    SubtaskRunContext,
    Verification,
    collect_execution,
    verify_completion,
)


def event(kind, **data):
    return {"type": kind, "data": data}


class FakeAgent:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.step_count = 0
        self.context = []
        self.feedback = []
        self.budgets = []
        self.resets = 0
        self.model_config = ModelConfig(base_url="https://example.test/v1")
        self.agent_config = AgentConfig()
        self.device = SimpleNamespace(
            outcome_unknown=False,
            get_screenshot=AsyncMock(
                return_value=Screenshot(
                    base64.b64encode(b"fresh-frame").decode(), 10, 10
                )
            ),
            drain=AsyncMock(return_value=True),
        )

    def reset(self):
        self.resets += 1
        self.step_count = 0
        self.context.clear()

    async def cancel(self):
        pass

    async def stream(self, task, *, continue_with=None, step_budget=None):
        self.feedback.append(continue_with)
        self.budgets.append(step_budget)
        self.context.append(task if continue_with is None else continue_with)
        for value in next(self.batches):
            if isinstance(value, BaseException):
                raise value
            if value["type"] == "step":
                self.step_count += 1
            yield value


@pytest.mark.parametrize(
    "events,status,reason",
    [
        ([event("done", success=True)], "completion_requested", "finish_requested"),
        ([event("done", success=False)], "failed", "execution_error"),
        ([event("takeover", success=True)], "needs_user", "needs_user"),
        ([event("cancelled")], "cancelled", "user_stopped"),
        ([event("error", message="bad")], "failed", "execution_error"),
        (
            [event("error"), event("done", success=True)],
            "failed",
            "execution_error",
        ),
        (
            [event("error"), event("done", success=False, stop_reason="model_error")],
            "failed",
            "model_error",
        ),
        ([], "unknown", "missing_terminal_event"),
        (
            [event("done", stop_reason="step_budget_reached", success=False)],
            "partial",
            "step_budget_reached",
        ),
        (
            [event("done", stop_reason="watchdog_repeated_actions", success=False)],
            "blocked",
            "watchdog_repeated_actions",
        ),
    ],
)
def test_collector_preserves_terminal_meaning(events, status, reason):
    outcome = asyncio.run(collect_execution(FakeAgent([events]), "goal", None, 10))
    assert (outcome.status, outcome.stop_reason) == (status, reason)


def test_collector_propagates_cancellation():
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            collect_execution(
                FakeAgent([[event("cancelled"), asyncio.CancelledError()]]),
                "goal",
                None,
                10,
            )
        )


def setup_context(monkeypatch, batches, max_steps=100):
    agent = FakeAgent(batches)
    manager = SimpleNamespace(
        acquire_device_lease=AsyncMock(),
        validate_device_lease=AsyncMock(),
        get_agent_with_context_async=AsyncMock(return_value=agent),
        dispose_device_lease=AsyncMock(return_value=True),
        destroy_agent_async=AsyncMock(),
        quarantine_device=AsyncMock(),
    )
    monkeypatch.setattr(PhoneAgentManager, "get_instance", lambda: manager)
    monkeypatch.setattr(
        SubtaskRunContext, "fingerprint", staticmethod(lambda: "config")
    )
    context = SubtaskRunContext("task", "session", "phone", max_steps, "config")
    return context, agent, manager


def verification(verdict="met"):
    return Verification(
        criteria=[
            {"criterion": "title", "verdict": verdict, "reason": "visible title"}
        ],
        facts=["title"],
        observed_at="2026-01-01T00:00:00+00:00",
        screenshot_sha256="frame-digest",
    )


def test_partial_resume_preserves_context_lease_and_incremental_steps(monkeypatch):
    context, agent, manager = setup_context(
        monkeypatch,
        [
            [
                event("step"),
                event("done", success=False, stop_reason="step_budget_reached"),
            ],
            [event("step"), event("done", success=True)],
        ],
    )
    monkeypatch.setattr(
        subtasks, "verify_completion", AsyncMock(return_value=verification())
    )

    async def scenario():
        first = await context.execute("phone", "goal", ["title"], [], 1)
        assert first.status == "partial" and first.checkpoint
        second = await context.execute(
            "phone", "goal", ["title"], [], 1, first.checkpoint
        )
        assert second.status == "completed"
        assert first.subtask_id == second.subtask_id
        assert (first.steps, second.steps, second.total_steps) == (1, 1, 2)
        assert context.total_steps == 2
        assert context.final_result() == (True, "completed", "goal_verified")
        assert agent.resets == 1 and agent.feedback[1]
        manager.acquire_device_lease.assert_awaited_once()
        manager.dispose_device_lease.assert_not_awaited()
        await context.close()
        await context.close()
        manager.dispose_device_lease.assert_awaited_once()
        assert not context.checkpoints
        expired = await context.execute(
            "phone", "goal", ["title"], [], 1, first.checkpoint
        )
        assert expired.status == "blocked"

    asyncio.run(scenario())


def test_failed_verification_resumes_with_feedback(monkeypatch):
    context, agent, _ = setup_context(
        monkeypatch, [[event("done", success=True)], [event("done", success=True)]]
    )
    verifier = AsyncMock(side_effect=[verification("unmet"), verification("met")])
    monkeypatch.setattr(subtasks, "verify_completion", verifier)

    async def scenario():
        first = await context.execute("phone", "goal", ["title"], [])
        assert first.stop_reason == "verification_unmet" and first.checkpoint
        second = await context.execute(
            "phone", "goal", ["title"], [], checkpoint=first.checkpoint
        )
        assert second.success
        assert "visible title" in agent.feedback[1]
        await context.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["foreign", "goal", "criteria", "constraints", "device"]
)
def test_checkpoint_binding_rejects_mismatch_without_actions(monkeypatch, kind):
    context, agent, _ = setup_context(
        monkeypatch, [[event("done", success=False, stop_reason="step_budget_reached")]]
    )

    async def scenario():
        first = await context.execute("phone", "goal", ["title"], [])
        result = await context.execute(
            "other" if kind == "device" else "phone",
            "other" if kind == "goal" else "goal",
            ["other"] if kind == "criteria" else ["title"],
            ["other"] if kind == "constraints" else [],
            checkpoint="foreign" if kind == "foreign" else first.checkpoint,
        )
        assert result.status == "blocked" and len(agent.feedback) == 1
        await context.close()

    asyncio.run(scenario())


def test_total_budget_across_subtasks(monkeypatch):
    context, agent, _ = setup_context(
        monkeypatch,
        [
            [event("step"), event("done", success=True)],
            [
                event("step"),
                event("done", success=False, stop_reason="step_budget_reached"),
            ],
        ],
        max_steps=2,
    )
    monkeypatch.setattr(
        subtasks, "verify_completion", AsyncMock(return_value=verification())
    )

    async def scenario():
        assert (await context.execute("phone", "goal", ["title"], [])).success
        result = await context.execute("phone", "next goal", ["title"], [])
        assert result.status == "blocked" and not result.checkpoint
        assert result.stop_reason == "task_step_budget_reached"
        assert agent.budgets == [2, 1]
        await context.close()

    asyncio.run(scenario())


def test_unknown_device_outcome_is_quarantined(monkeypatch):
    context, agent, manager = setup_context(
        monkeypatch, [[event("step"), event("done", success=True)]]
    )
    agent.device.outcome_unknown = True
    verifier = AsyncMock()
    monkeypatch.setattr(subtasks, "verify_completion", verifier)

    async def scenario():
        result = await context.execute("phone", "goal", ["title"], [])
        assert result.status == "unknown"
        manager.quarantine_device.assert_awaited_once()
        verifier.assert_not_awaited()
        await context.close()

    asyncio.run(scenario())


def test_cancel_during_verification_clears_checkpoint_and_releases_after_drain(
    monkeypatch,
):
    context, agent, manager = setup_context(
        monkeypatch, [[event("done", success=True)]]
    )

    async def scenario():
        started = asyncio.Event()

        async def verify(*args):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(subtasks, "verify_completion", verify)
        active = asyncio.create_task(context.execute("phone", "goal", ["title"], []))
        await started.wait()
        await context.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert context.final_result()[1] == "cancelled"
        manager.dispose_device_lease.assert_not_awaited()
        await context.close()
        agent.device.drain.assert_awaited_once()
        manager.dispose_device_lease.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content,status",
    [
        (
            '{"criteria":[{"criterion":"title","verdict":"met","reason":"seen"}],"facts":["title"]}',
            "met",
        ),
        (
            '{"criteria":[{"criterion":"title","verdict":"unmet","reason":"absent"}],"facts":[]}',
            "unmet",
        ),
        (
            '{"criteria":[{"criterion":"title","verdict":"unknown","reason":"hidden"}],"facts":[]}',
            "unknown",
        ),
        ('{"criteria":[],"facts":[]}', "invalid"),
        (
            '{"criteria":[{"criterion":"wrong","verdict":"met","reason":"seen"}],"facts":[]}',
            "invalid",
        ),
        ("not-json", "invalid"),
    ],
)
def test_verifier_uses_fresh_image_no_tools_and_strict_conditions(
    monkeypatch, content, status
):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0

        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(subtasks, "AsyncOpenAI", Client)
    agent = FakeAgent([])
    result = asyncio.run(
        verify_completion(agent.device, agent.model_config, "goal", ["title"])
    )
    agent.device.get_screenshot.assert_awaited_once()
    request = create.call_args.kwargs
    assert "tools" not in request
    assert "fresh-frame" not in result.model_dump_json()
    assert request["messages"][1]["content"][0]["image_url"]["url"].endswith(
        base64.b64encode(b"fresh-frame").decode()
    )
    assert result.screenshot_sha256
    if status == "invalid":
        assert result.error == "verification_unavailable"
    else:
        assert result.criteria[0].verdict == status and result.error is None


def test_verifier_request_failure_is_not_completion(monkeypatch):
    agent = FakeAgent([])
    agent.device.get_screenshot.side_effect = TimeoutError("private-provider-detail")
    result = asyncio.run(
        verify_completion(agent.device, agent.model_config, "goal", ["title"])
    )
    assert result.error == "verification_unavailable"
    assert "private-provider-detail" not in result.model_dump_json()


def test_planner_natural_completion_cannot_override_partial(monkeypatch):
    from AutoGLM_GUI.layered_agent_service import LayeredTaskRun

    context, _, _ = setup_context(monkeypatch, [])
    context.results["subtask"] = SubtaskResult(
        subtask_id="subtask",
        goal="goal",
        status="partial",
        message="未完成",
        stop_reason="step_budget_reached",
    )

    class Result:
        final_output = "任务全部成功！"

        async def stream_events(self):
            if False:
                yield

    async def scenario():
        run = LayeredTaskRun("task", "session", Result(), context=context)
        events = [value async for value in run.stream_events()]
        assert events[-1]["payload"]["success"] is False
        assert events[-1]["payload"]["result_status"] == "partial"
        assert events[-1]["payload"]["content"] == "未完成"
        assert context.closed

    asyncio.run(scenario())


def test_tool_envelope_keeps_structured_result_and_delta():
    from AutoGLM_GUI.layered_agent_service import LayeredTaskRun

    result = SubtaskResult(
        subtask_id="subtask",
        goal="goal",
        status="completed",
        message="done",
        stop_reason="goal_verified",
        steps=2,
        total_steps=12,
    )
    parsed, steps, success = LayeredTaskRun._parse_tool_output(result.envelope())
    assert parsed == result.model_dump()
    assert steps == 2 and success is True
    assert json.loads(result.envelope())["success"] is True


def test_layered_stream_combines_progress_tool_results_and_verified_finish(monkeypatch):
    from agents.stream_events import RunItemStreamEvent

    from AutoGLM_GUI.layered_agent_service import LayeredTaskRun

    context, _, manager = setup_context(
        monkeypatch,
        [
            [
                event("step"),
                event("done", success=False, stop_reason="step_budget_reached"),
            ],
            [event("step"), event("done", success=True)],
        ],
    )
    monkeypatch.setattr(
        subtasks, "verify_completion", AsyncMock(return_value=verification())
    )

    class Result:
        final_output = "已完成"

        async def stream_events(self):
            checkpoint = None
            for call_id in ["call-1", "call-2"]:
                yield RunItemStreamEvent(
                    name="tool_called",
                    item=SimpleNamespace(
                        type="tool_call_item",
                        raw_item={
                            "call_id": call_id,
                            "name": "execute_subtask",
                            "arguments": '{"goal":"goal"}',
                        },
                    ),
                )
                result = await context.execute(
                    "phone", "goal", ["title"], [], 1, checkpoint
                )
                checkpoint = result.checkpoint
                yield RunItemStreamEvent(
                    name="tool_output",
                    item=SimpleNamespace(
                        type="tool_call_output_item",
                        raw_item={"call_id": call_id},
                        output=result.envelope(),
                    ),
                )

    async def scenario():
        run = LayeredTaskRun("task", "session", Result(), context=context)
        events = [value async for value in run.stream_events()]
        progress = [
            value["payload"]["phase"]
            for value in events
            if value["type"] == "subtask_progress"
        ]
        assert progress == ["executing", "executing", "verifying"]
        outputs = [
            value["payload"] for value in events if value["type"] == "tool_result"
        ]
        assert [value["tool_name"] for value in outputs] == ["execute_subtask"] * 2
        assert [value["result"]["status"] for value in outputs] == [
            "partial",
            "completed",
        ]
        assert sum(value["steps"] for value in outputs) == 2
        verifying_index = next(
            index
            for index, value in enumerate(events)
            if value["type"] == "subtask_progress"
            and value["payload"]["phase"] == "verifying"
        )
        completed_index = next(
            index
            for index, value in enumerate(events)
            if value["type"] == "tool_result"
            and value["payload"]["result"]["status"] == "completed"
        )
        assert verifying_index < completed_index
        assert events[-1]["payload"]["success"] is True
        manager.dispose_device_lease.assert_awaited_once()

    asyncio.run(scenario())


def test_parallel_checkpoint_request_is_rejected_without_second_stream(monkeypatch):
    context, agent, _ = setup_context(monkeypatch, [[event("done", success=True)]])

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def verify(*args):
            entered.set()
            await release.wait()
            return verification()

        monkeypatch.setattr(subtasks, "verify_completion", verify)
        running = asyncio.create_task(context.execute("phone", "goal", ["title"], []))
        await entered.wait()
        second = await context.execute("phone", "goal", ["title"], [])
        assert second.stop_reason == "concurrent_subtask"
        release.set()
        await running
        assert len(agent.feedback) == 1
        assert context.final_result()[0] is False
        await context.close()

    asyncio.run(scenario())


def test_same_checkpoint_is_single_use(monkeypatch):
    context, _, _ = setup_context(
        monkeypatch,
        [
            [event("done", success=False, stop_reason="step_budget_reached")],
            [event("done", success=False, stop_reason="step_budget_reached")],
        ],
    )

    async def scenario():
        first = await context.execute("phone", "goal", ["title"], [])
        second = await context.execute(
            "phone", "goal", ["title"], [], checkpoint=first.checkpoint
        )
        assert second.checkpoint != first.checkpoint
        repeated = await context.execute(
            "phone", "goal", ["title"], [], checkpoint=first.checkpoint
        )
        assert repeated.stop_reason == "invalid_checkpoint"
        await context.close()

    asyncio.run(scenario())


def test_config_change_invalidates_resume(monkeypatch):
    context, agent, _ = setup_context(
        monkeypatch, [[event("done", success=False, stop_reason="step_budget_reached")]]
    )

    async def scenario():
        first = await context.execute("phone", "goal", ["title"], [])
        monkeypatch.setattr(
            SubtaskRunContext, "fingerprint", staticmethod(lambda: "new")
        )
        result = await context.execute(
            "phone", "goal", ["title"], [], checkpoint=first.checkpoint
        )
        assert result.stop_reason == "configuration_changed"
        assert len(agent.feedback) == 1
        await context.close()

    asyncio.run(scenario())


def test_cleanup_survives_cancel_and_can_be_awaited_again(monkeypatch):
    context, agent, manager = setup_context(
        monkeypatch, [[event("done", success=False, stop_reason="step_budget_reached")]]
    )

    async def scenario():
        await context.execute("phone", "goal", ["title"], [])
        draining = asyncio.Event()
        settled = asyncio.Event()

        async def drain(**kwargs):
            draining.set()
            await settled.wait()
            return True

        agent.device.drain = drain
        caller = asyncio.create_task(context.close())
        await draining.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        manager.dispose_device_lease.assert_not_awaited()
        settled.set()
        await context.close()
        manager.dispose_device_lease.assert_awaited_once()
        assert not context._leased

    asyncio.run(scenario())


def test_later_rejection_cannot_hide_unknown_result(monkeypatch):
    context, agent, _ = setup_context(monkeypatch, [[event("done", success=True)]])
    agent.device.outcome_unknown = True

    async def scenario():
        first = await context.execute("phone", "goal", ["title"], [])
        assert first.status == "unknown"
        rejected = await context.execute("phone", "another goal", ["title"], [])
        assert rejected.stop_reason == "task_not_active"
        assert context.final_result() == (False, "unknown", "device_outcome_unknown")
        await context.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("drained,outcome_unknown", [(False, False), (True, True)])
def test_cancel_cleanup_reports_unconfirmed_device_effect(
    monkeypatch, drained, outcome_unknown
):
    context, agent, manager = setup_context(
        monkeypatch, [[event("done", success=False, stop_reason="step_budget_reached")]]
    )

    async def scenario():
        await context.execute("phone", "goal", ["title"], [])
        await context.cancel()
        agent.device.drain.return_value = drained
        agent.device.outcome_unknown = outcome_unknown
        await context.close()
        assert context.final_result() == (False, "unknown", "device_outcome_unknown")
        manager.quarantine_device.assert_awaited()
        manager.dispose_device_lease.assert_awaited_once()

    asyncio.run(scenario())
