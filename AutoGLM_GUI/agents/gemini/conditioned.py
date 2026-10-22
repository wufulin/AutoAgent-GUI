"""Conditioned skill decisions through the existing serialized action handler."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.dianping_controller import DianpingConditionError
from AutoGLM_GUI.phone_agent_trace import record_phone_agent_event
from AutoGLM_GUI.trace import trace_span

if TYPE_CHECKING:
    from .async_agent import AsyncGeminiAgent

# Includes pixel acquisition (about 0.9 s on the reference phone) and the
# foreground recheck. Each action still requires a newly captured observation.
OBSERVATION_MAX_AGE_SECONDS = 2.0


async def execute_conditioned_step(
    agent: AsyncGeminiAgent,
) -> AsyncGenerator[dict[str, Any], None]:
    agent._step_count += 1
    step = agent._step_count
    action: dict[str, Any] | None = None
    screenshot = None
    phase = "observe"
    reason = ""
    try:
        controller = agent.dianping_controller
        if controller is None:
            raise DianpingConditionError("条件化技能未绑定")
        if step == 1 and agent._dianping_xml is not None:
            xml = agent._dianping_xml
            started_at = agent._dianping_xml_started_at
            xml_epoch = agent._dianping_xml_epoch
            observation = agent._initial_observation
            agent._dianping_xml = None
            agent._dianping_xml_epoch = None
        else:
            started_at = time.monotonic()
            broker = agent.device.observations
            xml_epoch = (broker.session_id, broker.generation)
            xml = await agent.device.get_ui_hierarchy()
            with trace_span(
                "step.capture_screenshot",
                attrs={"step": step, "purpose": "conditioned_observation"},
            ):
                await agent._capture_observation("conditioned_observation")
            observation = agent._last_observation
        if observation is None:
            raise DianpingConditionError("缺少当前观察")
        if xml_epoch != (observation.session_id, observation.generation):
            raise DianpingConditionError("界面节点与截图不属于同一观察代际")
        screenshot = observation.screenshot
        # UIAutomator's bounded dump includes an idle wait. Reject slow or
        # cross-generation captures instead of executing old node coordinates.
        if not 0 <= time.monotonic() - started_at <= 5.0:
            raise DianpingConditionError("界面节点观察已过期")
        if not agent.device.observations.is_current(
            observation, max_age_seconds=OBSERVATION_MAX_AGE_SECONDS
        ):
            raise DianpingConditionError("屏幕观察已失效")
        current_app = await agent.device.get_current_app()
        if current_app != "大众点评":
            if step != 1:
                raise DianpingConditionError("前台应用已改变")
            action = {"_metadata": "do", "action": "Launch", "app": "大众点评"}
            phase, reason = "launch", "启动目标应用后重新观察"
        else:
            decision = controller.decide(xml, screenshot.width, screenshot.height)
            action, phase, reason = decision.action, decision.phase, decision.reason
        evidence = {
            "observation_id": observation.observation_id,
            "observation_session_id": observation.session_id,
            "geometry_id": observation.geometry_id,
            "action_sequence": observation.action_sequence,
            "skill_phase": phase,
        }
        # Store pixels once. Pre-action and preceding post-action events refer
        # to that same fresh observation; recording is not another capture.
        await record_phone_agent_event(
            "observation",
            step,
            screenshot=screenshot,
            observation_source="conditioned",
            **evidence,
        )
        if step > 1:
            await record_phone_agent_event("post_action", step - 1, **evidence)
        await record_phone_agent_event(
            "action_selected", step, action=action, **evidence
        )
        await record_phone_agent_event("pre_action", step, **evidence)
        if await agent.device.get_current_app() != current_app:
            raise DianpingConditionError("动作前前台应用已改变")
        if agent._cancel_event.is_set():
            raise asyncio.CancelledError()
        if (
            agent.device.pending_writes
            or agent.device.outcome_unknown
            or not agent.device.observations.is_current(
                observation, max_age_seconds=OBSERVATION_MAX_AGE_SECONDS
            )
            or time.monotonic() - started_at > 5.0
        ):
            raise DianpingConditionError("动作前观察或设备状态已失效")
        with trace_span(
            "skill.conditioned_decision",
            attrs={"phase": phase, "action_name": action.get("action"), **evidence},
        ):
            pass
        with trace_span(
            "step.execute_action",
            attrs={
                "step": step,
                "action_name": action.get("action"),
                "caller": "conditioned_skill",
            },
        ):
            result = await agent.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
        if not result.success or result.requires_confirmation:
            result.should_finish = True
    except asyncio.CancelledError:
        agent.device.observations.invalidate()
        raise
    except Exception as exc:
        agent.device.observations.invalidate()
        reason = (
            str(exc)
            if isinstance(exc, DianpingConditionError)
            else "条件化观察或执行不可用"
        )
        result = ActionResult(False, True, message=reason)
    await record_phone_agent_event(
        "action_result",
        step,
        action=action,
        success=result.success,
        message=result.message,
        skill_phase=phase,
    )
    yield {
        "type": "step",
        "data": {
            "step": step,
            "thinking": reason,
            "action": action,
            "success": result.success,
            "finished": result.should_finish,
            "message": result.message or reason,
            "skill_phase": phase,
            "stop_reason": "conditioned_skill_stopped" if not result.success else "",
            "screenshot": screenshot.base64_data if screenshot else None,
        },
    }
