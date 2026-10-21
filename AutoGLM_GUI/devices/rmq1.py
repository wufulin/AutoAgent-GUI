"""RMQ1 iPhone preview and bounded manual gestures (no Android emulation)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import av
from PIL import Image

from AutoGLM_GUI.devices.rmq1_transport import RMQ1Error, RMQ1Transport


@dataclass(frozen=True)
class Geometry:
    width: int
    height: int
    orientation: str

    @classmethod
    def parse(cls, config: dict[str, Any]) -> Geometry:
        settings = config.get("remote_device_settings", {})
        kind = settings.get("type", "")
        if kind not in (
            "iphone_16_15_15pro",
            "iphone_17_17pro_16pro",
            "iphone_17pro_max_16pro_max",
        ) and not (
            kind == "other_apple_model"
            and config.get("custom_apple_device_type") == "iphone"
        ):
            raise RMQ1Error("请先在 RMQ1 控制台选择正确的 iPhone 型号")
        ratio = (
            config.get("custom_apple_resolution")
            if kind == "other_apple_model"
            else settings.get("ratio")
        )
        orientation = config.get("mobile_orientation")
        if (
            not isinstance(ratio, list)
            or len(ratio) != 2
            or not all(type(v) is int and 100 <= v <= 16384 for v in ratio)
            or orientation
            not in (
                "portrait",
                "landscape",
                "portrait_system_landscape",
                "portrait_system_landscape_flipped",
                "landscape_system_portrait",
                "landscape_system_portrait_flipped",
            )
        ):
            raise RMQ1Error("RMQ1 的分辨率或旋转设置不支持")
        width, height = reversed(ratio) if orientation.startswith("portrait") else ratio
        if orientation == "landscape":
            orientation = "landscape_system_portrait"
        return cls(width, height, orientation)

    def content_rect(self, frame_width: int, frame_height: int) -> dict[str, float]:
        """Visible phone bounds in normalized, unrotated capture coordinates."""
        scale = min(frame_width / self.width, frame_height / self.height)
        w, h = self.width * scale / frame_width, self.height * scale / frame_height
        return {"x": (1 - w) / 2, "y": (1 - h) / 2, "width": w, "height": h}

    def map_point(
        self, x: float, y: float, frame_width: int, frame_height: int
    ) -> tuple[float, float]:
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise RMQ1Error("坐标超出画面")
        rect = self.content_rect(frame_width, frame_height)
        x, y = (x - 0.5) / rect["width"] + 0.5, (y - 0.5) / rect["height"] + 0.5
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise RMQ1Error("请在手机画面内操作，不能点击黑边")
        if self.orientation in (
            "landscape_system_portrait",
            "portrait_system_landscape_flipped",
        ):
            return 1 - y, x
        if self.orientation in (
            "portrait_system_landscape",
            "landscape_system_portrait_flipped",
        ):
            return y, 1 - x
        return x, y


@dataclass(frozen=True)
class ObservedFrame:
    at: float
    width: int
    height: int
    geometry: Geometry


class RMQ1Device:
    def __init__(self, transport: RMQ1Transport, storage_dir: Path):
        self.transport = transport
        self.device_id = f"rmq1:{transport.host}"
        self.session_id = uuid4().hex
        # Key recovery by host, not the certificate, so re-pinning cannot bypass it.
        self._marker = storage_dir / (
            "rmq1-" + hashlib.sha256(transport.host.encode()).hexdigest() + ".pending"
        )
        self._operation = threading.Lock()
        self._control_io = threading.Lock()
        self._control: Any = None
        self._control_worker: threading.Thread | None = None
        self._hid_state: dict[str, Any] = {}
        self._mouse_output: str | None = None
        self._frames_lock = threading.Condition()
        self._stop = threading.Event()
        self._video: Any = None
        self._worker: threading.Thread | None = None
        self._latest: tuple[float, Image.Image | str, int, int] | None = None
        self._preview_lock = threading.Lock()
        self._preview_geometry: tuple[float, Geometry] | None = None
        self._jpeg: tuple[float, str] | None = None
        self._observed: OrderedDict[str, ObservedFrame] = OrderedDict()
        self._actions: dict[str, tuple[str, dict[str, Any]]] = {}
        self._issue = ""
        self._input_finished_at = 0.0
        self._input_active = False

    @property
    def recovery_required(self) -> bool:
        return self._marker.exists()

    @property
    def online(self) -> bool:
        with self._frames_lock:
            return bool(
                not self._stop.is_set()
                and not self._issue
                and self._latest
                and time.monotonic() - self._latest[0] < 3
            )

    def geometry(self) -> Geometry:
        return Geometry.parse(
            self.transport.result("/api/system/get_config").get("config", {})
        )

    def check_hid(self) -> None:
        self._check_hid_state(self.transport.result("/api/hid"))

    def _check_hid_state(
        self, hid: dict[str, Any], *, initializing: bool = False
    ) -> bool:
        mouse = hid.get("mouse", {})
        output = mouse.get("outputs", {}).get("active")
        if self._mouse_output is not None and output != self._mouse_output:
            self._issue = "RMQ1 鼠标模式已变化，请重新连接"
            raise RMQ1Error(self._issue)
        # Hybrid keeps absolute positioning, but routes buttons separately.
        # Relative and touch outputs require different input semantics.
        if hid.get("connected") is not True:
            raise RMQ1Error("RMQ1 的 USB 连接未就绪，请确认 iPhone 已连接并解锁")
        if mouse.get("absolute") is not True or output not in ("usb", "usb_hybrid"):
            raise RMQ1Error("请在 RMQ1 控制台启用绝对或混合鼠标")
        self._mouse_output = output
        if mouse.get("online") is not True:
            if initializing:
                return False
            raise RMQ1Error("RMQ1 鼠标 HID 未就绪，请保持 iPhone 解锁并重新连接")
        return True

    def start(self) -> None:
        self._preview_geometry = (time.monotonic(), self.geometry())
        # GLKVM marks a previously offline HID online only after a successful
        # report write. Opening the control session clears/releases HID and can
        # refresh that stale flag. Validate USB/configuration first, then require
        # an online HID event before starting video or exposing this device.
        self._check_hid_state(self.transport.result("/api/hid"), initializing=True)
        # GLKVM's on-demand streamer requires a live /ws stream client.
        # Retain this same channel for input; opening/closing another channel
        # would clear HID state in the firmware.
        self._control = self.transport.websocket("/api/ws")
        self._await_control_event(
            self._control, "hid", self._hid_state, initializing=True
        )
        self._control_worker = threading.Thread(target=self._keep_control, daemon=True)
        self._control_worker.start()
        self._video = self.transport.websocket("/api/media/ws")
        self._worker = threading.Thread(target=self._read_video, daemon=True)
        self._worker.start()
        deadline = time.monotonic() + 10
        with self._frames_lock:
            while not self._latest and not self._issue:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RMQ1Error("未收到 RMQ1 画面，请确认 iPhone 已解锁并输出视频")
                self._frames_lock.wait(remaining)
            if self._issue:
                raise RMQ1Error(self._issue)

    def _keep_control(self) -> None:
        while not self._stop.wait(1):
            if not self._control_io.acquire(timeout=0.1):
                continue
            try:
                self._control.send(json.dumps({"event_type": "ping", "event": {}}))
                self._await_control_event(self._control, "pong", self._hid_state)
            except Exception:
                with self._frames_lock:
                    self._issue = "RMQ1 控制通道已断开，请重新连接"
                    self._frames_lock.notify_all()
                self._control.close()
                return
            finally:
                self._control_io.release()

    def _read_video(self) -> None:
        try:
            decoder = av.CodecContext.create("h264", "r")
            started = False
            last_image = 0.0
            while not self._stop.is_set():
                packet = self._video.recv(timeout=5)
                if isinstance(packet, str):
                    value = json.loads(packet)
                    if value.get("event_type") == "media":
                        if started:
                            raise RMQ1Error("RMQ1 视频格式已变化，请重新连接")
                        profile = value.get("event", {}).get("video", {}).get("h264")
                        if not profile:
                            raise RMQ1Error("RMQ1 不支持 H.264 视频")
                        self._video.send(
                            json.dumps(
                                {
                                    "event_type": "start",
                                    "event": {"type": "video", "format": "h264"},
                                }
                            )
                        )
                        started = True
                    continue
                if packet and packet[0] == 255:
                    continue
                if not started or len(packet) < 3 or packet[0] != 1:
                    raise RMQ1Error("RMQ1 视频协议不支持")
                for decoded in decoder.decode(av.Packet(packet[2:])):
                    if decoded.width * decoded.height > 16_777_216:
                        raise RMQ1Error("RMQ1 视频分辨率过大")
                    now = time.monotonic()
                    if now - last_image < 0.05:
                        continue
                    # Decode once, retain only the latest image. PNG is encoded
                    # on demand for the legacy snapshot API, never per frame.
                    image = decoded.to_image()
                    with self._frames_lock:
                        self._latest = (
                            now,
                            image,
                            decoded.width,
                            decoded.height,
                        )
                        self._frames_lock.notify_all()
                    last_image = now
                self._video.send(b"\x00")
        except Exception:
            with self._frames_lock:
                self._latest = None
                self._issue = "RMQ1 视频已断开，请重新连接"
                self._frames_lock.notify_all()
        finally:
            if self._video:
                self._video.close()

    @staticmethod
    def _encode(image: Image.Image | str, format: str) -> str:
        if isinstance(image, str):
            return image
        output = io.BytesIO()
        image.save(
            output, format=format, **({"quality": 80} if format == "JPEG" else {})
        )
        return base64.b64encode(output.getvalue()).decode()

    def _frame_payload(
        self, image: str, frame: ObservedFrame, frame_id: str
    ) -> dict[str, Any]:
        return {
            "success": True,
            "image": image,
            "width": frame.width,
            "height": frame.height,
            "content_rect": frame.geometry.content_rect(frame.width, frame.height),
            "geometry_id": f"{frame.geometry.width}:{frame.geometry.height}:{frame.geometry.orientation}",
            "session_id": self.session_id,
            "frame_id": frame_id,
            "recovery_required": self.recovery_required and not self._input_active,
            "input_active": self._input_active,
        }

    def acknowledge_preview(self, frame_id: str, frame: ObservedFrame) -> None:
        with self._frames_lock:
            if not self.online or time.monotonic() - frame.at >= 3:
                raise RMQ1Error(self._issue or "RMQ1 画面已过期")
            self._observed[frame_id] = frame
            while len(self._observed) > 128:
                self._observed.popitem(last=False)

    def preview(
        self, after: float, timeout: float = 1
    ) -> tuple[dict[str, Any], ObservedFrame] | None:
        deadline = time.monotonic() + timeout
        with self._frames_lock:
            while self.online and self._latest and self._latest[0] <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._frames_lock.wait(remaining)
            if not self.online or not self._latest:
                raise RMQ1Error(self._issue or "RMQ1 画面已过期")
            at, image, width, height = self._latest
        # Rendering metadata can be cached. DOWN independently fetches current
        # geometry and HID, so this cache never authorizes stale coordinates.
        with self._preview_lock:
            if (
                self._preview_geometry is None
                or time.monotonic() - self._preview_geometry[0] >= 1
            ):
                self._preview_geometry = (time.monotonic(), self.geometry())
            geometry = self._preview_geometry[1]
            if self._jpeg is None or self._jpeg[0] != at:
                self._jpeg = (at, self._encode(image, "JPEG"))
            encoded = self._jpeg[1]
        frame = ObservedFrame(at, width, height, geometry)
        return {
            "type": "frame",
            "mime": "image/jpeg",
            **self._frame_payload(encoded, frame, uuid4().hex),
        }, frame

    def observe(self) -> dict[str, Any]:
        geometry = self.geometry()
        with self._frames_lock:
            if not self.online or not self._latest:
                raise RMQ1Error(self._issue or "RMQ1 画面已过期")
            at, image, width, height = self._latest
            if at <= self._input_finished_at:
                raise RMQ1Error("正在等待操作后的新画面")
        frame = ObservedFrame(at, width, height, geometry)
        frame_id = uuid4().hex
        self.acknowledge_preview(frame_id, frame)
        return self._frame_payload(self._encode(image, "PNG"), frame, frame_id)

    def _validate_frame(self, session_id: str, frame_id: str) -> ObservedFrame:
        if self.recovery_required:
            raise RMQ1Error("上次操作结果未知，请先核对手机并恢复控制")
        if session_id != self.session_id or not self.online:
            raise RMQ1Error("连接或画面已失效，请刷新画面")
        with self._frames_lock:
            frame = self._observed.get(frame_id)
        if not frame or time.monotonic() - frame.at >= 3:
            raise RMQ1Error("画面已过期，请等待新画面后操作")
        if frame.at <= self._input_finished_at:
            raise RMQ1Error("正在等待操作后的新画面")
        geometry = self.geometry()
        self.check_hid()
        if geometry != frame.geometry or not self._frame_current(frame):
            raise RMQ1Error("画面或旋转设置已变化，请等待新画面")
        return frame

    def _frame_current(self, frame: ObservedFrame) -> bool:
        with self._frames_lock:
            return bool(
                self.online
                and time.monotonic() - frame.at < 3
                and self._latest
                and self._latest[2:] == (frame.width, frame.height)
                and (
                    self._preview_geometry is None
                    or self._preview_geometry[0] < frame.at
                    or self._preview_geometry[1] == frame.geometry
                )
            )

    def _persist_marker(self, action_id: str) -> None:
        self._marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(action_id)
            file.flush()
            os.fsync(file.fileno())
        if os.name == "posix":
            directory = os.open(self._marker.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def cached_action(self, action_id: str, signature: str) -> dict[str, Any] | None:
        """Read a receipt without reacquiring input authority or replaying HID."""
        cached = self._actions.get(action_id)
        if cached is None:
            return None
        previous, result = cached
        if previous != signature:
            raise RMQ1Error("操作 ID 已被其他请求使用")
        return result.copy()

    def gesture(
        self,
        *,
        action_id: str,
        session_id: str,
        frame_id: str,
        start: tuple[float, float],
        end: tuple[float, float] | None,
        duration_ms: int,
    ) -> dict[str, Any]:
        if not self._operation.acquire(blocking=False):
            raise RMQ1Error("另一个操作正在执行")
        try:
            signature = repr((session_id, frame_id, start, end, duration_ms))
            if action_id in self._actions:
                previous, result = self._actions[action_id]
                if signature != previous:
                    raise RMQ1Error("操作 ID 已被其他请求使用")
                return result
            if len(self._actions) >= 1000:
                raise RMQ1Error("本次连接操作次数已达上限，请重新连接")
            frame = self._validate_frame(session_id, frame_id)
            geometry = frame.geometry
            first = geometry.map_point(*start, frame.width, frame.height)
            last = geometry.map_point(*(end or start), frame.width, frame.height)
            if not 50 <= duration_ms <= 1500:
                raise RMQ1Error("手势时长不支持")
            # Persist before any HID write; a crash never makes uncertain input
            # eligible for automatic replay, including after a new login.
            if not self._control_io.acquire(timeout=2):
                raise RMQ1Error("RMQ1 控制通道忙，请等待新画面")
            control = self._control
            try:
                if control is None:
                    raise RMQ1Error("RMQ1 控制通道未连接")
                control.send(json.dumps({"event_type": "ping", "event": {}}))
                self._await_control_event(control, "pong", self._hid_state)
                self._check_hid_state(self._hid_state)
                if not self.online or time.monotonic() - frame.at >= 3:
                    raise RMQ1Error("画面已过期，请等待新画面")
                self._input_active = True
                self._persist_marker(action_id)
                result: dict[str, Any] = {
                    "success": False,
                    "outcome": "unknown",
                    "action_id": action_id,
                    "error": "操作结果未知，请核对手机并恢复控制；不要重复操作",
                    "recovery_required": True,
                }
                self._actions[action_id] = (signature, result)
                try:
                    self._move(control, first)
                    if self._mouse_output == "usb_hybrid":
                        # iOS must consume absolute motion before a button on
                        # the hybrid device's separate USB endpoint. This is
                        # pacing, not an acknowledgment from the phone.
                        time.sleep(0.08)
                        self.check_hid()
                        current_geometry = self.geometry()
                        control.send(json.dumps({"event_type": "ping", "event": {}}))
                        self._await_control_event(control, "pong", self._hid_state)
                        if (
                            not self.online
                            or time.monotonic() - frame.at >= 3
                            or current_geometry != geometry
                            or not self._latest
                            or self._latest[2:] != (frame.width, frame.height)
                        ):
                            raise RMQ1Error("画面或输入状态已变化，请等待新画面")
                    control.send(b"\x02\x01left")
                    steps = max(1, round(duration_ms / 30)) if end else 1
                    for n in range(1, steps + 1):
                        time.sleep(duration_ms / 1000 / steps)
                        if end:
                            self._move(
                                control,
                                tuple(
                                    a + (b - a) * n / steps for a, b in zip(first, last)
                                ),
                            )
                    control.send(b"\x02\x00left")
                    control.send(json.dumps({"event_type": "ping", "event": {}}))
                    self._await_control_event(control, "pong", self._hid_state)
                    self._marker.unlink()
                    result = {
                        "success": True,
                        "outcome": "completed",
                        "action_id": action_id,
                        "receipt_scope": "kvm_transport",
                        "recovery_required": False,
                    }
                    self._actions[action_id] = (signature, result)
                except Exception:
                    # Release only on the original channel. No reconnect/replay.
                    try:
                        control.send(b"\x02\x00left")
                    except Exception:
                        pass
                    control.close()
                with self._frames_lock:
                    self._observed.clear()
                    self._input_finished_at = time.monotonic()
                return result
            finally:
                self._control_io.release()
        finally:
            self._input_active = False
            self._operation.release()

    @staticmethod
    def _move(control: Any, point: tuple[float, ...]) -> None:
        control.send(struct.pack(">Bhh", 3, *(round(v * 65535 - 32768) for v in point)))

    def _await_control_event(
        self,
        control: Any,
        expected: str,
        hid_state: dict[str, Any],
        *,
        initializing: bool = False,
    ) -> None:
        # Drain initial status events so websocket backpressure cannot prevent
        # pong delivery. This acknowledgment only covers the KVM transport.
        deadline = time.monotonic() + (5 if initializing else 2)
        for _ in range(64):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = control.recv(timeout=remaining)
            except TimeoutError:
                break
            if not isinstance(raw, str):
                raise RMQ1Error("RMQ1 控制协议不支持")
            value = json.loads(raw)
            kind = value.get("event_type")
            if kind == "kickout":
                raise RMQ1Error("RMQ1 控制权已被其他客户端占用")
            if kind == "hid":
                change = value.get("event", {})
                mouse = {**hid_state.get("mouse", {}), **change.get("mouse", {})}
                mouse["outputs"] = {
                    **hid_state.get("mouse", {}).get("outputs", {}),
                    **change.get("mouse", {}).get("outputs", {}),
                }
                hid_state.update(change)
                hid_state["mouse"] = mouse
                if not self._check_hid_state(hid_state, initializing=initializing):
                    continue
            if kind == expected:
                return
        if initializing:
            raise RMQ1Error("RMQ1 鼠标 HID 未就绪，请保持 iPhone 解锁并重新连接")
        raise RMQ1Error("RMQ1 未确认控制通道")

    def recover(self, session_id: str) -> None:
        # A confirmation applies only to the idle state the user observed.
        # Queuing behind input could erase a NEW unknown result produced after
        # that confirmation, so recovery must reject active input immediately.
        if not self._operation.acquire(blocking=False):
            raise RMQ1Error("另一个操作正在执行，请等待结束并核对手机后再恢复")
        try:
            if self._stop.is_set() or session_id != self.session_id:
                raise RMQ1Error("连接已失效")
            # No gesture can be active during this explicit human action.
            self._marker.unlink(missing_ok=True)
            with self._frames_lock:
                self._observed.clear()
        finally:
            self._operation.release()

    def close(self) -> None:
        # Signal active input before waiting for the operation lock.
        self._stop.set()
        with self._frames_lock:
            self._frames_lock.notify_all()
        with self._operation:
            if self._control:
                self._control.close()
            if self._control_worker:
                self._control_worker.join(timeout=4)
            if self._video:
                self._video.close()
            if self._worker:
                self._worker.join(timeout=6)
            self.transport.close()
            with self._frames_lock:
                self._latest = None
                self._observed.clear()
