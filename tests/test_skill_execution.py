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
                {
                    "criterion": criterion,
                    "verdict": verdict.get(criterion, "met")
                    if isinstance(verdict, dict)
                    else verdict,
                    "reason": "fresh screen",
                }
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


@pytest.mark.parametrize("city, shop_name", [("广州", "霸王茶姬"), ("深圳", "古茗")])
def test_continuous_dianping_keeps_one_executor_and_final_verification(
    monkeypatch, tmp_path, city, shop_name
):
    context, agent, manager, timeline = setup(
        monkeypatch,
        [[*[event("step") for _ in range(11)], event("done", success=True)]],
    )
    compiled = SkillManager(tmp_path / "skills.json").compile_skill(
        "dianping_search_shops", 2, {"city": city, "shop_name": shop_name}
    )

    async def scenario():
        result = await execute_skill(
            context,
            "phone",
            compiled,
            task_goal="只搜索商户",
            task_criteria=["额外的任务完成条件"],
        )
        assert result.success
        assert context.total_steps == 11
        assert context.final_result() == (True, "completed", "goal_verified")
        await context.close()

    asyncio.run(scenario())
    assert [item[0] for item in timeline] == ["execute", "verify"]
    assert agent.resets == 1
    assert manager.acquire_device_lease.await_count == 1
    assert timeline[-1][2] == [
        *compiled.steps[0].success_criteria,
        *compiled.success_criteria,
        "额外的任务完成条件",
    ]


@pytest.mark.parametrize("verdict", ["unmet", "unknown"])
def test_continuous_dianping_never_replays_or_succeeds_after_failed_final_check(
    monkeypatch, tmp_path, verdict
):
    context, agent, _, timeline = setup(
        monkeypatch,
        [[event("step"), event("done", success=True)]],
        verdicts=[verdict],
    )
    compiled = SkillManager(tmp_path / "skills.json").compile_skill(
        "dianping_search_shops", 2, {"city": "广州", "shop_name": "霸王茶姬"}
    )

    async def scenario():
        result = await execute_skill(context, "phone", compiled)
        assert not result.success
        assert context.final_result()[0] is False
        await context.close()

    asyncio.run(scenario())
    assert [item[0] for item in timeline] == ["execute", "verify"]
    assert agent.resets == 1
    assert context.continuations == 0


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
    ]
    assert [item[2] for item in timeline if item[0] == "verify"] == [
        ["ready"],
        ["first condition"],
        ["second condition", "overall condition"],
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
        monkeypatch,
        [[event("step"), event("done", success=True)]],
        [{"first condition": "met", "overall condition": "unmet"}],
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
    assert [entry[0] for entry in timeline] == ["execute", "verify"]
    assert len(agent.feedback) == 1


@pytest.mark.parametrize("verdict", ["unmet", "unknown"])
def test_combined_final_failure_never_replays_completed_last_step(monkeypatch, verdict):
    context, agent, _, timeline = setup(
        monkeypatch,
        [[event("step"), event("done", success=True)]],
        [{"overall condition": verdict}],
    )

    async def scenario():
        result = await execute_skill(context, "phone", skill(max_resumes=3))
        assert result.stop_reason == "skill_final_not_verified"
        assert not result.success
        assert context.continuations == 0
        await context.close()

    asyncio.run(scenario())
    assert len(agent.feedback) == 1
    assert len([item for item in timeline if item[0] == "verify"]) == 1


def test_task_conditions_are_checked_with_step_and_skill_in_one_fresh_call(monkeypatch):
    context, _, _, timeline = setup(
        monkeypatch,
        [[event("step"), event("done", success=True)]],
        [{"task condition": "unmet"}],
    )

    async def scenario():
        result = await execute_skill(
            context,
            "phone",
            skill(),
            task_goal="user goal",
            task_criteria=["task condition"],
        )
        assert result.stop_reason == "task_final_not_verified"
        assert not context.final_result()[0]
        await context.close()

    asyncio.run(scenario())
    checks = [item for item in timeline if item[0] == "verify"]
    assert len(checks) == 1
    assert checks[0][2] == ["first condition", "overall condition", "task condition"]
    assert "user goal" in checks[0][1] and "overall goal" in checks[0][1]


def test_identical_condition_text_is_preserved_for_each_goal_scope(monkeypatch):
    context, _, _, timeline = setup(
        monkeypatch, [[event("step"), event("done", success=True)]]
    )

    async def scenario():
        result = await execute_skill(
            context, "phone", skill(), task_criteria=["overall condition"]
        )
        assert result.success
        assert len(result.verification.criteria) == 3
        await context.close()

    asyncio.run(scenario())
    assert timeline[-1][2] == [
        "first condition",
        "overall condition",
        "overall condition",
    ]


@pytest.mark.parametrize(
    "version,builtin,skill_id,expected_profile",
    [
        (3, True, "dianping_search_shops", ("广州", "霸王茶姬")),
        (2, True, "dianping_search_shops", None),
        (3, False, "custom_dianping", None),
        (3, False, "dianping_search_shops", None),
        (3, True, "other_builtin", None),
    ],
)
def test_conditioned_profile_is_only_passed_for_exact_builtin_revision(
    monkeypatch, tmp_path, version, builtin, skill_id, expected_profile
):
    from dataclasses import replace
    from unittest.mock import Mock

    from AutoGLM_GUI.agents.gemini import async_agent

    context, agent, _, timeline = setup(
        monkeypatch, [[event("step"), event("done", success=True)]]
    )
    monkeypatch.setattr(async_agent, "AsyncGeminiAgent", ScriptedAgent)
    agent.bind_dianping = Mock()
    original_execute = context.execute
    context.execute = AsyncMock(wraps=original_execute)
    compiled = SkillManager(tmp_path / "skills.json").compile_skill(
        "dianping_search_shops", version, {"city": "广州", "shop_name": "霸王茶姬"}
    )
    compiled = replace(compiled, builtin=builtin, id=skill_id)

    async def scenario():
        result = await execute_skill(context, "phone", compiled)
        assert result.success
        options = context.execute.await_args.kwargs
        if expected_profile is None:
            assert "dianping_parameters" not in options
            agent.bind_dianping.assert_not_called()
        else:
            assert options["dianping_parameters"] == expected_profile
            agent.bind_dianping.assert_called_once_with(*expected_profile)
        assert agent.independent_verification is True
        assert [entry[0] for entry in timeline] == ["execute", "verify"]
        await context.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("verdict", ["met", "unmet"])
def test_visual_skill_segments_share_one_final_verifier(monkeypatch, tmp_path, verdict):
    """Budget segmentation must not reintroduce intermediate model acceptance."""
    from AutoGLM_GUI.agents import visual_dianping

    context, agent, _, timeline = setup(
        monkeypatch,
        [
            [
                event("step"),
                event("done", status="partial", stop_reason="step_budget_reached"),
            ],
            [event("step"), event("done", success=True)],
        ],
        verdicts=[verdict],
    )
    bound = []

    def wrap(original, executor_config, city, keyword):
        bound.append((original, executor_config, city, keyword))
        return original

    monkeypatch.setattr(visual_dianping, "VisualDianpingAgent", wrap)
    context.observe = AsyncMock(
        side_effect=AssertionError("unexpected intermediate verification")
    )
    compiled = SkillManager(tmp_path / "skills.json").compile_skill(
        "dianping_search_shops", 4, {"city": "广州", "shop_name": "霸王茶姬"}
    )

    async def run():
        result = await execute_skill(context, "phone", compiled)
        assert result.success is (verdict == "met")
        if verdict == "unmet":
            assert result.stop_reason == "visual_final_not_verified"
        assert context.continuations == 1
        context.observe.assert_not_called()
        assert len(bound) == 1 and bound[0][2:] == ("广州", "霸王茶姬")
        assert [row[0] for row in timeline] == ["execute", "execute", "verify"]
        await context.close()

    asyncio.run(run())


def test_untrusted_v4_copy_does_not_bind_visual_executor(monkeypatch, tmp_path):
    from dataclasses import replace
    from AutoGLM_GUI.agents import visual_dianping

    context, _, _, _ = setup(monkeypatch, [[event("done", success=True)]])
    monkeypatch.setattr(
        visual_dianping,
        "VisualDianpingAgent",
        lambda *args: pytest.fail("untrusted binding"),
    )
    compiled = SkillManager(tmp_path / "skills.json").compile_skill(
        "dianping_search_shops", 4, {"city": "广州", "shop_name": "霸王茶姬"}
    )

    async def run():
        assert (
            await execute_skill(context, "phone", replace(compiled, builtin=False))
        ).success
        await context.close()

    asyncio.run(run())
