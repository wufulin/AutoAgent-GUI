"""Task-owned visual observations and cancellable input over the existing RMQ1 session."""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from PIL import Image, ImageChops, ImageFilter, ImageStat

from AutoGLM_GUI.device_protocol import FrameReference, Screenshot
from AutoGLM_GUI.observation import Observation, StaleObservationError
from AutoGLM_GUI.devices.async_adapter import AsyncDeviceAdapter
from AutoGLM_GUI.devices.rmq1 import RMQ1Device
from AutoGLM_GUI.devices.rmq1_live import LiveGesture
from AutoGLM_GUI.devices.rmq1_transport import RMQ1Error


class SceneChanged(RMQ1Error):
    """No input was dispatched; the model must observe and decide again."""


@dataclass(frozen=True)
class AIObservation:
    screenshot: Screenshot
    image: Image.Image
    session_id: str
    geometry_id: str
    frame_id: str
    width: int
    height: int
    rect: dict[str, float]

    def point(self, x: int, y: int) -> tuple[float, float]:
        if (
            type(x) is not int
            or type(y) is not int
            or not (0 <= x <= 1000 and 0 <= y <= 1000)
        ):
            raise RMQ1Error("AI 坐标必须是 0–1000 的整数")
        return (
            self.rect["x"] + x / 1000 * self.rect["width"],
            self.rect["y"] + y / 1000 * self.rect["height"],
        )


def _similar(before: Image.Image, after: Image.Image, *, local: bool = False) -> bool:
    if before.size != after.size:
        return False
    # Decode pixels rather than comparing PNG/JPEG bytes. Blur tolerates codec
    # noise; the changed-area bound still rejects small banners/target changes.
    size = (min(before.width, 320), min(before.height, 640))
    a = before.convert("RGB").resize(size).filter(ImageFilter.GaussianBlur(0.7))
    b = after.convert("RGB").resize(size).filter(ImageFilter.GaussianBlur(0.7))
    diff = ImageChops.difference(a, b).convert("L")
    changed = sum(diff.histogram()[13:]) / (diff.width * diff.height)
    return ImageStat.Stat(diff).mean[0] <= (2 if local else 3) and changed <= (
        0.01 if local else 0.008
    )


def scene_stable(
    before: AIObservation, after: AIObservation, points: list[tuple[int, int]]
) -> bool:
    if (
        before.session_id,
        before.geometry_id,
        before.width,
        before.height,
        before.rect,
    ) != (after.session_id, after.geometry_id, after.width, after.height, after.rect):
        return False
    w, h = before.image.size
    # Only the fixed status bar is omitted. Notification banners, keyboard,
    # containers and overlays remain protected, not arbitrary model masks.
    body = (0, round(h * 0.055), w, h)
    if not _similar(before.image.crop(body), after.image.crop(body)):
        return False
    for x, y in points:
        box = (
            max(0, round(w * (x - 60) / 1000)),
            max(0, round(h * (y - 45) / 1000)),
            min(w, round(w * (x + 60) / 1000)),
            min(h, round(h * (y + 45) / 1000)),
        )
        if not _similar(before.image.crop(box), after.image.crop(box), local=True):
            return False
    return True


class Rmq1AIAdapter(AsyncDeviceAdapter):
    """No Android app/package/navigation semantics are exposed to the iPhone agent."""

    def __init__(
        self,
        device: RMQ1Device,
        resolve_device: Callable[[str], RMQ1Device] | None = None,
    ):
        super().__init__(device)  # type: ignore[arg-type]
        self._resolve_device = resolve_device or (lambda _: device)
        # Only inspect the stable host-keyed marker through this reference;
        # cleanup must remain possible after the registration is removed.
        self._recovery_device = device
        self.lease_owner = ""
        self._cancelled = threading.Event()
        self._live: LiveGesture | None = None
        self._pending: asyncio.Task[dict[str, Any]] | None = None
        self._uncertain = False

    @property
    def rmq1(self) -> RMQ1Device:
        # Registrations survive explicit reconnects; cached adapters must use
        # the current generation without clearing their own recovery state.
        return self._resolve_device(self.device_id)

    @property
    def pending_writes(self) -> bool:
        return self._pending is not None and not self._pending.done()

    @property
    def outcome_unknown(self) -> bool:
        return self._uncertain or (
            self._recovery_device.recovery_required and not self.pending_writes
        )

    async def validate_owner(self) -> None:
        from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

        if not self.lease_owner:
            raise RMQ1Error("iPhone AI 仅允许在已授权任务中执行")
        if self._cancelled.is_set():
            raise asyncio.CancelledError()
        await PhoneAgentManager.get_instance().validate_device_lease(
            self.device_id, self.lease_owner
        )

    async def observe(self, timeout: float = 5) -> AIObservation:
        deadline = time.monotonic() + timeout
        while True:
            try:
                payload = await asyncio.to_thread(self.rmq1.observe)
                break
            except RMQ1Error:
                if (
                    not self.rmq1.online
                    or self.rmq1.recovery_required
                    or time.monotonic() >= deadline
                ):
                    raise
                await asyncio.sleep(0.05)
        image = Image.open(
            io.BytesIO(base64.b64decode(payload["image"], validate=True))
        ).convert("RGB")
        rect = payload["content_rect"]
        w, h = image.size
        image = image.crop(
            (
                round(w * rect["x"]),
                round(h * rect["y"]),
                round(w * (rect["x"] + rect["width"])),
                round(h * (rect["y"] + rect["height"])),
            )
        )
        encoded = io.BytesIO()
        image.save(encoded, "PNG")
        screenshot = Screenshot(
            base64.b64encode(encoded.getvalue()).decode(),
            image.width,
            image.height,
            frame=FrameReference(
                payload["session_id"],
                payload["geometry_id"],
                payload["frame_id"],
                w,
                h,
                (rect["x"], rect["y"], rect["width"], rect["height"]),
            ),
        )
        return AIObservation(
            screenshot,
            image,
            payload["session_id"],
            payload["geometry_id"],
            payload["frame_id"],
            w,
            h,
            rect,
        )

    async def get_screenshot(self, timeout: int = 10) -> Screenshot:
        return (await self.observe(timeout)).screenshot

    async def get_current_app(self) -> str:
        return "unknown (visual iOS observation only)"

    async def execute_observation(
        self,
        observed: Observation,
        *,
        x: int,
        y: int,
        end: tuple[int, int] | None = None,
        duration_ms: int = 100,
    ) -> dict[str, Any]:
        """Use the common observation without losing RMQ1's frame/lease guard."""
        if (
            observed.frame is None
            or not self.observations.is_current(observed, max_age_seconds=30)
            or self.pending_writes
            or self.outcome_unknown
        ):
            raise StaleObservationError("iPhone observation is no longer current")
        ref = observed.frame
        shot = observed.screenshot
        with Image.open(
            io.BytesIO(base64.b64decode(shot.base64_data, validate=True))
        ) as raw:
            pixels = raw.convert("RGB")
        native = AIObservation(
            shot,
            pixels,
            ref.session_id,
            ref.geometry_id,
            ref.frame_id,
            ref.width,
            ref.height,
            dict(zip(("x", "y", "width", "height"), ref.content_rect, strict=True)),
        )
        self.observations.action_started()
        try:
            # execute obtains a fresh transport frame, compares scene and target,
            # then binds input to that exact frame through LiveGesture.
            return await self.execute(
                native, x=x, y=y, end=end, duration_ms=duration_ms
            )
        finally:
            self.observations.action_completed()

    async def execute(
        self,
        observed: AIObservation,
        *,
        x: int,
        y: int,
        end: tuple[int, int] | None = None,
        duration_ms: int = 100,
    ) -> dict[str, Any]:
        await self.validate_owner()
        if self.pending_writes or self.outcome_unknown:
            raise RMQ1Error("设备忙碌或需要人工恢复")
        fresh = await self.observe()
        points = [(x, y)] + ([end] if end else [])
        if not scene_stable(observed, fresh, points):
            raise SceneChanged("界面或目标已变化，已丢弃旧动作")
        await self.validate_owner()
        # The fresh frame is revalidated again by LiveGesture just before HID.
        start = fresh.point(x, y)
        last = fresh.point(*end) if end else start
        action_id = uuid4().hex
        task = asyncio.create_task(
            asyncio.to_thread(self._perform, fresh, action_id, start, last, duration_ms)
        )
        self._pending = task
        try:
            result = await asyncio.shield(task)
            if result.get("outcome") == "unknown":
                self._uncertain = True
            return result
        except asyncio.CancelledError:
            self.cancel_input()
            # The worker retains the adapter and durable marker until settlement.
            task.add_done_callback(self._settled)
            raise
        except Exception:
            self._uncertain = True
            raise

    def _settled(self, task: asyncio.Task[dict[str, Any]]) -> None:
        if task.cancelled():
            self._uncertain = True
            return
        try:
            self._uncertain |= task.result().get("outcome") == "unknown"
        except Exception:
            self._uncertain = True

    def _perform(
        self,
        frame: AIObservation,
        action_id: str,
        start: tuple[float, float],
        end: tuple[float, float],
        duration_ms: int,
    ) -> dict[str, Any]:
        if self._cancelled.is_set():
            return {"outcome": "rejected", "success": False, "cancelled": True}
        started, done = threading.Event(), threading.Event()
        result: dict[str, Any] = {}

        def notify(event: dict[str, Any]) -> None:
            if event["type"] == "started":
                started.set()
            elif event["type"] == "result":
                result.update(event)
                done.set()

        live = LiveGesture(
            self.rmq1,
            action_id=action_id,
            session_id=frame.session_id,
            frame_id=frame.frame_id,
            point=start,
            notify=notify,
        )
        self._live = live
        try:
            live.start()
            deadline = time.monotonic() + 12
            while not started.wait(0.01):
                if done.is_set():
                    return result
                if self._cancelled.is_set() or time.monotonic() >= deadline:
                    live.cancel()
                    live.join()
                    return result or {"outcome": "unknown", "success": False}
            steps = max(1, round(duration_ms / 30))
            for n in range(1, steps + 1):
                if self._cancelled.wait(duration_ms / 1000 / steps):
                    live.cancel()
                    break
                point = tuple(a + (b - a) * n / steps for a, b in zip(start, end))
                live.update(point, finish=n == steps)  # type: ignore[arg-type]
            if not done.wait(4):
                live.cancel()
            live.join()
            return result or {"outcome": "unknown", "success": False}
        except RMQ1Error as error:
            return {"outcome": "rejected", "success": False, "error": str(error)}
        finally:
            self._live = None

    def cancel_input(self) -> None:
        self._cancelled.set()
        if self._live is not None:
            self._live.cancel()

    async def drain(self, timeout: float = 5) -> bool:
        if self._pending is None:
            return True
        try:
            result = await asyncio.wait_for(asyncio.shield(self._pending), timeout)
            self._uncertain |= result.get("outcome") == "unknown"
            return not self._uncertain
        except (TimeoutError, asyncio.CancelledError):
            return False
        except Exception:
            self._uncertain = True
            return False

    def recover(self) -> None:
        if self.pending_writes or self.rmq1.recovery_required:
            raise RMQ1Error("请先核对并恢复 RMQ1 控制")
        self._uncertain = False
