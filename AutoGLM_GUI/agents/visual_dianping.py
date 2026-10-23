"""One visual policy call per observation; the kernel owns final verification."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any

from openai import AsyncOpenAI
from PIL import Image, ImageChops, ImageStat

from AutoGLM_GUI.actions import AsyncActionHandler
from AutoGLM_GUI.agents.gemini.screen_guard import compare_modal_transition
from AutoGLM_GUI.agents.protocols import AsyncAgent
from AutoGLM_GUI.config import ModelConfig, StepResult
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.devices.async_adapter import AsyncDeviceAdapter
from AutoGLM_GUI.devices.rmq1_ai import Rmq1AIAdapter
from AutoGLM_GUI.execution_metrics import record_model_call, record_usage
from AutoGLM_GUI.model import MessageBuilder
from AutoGLM_GUI.observation import Observation, StaleObservationError
from AutoGLM_GUI.phone_agent_trace import record_phone_agent_event
from AutoGLM_GUI.trace import trace_span
from AutoGLM_GUI.visual_dianping import (
    VISUAL_PROMPT,
    VisualConditionError,
    VisualDianpingController,
    VisualPage,
    parse_page,
)

OBSERVATION_MAX_AGE = 3.0
DECISION_MAX_AGE = 30.0


def blank_loading_frame(screenshot: Screenshot) -> bool:
    """Only suppress policy on nearly white empty bodies; never authorize input."""
    with Image.open(
        BytesIO(base64.b64decode(screenshot.base64_data, validate=True))
    ) as image:
        if image.size != (screenshot.width, screenshot.height):
            raise ValueError("invalid screenshot geometry")
        thumb = image.convert("L").resize((60, 120))
        body = list(thumb.crop((0, 8, 60, 114)).tobytes())
        return sum(pixel >= 248 for pixel in body) / len(body) >= 0.997


def protected_regions_match(
    before: Screenshot, after: Screenshot, action: dict[str, Any]
) -> bool:
    """Recheck the header/target or focused input regions, tolerating moving ads.

    This is change detection, not semantic recognition or independent acceptance.
    Any invalid image, geometry change or modal transition fails closed.
    """
    if compare_modal_transition(before, after).reason:
        return False
    images = []
    for screenshot in (before, after):
        with Image.open(
            BytesIO(base64.b64decode(screenshot.base64_data, validate=True))
        ) as image:
            images.append(image.convert("RGB").resize((300, 600)))
    regions = [(0, 25, 300, 120)]
    if action["action"] == "Type":
        regions += [(0, 120, 300, 180), (0, 360, 300, 600)]
    else:
        x, y = action["element"]
        px, py = x * 300 // 1000, y * 600 // 1000
        regions.append(
            (max(0, px - 32), max(0, py - 30), min(300, px + 32), min(600, py + 30))
        )
    for region in regions:
        diff = ImageChops.difference(*(image.crop(region) for image in images))
        if max(ImageStat.Stat(diff).mean) > 4:
            return False
    return True


class VisualDianpingAgent:
    """Task-scoped policy using the managed agent's leased device and verifier config.

    The owner closes the original model client before installing this wrapper.
    Manager/lease ownership is retained; cleanup closes this one policy client.
    """

    def __init__(
        self,
        original: AsyncAgent,
        executor_config: ModelConfig,
        city: str,
        keyword: str,
    ) -> None:
        device = getattr(original, "device", None)
        if not isinstance(device, AsyncDeviceAdapter):
            raise ValueError("visual skill requires a managed observation adapter")
        self.original = original
        self.device = device
        self.model_config = original.model_config  # Independent verifier stays fixed.
        self.executor_config = executor_config
        self.agent_config = original.agent_config
        self.controller = VisualDianpingController(
            city, keyword, ios=isinstance(device, Rmq1AIAdapter)
        )
        self.action_handler = AsyncActionHandler(device=device)
        self.openai_client = AsyncOpenAI(
            base_url=executor_config.base_url,
            api_key=executor_config.api_key,
            timeout=30,
            max_retries=0,
        )
        self._running = False
        self._cancelled = False
        self._step_count = 0
        self._context: list[dict[str, Any]] = []
        self._recent: list[dict[str, Any]] = []
        self._pending_observation: Observation | None = None
        self._active_task: asyncio.Task[Any] | None = None

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def context(self) -> list[dict[str, Any]]:
        return self._context

    @property
    def is_running(self) -> bool:
        return self._running

    def reset(self) -> None:
        if self._running or self.device.pending_writes or self._cancelled:
            raise RuntimeError("cannot reset active or cancelled policy")
        self._step_count = 0
        self._context = []
        self._recent = []
        self._pending_observation = None
        self.controller = VisualDianpingController(
            self.controller.city, self.controller.keyword, ios=self.controller.ios
        )
        self.device.observations.invalidate()

    async def cancel(self) -> None:
        self._cancelled = True
        self.device.observations.invalidate()
        await self.original.cancel()
        if (
            self._active_task is not None
            and self._active_task is not asyncio.current_task()
        ):
            self._active_task.cancel()

    def step(self, task: str | None = None) -> StepResult:
        raise NotImplementedError("Use the async visual skill stream")

    async def run(self, task: str) -> str:
        message = ""
        async for event in self.stream(task):
            message = str(event.get("data", {}).get("message", message))
        return message

    async def _perceive(self, observation: Observation) -> VisualPage:
        config = self.executor_config
        self._context = [
            MessageBuilder.create_system_message(VISUAL_PROMPT),
            MessageBuilder.create_user_message(
                json.dumps(
                    {
                        "city": self.controller.city,
                        "keyword": self.controller.keyword,
                        "platform": "ios" if self.controller.ios else "android",
                        "city_confirmed": self.controller.city_confirmed,
                        "image_width": observation.screenshot.width,
                        "image_height": observation.screenshot.height,
                        "recent_actions": self._recent[-4:],
                    },
                    ensure_ascii=False,
                ),
                observation.screenshot.base64_data,
            ),
        ]
        extra = dict(config.extra_body)
        if config.model_name == "deepseek-flash":
            extra["thinking"] = {"type": "disabled"}
        with trace_span(
            "step.llm",
            attrs={
                "agent_type": "VisualDianpingAgent",
                "model_name": config.model_name,
            },
        ):
            call_id = record_model_call("executor")
            async with asyncio.timeout(35):
                response = await self.openai_client.chat.completions.create(
                    model=config.model_name,
                    messages=self._context,  # type: ignore[arg-type]
                    temperature=config.temperature,
                    max_tokens=800,
                    response_format={"type": "json_object"},
                    extra_body=extra,
                )
            record_usage("executor", response.usage, call_id=call_id)
        if len(response.choices) != 1 or response.choices[0].finish_reason != "stop":
            raise VisualConditionError("视觉响应不完整，已停止")
        return parse_page(response.choices[0].message.content or "")

    def _active(self, observation: Observation, max_age: float) -> None:
        if self._cancelled:
            raise asyncio.CancelledError()
        if (
            self.device.pending_writes
            or self.device.outcome_unknown
            or not self.device.observations.is_current(
                observation, max_age_seconds=max_age
            )
        ):
            raise StaleObservationError("观察失效或动作结果未知")

    async def _guard(
        self, observed: Observation, action: dict[str, Any]
    ) -> Observation | None:
        self._active(observed, DECISION_MAX_AGE)
        if isinstance(self.device, Rmq1AIAdapter):
            return observed  # Native execution rechecks the fresh frame and lease.
        if self.device.observations.is_current(
            observed, max_age_seconds=OBSERVATION_MAX_AGE
        ):
            return observed
        current = await self.device.get_observation("visual_pre_action", fresh=True)
        self._active(observed, DECISION_MAX_AGE)
        if observed.geometry_id != current.geometry_id:
            raise StaleObservationError("设备画面几何已改变")
        await record_phone_agent_event(
            "observation",
            self.step_count,
            screenshot=current.screenshot,
            observation_id=current.observation_id,
            observation_source="visual_pre_action",
        )
        if not protected_regions_match(observed.screenshot, current.screenshot, action):
            self._pending_observation = current
            return None
        self._active(current, OBSERVATION_MAX_AGE)
        return current

    async def _act(self, observation: Observation, action: dict[str, Any]) -> None:
        if isinstance(self.device, Rmq1AIAdapter):
            self._active(observation, DECISION_MAX_AGE)
            x, y = action["element"]
            result = await self.device.execute_observation(observation, x=x, y=y)
            if not result.get("success") or result.get("outcome") != "completed":
                raise VisualConditionError("iPhone 动作未确认完成，已停止")
        else:
            self._active(observation, OBSERVATION_MAX_AGE)
            screenshot = observation.screenshot
            result = await self.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
            if not result.success or result.requires_confirmation:
                raise VisualConditionError("设备动作未确认成功，已停止")

    @staticmethod
    def _terminal(status: str, reason: str, message: str) -> dict[str, Any]:
        return {
            "type": "done",
            "data": {
                "status": status,
                "stop_reason": reason,
                "message": message,
                "success": status == "completion_requested",
            },
        }

    async def stream(
        self,
        task: str,
        *,
        continue_with: str | None = None,
        step_budget: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._running:
            raise RuntimeError("visual policy already running")
        self._running = True
        self._active_task = asyncio.current_task()
        stale = 0
        blank_frames = 0
        try:
            for _ in range(step_budget or 20):
                self._step_count += 1
                if self._cancelled:
                    raise asyncio.CancelledError()
                if isinstance(self.device, Rmq1AIAdapter):
                    await self.device.validate_owner()
                with trace_span(
                    "step.capture_screenshot",
                    attrs={"step": self.step_count, "purpose": "visual_policy"},
                ):
                    reused = self._pending_observation is not None
                    observed = (
                        self._pending_observation
                        or await self.device.get_observation("visual_policy")
                    )
                    self._pending_observation = None
                self._active(observed, DECISION_MAX_AGE)
                evidence = {
                    "observation_id": observed.observation_id,
                    "observation_session_id": observed.session_id,
                    "geometry_id": observed.geometry_id,
                    "action_sequence": observed.action_sequence,
                }
                await record_phone_agent_event(
                    "observation",
                    self.step_count,
                    screenshot=None if reused else observed.screenshot,
                    observation_source="visual_policy",
                    **evidence,
                )
                if self.step_count > 1:
                    await record_phone_agent_event(
                        "post_action", self.step_count - 1, **evidence
                    )
                if blank_loading_frame(observed.screenshot):
                    blank_frames += 1
                    if blank_frames > 4:
                        raise VisualConditionError("空白页面持续加载，已停止")
                    await asyncio.sleep(0.5)
                    continue
                blank_frames = 0
                page = await self._perceive(observed)
                self._active(observed, DECISION_MAX_AGE)
                with trace_span(
                    "skill.visual_conditions",
                    attrs={
                        "page": page.page,
                        "app": page.app,
                        "blocked": page.blocked,
                        "city_matches": page.city == self.controller.city,
                        "query_matches": page.query == self.controller.keyword,
                        "target_role": page.target.role,
                        "focused_search": page.focused_search,
                        **evidence,
                    },
                ):
                    pass
                next_controller = copy.deepcopy(self.controller)
                action = next_controller.decide(page)
                if (
                    len(self._recent) >= 2
                    and page.target.role != "keyboard_key"
                    and all(
                        item["page"] == page.page and item["action"] == action
                        for item in self._recent[-2:]
                    )
                ):
                    raise VisualConditionError("同一目标重复操作无进展，已停止")
                await record_phone_agent_event(
                    "action_selected",
                    self.step_count,
                    action=action,
                    skill_phase=page.page,
                    **evidence,
                )
                if action.get("_metadata") == "finish":
                    yield self._terminal(
                        "completion_requested",
                        "finish_requested",
                        str(action["message"]),
                    )
                    return
                if action["action"] == "Wait":
                    await asyncio.sleep(0.5)
                else:
                    authorized = await self._guard(observed, action)
                    if authorized is None:
                        stale += 1
                        if stale > 2:
                            raise VisualConditionError("画面持续变化，已停止")
                        continue
                    await record_phone_agent_event(
                        "pre_action",
                        self.step_count,
                        observation_id=authorized.observation_id,
                    )
                    with trace_span(
                        "step.execute_action",
                        attrs={
                            "step": self.step_count,
                            "action_name": action.get("action"),
                            "caller": "visual_skill",
                        },
                    ):
                        await self._act(authorized, action)
                    stale = 0
                self.controller = next_controller
                self._recent.append(
                    {"page": page.page, "action": action, "target": page.target.label}
                )
                self._recent = self._recent[-4:]
                await record_phone_agent_event(
                    "action_result",
                    self.step_count,
                    action=action,
                    success=True,
                    skill_phase=page.page,
                )
                yield {
                    "type": "step",
                    "data": {
                        "step": self.step_count,
                        "thinking": "依据当前截图和任务条件执行",
                        "action": action,
                        "success": True,
                        "finished": False,
                        "skill_phase": page.page,
                    },
                }
            yield self._terminal(
                "partial", "step_budget_reached", "本段步数预算已用完。"
            )
        except asyncio.CancelledError:
            self.device.observations.invalidate()
            raise
        except Exception as exc:
            self.device.observations.invalidate()
            message = (
                str(exc)
                if isinstance(exc, (VisualConditionError, StaleObservationError))
                else "视觉观察或执行不可用，已停止。"
            )
            yield self._terminal("blocked", "visual_skill_stopped", message)
        finally:
            self._running = False
            self._active_task = None
