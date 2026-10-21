"""Explicit RMQ1 connection, observation and bounded manual control routes."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from AutoGLM_GUI.device_manager import DeviceManager
from AutoGLM_GUI.devices.rmq1_live import LiveGesture
from AutoGLM_GUI.devices.rmq1_transport import (
    RMQ1Error,
    normalize_fingerprint,
    probe_certificate,
    validate_host,
)

router = APIRouter(prefix="/api/rmq1")


class ProbeRequest(BaseModel):
    host: str

    _host = field_validator("host")(validate_host)


class ConnectRequest(ProbeRequest):
    fingerprint: str
    username: str = Field(default="admin", min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=1024)

    _fingerprint = field_validator("fingerprint")(normalize_fingerprint)


class DeviceRequest(BaseModel):
    device_id: str = Field(pattern=r"^rmq1:", max_length=64)


class RecoveryRequest(DeviceRequest):
    session_id: str = Field(min_length=32, max_length=32)
    confirmed_idle: Literal[True]


Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class GestureRequest(DeviceRequest):
    action_id: UUID
    session_id: str = Field(min_length=32, max_length=32)
    frame_id: str = Field(min_length=32, max_length=32)
    start: tuple[Coordinate, Coordinate]
    end: tuple[Coordinate, Coordinate] | None = None
    duration_ms: int = Field(default=100, ge=50, le=1500)


def safe_error(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=str(error)
        if isinstance(error, RMQ1Error)
        else "RMQ1 操作失败，请检查连接",
    )


@router.post("/probe")
def probe(request: ProbeRequest) -> dict[str, str]:
    try:
        return {"fingerprint": probe_certificate(request.host)}
    except Exception as error:
        raise safe_error(error) from None


@router.post("/connect")
def connect(request: ConnectRequest) -> dict[str, Any]:
    try:
        serial = DeviceManager.get_instance().add_rmq1_device(
            request.host,
            request.fingerprint,
            request.username,
            request.password.get_secret_value(),
        )
        return {"success": True, "serial": serial}
    except Exception as error:
        raise safe_error(error) from None


@router.post("/disconnect")
def disconnect(request: DeviceRequest) -> dict[str, bool]:
    try:
        DeviceManager.get_instance().remove_rmq1_device(request.device_id)
        return {"success": True}
    except Exception as error:
        raise safe_error(error) from None


@router.post("/observe")
def observe(request: DeviceRequest) -> dict[str, Any]:
    try:
        return DeviceManager.get_instance().get_rmq1_device(request.device_id).observe()
    except Exception as error:
        raise safe_error(error) from None


@router.post("/gesture", response_model=None)
def gesture(request: GestureRequest) -> dict[str, Any] | JSONResponse:
    device = None
    try:
        device = DeviceManager.get_instance().get_rmq1_device(request.device_id)
        return device.gesture(
            action_id=str(request.action_id),
            session_id=request.session_id,
            frame_id=request.frame_id,
            start=request.start,
            end=request.end,
            duration_ms=request.duration_ms,
        )
    except RMQ1Error as error:
        # Gesture catches all exceptions after its durable marker/HID boundary.
        # These validation failures therefore represent no new HID writes.
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "action_id": str(request.action_id),
                "outcome": "rejected",
                "error": str(error),
                "detail": str(error),
                "recovery_required": device.recovery_required if device else False,
            },
        )
    except Exception as error:
        raise safe_error(error) from None


@router.post("/recover")
def recover(request: RecoveryRequest) -> dict[str, bool]:
    try:
        DeviceManager.get_instance().get_rmq1_device(request.device_id).recover(
            request.session_id
        )
        return {"success": True}
    except Exception as error:
        raise safe_error(error) from None


class PointerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: UUID


class PointerDown(PointerMessage):
    type: Literal["down"]
    session_id: str = Field(min_length=32, max_length=32)
    frame_id: str = Field(min_length=32, max_length=32)
    point: tuple[Coordinate, Coordinate]


class PointerUpdate(PointerMessage):
    type: Literal["move", "up"]
    point: tuple[Coordinate, Coordinate]


class PointerCancel(PointerMessage):
    type: Literal["cancel"]


_pointer_message = TypeAdapter(
    Annotated[PointerDown | PointerUpdate | PointerCancel, Field(discriminator="type")]
)


def _trusted_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # Native, non-browser clients do not send an Origin.
    allowed = [
        value.strip()
        for value in os.getenv(
            "AUTOGLM_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",")
    ]
    if origin in allowed or "*" in allowed:
        return True
    parsed = urlsplit(origin)
    return bool(
        parsed.scheme == ("https" if websocket.url.scheme == "wss" else "http")
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.netloc == websocket.headers.get("host")
    )


async def _open_socket(websocket: WebSocket):
    if not _trusted_origin(websocket):
        await websocket.close(code=1008)
        return None
    await websocket.accept()
    try:
        request = DeviceRequest(device_id=websocket.query_params.get("device_id", ""))
        return DeviceManager.get_instance().get_rmq1_device(request.device_id)
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, RMQ1Error)
            else "RMQ1 连接不可用，请重新连接"
        )
        await websocket.send_json({"type": "error", "error": message})
        await websocket.close(code=1008)
        return None


async def _receive_bounded(websocket: WebSocket) -> str:
    raw = await websocket.receive_text()
    if len(raw) > 4096:
        raise RMQ1Error("RMQ1 输入消息超过限制")
    return raw


@router.websocket("/preview")
async def preview_socket(websocket: WebSocket) -> None:
    device = await _open_socket(websocket)
    if device is None:
        return
    after = 0.0
    last_sent = 0.0
    try:
        while True:
            await asyncio.sleep(max(0, 0.05 - (time.monotonic() - last_sent)))
            snapshot = await asyncio.to_thread(device.preview, after)
            if snapshot is None:
                continue
            payload, frame = snapshot
            await asyncio.wait_for(websocket.send_json(payload), timeout=3)
            last_sent = time.monotonic()
            # One image in flight. Slow renderers skip old frames rather than
            # building a queue. Only an explicitly rendered frame enables DOWN.
            raw = await asyncio.wait_for(_receive_bounded(websocket), timeout=5)
            ack = json.loads(raw)
            if ack != {"type": "ack", "frame_id": payload["frame_id"]}:
                raise RMQ1Error("RMQ1 画面确认无效")
            device.acknowledge_preview(payload["frame_id"], frame)
            await asyncio.wait_for(
                websocket.send_json({"type": "ready", "frame_id": payload["frame_id"]}),
                timeout=3,
            )
            after = frame.at
    except WebSocketDisconnect:
        pass
    except Exception as error:
        try:
            await asyncio.wait_for(
                websocket.send_json(
                    {
                        "type": "error",
                        "error": str(error)
                        if isinstance(error, RMQ1Error)
                        else "RMQ1 预览已断开，请重新连接",
                    }
                ),
                timeout=1,
            )
            await websocket.close(code=1008)
        except Exception:
            pass


@router.websocket("/control")
async def control_socket(websocket: WebSocket) -> None:
    device = await _open_socket(websocket)
    if device is None:
        return
    loop = asyncio.get_running_loop()
    updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
    active: LiveGesture | None = None
    send_lock = asyncio.Lock()

    async def send(value: dict[str, Any]) -> None:
        async with send_lock:
            await asyncio.wait_for(websocket.send_json(value), timeout=3)

    def notify(value: dict[str, Any]) -> None:
        def publish() -> None:
            if not updates.full():
                updates.put_nowait(value)
            elif active is not None:
                active.cancel()

        loop.call_soon_threadsafe(publish)

    async def writer() -> None:
        while True:
            await send(await updates.get())

    async def reader() -> None:
        nonlocal active
        while True:
            raw = await _receive_bounded(websocket)
            try:
                message = _pointer_message.validate_json(raw)
            except ValidationError:
                raise RMQ1Error("RMQ1 输入消息格式无效") from None
            action_id = str(message.action_id)
            if (
                isinstance(message, PointerDown)
                and active is not None
                and not active.done.is_set()
                and active.action_id == action_id
            ):
                if (message.session_id, message.frame_id, message.point) == (
                    active.session_id,
                    active.frame_id,
                    active.point,
                ):
                    # A repeated DOWN is still in flight, never a terminal
                    # rejection that could make the client forget a held key.
                    continue
                raise RMQ1Error("操作 ID 已被其他请求使用")
            try:
                if isinstance(message, PointerDown):
                    if active is not None and not active.done.is_set():
                        raise RMQ1Error("另一个操作正在执行")
                    candidate = LiveGesture(
                        device,
                        action_id=action_id,
                        session_id=message.session_id,
                        frame_id=message.frame_id,
                        point=message.point,
                        notify=notify,
                    )
                    candidate.start()
                    active = candidate
                elif active is None or active.action_id != action_id:
                    raise RMQ1Error("操作 ID 不匹配")
                elif isinstance(message, PointerCancel):
                    active.cancel()
                else:
                    active.update(message.point, finish=message.type == "up")
            except RMQ1Error as error:
                await send(
                    {
                        "type": "result",
                        "action_id": action_id,
                        "success": False,
                        "outcome": "rejected",
                        "recovery_required": device.recovery_required,
                        "error": str(error),
                    }
                )

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as error:
        try:
            await send(
                {
                    "type": "error",
                    "error": str(error)
                    if isinstance(error, RMQ1Error)
                    else "RMQ1 控制已断开，请重新连接",
                }
            )
            await websocket.close(code=1008)
        except Exception:
            pass
    finally:
        # Signal cancellation synchronously before any cancellable await.
        if active is not None:
            active.cancel()
        for task in tasks:
            task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            if active is not None:
                await asyncio.to_thread(active.join)
