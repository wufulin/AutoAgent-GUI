"""One bounded, non-replayable manual pointer stream on the retained HID channel."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from AutoGLM_GUI.devices.rmq1 import RMQ1Device
from AutoGLM_GUI.devices.rmq1_transport import RMQ1Error


class _Cancelled(RMQ1Error):
    pass


class LiveGesture:
    MAX_HOLD_SECONDS = 1.5
    MOVE_INTERVAL = 1 / 60

    def __init__(
        self,
        device: RMQ1Device,
        *,
        action_id: str,
        session_id: str,
        frame_id: str,
        point: tuple[float, float],
        notify: Callable[[dict[str, Any]], None],
    ):
        self.device = device
        self.action_id = action_id
        self.session_id = session_id
        self.frame_id = frame_id
        self.point = point
        self.notify = notify
        self._condition = threading.Condition()
        self._latest: tuple[float, float] | None = None
        self._end = False
        self._cancelled = False
        self.done = threading.Event()
        self._aborted = threading.Event()
        self._hold_deadline: float | None = time.monotonic() + 12
        self._cancelled_at: float | None = None
        self._original_control: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.device._operation.acquire(blocking=False):
            raise RMQ1Error("另一个操作正在执行")
        self._thread = threading.Thread(target=self._run, daemon=True)
        try:
            threading.Thread(target=self._watchdog, daemon=True).start()
            self._thread.start()
        except BaseException:
            self.done.set()
            self.device._operation.release()
            raise

    def update(self, point: tuple[float, float], *, finish: bool = False) -> None:
        with self._condition:
            if self.done.is_set() or self._end or self._cancelled:
                return
            # At most one pending MOVE, and UP carries its own final point.
            self._latest = point
            self._end = finish
            self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            if self._cancelled_at is None:
                self._cancelled_at = time.monotonic()
            self._condition.notify_all()

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                self._abort_transport()
                # HTTP setup calls have a 5s timeout. The aborted flag forbids
                # later HID writes even if setup finishes after this join.
                self._thread.join(timeout=6)

    def _abort_transport(self) -> None:
        self._aborted.set()
        control = self._original_control
        if control is not None:
            with self.device._frames_lock:
                self.device._issue = "RMQ1 控制通道超时，请重新连接"
                self.device._frames_lock.notify_all()
        raw = getattr(control, "socket", None)
        if raw is not None:
            # Do not call WebSocket.close(): it can wait on the protocol mutex
            # held by a stuck sendall(). Shutdown the ORIGINAL socket instead.
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                raw.close()
            except OSError:
                pass

    def _watchdog(self) -> None:
        while not self.done.wait(0.025):
            now = time.monotonic()
            if self.device._stop.is_set() or (
                self._hold_deadline is not None and now >= self._hold_deadline
            ):
                self.cancel()
            if self._cancelled_at is not None and now - self._cancelled_at >= 0.5:
                self._abort_transport()
                return

    def _ensure_active(self) -> None:
        if self._aborted.is_set():
            raise RMQ1Error("RMQ1 控制通道未及时释放，请核对手机状态")
        if self._cancelled or self.device._stop.is_set():
            raise _Cancelled("操作已取消")

    def _run(self) -> None:
        device = self.device
        signature = repr(("live", self.session_id, self.frame_id, self.point))
        control: Any = None
        acquired = False
        wrote_marker = False
        result: dict[str, Any] = {
            "type": "result",
            "action_id": self.action_id,
            "success": False,
            "outcome": "rejected",
            "recovery_required": device.recovery_required,
        }
        try:
            if self.action_id in device._actions:
                previous, cached = device._actions[self.action_id]
                if previous != signature:
                    raise RMQ1Error("操作 ID 已被其他请求使用")
                result = {"type": "result", **cached}
                return
            if len(device._actions) >= 1000:
                raise RMQ1Error("本次连接操作次数已达上限，请重新连接")
            frame = device._validate_frame(self.session_id, self.frame_id)
            first = frame.geometry.map_point(*self.point, frame.width, frame.height)
            self._ensure_active()
            acquired = device._control_io.acquire(timeout=2)
            if not acquired:
                raise RMQ1Error("RMQ1 控制通道忙，请等待新画面")
            control = device._control
            self._original_control = control
            if control is None:
                raise RMQ1Error("RMQ1 控制通道未连接")
            control.send(json.dumps({"event_type": "ping", "event": {}}))
            device._await_control_event(control, "pong", device._hid_state)
            device._check_hid_state(device._hid_state)
            if not device._frame_current(frame):
                raise RMQ1Error("画面已过期，请等待新画面")
            self._ensure_active()
            device._input_active = True
            device._persist_marker(self.action_id)
            wrote_marker = True
            result.update(
                outcome="unknown",
                recovery_required=True,
                error="操作结果未知，请核对手机并恢复控制；不要重复操作",
            )
            device._actions[self.action_id] = (signature, result.copy())
            # Bound even a stuck initial positioning report; no button is down
            # yet. The stricter hold deadline starts immediately before DOWN.
            self._hold_deadline = time.monotonic() + 2
            device._move(control, first)
            if device._mouse_output == "usb_hybrid":
                # Preserve iOS hybrid endpoint settling and revalidation.
                with self._condition:
                    self._condition.wait_for(lambda: self._cancelled, timeout=0.08)
                self._ensure_active()
                device.check_hid()
                geometry = device.geometry()
                control.send(json.dumps({"event_type": "ping", "event": {}}))
                device._await_control_event(control, "pong", device._hid_state)
                if geometry != frame.geometry or not device._frame_current(frame):
                    raise RMQ1Error("画面或输入状态已变化，请等待新画面")
            self._ensure_active()
            self._hold_deadline = time.monotonic() + self.MAX_HOLD_SECONDS
            control.send(b"\x02\x01left")
            down_at = time.monotonic()
            deadline = down_at + self.MAX_HOLD_SECONDS
            last_move_at = down_at
            last_point = self.point
            self.notify({"type": "started", "action_id": self.action_id})
            while True:
                self._ensure_active()
                now = time.monotonic()
                if now >= deadline:
                    raise _Cancelled("操作达到 1500ms，已释放输入")
                if not device._frame_current(frame):
                    raise RMQ1Error("画面已过期或尺寸变化，已停止输入")
                with self._condition:
                    ending = self._end
                    point = self._latest
                    # Final UP always takes priority over pacing/coalescing.
                    if point is not None and (
                        ending or now - last_move_at >= self.MOVE_INTERVAL
                    ):
                        self._latest = None
                    else:
                        point = None
                if point is not None and point != last_point:
                    device._move(
                        control,
                        frame.geometry.map_point(*point, frame.width, frame.height),
                    )
                    last_point = point
                    last_move_at = time.monotonic()
                if ending and now - down_at >= 0.05:
                    break
                with self._condition:
                    self._condition.wait(
                        min(self.MOVE_INTERVAL, max(0, deadline - time.monotonic()))
                    )
            control.send(b"\x02\x00left")
            control.send(json.dumps({"event_type": "ping", "event": {}}))
            device._await_control_event(control, "pong", device._hid_state)
            if self._aborted.is_set():
                raise RMQ1Error("RMQ1 控制通道未及时释放")
            device._marker.unlink()
            result = {
                "type": "result",
                "action_id": self.action_id,
                "success": True,
                "outcome": "completed",
                "receipt_scope": "kvm_transport",
                "recovery_required": False,
            }
        except Exception as error:
            if wrote_marker:
                # Release on the SAME channel. An ambiguous write is never
                # reconnected/replayed; keep its durable recovery marker.
                try:
                    control.send(b"\x02\x00left")
                    if isinstance(error, _Cancelled):
                        control.send(json.dumps({"event_type": "ping", "event": {}}))
                        device._await_control_event(control, "pong", device._hid_state)
                        if self._aborted.is_set():
                            raise RMQ1Error("RMQ1 控制通道未及时释放")
                        device._marker.unlink()
                        result = {
                            "type": "result",
                            "action_id": self.action_id,
                            "success": True,
                            "outcome": "completed",
                            "cancelled": True,
                            "receipt_scope": "kvm_transport",
                            "recovery_required": False,
                        }
                except Exception:
                    pass
                if result["outcome"] == "unknown":
                    try:
                        control.close()
                    except Exception:
                        pass
            elif isinstance(error, _Cancelled):
                # Cancellation during validation/preparation is a completed
                # no-input operation, not a device failure. Preserve its ID
                # so a repeated DOWN cannot turn that cancellation into input.
                result.update(
                    success=True,
                    outcome="completed",
                    cancelled=True,
                    receipt_scope="no_input",
                    recovery_required=device.recovery_required,
                )
                device._actions[self.action_id] = (signature, result.copy())
            else:
                result["error"] = (
                    str(error)
                    if isinstance(error, RMQ1Error)
                    else "RMQ1 操作失败，请检查连接"
                )
                result["recovery_required"] = device.recovery_required
        finally:
            if wrote_marker:
                device._actions[self.action_id] = (signature, result.copy())
                with device._frames_lock:
                    device._observed.clear()
                    device._input_finished_at = time.monotonic()
            if acquired:
                device._control_io.release()
            device._input_active = False
            device._operation.release()
            self.done.set()
            self.notify(result)
