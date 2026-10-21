"""Whole-run routing, cancellation, deadlines and queue persistence boundaries."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from AutoGLM_GUI import execution
from AutoGLM_GUI.execution import ExecutionRun
from AutoGLM_GUI.execution_models import ExecutionRequest
from AutoGLM_GUI.task_store import TaskStore
from tests.test_skill_execution import event, setup


def run_for(request=None):
    return ExecutionRun(
        "task",
        "session",
        "goal",
        "phone",
        request or ExecutionRequest(route="direct", success_criteria=["visible title"]),
    )


async def collect(run):
    return [event async for event in run.stream_events()]


def test_direct_uses_verified_executor_without_planner(monkeypatch):
    _, agent, manager, _ = setup(
        monkeypatch, [[event("step"), event("done", success=True)]]
    )
    import AutoGLM_GUI.layered_agent_service as planner

    monkeypatch.setattr(
        planner, "start_run", lambda **_: pytest.fail("Planner must not be called")
    )
    run = run_for()
    events = asyncio.run(collect(run))
    assert events[0]["payload"]["route"] == "direct"
    assert events[-1]["payload"]["success"] is True
    summary = next(e["payload"] for e in events if e["type"] == "execution_summary")
    assert summary["steps"] == 1 and summary["planner_calls"] == 0
    assert agent.resets == 1 and run.finished
    manager.dispose_device_lease.assert_awaited_once()


def test_cancel_before_stream_never_acquires_device(monkeypatch):
    _, _, manager, _ = setup(monkeypatch, [])
    run = run_for()

    async def scenario():
        await run.cancel()
        return await asyncio.wait_for(collect(run), 1)

    events = asyncio.run(scenario())
    assert events[-1]["type"] == "cancelled"
    manager.acquire_device_lease.assert_not_awaited()


@pytest.mark.parametrize("unknown", [False, True])
def test_cancel_during_device_execution_settles_before_terminal(monkeypatch, unknown):
    _, agent, manager, _ = setup(monkeypatch, [])
    entered = asyncio.Event()

    async def blocking(*args, **kwargs):
        entered.set()
        agent.step_count += 1
        yield event("step", step=1)
        await asyncio.Event().wait()

    agent.stream = blocking
    agent.device.drain = AsyncMock(return_value=not unknown)
    run = run_for()

    async def scenario():
        consumer = asyncio.create_task(collect(run))
        await entered.wait()
        await run.cancel()
        return await asyncio.wait_for(consumer, 1)

    events = asyncio.run(scenario())
    summary = next(e["payload"] for e in events if e["type"] == "execution_summary")
    assert summary["status"] == ("unknown" if unknown else "cancelled")
    assert summary["steps"] == 1
    agent.device.drain.assert_awaited_once()
    manager.dispose_device_lease.assert_awaited_once()
    if unknown:
        manager.quarantine_device.assert_awaited()
        assert events[-1]["payload"]["result_status"] == "unknown"


def test_task_deadline_is_not_reported_as_user_cancel(monkeypatch):
    _, agent, _, _ = setup(monkeypatch, [])

    async def blocking(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    agent.stream = blocking
    # Public validation has a 10 s minimum; shrink only the internal fixture clock.
    request = ExecutionRequest.model_construct(
        route="direct", success_criteria=["title"], max_seconds=0.01
    )
    events = asyncio.run(asyncio.wait_for(collect(run_for(request)), 1))
    terminal = events[-1]["payload"]
    assert terminal["result_status"] == "blocked"
    assert terminal["stop_reason"] == "task_time_budget_reached"


def test_producer_initialization_failure_cannot_hang_stream(monkeypatch):
    monkeypatch.setattr(
        execution.config_manager,
        "get_effective_config",
        lambda: (_ for _ in ()).throw(ValueError("invalid config")),
    )
    run = run_for()
    with pytest.raises(ValueError, match="invalid config"):
        asyncio.run(asyncio.wait_for(collect(run), 1))
    assert run.finished


def test_cleanup_failure_overrides_verified_success(monkeypatch):
    setup(monkeypatch, [[event("done", success=True)]])
    monkeypatch.setattr(
        execution.SubtaskRunContext,
        "close",
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    events = asyncio.run(asyncio.wait_for(collect(run_for()), 1))
    assert events[-1]["payload"]["result_status"] == "unknown"
    assert events[-1]["payload"]["success"] is False


def test_execution_request_is_committed_with_task_before_queue_can_claim(
    tmp_path, monkeypatch
):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    observer = sqlite3.connect(path)
    original_append = store._append_event_locked
    observed = []

    def append(**kwargs):
        if kwargs["event_type"] == "user_message":
            # A separate queue connection must see neither a task nor its route
            # until both become durable in the same transaction.
            observed.append(
                observer.execute("SELECT count(*) FROM task_runs").fetchone()[0]
            )
        return original_append(**kwargs)

    monkeypatch.setattr(store, "_append_event_locked", append)
    payload = {
        "message": "open",
        "execution": {
            "route": "skill",
            "skill_id": "open_wechat",
            "skill_version": 1,
            "parameters": {},
        },
    }
    task = store.create_task_run(
        source="chat",
        executor_key="layered_chat",
        device_id="phone",
        device_serial="phone",
        input_text="open",
        user_message=payload,
    )
    assert observed == [0]
    assert store.list_task_events(task["id"])[1]["payload"] == payload
    assert observer.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
    observer.close()
    store.close()


def test_failed_request_event_rolls_back_task_and_cannot_run_later(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "tasks.db")
    original = store._append_event_locked

    def fail_request(**kwargs):
        if kwargs["event_type"] == "user_message":
            raise RuntimeError("event write failed")
        return original(**kwargs)

    monkeypatch.setattr(store, "_append_event_locked", fail_request)
    with pytest.raises(RuntimeError, match="event write failed"):
        store.create_task_run(
            task_id="failed",
            source="chat",
            executor_key="layered_chat",
            device_id="phone",
            device_serial="phone",
            input_text="open",
            user_message={"execution": {"route": "skill"}},
        )
    assert store.get_task("failed") is None
    assert store.list_task_events("failed") == []
    monkeypatch.setattr(store, "_append_event_locked", original)
    store.create_session(
        kind="chat", mode="layered", device_id="phone", device_serial="phone"
    )
    assert store.claim_next_queued_task("phone") is None
    store.close()


def test_cancel_during_task_setup_prevents_execution(tmp_path, monkeypatch):
    from AutoGLM_GUI.task_manager import TaskManager

    async def scenario():
        store = TaskStore(tmp_path / "tasks.db")
        manager = TaskManager(store)
        setup_started, continue_setup = asyncio.Event(), asyncio.Event()
        actions = []

        async def replay(**kwargs):
            setup_started.set()
            await continue_setup.wait()

        class Run:
            context = None
            final_output = ""
            cancelled = False

            async def cancel(self):
                self.cancelled = True

            async def stream_events(self):
                if not self.cancelled:
                    actions.append("device write")
                yield {"type": "cancelled", "payload": {"message": "cancelled"}}

        monkeypatch.setattr(manager, "_write_replay_task_start", replay)
        monkeypatch.setattr(execution, "start_run", lambda **_: Run())
        task = store.create_task_run(
            source="chat",
            executor_key="layered_chat",
            device_id="phone",
            device_serial="phone",
            input_text="goal",
            status="RUNNING",
        )
        worker = asyncio.create_task(
            manager._execute_layered_task(
                task,
                session_id="s",
                clear_session_after_run=False,
                metrics_source="layered",
            )
        )
        await setup_started.wait()
        await manager.cancel_task(task["id"])
        continue_setup.set()
        await worker
        assert actions == []
        assert store.get_task(task["id"])["status"] == "CANCELLED"
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("parameters", ["[]", "null", "1", '"name"'])
def test_planner_malformed_skill_parameters_latch_stop(monkeypatch, parameters):
    from agents import RunContextWrapper
    from AutoGLM_GUI.layered_agent_service import execute_skill as tool
    import json

    context, _, manager, _ = setup(monkeypatch, [])
    output = asyncio.run(
        tool.on_invoke_tool(
            RunContextWrapper(context=context),
            json.dumps(
                {
                    "device_id": "phone",
                    "skill_id": "open_wechat",
                    "skill_version": 1,
                    "parameters_json": parameters,
                    "constraints": [],
                }
            ),
        )
    )
    assert json.loads(output)["result"]["stop_reason"] == "invalid_skill"
    assert context.fatal_result is not None
    manager.acquire_device_lease.assert_not_awaited()


def test_planner_claim_cannot_skip_original_goal_acceptance(monkeypatch):
    from AutoGLM_GUI.layered_agent_service import LayeredTaskRun

    context, _, _, timeline = setup(
        monkeypatch, [[event("done", success=True)]], verdicts=["met", "unmet"]
    )

    class PlannerResult:
        final_output = "Everything finished"

        async def stream_events(self):
            await context.execute("phone", "only first goal", ["first condition"], [])
            if False:
                yield

    run = LayeredTaskRun(
        "task",
        "session",
        PlannerResult(),
        device_id="phone",
        context=context,
        overall_goal="both goals",
        overall_criteria=["second goal also finished"],
    )
    events = asyncio.run(collect(run))
    assert events[-1]["payload"]["result_status"] == "blocked"
    assert events[-1]["payload"]["stop_reason"] == "task_final_not_verified"
    assert timeline[-1] == ("verify", "both goals", ["second goal also finished"])


def test_skill_deadline_retains_budget_reason_after_cancelled_await(monkeypatch):
    from dataclasses import replace
    from tests.test_skill_execution import skill

    context, agent, _, _ = setup(monkeypatch, [])

    async def blocking(*args, **kwargs):
        await asyncio.Event().wait()
        yield

    agent.stream = blocking
    compiled = replace(skill(), max_seconds=0.01)

    async def scenario():
        await execution.execute_skill(context, "phone", compiled)
        await context.close()
        return context.final_result()

    assert asyncio.run(asyncio.wait_for(scenario(), 1)) == (
        False,
        "blocked",
        "skill_time_budget_reached",
    )


def test_explicit_skill_honors_additional_task_criteria_and_constraints(monkeypatch):
    from tests.test_skill_execution import skill

    _, agent, _, timeline = setup(
        monkeypatch,
        [[event("done", success=True)]],
        verdicts=[{"extra user condition": "unmet"}],
    )
    compiled = skill()
    monkeypatch.setattr(
        execution,
        "resolve_route",
        lambda message, request: ("skill", "explicit_skill", compiled),
    )
    request = ExecutionRequest(
        route="skill",
        skill_id="navigation",
        skill_version=1,
        success_criteria=["extra user condition"],
        constraints=["do not submit"],
    )
    events = asyncio.run(collect(run_for(request)))
    assert events[-1]["payload"]["result_status"] == "blocked"
    assert timeline[-1][2] == [
        "first condition",
        "overall condition",
        "extra user condition",
    ]
    assert len([item for item in timeline if item[0] == "verify"]) == 1
    assert "do not submit" in agent.context[0]
