"""Skill state machine and conservative routing over the V1 execution kernel."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from AutoGLM_GUI.config_manager import config_manager
from AutoGLM_GUI.execution_metrics import execution_metrics
from AutoGLM_GUI.execution_models import ExecutionRequest
from AutoGLM_GUI.skills import CompiledSkill, skill_manager
from AutoGLM_GUI.subtasks import SubtaskResult, SubtaskRunContext


def resolve_route(
    message: str, request: ExecutionRequest
) -> tuple[str, str, CompiledSkill | None]:
    """No semantic guessing or LLM call is needed for exact known skill commands."""
    if request.route == "skill":
        assert request.skill_id is not None and request.skill_version is not None
        return (
            "skill",
            "explicit_skill",
            skill_manager.compile_skill(
                request.skill_id, request.skill_version, request.parameters
            ),
        )
    if request.route == "auto" and message.strip() in {"打开微信", "打开微信。"}:
        skill = skill_manager.get_skill("open_wechat")
        if skill and skill.enabled:
            return (
                "skill",
                "exact_command",
                skill_manager.compile_skill(skill.id, skill.version, {}),
            )
    if request.route == "direct":
        return "direct", "explicit_direct", None
    return (
        "planner",
        "explicit_planner" if request.route == "planner" else "requires_planning",
        None,
    )


def emit(context: SubtaskRunContext, kind: str, payload: dict[str, Any]) -> None:
    context.progress.put_nowait({"type": kind, "payload": payload})


def emit_result(context: SubtaskRunContext, result: SubtaskResult) -> None:
    emit(
        context,
        "tool_result",
        {
            "tool_name": "execute_subtask",
            "result": result.model_dump(),
            "steps": result.steps,
            "success": result.success,
        },
    )


async def execute_skill(
    context: SubtaskRunContext,
    device_id: str,
    skill: CompiledSkill,
    constraints: list[str] | None = None,
    *,
    task_goal: str | None = None,
    task_criteria: list[str] | None = None,
) -> SubtaskResult:
    """Verify every step; include all terminal conditions in the last observation.

    This does not cache an earlier verdict or infer semantic equivalence. The
    last independent verification receives every original condition, with its
    goal context. Planner-wide acceptance remains a separate operation because
    a skill may be only one part of a larger task.
    """
    original_budget = context.max_steps
    skill_limit = context.total_steps + skill.max_steps
    context.max_steps = (
        min(original_budget, skill_limit)
        if original_budget is not None
        else skill_limit
    )
    try:
        async with asyncio.timeout(skill.max_seconds):
            if skill.preconditions:
                result = await context.check(
                    device_id,
                    skill.goal,
                    list(skill.preconditions),
                    phase="precondition",
                )
                emit_result(context, result)
                if not result.success:
                    return result
            result = None
            for index, step in enumerate(skill.steps):
                last_step = index == len(skill.steps) - 1
                criteria = list(step.success_criteria)
                goal = step.goal
                if last_step:
                    criteria.extend(skill.success_criteria)
                    criteria.extend(task_criteria or [])
                    # Preserve order and even repeated text: equal wording can
                    # belong to different goals and is not proof of equivalence.
                    goal = json.dumps(
                        {
                            "step_goal": step.goal,
                            "step_conditions": list(step.success_criteria),
                            "skill_goal": skill.goal,
                            "skill_final_conditions": list(skill.success_criteria),
                            "task_goal": task_goal,
                            "task_final_conditions": task_criteria or [],
                            "instruction": (
                                "只执行本步骤，在最后动作后申请独立验收。完成条件依次"
                                "对应上述步骤、技能及任务目标；整体条件不允许扩大操作范围。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                emit(
                    context,
                    "skill_progress",
                    {
                        "skill_id": skill.id,
                        "skill_version": skill.version,
                        "skill_name": skill.name,
                        "step_id": step.id,
                        "step_index": index + 1,
                        "step_count": len(skill.steps),
                        "phase": "executing",
                    },
                )
                checkpoint = None
                previous_met = -1
                for attempt in range(step.max_resumes + 1):
                    bound_constraints = (
                        list(skill.constraints)
                        + list(step.constraints)
                        + (constraints or [])
                    )
                    if step.effect in {"navigation", "read"}:
                        bound_constraints.append(
                            "本步骤只允许页面导航和读取，不得发送、提交、支付、删除或修改业务数据。"
                        )
                    emit(
                        context,
                        "tool_call",
                        {
                            "tool_name": "execute_subtask",
                            "tool_args": {
                                "device_id": device_id,
                                "goal": goal,
                                "success_criteria": criteria,
                                "skill_id": skill.id,
                                "step_id": step.id,
                            },
                        },
                    )
                    result = await context.execute(
                        device_id,
                        goal,
                        criteria,
                        bound_constraints,
                        step.max_steps,
                        checkpoint,
                    )
                    emit_result(context, result)
                    if result.success:
                        break
                    if last_step and result.verification:
                        verification = result.verification
                        checked = verification.criteria
                        if (
                            not verification.error
                            and [item.criterion for item in checked] == criteria
                            and all(
                                item.verdict == "met"
                                for item in checked[: len(step.success_criteria)]
                            )
                        ):
                            # A finished last step must not be replayed merely
                            # because a broader terminal condition failed.
                            skill_end = len(step.success_criteria) + len(
                                skill.success_criteria
                            )
                            phase = (
                                "skill_final"
                                if any(
                                    item.verdict != "met"
                                    for item in checked[
                                        len(step.success_criteria) : skill_end
                                    ]
                                )
                                else "task_final"
                            )
                            return context.stop(
                                skill.goal,
                                f"{phase}_not_verified",
                                "最后步骤已完成，但整体完成条件未通过独立验收，已停止。",
                            )
                    if result.status != "partial" or not result.checkpoint:
                        return result
                    if step.effect == "write" or attempt == step.max_resumes:
                        return context.stop(
                            skill.goal,
                            "skill_resume_budget_reached",
                            "技能步骤未完成，已达到本步骤的续跑上限。",
                        )
                    # Only fresh criterion progress can grant additional segments.
                    # A changing screenshot is deliberately not treated as progress.
                    observed = result.verification or await context.observe(
                        device_id, goal, criteria
                    )
                    if observed.error or any(
                        c.verdict == "unknown" for c in observed.criteria
                    ):
                        return context.stop(
                            skill.goal,
                            "progress_unverifiable",
                            "无法验证当前进度，已停止自动续跑。",
                        )
                    met = sum(c.verdict == "met" for c in observed.criteria)
                    if result.steps == 0 or (attempt > 0 and met <= previous_met):
                        return context.stop(
                            skill.goal,
                            "no_verified_progress",
                            "没有新的已验证进度，已停止自动续跑。",
                        )
                    previous_met = met
                    checkpoint = result.checkpoint
                    emit(
                        context,
                        "execution_continuation",
                        {
                            "skill_id": skill.id,
                            "step_id": step.id,
                            "attempt": attempt + 1,
                            "met_criteria": met,
                            "reason": "bounded_initial_continuation"
                            if attempt == 0
                            else "verified_progress",
                        },
                    )
                else:
                    return context.stop(
                        skill.goal, "skill_incomplete", "技能步骤未完成。"
                    )
            if result is None:
                return context.stop(
                    skill.goal, "skill_incomplete", "技能没有可执行步骤。"
                )
            emit(
                context,
                "skill_progress",
                {
                    "skill_id": skill.id,
                    "skill_version": skill.version,
                    "phase": "skill_final",
                    "verification": "combined_with_last_step",
                    "subtask_id": result.subtask_id,
                },
            )
            return result
    except TimeoutError:
        context.deadline_reason = "skill_time_budget_reached"
        return context.stop(
            skill.goal,
            "skill_time_budget_reached",
            "技能耗时预算已用完，已停止执行并确认设备状态。",
        )
    finally:
        context.max_steps = original_budget


@dataclass
class ExecutionRun:
    task_id: str
    session_id: str
    message: str
    device_id: str
    request: ExecutionRequest
    final_output: str = ""
    success: bool = False
    finished: bool = False
    cancelled: bool = False
    context: SubtaskRunContext | None = None
    _worker: asyncio.Task[None] | None = field(default=None, init=False)
    _planner: Any = field(default=None, init=False)
    _events: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=asyncio.Queue, init=False
    )

    async def cancel(self) -> None:
        self.cancelled = True
        if self._planner is not None:
            await self._planner.cancel()
        elif self.context is not None:
            await self.context.cancel()
        if (
            self.context is not None
            and self._worker is not None
            and not self._worker.done()
        ):
            self._worker.cancel()

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        if self._worker is not None:
            raise RuntimeError("Execution stream can only be consumed once")
        self._worker = asyncio.create_task(self._produce())
        # Initialization failures must also wake the stream consumer.
        self._worker.add_done_callback(lambda _: self._events.put_nowait(None))
        try:
            while (event := await self._events.get()) is not None:
                yield event
        finally:
            if not self._worker.done():
                await self.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker
            finally:
                self.finished = True

    async def _forward_progress(self) -> None:
        assert self.context is not None
        while True:
            await self._events.put(await self.context.progress.get())

    async def _produce(self) -> None:
        route = self.request.route
        status, reason = "blocked", "execution_not_started"
        continuations = 0
        time_budget_reason: str | None = None
        progress_task = None
        cfg = config_manager.get_effective_config()
        max_steps = (
            min(self.request.max_steps, cfg.default_max_steps)
            if cfg.default_max_steps is not None
            else self.request.max_steps
        )
        self.context = SubtaskRunContext(
            self.task_id,
            self.session_id,
            self.device_id,
            max_steps,
            SubtaskRunContext.fingerprint(),
            task_constraints=self.request.constraints,
        )
        with execution_metrics(route) as metrics:
            try:
                route, route_reason, skill = resolve_route(self.message, self.request)
                metrics.route = route
                await self._events.put(
                    {
                        "type": "execution_route",
                        "payload": {
                            "route": route,
                            "reason": route_reason,
                            "skill_id": skill.id if skill else None,
                            "skill_version": skill.version if skill else None,
                            "skill_name": skill.name if skill else None,
                        },
                    }
                )
                if self.cancelled:
                    raise asyncio.CancelledError()
                async with asyncio.timeout(self.request.max_seconds):
                    if route == "planner":
                        from AutoGLM_GUI.layered_agent_service import (
                            start_run as start_planner,
                        )

                        self._planner = start_planner(
                            task_id=self.task_id,
                            session_id=self.session_id,
                            message=self.message,
                            device_id=self.device_id,
                            execution_context=self.context,
                        )
                        self._planner.overall_goal = self.message
                        self._planner.overall_criteria = (
                            self.request.success_criteria or [self.message]
                        )
                        async for event in self._planner.stream_events():
                            if event["type"] == "done":
                                self.final_output = event["payload"].get("content", "")
                            elif event["type"] == "error":
                                self.context.stop(
                                    self.message,
                                    "planner_error",
                                    "规划模型未能完成任务。",
                                )
                            elif event["type"] not in {"cancelled"}:
                                await self._events.put(event)
                    else:
                        progress_task = asyncio.create_task(self._forward_progress())
                        if skill is not None:
                            result = await execute_skill(
                                self.context,
                                self.device_id,
                                skill,
                                self.request.constraints,
                                task_goal=self.message,
                                task_criteria=self.request.success_criteria,
                            )
                        else:
                            emit(
                                self.context,
                                "tool_call",
                                {
                                    "tool_name": "execute_subtask",
                                    "tool_args": {"goal": self.message},
                                },
                            )
                            result = await self.context.execute(
                                self.device_id,
                                self.message,
                                self.request.success_criteria,
                                self.request.constraints,
                                min(20, max_steps),
                            )
                            emit_result(self.context, result)
                        self.final_output = result.message
            except asyncio.CancelledError:
                self.cancelled = True
                await self.context.cancel()
            except TimeoutError:
                time_budget_reason = "task_time_budget_reached"
                self.context.deadline_reason = time_budget_reason
                self.context.stop(
                    self.message,
                    "task_time_budget_reached",
                    "任务耗时预算已用完，已停止执行。",
                )
            except (ValueError, KeyError):
                self.context.stop(
                    self.message,
                    "invalid_execution_request",
                    "技能、版本或参数无效，请重新选择技能。",
                )
            except Exception:
                self.context.stop(
                    self.message,
                    "execution_unavailable",
                    "执行服务不可用，请检查设备和配置。",
                )
            finally:
                if progress_task is not None:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                try:
                    await self.context.close()
                except Exception:
                    # Failed cleanup is not proof that device effects settled.
                    self.context._mark_unknown(self.device_id)
                finally:
                    while not self.context.progress.empty():
                        await self._events.put(self.context.progress.get_nowait())
                    self.success, status, reason = self.context.final_result()
                    if self.cancelled and status != "unknown":
                        self.success, status, reason = (
                            False,
                            "cancelled",
                            "user_stopped",
                        )
                    elif time_budget_reason and status != "unknown":
                        self.success, status, reason = (
                            False,
                            "blocked",
                            time_budget_reason,
                        )
                    elif (
                        self.context.fatal_result
                        and self.context.fatal_result.stop_reason
                        == "skill_time_budget_reached"
                        and status != "unknown"
                    ):
                        self.success, status, reason = (
                            False,
                            "blocked",
                            "skill_time_budget_reached",
                        )
                    if not self.success:
                        latest = self.context.fatal_result or next(
                            (r for r in self.context.results.values() if not r.success),
                            None,
                        )
                        self.final_output = latest.message if latest else "任务未完成。"
                    # Count emitted continuation events, not attempted/planned resumes.
                    continuations = getattr(self.context, "continuations", 0)
                    await self._events.put(
                        {
                            "type": "execution_summary",
                            "payload": metrics.summary(
                                status,
                                reason,
                                steps=self.context.total_steps,
                                subtasks=len(self.context.results),
                                continuations=continuations,
                            ),
                        }
                    )
                    await self._events.put(
                        {
                            "type": "cancelled" if status == "cancelled" else "done",
                            "payload": {
                                "content": self.final_output,
                                "success": self.success,
                                "message": self.final_output,
                                "result_status": status,
                                "stop_reason": reason,
                            },
                        }
                    )


def start_run(
    *,
    task_id: str,
    session_id: str,
    message: str,
    device_id: str,
    execution: dict[str, Any] | None = None,
) -> ExecutionRun:
    return ExecutionRun(
        task_id,
        session_id,
        message,
        device_id,
        ExecutionRequest.model_validate(execution or {}),
    )
