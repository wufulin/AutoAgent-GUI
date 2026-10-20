"""Socket.IO server for Scrcpy video streaming."""

from __future__ import annotations

import asyncio
from collections import deque
import time
from typing import Any

from typing import NotRequired
from typing_extensions import TypedDict

import socketio

from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.scrcpy_protocol import SCRCPY_CODEC_NAME_TO_ID, ScrcpyMediaStreamPacket
from AutoGLM_GUI.scrcpy_stream import ScrcpyStreamer
from AutoGLM_GUI.scrcpy_control import ControlCommand, ControlSession


class VideoPacketPayload(TypedDict):
    type: str
    data: bytes
    timestamp: int
    keyframe: NotRequired[bool | None]
    epoch: NotRequired[int]
    pts: NotRequired[int | None]


sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    server_kwargs={"socketio_path": "/socket.io"},
)

_socket_streamers: dict[str, ScrcpyStreamer] = {}
_stream_tasks: dict[str, asyncio.Task[None]] = {}
_control_sessions: dict[str, ControlSession] = {}
_watchdogs: dict[str, asyncio.Task[None]] = {}
_connected_sids: set[str] = set()
_startup_tokens: dict[str, object] = {}
_device_locks: dict[
    str, asyncio.Lock
] = {}  # Lock per device to prevent concurrent connections


async def _stop_stream_for_sid(
    sid: str, expected: ScrcpyStreamer | None = None
) -> None:
    if expected is not None and _socket_streamers.get(sid) is not expected:
        return  # A delayed finalizer must not stop a replacement session.
    _startup_tokens.pop(sid, None)
    task = _stream_tasks.pop(sid, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    watchdog = _watchdogs.pop(sid, None)
    if watchdog and watchdog is not asyncio.current_task():
        watchdog.cancel()
    session = _control_sessions.pop(sid, None)
    streamer = _socket_streamers.pop(sid, None)
    if session:
        try:
            await session.close()
        except Exception:
            logger.warning("Control release unconfirmed; device remains blocked")
    if streamer:
        streamer.stop()


async def _watch_control(sid: str, session: ControlSession) -> None:
    try:
        while _control_sessions.get(sid) is session:
            await asyncio.sleep(0.5)
            if await session.expire():
                await sio.emit(
                    "control-error",
                    {"message": "Gesture expired; input released"},
                    to=sid,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        await sio.emit(
            "control-error",
            {
                "message": "Release unconfirmed; check the phone before recovery",
                "uncertain": True,
            },
            to=sid,
        )


@sio.on("control-input")  # type: ignore[misc]
async def control_input(sid: str, data: dict[str, Any]) -> dict[str, object]:
    session = _control_sessions.get(sid)
    if not session:
        return {"status": "rejected", "error": "No control session"}
    try:
        command = ControlCommand.model_validate(data)
        return await session.execute(command)
    except Exception as exc:
        return {
            "status": "unknown" if session.failed else "rejected",
            "error": str(exc),
            "sequence": session.sequence,
        }


@sio.on("control-recover")  # type: ignore[misc]
async def control_recover(sid: str, data: dict[str, Any]) -> dict[str, str]:
    session = _control_sessions.get(sid)
    if (
        not session
        or data.get("session") != session.id
        or data.get("confirmed") is not True
    ):
        return {"status": "rejected"}
    # The browser requires explicit confirmation that the phone has no held touch.
    await session.manager.recover_manual_control(session.device_id)
    return {"status": "recovered"}


def _classify_error(exc: Exception) -> dict[str, Any]:
    """Classify error and return user-friendly message."""
    error_str = str(exc)

    if "Address already in use" in error_str or (
        "Port" in error_str and "occupied" in error_str
    ):
        return {
            "message": "端口冲突，视频流端口仍被占用。通常会自动解决，如果持续出现请重启应用。",
            "type": "port_conflict",
            "technical_details": error_str,
        }
    elif "Device" in error_str and (
        "not available" in error_str or "not found" in error_str
    ):
        return {
            "message": "设备无响应，请检查 USB/WiFi 连接。",
            "type": "device_offline",
            "technical_details": error_str,
        }
    elif "timeout" in error_str.lower() or "timed out" in error_str.lower():
        return {
            "message": "连接超时，请检查设备连接后重试。",
            "type": "timeout",
            "technical_details": error_str,
        }
    elif "Failed to connect" in error_str:
        return {
            "message": "无法连接到 scrcpy 服务器，请检查设备连接。",
            "type": "connection_failed",
            "technical_details": error_str,
        }
    else:
        return {
            "message": error_str,
            "type": "unknown",
            "technical_details": error_str,
        }


def stop_streamers(device_id: str | None = None) -> None:
    """Stop active scrcpy streamers (all or by device)."""
    sids = list(_socket_streamers.keys())
    for sid in sids:
        streamer = _socket_streamers.get(sid)
        if not streamer:
            continue
        if device_id and streamer.device_id != device_id:
            continue
        if sid in _control_sessions:
            asyncio.create_task(_stop_stream_for_sid(sid))
            continue
        task = _stream_tasks.pop(sid, None)
        if task:
            task.cancel()
        streamer.stop()
        _socket_streamers.pop(sid, None)


async def stop_streamers_async(device_id: str | None = None) -> None:
    """Complete release and shutdown before acknowledging a reset."""
    for sid, streamer in list(_socket_streamers.items()):
        if device_id is None or streamer.device_id == device_id:
            await _stop_stream_for_sid(sid, expected=streamer)


async def _stream_packets(
    sid: str, streamer: ScrcpyStreamer, video_window: int = 1
) -> None:
    pending: deque[tuple[asyncio.Future[None], float]] = deque()

    async def wait_for_frame() -> None:
        frame, deadline = pending.popleft()
        await asyncio.wait_for(
            frame, timeout=max(0, deadline - asyncio.get_running_loop().time())
        )

    try:
        async for packet in streamer.iter_packets():
            payload = _packet_to_payload(packet)
            session = _control_sessions.get(sid)
            if session:
                if packet.type == "configuration":
                    # Reject old input as soon as a geometry change is known,
                    # then drain old renders before configuring the decoder.
                    session.epoch += 1
                    while pending:
                        await wait_for_frame()
                payload["epoch"] = session.epoch
                if video_window == 1 or packet.type == "configuration":
                    await sio.call("video-data", payload, to=sid, timeout=3)
                else:
                    # One producer emits complete binary events in order. Allow
                    # a bounded decode pipeline, with an ACK for each render.
                    rendered = asyncio.get_running_loop().create_future()
                    pending.append((rendered, asyncio.get_running_loop().time() + 3))

                    def acknowledge(
                        *_: Any, frame: asyncio.Future[None] = rendered
                    ) -> None:
                        if not frame.done():
                            frame.set_result(None)

                    await sio.emit("video-data", payload, to=sid, callback=acknowledge)
                    if len(pending) >= video_window:
                        await wait_for_frame()
            else:
                await sio.emit("video-data", payload, to=sid)
        while pending:
            await wait_for_frame()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Video streaming failed: %s", exc)
        try:
            await sio.emit("error", {"message": str(exc)}, to=sid)
        except Exception as emit_exc:
            logger.debug(
                "Failed to emit Socket.IO stream error to %s: %s", sid, emit_exc
            )
    finally:
        for frame, _ in pending:
            frame.cancel()
        await _stop_stream_for_sid(sid, expected=streamer)


def _packet_to_payload(packet: ScrcpyMediaStreamPacket) -> VideoPacketPayload:
    payload: VideoPacketPayload = {
        "type": packet.type,
        "data": packet.data,
        "timestamp": int(time.time() * 1000),
    }
    if packet.type == "data":
        payload["keyframe"] = packet.keyframe
        payload["pts"] = packet.pts
    return payload


@sio.event
async def connect(sid: str, environ: dict[str, Any]) -> None:
    _connected_sids.add(sid)
    logger.info("Socket.IO client connected: %s", sid)


@sio.event
async def disconnect(sid: str) -> None:
    _connected_sids.discard(sid)
    logger.info("Socket.IO client disconnected: %s", sid)
    await _stop_stream_for_sid(sid)


@sio.on("connect-device")  # type: ignore[misc]
async def connect_device(sid: str, data: dict[str, Any] | None) -> None:
    payload = data or {}
    device_id = payload.get("device_id") or payload.get("deviceId")
    if not device_id:
        await sio.emit(
            "error",
            {"message": "Device ID is required", "type": "invalid_request"},
            to=sid,
        )
        return

    if str(device_id).startswith("rmq1:"):
        await sio.emit("error", {"message": "请使用 RMQ1 预览面板"}, to=sid)
        return

    controlled = payload.get("control") is True
    try:
        max_size = max(320, min(1920, int(payload.get("maxSize") or 1280)))
        bit_rate = max(1000, min(8_000_000, int(payload.get("bitRate") or 4_000_000)))
        max_fps = max(10, min(60, int(payload.get("maxFps") or 20)))
        video_window = max(1, min(4, int(payload.get("videoWindow") or 1)))
    except (TypeError, ValueError):
        await sio.emit("error", {"message": "Invalid video options"}, to=sid)
        return

    # Stop any existing stream for this sid
    await _stop_stream_for_sid(sid)

    if controlled and sid not in _connected_sids:
        return
    token = object()
    _startup_tokens[sid] = token

    # Get or create a lock for this device
    if device_id not in _device_locks:
        _device_locks[device_id] = asyncio.Lock()

    device_lock = _device_locks[device_id]

    # Acquire lock to prevent concurrent connections to the same device
    async with device_lock:
        if _startup_tokens.get(sid) is not token:
            return
        logger.debug(f"Acquired device lock for {device_id}, sid: {sid}")

        # Stop any existing streams for the same device (from other sids)
        sids_to_stop = [
            s
            for s, streamer in _socket_streamers.items()
            if s != sid and streamer.device_id == device_id
        ]
        for s in sids_to_stop:
            logger.info(f"Stopping existing stream for device {device_id} from sid {s}")
            await _stop_stream_for_sid(s)

        if controlled:
            streamer = ScrcpyStreamer(
                device_id=device_id,
                max_size=max_size,
                bit_rate=bit_rate,
                control_enabled=True,
                max_fps=max_fps,
            )
        else:
            streamer = ScrcpyStreamer(
                device_id=device_id, max_size=max_size, bit_rate=bit_rate
            )

        try:
            await streamer.start()  # ScrcpyStreamer has built-in retry logic
            metadata = await asyncio.wait_for(
                streamer.read_video_metadata(), timeout=10
            )
            # Older clients and non-H.264 decoders retain one-frame flow control.
            if not controlled or metadata.codec != SCRCPY_CODEC_NAME_TO_ID["h264"]:
                video_window = 1
            if _startup_tokens.get(sid) is not token:
                streamer.stop()
                return
            session = ControlSession(device_id, streamer) if controlled else None
            _socket_streamers[sid] = streamer
            if session:
                _control_sessions[sid] = session
            await sio.emit(
                "video-metadata",
                {
                    "deviceName": metadata.device_name,
                    "width": metadata.width,
                    "height": metadata.height,
                    "codec": metadata.codec,
                    **(
                        {
                            "session": session.id,
                            "recoveryRequired": await session.manager.manual_recovery_required(
                                device_id
                            ),
                        }
                        if session
                        else {}
                    ),
                },
                to=sid,
            )

            if _startup_tokens.get(sid) is not token:
                await _stop_stream_for_sid(sid, expected=streamer)
                return
            if session:
                _watchdogs[sid] = asyncio.create_task(_watch_control(sid, session))
            _stream_tasks[sid] = asyncio.create_task(
                _stream_packets(sid, streamer, video_window)
            )

        except BaseException as exc:
            await _stop_stream_for_sid(sid, expected=streamer)
            streamer.stop()
            if not isinstance(exc, Exception):
                raise
            logger.exception("Failed to start scrcpy stream: %s", exc)
            # Use unified error classification
            error_info = _classify_error(exc)
            await sio.emit("error", error_info, to=sid)
