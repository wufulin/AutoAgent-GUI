"""scrcpy 3.3.3 control protocol and ordered, connection-bound gestures.

Wire layouts: Genymobile/scrcpy v3.3.3 app/src/control_msg.c.
A socket write acknowledges transport only, never Android injection or UI success.
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager


class ControlCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session: str
    sequence: int = Field(ge=1)
    action: Literal["down", "move", "up", "cancel", "scroll", "heartbeat"]
    x: int = Field(ge=0, le=65535)
    y: int = Field(ge=0, le=65535)
    width: int = Field(gt=0, le=65535)
    height: int = Field(gt=0, le=65535)
    epoch: int = Field(ge=0)
    scroll: float = Field(default=0, ge=-16, le=16, allow_inf_nan=False)


def encode_touch(action: str, x: int, y: int, width: int, height: int) -> bytes:
    # A stable non-negative pointer ID selects a touchscreen finger (not a mouse).
    code = {"down": 0, "up": 1, "move": 2, "cancel": 3}[action]
    return struct.pack(
        ">BBQiiHHHII",
        2,
        code,
        0,
        x,
        y,
        width,
        height,
        0 if action in {"up", "cancel"} else 65535,
        0,
        0,
    )


def encode_scroll(x: int, y: int, width: int, height: int, amount: float) -> bytes:
    value = max(-32768, min(32767, round(amount / 16 * 32768)))
    return struct.pack(
        ">BiiHHhhI",
        3,
        x,
        y,
        width,
        height,
        0,
        value,
        0,
    )


class ControlTransport(Protocol):
    async def send_control(self, data: bytes) -> None: ...


class ControlSession:
    def __init__(self, device_id: str, transport: ControlTransport):
        self.id = uuid4().hex
        self.device_id = device_id
        self.transport = transport
        self.epoch = 0
        self.sequence = 0
        self.lock = asyncio.Lock()
        self.active: ControlCommand | None = None
        self.last_activity = time.monotonic()
        self.closed = False
        self.failed = False
        self.manager = PhoneAgentManager.get_instance()

    async def _write(self, data: bytes) -> None:
        try:
            await asyncio.wait_for(self.transport.send_control(data), timeout=1.5)
        except BaseException:
            self.failed = True
            await self.manager.quarantine_manual_control(self.device_id, self.id)
            raise

    async def execute(self, command: ControlCommand) -> dict[str, object]:
        started = time.monotonic()
        async with self.lock:
            if self.closed or self.failed or command.session != self.id:
                raise ValueError(
                    "Control session expired; reconnect without replaying input"
                )
            if command.sequence != self.sequence + 1:
                raise ValueError(
                    "Out-of-order input; cancel the gesture before continuing"
                )
            self.sequence = command.sequence
            if command.x >= command.width or command.y >= command.height:
                raise ValueError("Coordinates outside the video frame")
            if command.action == "cancel":
                # New geometry can release an old gesture after rotation.
                if command.epoch != self.epoch:
                    raise ValueError("Video geometry changed")
                await self._release(command)
            else:
                if command.epoch != self.epoch:
                    raise ValueError(
                        "Video geometry changed; cancel the current gesture"
                    )
                if command.action in {"down", "scroll"}:
                    if self.active:
                        raise ValueError("A gesture is already active")
                    await self.manager.acquire_manual_control(self.device_id, self.id)
                    # Retain ownership if the transport outcome becomes unknown.
                    self.active = command
                elif not self.active:
                    raise ValueError("No active gesture")
                elif (command.width, command.height) != (
                    self.active.width,
                    self.active.height,
                ):
                    raise ValueError(
                        "Video geometry changed; cancel the current gesture"
                    )
                self.last_activity = time.monotonic()
                if command.action == "heartbeat":
                    return {"status": "active", "sequence": self.sequence}
                if command.action == "scroll":
                    await self._write(
                        encode_scroll(
                            command.x,
                            command.y,
                            command.width,
                            command.height,
                            command.scroll,
                        )
                    )
                else:
                    await self._write(
                        encode_touch(
                            command.action,
                            command.x,
                            command.y,
                            command.width,
                            command.height,
                        )
                    )
                self.active = command
                if command.action in {"up", "scroll"}:
                    self.active = None
                    await self.manager.release_manual_control(self.device_id, self.id)
            return {
                "status": "sent",
                "sequence": self.sequence,
                "server_ms": round((time.monotonic() - started) * 1000, 2),
            }

    async def _release(self, geometry: ControlCommand | None = None) -> None:
        if self.active:
            if geometry is None and self.active.epoch != self.epoch:
                self.failed = True
                await self.manager.quarantine_manual_control(self.device_id, self.id)
                raise ValueError(
                    "Release geometry unknown after rotation; check the phone"
                )
            last = self.active
            width = geometry.width if geometry else last.width
            height = geometry.height if geometry else last.height
            x = min(width - 1, round(last.x * width / last.width))
            y = min(height - 1, round(last.y * height / last.height))
            await self._write(encode_touch("cancel", x, y, width, height))
            self.active = None
        await self.manager.release_manual_control(self.device_id, self.id)

    async def expire(self) -> bool:
        async with self.lock:
            if self.active and time.monotonic() - self.last_activity > 2.5:
                await self._release()
                return True
            return False

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            if not self.failed:
                await self._release()
