"""AsyncGeminiAgent - 通用视觉模型 Agent，使用 OpenAI 兼容的 function calling。

支持 Gemini、GPT-4o、Claude 等任何支持 vision + tool use 的模型，
通过 OpenAI 兼容 API 端点接入。
"""

import asyncio
import json
import time
import traceback
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, cast

from AutoGLM_GUI.actions import ActionResult
from AutoGLM_GUI.agents.base import AsyncAgentBase
from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.execution_metrics import record_model_call, record_usage
from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.model import MessageBuilder
from AutoGLM_GUI.model.error_details import (
    model_error_message,
    serialize_model_error_async,
    trace_error_attrs,
)
from AutoGLM_GUI.trace import summarize_text, trace_span
from AutoGLM_GUI.phone_agent_trace import (
    phone_agent_replay_active,
    record_phone_agent_event,
)

from .action_mapper import InvalidToolCallError, tool_call_to_action
from .prompts import INDEPENDENT_VERIFICATION_PROMPT, get_system_prompt
from .screen_guard import compare_modal_transition
from .tools import get_device_tools


class AsyncGeminiAgent(AsyncAgentBase):
    """通用视觉模型 Agent，使用 function calling 而非自定义格式解析。"""

    max_consecutive_invalid_tool_calls = 3
    observation_reuse_ttl_seconds = 1.0
    verification_settle_seconds = 1.0

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._consecutive_invalid_tool_calls = 0
        self._independent_verification = False
        self._cached_observation: tuple[Screenshot, float, int] | None = None

    @property
    def independent_verification(self) -> bool:
        return self._independent_verification

    @independent_verification.setter
    def independent_verification(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("independent_verification must be a boolean")
        self._independent_verification = enabled
        self._cached_observation = None
        self._initial_system_message = MessageBuilder.create_system_message(
            self.get_system_prompt()
        )
        self._context[0] = self._initial_system_message.copy()

    def get_system_prompt(self) -> str:
        prompt = super().get_system_prompt()
        if getattr(self, "_independent_verification", False):
            prompt += INDEPENDENT_VERIFICATION_PROMPT
        return prompt

    def reset(self) -> None:
        self._independent_verification = False
        self._cached_observation = None
        super().reset()
        self._consecutive_invalid_tool_calls = 0

    async def cancel(self) -> None:
        self._cached_observation = None
        await super().cancel()

    async def stream(
        self,
        task: str,
        *,
        continue_with: str | None = None,
        step_budget: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if self._is_running:
            raise RuntimeError("Agent already has an active stream")
        # No frame may cross a stream, continuation, or cancellation boundary.
        self._cached_observation = None
        try:
            # The base implementation is an async generator, annotated as the
            # broader AsyncIterator. Own its closure when our consumer stops.
            inner = cast(
                AsyncGenerator[dict[str, Any], None],
                super().stream(
                    task, continue_with=continue_with, step_budget=step_budget
                ),
            )
            async with aclosing(inner):
                async for event in inner:
                    yield event
        finally:
            self._cached_observation = None

    def _take_cached_observation(self) -> tuple[Screenshot, float, int] | None:
        cached, self._cached_observation = self._cached_observation, None
        if cached is None or not self.independent_verification:
            return None
        screenshot, captured_at, source_step = cached
        age = time.monotonic() - captured_at
        if (
            not 0 <= age <= self.observation_reuse_ttl_seconds
            or source_step != self._step_count - 1
            or screenshot.is_sensitive
            or screenshot.width <= 0
            or screenshot.height <= 0
            or not screenshot.base64_data
            or self._cancel_event.is_set()
            or self.device.outcome_unknown
            or self.device.pending_writes
        ):
            return None
        return screenshot, age, source_step

    def _get_default_system_prompt(self, lang: str) -> str:
        return get_system_prompt(lang)

    def _prepare_initial_context(
        self,
        task: str,
        screenshot_base64: str,
        current_app: str,
        reference_images: list[dict[str, str]] | None = None,
    ) -> None:
        reference_images = reference_images or []
        reference_notice = MessageBuilder.build_user_reference_images_notice(
            len(reference_images)
        )
        reference_section = (
            f"\n\nUser reference images: {reference_notice}" if reference_notice else ""
        )
        self._consecutive_invalid_tool_calls = 0
        self._context.append(
            MessageBuilder.create_user_message_with_images(
                text=f"{task}{reference_section}\n\nCurrent app: {current_app}",
                images=[
                    {"mime_type": "image/png", "data": screenshot_base64},
                    *reference_images,
                ],
            )
        )

    async def _execute_step(self) -> AsyncGenerator[dict[str, Any], None]:
        """执行单步：调用 LLM → 解析 tool call → 执行动作。"""
        if self._cancel_event.is_set():
            self._cached_observation = None
            raise asyncio.CancelledError()
        self._step_count += 1
        screenshot = self._initial_screenshot if self._step_count == 1 else None
        observation_source = "initial" if self._step_count == 1 else "capture"
        observation_source_step = self._step_count

        # 1. 获取截图（非首步）
        if self._step_count > 1:
            try:
                cached = self._take_cached_observation()
                if cached is not None:
                    screenshot, age, observation_source_step = cached
                    observation_source = "post_action_reuse"
                    with trace_span(
                        "step.reuse_observation",
                        attrs={
                            "step": self._step_count,
                            "source_step": observation_source_step,
                            "age_ms": age * 1000,
                            "fresh_capture": False,
                        },
                    ):
                        pass
                else:
                    with trace_span(
                        "step.capture_screenshot",
                        attrs={
                            "step": self._step_count,
                            "agent_type": self.__class__.__name__,
                            "purpose": "observation",
                        },
                    ):
                        screenshot = await self.device.get_screenshot()
                with trace_span(
                    "step.get_current_app",
                    attrs={
                        "step": self._step_count,
                        "agent_type": self.__class__.__name__,
                    },
                ):
                    current_app = await self.device.get_current_app()
            except Exception as e:
                logger.error(f"Failed to get device info: {e}")
                yield {"type": "error", "data": {"message": f"Device error: {e}"}}
                yield {
                    "type": "step",
                    "data": {
                        "step": self._step_count,
                        "thinking": "",
                        "action": None,
                        "success": False,
                        "finished": True,
                        "message": f"Device error: {e}",
                        "stop_reason": "device_error",
                    },
                }
                return

            with trace_span(
                "step.build_message",
                attrs={"step": self._step_count, "agent_type": self.__class__.__name__},
            ):
                self._context.append(
                    MessageBuilder.create_user_message(
                        text=f"Current app: {current_app}",
                        image_base64=screenshot.base64_data,
                    )
                )

        observation = screenshot
        await record_phone_agent_event(
            "observation",
            self._step_count,
            screenshot=observation,
            observation_source=observation_source,
            source_step=observation_source_step,
        )

        # 2. 调用 LLM with tools
        try:
            with trace_span(
                "step.llm",
                attrs={
                    "step": self._step_count,
                    "agent_type": self.__class__.__name__,
                    "model_name": self.model_config.model_name,
                    "message_count": len(self._context),
                },
            ) as span:
                try:
                    (
                        thinking,
                        reasoning_content,
                        tool_name,
                        tool_args,
                    ) = await self._call_llm_with_tools()
                    span.set_attributes(
                        {
                            "thinking_chars": len(thinking),
                            "reasoning_content_present": bool(reasoning_content),
                        }
                    )
                except Exception as exc:
                    error_details = await serialize_model_error_async(
                        exc,
                        model_config=self.model_config,
                        call_site="AutoGLM_GUI.agents.gemini.async_agent.AsyncGeminiAgent._call_llm_with_tools",
                    )
                    span.set_attributes(trace_error_attrs(error_details))
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"LLM error: {e}")
            if self.agent_config.verbose:
                logger.debug(traceback.format_exc())
            error_details = await serialize_model_error_async(
                e,
                model_config=self.model_config,
                call_site="AutoGLM_GUI.agents.gemini.async_agent.AsyncGeminiAgent._call_llm_with_tools",
            )
            message = model_error_message(e)
            yield {
                "type": "error",
                "data": {"message": message, "error_details": error_details},
            }
            yield {
                "type": "step",
                "data": {
                    "step": self._step_count,
                    "thinking": "",
                    "action": None,
                    "success": False,
                    "finished": True,
                    "message": message,
                    "error_details": error_details,
                    "stop_reason": "model_error",
                },
            }
            return

        if thinking:
            yield {"type": "thinking", "data": {"chunk": thinking}}

        # 3. 转换 tool call → action
        with trace_span(
            "step.parse_action",
            attrs={"step": self._step_count, "agent_type": self.__class__.__name__},
        ):
            with trace_span(
                "tool.call",
                attrs={
                    "step": self._step_count,
                    "caller": "gemini_model",
                    "tool_name": tool_name,
                    "tool_arg_keys": sorted(tool_args.keys()),
                    "tool_args_preview": summarize_text(
                        json.dumps(tool_args, ensure_ascii=False, default=str),
                        limit=512,
                    ),
                },
            ) as span:
                try:
                    action = tool_call_to_action(
                        tool_name,
                        tool_args,
                        independent_verification=self.independent_verification,
                    )
                except InvalidToolCallError as exc:
                    self._consecutive_invalid_tool_calls += 1
                    error_message = self._invalid_tool_call_message(exc)
                    logger.warning(
                        f"Invalid Gemini tool call on step {self._step_count} "
                        f"({tool_name}): {error_message}"
                    )
                    span.set_attributes(
                        {
                            "success": False,
                            "error_kind": "invalid_arguments",
                            "error_message": error_message,
                            "invalid_count": self._consecutive_invalid_tool_calls,
                        }
                    )
                    action = {
                        "_metadata": "tool_error",
                        "tool_name": tool_name,
                        "arguments": tool_args,
                        "message": error_message,
                    }
                else:
                    self._consecutive_invalid_tool_calls = 0
                    span.set_attributes(
                        {"success": True, "action_name": action.get("action")}
                    )

        if self.agent_config.verbose:
            logger.debug(f"🎯 Tool call: {tool_name}({tool_args})")
            logger.debug(f"   Action: {json.dumps(action, ensure_ascii=False)}")

        if action.get("_metadata") == "tool_error":
            limit_reached = (
                self._consecutive_invalid_tool_calls
                >= self.max_consecutive_invalid_tool_calls
            )
            error_message = str(action.get("message", "Invalid tool call"))
            tool_result = {
                "success": False,
                "error_code": "invalid_tool_arguments",
                "message": error_message,
                "retryable": not limit_reached,
            }
            with trace_span(
                "step.update_context",
                attrs={"step": self._step_count, "agent_type": self.__class__.__name__},
            ):
                self._remove_latest_message_images()
                self._append_tool_exchange(
                    thinking=thinking,
                    reasoning_content=reasoning_content,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_result=tool_result,
                )

            message = error_message
            if limit_reached:
                message = (
                    "Tool call validation failed "
                    f"{self._consecutive_invalid_tool_calls} consecutive times: "
                    f"{error_message}"
                )

            yield {
                "type": "step",
                "data": {
                    "step": self._step_count,
                    "thinking": thinking,
                    "action": action,
                    "success": False,
                    "finished": limit_reached,
                    "stop_reason": "invalid_tool_arguments",
                    "message": message,
                    "screenshot": screenshot.base64_data if screenshot else None,
                },
            }
            return

        # 4. 执行 action
        await record_phone_agent_event(
            "action_selected", self._step_count, action=action
        )
        self._cached_observation = None
        try:
            if action.get("_metadata") != "finish":
                with trace_span(
                    "step.capture_screenshot",
                    attrs={
                        "step": self._step_count,
                        "agent_type": self.__class__.__name__,
                        "purpose": "pre_action",
                    },
                ):
                    screenshot = await self.device.get_screenshot()
                await record_phone_agent_event(
                    "pre_action", self._step_count, screenshot=screenshot
                )
                if observation is not None and (
                    observation.width,
                    observation.height,
                ) != (screenshot.width, screenshot.height):
                    raise ValueError(
                        "SCREEN_CHANGED: screen dimensions changed since observation; "
                        "action was not executed"
                    )
                if self.independent_verification:
                    with trace_span(
                        "step.screen_guard", attrs={"step": self._step_count}
                    ) as guard:
                        try:
                            if observation is None:
                                raise ValueError("missing observation")
                            comparison = compare_modal_transition(
                                observation, screenshot
                            )
                        except (ValueError, OSError) as exc:
                            raise ValueError(
                                f"SCREEN_CHANGED: invalid screenshot; {exc}"
                            ) from exc
                        guard.set_attributes(
                            {**comparison.metrics, "reason": comparison.reason}
                        )
                        if comparison.reason:
                            raise ValueError(
                                f"SCREEN_CHANGED: {comparison.reason}; action was not executed"
                            )
            if self._cancel_event.is_set():
                raise asyncio.CancelledError()
            if self.device.outcome_unknown or self.device.pending_writes:
                raise ValueError(
                    "Device action outcome is unknown; action was not executed"
                )
            with trace_span(
                "step.execute_action",
                attrs={
                    "step": self._step_count,
                    "agent_type": self.__class__.__name__,
                    "action_name": action.get("action"),
                    "action_type": action.get("_metadata"),
                },
            ):
                result = await self.action_handler.execute(
                    action,
                    screenshot.width if screenshot else 0,
                    screenshot.height if screenshot else 0,
                )
        except Exception as e:
            logger.error(f"Action execution error: {e}")
            result = ActionResult(success=False, should_finish=True, message=str(e))

        await record_phone_agent_event(
            "action_result",
            self._step_count,
            action=action,
            success=result.success,
            message=result.message,
        )
        if phone_agent_replay_active() and action.get("_metadata") != "finish":
            try:
                with trace_span(
                    "step.capture_screenshot",
                    attrs={"step": self._step_count, "purpose": "post_action"},
                ):
                    after = await self.device.get_screenshot()
                captured_at = time.monotonic()
                if (
                    self.independent_verification
                    and result.success
                    and not result.should_finish
                    and not result.requires_confirmation
                    and not self._cancel_event.is_set()
                    and not self.device.outcome_unknown
                    and not self.device.pending_writes
                    and not after.is_sensitive
                    and after.width > 0
                    and after.height > 0
                    and bool(after.base64_data)
                    and screenshot is not None
                    and (after.width, after.height)
                    == (screenshot.width, screenshot.height)
                ):
                    self._cached_observation = (after, captured_at, self._step_count)
                await record_phone_agent_event(
                    "post_action", self._step_count, screenshot=after
                )
            except Exception:
                self._cached_observation = None
                # Observation failure must never turn an executed tap into a
                # retryable action or hide the original execution result.
                await record_phone_agent_event(
                    "post_action_unavailable",
                    self._step_count,
                    message="Post-action screenshot unavailable; do not replay blindly",
                )

        # 5. 更新上下文
        with trace_span(
            "step.update_context",
            attrs={"step": self._step_count, "agent_type": self.__class__.__name__},
        ):
            self._remove_latest_message_images()
            self._append_tool_exchange(
                thinking=thinking,
                reasoning_content=reasoning_content,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result={
                    "success": result.success,
                    "message": result.message or "OK",
                },
            )

        # 6. 检查完成
        verification_requested = (
            self.independent_verification
            and action.get("verify_after") is True
            and result.success
            and not result.should_finish
            and not result.requires_confirmation
            and not self.device.outcome_unknown
            and not self.device.pending_writes
            and not self._cancel_event.is_set()
        )
        if verification_requested:
            self._cached_observation = None
            # Replaces some incidental loading time formerly spent on a finish
            # model turn. It is a bounded buffer, never evidence of readiness.
            with trace_span(
                "step.verification_settle",
                attrs={
                    "step": self._step_count,
                    "seconds": self.verification_settle_seconds,
                },
            ):
                try:
                    await asyncio.wait_for(
                        self._cancel_event.wait(),
                        timeout=self.verification_settle_seconds,
                    )
                except TimeoutError:
                    pass
            if self._cancel_event.is_set():
                raise asyncio.CancelledError()
            if self.device.outcome_unknown or self.device.pending_writes:
                verification_requested = False
                result = ActionResult(
                    success=False,
                    should_finish=True,
                    message="Device action outcome is unknown; observe and recover before continuing",
                )
        finished = (
            action.get("_metadata") == "finish"
            or result.should_finish
            or verification_requested
        )
        if finished:
            self._cached_observation = None
        message = result.message or action.get("message")
        if verification_requested and not message:
            message = "已执行最后一步。"

        yield {
            "type": "step",
            "data": {
                "step": self._step_count,
                "thinking": thinking,
                "action": action,
                "success": result.success,
                "finished": finished,
                **(
                    {"stop_reason": "verification_requested"}
                    if verification_requested
                    else {}
                ),
                "message": message,
                "screenshot": screenshot.base64_data if screenshot else None,
            },
        }

    async def _call_llm_with_tools(
        self,
    ) -> tuple[str, str | None, str, dict[str, Any]]:
        """调用 LLM，返回 (thinking, reasoning_content, tool_name, tool_args)。"""
        if self._cancel_event.is_set():
            raise asyncio.CancelledError()

        call_id = record_model_call("executor")
        response = await self.openai_client.chat.completions.create(
            messages=self._context,  # type: ignore[arg-type]
            model=self.model_config.model_name,
            max_tokens=self.model_config.max_tokens,
            temperature=self.model_config.temperature,
            tools=get_device_tools(
                independent_verification=self.independent_verification
            ),  # type: ignore[arg-type]
            tool_choice="required",
            # DeepSeek defaults to thinking, which rejects required tool choice.
            extra_body=(
                {**self.model_config.extra_body, "thinking": {"type": "disabled"}}
                if self.model_config.model_name == "deepseek-flash"
                else self.model_config.extra_body or None
            ),
        )

        record_usage("executor", getattr(response, "usage", None), call_id=call_id)
        choice = response.choices[0]
        message = choice.message

        thinking, reasoning_content = self._extract_thinking(message)

        if message.tool_calls and len(message.tool_calls) > 0:
            tool_call = message.tool_calls[0]
            tool_name = tool_call.function.name  # type: ignore[union-attr]
            try:
                parsed_args = json.loads(tool_call.function.arguments)  # type: ignore[union-attr]
                tool_args = parsed_args if isinstance(parsed_args, dict) else {}
            except json.JSONDecodeError as e:
                logger.warning(
                    f"Failed to parse tool arguments for {tool_name}: {e}. "
                    f"Raw: {tool_call.function.arguments!r}"  # type: ignore[union-attr]
                )
                tool_args = {}
            return thinking, reasoning_content, tool_name, tool_args

        logger.warning("Model did not return a tool call, treating as finish")
        return (
            thinking,
            reasoning_content,
            "finish",
            {"message": thinking or "No action returned"},
        )

    def _remove_latest_message_images(self) -> None:
        if len(self._context) > 1:
            self._context[-1] = MessageBuilder.remove_images_from_message(
                self._context[-1]
            )

    def _append_tool_exchange(
        self,
        *,
        thinking: str,
        reasoning_content: str | None,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> None:
        tool_call_id = f"call_{self._step_count}"
        assistant_message = {
            "role": "assistant",
            "content": thinking or "",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=False),
                    },
                }
            ],
        }
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        self._context.append(assistant_message)
        self._context.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(tool_result, ensure_ascii=False),
            }
        )

    @staticmethod
    def _invalid_tool_call_message(exc: InvalidToolCallError) -> str:
        return (
            f"Invalid tool call for {exc.tool_name}: {exc.message}. "
            "Return arguments that match the declared tool schema."
        )

    @classmethod
    def _extract_thinking(cls, message: Any) -> tuple[str, str | None]:
        content = cls._message_text_field(message, "content")
        reasoning_content = cls._message_text_field(
            message, "reasoning_content"
        ) or cls._message_text_field(message, "reasoning")
        return content or reasoning_content, reasoning_content or None

    @staticmethod
    def _message_text_field(message: Any, field_name: str) -> str:
        if isinstance(message, dict):
            value = message.get(field_name)
        else:
            value = getattr(message, field_name, None)
        return value if isinstance(value, str) else ""
