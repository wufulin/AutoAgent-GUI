"""Skill state transitions exercised through the real V1 lease/checkpoint context."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from AutoGLM_GUI import execution, subtasks
from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.execution import execute_skill, resolve_route
from AutoGLM_GUI.execution_models import ExecutionRequest
from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
from AutoGLM_GUI.skills import SkillDefinition, SkillError, SkillManager, SkillStep
from AutoGLM_GUI.subtasks import SubtaskRunContext, Verification


def event(kind, **data):
    return {"type": kind, "data": data}


class ScriptedAgent:
    def __init__(self, batches, timeline):
        self.batches = iter(batches)
        self.timeline = timeline
        self.step_count = 0
        self.context = []
        self.feedback = []
        self.resets = 0
        self.model_config = ModelConfig(base_url="https://example.test/v1")
        self.agent_config = AgentConfig()
        self.device = SimpleNamespace(
            outcome_unknown=False, drain=AsyncMock(return_value=True)
        )

    def reset(self):
        self.resets += 1
        self.step_count = 0
        self.context.clear()

    async def cancel(self):
        pass

    async def stream(self, task, *, continue_with=None, step_budget=None):
        self.timeline.append(("execute", task))
        self.feedback.append(continue_with)
        self.context.append(task if continue_with is None else continue_with)
        for value in next(self.batches):
            if value["type"] == "step":
                self.step_count += 1
            yield value


def setup(monkeypatch, batches, verdicts=None):
    timeline = []
    agent = ScriptedAgent(batches, timeline)
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
    verdict_iter = iter(verdicts) if verdicts is not None else None

    async def verify(device, model, goal, criteria):
        timeline.append(("verify", goal, criteria))
        verdict = next(verdict_iter) if verdict_iter is not None else "met"
        return Verification(
            criteria=[
                {"criterion": criterion, "verdict": verdict, "reason": "fresh screen"}
                for criterion in criteria
            ],
            facts=["visible"],
            observed_at="2026-10-21T00:00:00Z",
            screenshot_sha256="new-frame",
        )

    monkeypatch.setattr(subtasks, "verify_completion", verify)
    return (
        SubtaskRunContext("task", "session", "phone", 100, "config"),
        agent,
        manager,
        timeline,
    )


def skill(*, preconditions=None, steps=None, max_resumes=1):
    return SkillDefinition(
        id="navigation",
        name="导航",
        goal="overall goal",
        preconditions=preconditions or [],
        constraints=["global constraint"],
        steps=steps
        or [
            SkillStep(
                id="first",
                goal="first goal",
                success_criteria=["first condition"],
                constraints=["step constraint"],
                effect="navigation",
                max_resumes=max_resumes,
            )
        ],
        success_criteria=["overall condition"],
    ).compile({})


def test_skill_checks_preconditions_each_stage_and_final_goal(monkeypatch):
    context, agent, manager, timeline = setup(
        monkeypatch,
        [
            [event("step"), event("done", success=True)],
            [event("step"), event("done", success=True)],
        ],
    )
    compiled = skill(
        preconditions=["ready"],
        steps=[
            SkillStep(
                id="first",
                goal="first goal",
                success_criteria=["first condition"],
                effect="navigation",
            ),
            SkillStep(
                id="second",
                goal="second goal",
                success_criteria=["second condition"],
                effect="read",
            ),
        ],
    )

    async def scenario():
        result = await execute_skill(context, "phone", compiled, ["task constraint"])
        assert result.success
        assert context.final_result() == (True, "completed", "goal_verified")
        assert context.max_steps == 100
        await context.close()

    asyncio.run(scenario())
    assert [item[0] for item in timeline] == [
        "verify",
        "execute",
        "verify",
        "execute",
        "verify",
        "verify",
    ]
    assert [item[2] for item in timeline if item[0] == "verify"] == [
        ["ready"],
        ["first condition"],
        ["second condition"],
        ["overall condition"],
    ]
    assert all(
        "global constraint" in item[1] and "task constraint" in item[1]
        for item in timeline
        if item[0] == "execute"
    )
    assert agent.resets == 2
    manager.acquire_device_lease.assert_awaited_once()
    manager.dispose_device_lease.assert_awaited_once()


def test_failed_precondition_stops_before_any_action(monkeypatch):
    context, agent, manager, timeline = setup(monkeypatch, [], ["unmet"])

    async def scenario():
        result = await execute_skill(context, "phone", skill(preconditions=["ready"]))
        assert result.status == "blocked"
        assert result.stop_reason == "precondition_not_verified"
        await context.close()

    asyncio.run(scenario())
    assert agent.feedback == []
    assert [item[0] for item in timeline] == ["verify"]
    manager.dispose_device_lease.assert_awaited_once()


def test_final_skill_criteria_are_required_after_successful_step(monkeypatch):
    context, agent, _, _ = setup(
        monkeypatch, [[event("step"), event("done", success=True)]], ["met", "unmet"]
    )

    async def scenario():
        result = await execute_skill(context, "phone", skill())
        assert not result.success
        assert result.stop_reason == "skill_final_not_verified"
        assert not context.final_result()[0]
        await context.close()

    asyncio.run(scenario())
    assert len(agent.feedback) == 1


def test_partial_resume_keeps_same_executor_state_and_checkpoint(monkeypatch):
    context, agent, manager, _ = setup(
        monkeypatch,
        [
            [
                event("step"),
                event("done", success=False, stop_reason="step_budget_reached"),
            ],
            [event("step"), event("done", success=True)],
        ],
        ["unmet", "met", "met"],
    )

    async def scenario():
        result = await execute_skill(context, "phone", skill())
        assert result.success
        assert context.total_steps == 2
        assert context.continuations == 1
        assert agent.resets == 1
        assert len(agent.context) == 2
        assert agent.feedback[0] is None and agent.feedback[1]
        manager.acquire_device_lease.assert_awaited_once()
        assert len(context.checkpoints) == 0
        await context.close()

    asyncio.run(scenario())


def test_no_progress_stops_even_when_resume_budget_remains(monkeypatch):
    partial = [
        event("step"),
        event("done", success=False, stop_reason="step_budget_reached"),
    ]
    context, agent, _, _ = setup(
        monkeypatch, [partial, partial, partial], ["unmet", "unmet"]
    )

    async def scenario():
        result = await execute_skill(context, "phone", skill(max_resumes=3))
        assert result.stop_reason == "no_verified_progress"
        assert context.continuations == 1
        assert len(agent.feedback) == 2
        await context.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "terminal,expected",
    [
        ([], "unknown"),
        ([event("takeover", success=True)], "needs_user"),
        ([event("error", message="model failed")], "failed"),
    ],
)
def test_unknown_takeover_and_failure_never_retry(monkeypatch, terminal, expected):
    context, agent, _, timeline = setup(monkeypatch, [[event("step"), *terminal]])

    async def scenario():
        result = await execute_skill(context, "phone", skill(max_resumes=3))
        assert result.status == expected
        assert context.continuations == 0
        assert len(agent.feedback) == 1
        assert all(item[0] != "verify" for item in timeline)
        await context.close()

    asyncio.run(scenario())


def test_write_step_never_auto_resumes(monkeypatch):
    context, agent, _, _ = setup(
        monkeypatch,
        [
            [
                event("step"),
                event("done", success=False, stop_reason="step_budget_reached"),
            ]
        ],
    )
    compiled = skill(
        steps=[
            SkillStep(
                id="submit",
                goal="submit",
                success_criteria=["receipt visible"],
                effect="write",
            )
        ]
    )

    async def scenario():
        result = await execute_skill(context, "phone", compiled)
        assert result.stop_reason == "skill_resume_budget_reached"
        assert context.continuations == 0
        assert len(agent.feedback) == 1
        await context.close()

    asyncio.run(scenario())


def test_invalid_route_version_or_parameters_never_start_executor(
    tmp_path: Path, monkeypatch
):
    manager = SkillManager(tmp_path / "skills.json")
    monkeypatch.setattr(execution, "skill_manager", manager)
    acquire = AsyncMock()
    monkeypatch.setattr(
        PhoneAgentManager,
        "get_instance",
        lambda: SimpleNamespace(acquire_device_lease=acquire),
    )
    for data in [
        {"route": "skill", "skill_id": "open_wechat", "skill_version": 999},
        {
            "route": "skill",
            "skill_id": "open_wechat_chat",
            "skill_version": 1,
            "parameters": {},
        },
    ]:
        with pytest.raises(SkillError):
            resolve_route("ignored", ExecutionRequest.model_validate(data))
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate({"route": "missing"})
    assert resolve_route("打开微信", ExecutionRequest())[0:2] == (
        "skill",
        "exact_command",
    )
    assert resolve_route("打开微信然后发消息", ExecutionRequest())[0] == "planner"
    acquire.assert_not_awaited()


def test_builtin_open_wechat_can_start_from_launcher_without_app_precheck(
    tmp_path: Path, monkeypatch
):
    context, agent, _, timeline = setup(
        monkeypatch, [[event("step"), event("done", success=True)]]
    )
    compiled = SkillManager(tmp_path / "skills.json").compile_skill(
        "open_wechat", 1, {}
    )

    async def scenario():
        result = await execute_skill(context, "phone", compiled)
        assert result.success
        await context.close()

    asyncio.run(scenario())
    # Launch first: the launcher cannot prove WeChat is installed and logged in.
    assert [entry[0] for entry in timeline] == ["execute", "verify", "verify"]
    assert len(agent.feedback) == 1
