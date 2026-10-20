"""Task-local recording of inner Phone Agent observations and actions."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.trace import (
    capture_screenshot_mode,
    replay_trace_enabled,
    write_replay_event,
)

MAX_BUFFERED_FRAMES = 60


@dataclass
class PhoneAgentReplay:
    task_id: str
    trace_id: str | None
    device_id: str
    call_id: str = field(default_factory=lambda: uuid4().hex)
    sequence: int = 0
    # A segment has up to 20 steps and three observation/action frames per step.
    frames: list[dict[str, Any]] = field(default_factory=list)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def record(self, phase: str, step: int, **data: Any) -> None:
        if not self.trace_id or not replay_trace_enabled():
            return
        with self._lock:
            self._record(phase, step, **data)

    def _record(self, phase: str, step: int, **data: Any) -> None:
        self.sequence += 1
        event = {
            "seq": self.sequence,
            "event_type": "phone_agent",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": {
                **data,
                "step": step,
                "phase": phase,
                "phone_call_id": self.call_id,
            },
        }
        mode = capture_screenshot_mode()
        if mode == "off":
            event["payload"].pop("screenshot", None)
        # Default artifact mode writes immediately, including before inference.
        # on_error waits for the verified segment outcome, not each tap. Retain
        # the newest evidence if a recorder emits more than the expected frames.
        if mode == "on_error" and data.get("screenshot"):
            self.frames.append(event)
            del self.frames[:-MAX_BUFFERED_FRAMES]
        else:
            self._write(event)

    def _write(self, event: dict[str, Any]) -> None:
        if self.trace_id:
            write_replay_event(
                task_id=self.task_id,
                trace_id=self.trace_id,
                event_record=event,
                source="layered_phone",
                task={"device_id": self.device_id},
            )

    def finish(self, failed: bool) -> None:
        with self._lock:
            for event in self.frames:
                event["payload"]["subtask_failed"] = failed
                self._write(event)
            self.frames.clear()


_PHONE_REPLAY: ContextVar[PhoneAgentReplay | None] = ContextVar(
    "phone_agent_replay", default=None
)


@contextmanager
def phone_agent_replay(replay: PhoneAgentReplay) -> Iterator[None]:
    token = _PHONE_REPLAY.set(replay)
    try:
        yield
    finally:
        _PHONE_REPLAY.reset(token)


def phone_agent_replay_active() -> bool:
    return (
        _PHONE_REPLAY.get() is not None
        and replay_trace_enabled()
        and capture_screenshot_mode() != "off"
    )


async def record_phone_agent_event(
    phase: str,
    step: int,
    *,
    screenshot: Screenshot | None = None,
    **data: Any,
) -> None:
    replay = _PHONE_REPLAY.get()
    if replay is None:
        return
    if screenshot is not None:
        data["screen_width"] = screenshot.width
        data["screen_height"] = screenshot.height
        if not screenshot.is_sensitive:
            data["screenshot"] = screenshot.base64_data
    await asyncio.to_thread(replay.record, phase, step, **data)
