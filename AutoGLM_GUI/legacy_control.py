"""Compatibility for HTTP clients, sharing ownership with streamed input and AI."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4
from typing import Any

from AutoGLM_GUI import adb_plus
from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager


@dataclass
class LegacyGesture:
    owner: str = field(default_factory=lambda: f"http:{uuid4().hex}")
    x: int = 0
    y: int = 0
    timer: asyncio.Task[None] | None = None


_gestures: dict[str, LegacyGesture] = {}
_locks: dict[str, asyncio.Lock] = {}


_operations: set[asyncio.Task[None]] = set()


async def manual_operation(
    device_id: str, execute: Callable[..., None], **kwargs: Any
) -> None:
    manager = PhoneAgentManager.get_instance()
    owner = f"http:{uuid4().hex}"
    await manager.acquire_manual_control(device_id, owner)

    async def complete() -> None:
        try:
            await asyncio.to_thread(execute, **kwargs)
        except BaseException:
            await manager.quarantine_manual_control(device_id, owner)
            raise
        else:
            await manager.release_manual_control(device_id, owner)

    # A cancelled HTTP request cannot stop its ADB thread. Keep ownership until
    # that thread settles, including when the request is no longer awaiting it.
    task = asyncio.create_task(complete())
    _operations.add(task)

    def finished(result: asyncio.Task[None]) -> None:
        _operations.discard(result)
        if not result.cancelled():
            result.exception()

    task.add_done_callback(finished)
    await asyncio.shield(task)


async def _expire(device_id: str, gesture: LegacyGesture) -> None:
    await asyncio.sleep(15)
    async with _locks[device_id]:
        if _gestures.get(device_id) is not gesture:
            return
        manager = PhoneAgentManager.get_instance()
        try:
            await adb_plus.touch_up_async(gesture.x, gesture.y, device_id)
        except Exception:
            await manager.quarantine_manual_control(device_id, gesture.owner)
        else:
            await manager.release_manual_control(device_id, gesture.owner)
        finally:
            _gestures.pop(device_id, None)


async def legacy_touch(
    action: str,
    device_id: str | None,
    x: int,
    y: int,
    delay: float,
    execute: Callable[..., Awaitable[None]],
) -> None:
    if not device_id:
        raise ValueError("device_id is required")
    if device_id.startswith("rmq1:"):
        raise ValueError("RMQ1 不支持 ADB 持续触摸接口")
    lock = _locks.setdefault(device_id, asyncio.Lock())
    async with lock:
        manager = PhoneAgentManager.get_instance()
        gesture = _gestures.get(device_id)
        if action == "down" and gesture:
            raise ValueError("A gesture is already active")
        if action == "move" and not gesture:
            raise ValueError("No active gesture")
        if gesture is None:
            gesture = LegacyGesture()
        await manager.acquire_manual_control(device_id, gesture.owner)
        if gesture.timer:
            gesture.timer.cancel()
        try:
            await execute(x=x, y=y, device_id=device_id, delay=delay)
        except BaseException:
            _gestures.pop(device_id, None)
            await manager.quarantine_manual_control(device_id, gesture.owner)
            raise
        if action == "up":
            _gestures.pop(device_id, None)
            await manager.release_manual_control(device_id, gesture.owner)
        else:
            gesture.x, gesture.y = x, y
            _gestures[device_id] = gesture
            gesture.timer = asyncio.create_task(_expire(device_id, gesture))
