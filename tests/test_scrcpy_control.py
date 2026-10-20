"""Protocol and failure-boundary regression tests without a physical device."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock

import pytest

from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager
from AutoGLM_GUI.scrcpy_control import ControlCommand, ControlSession, encode_touch

pytestmark = [pytest.mark.contract, pytest.mark.release_gate]


@pytest.fixture
def manager(monkeypatch):
    instance = PhoneAgentManager()
    monkeypatch.setattr(PhoneAgentManager, "_instance", instance)
    return instance


def cmd(session, sequence, action, **kwargs):
    return ControlCommand(
        session=session.id,
        sequence=sequence,
        action=action,
        x=10,
        y=20,
        width=600,
        height=1200,
        epoch=session.epoch,
        **kwargs,
    )


def test_touch_wire_format_matches_scrcpy_333():
    data = encode_touch("down", 123, 456, 600, 1200)
    assert len(data) == 32
    assert struct.unpack(">BBQiiHHHII", data) == (
        2,
        0,
        0,
        123,
        456,
        600,
        1200,
        65535,
        0,
        0,
    )
    assert struct.unpack(">BBQiiHHHII", encode_touch("cancel", 1, 2, 600, 1200))[
        1:3
    ] == (3, 0)


@pytest.mark.anyio
async def test_sequence_and_ownership(manager):
    transport = AsyncMock()
    session = ControlSession("phone", transport)
    assert (await session.execute(cmd(session, 1, "down")))["status"] == "sent"
    with pytest.raises(ValueError, match="Out-of-order"):
        await session.execute(cmd(session, 3, "up"))
    with pytest.raises(Exception, match="manual control"):
        await manager.acquire_device_async("phone")
    await session.execute(cmd(session, 2, "move"))
    await session.execute(cmd(session, 3, "up"))
    assert not manager._manual_owners
    assert [c.args[0][1] for c in transport.send_control.call_args_list] == [0, 2, 1]
    with pytest.raises(ValueError, match="No active gesture"):
        await session.execute(cmd(session, 4, "move"))


@pytest.mark.anyio
async def test_disconnect_and_idle_expiry_release_once(manager):
    transport = AsyncMock()
    session = ControlSession("phone", transport)
    await session.execute(cmd(session, 1, "down"))
    session.last_activity -= 3
    assert await session.expire()
    await session.close()
    assert [c.args[0][1] for c in transport.send_control.call_args_list] == [0, 3]
    assert not manager._manual_owners
    transport.cancel_pending_touch.assert_not_awaited()


@pytest.mark.anyio
async def test_held_disconnect_keeps_ownership_until_native_cancel_finishes(manager):
    transport = AsyncMock()
    started, finish = asyncio.Event(), asyncio.Event()

    async def cancel():
        started.set()
        await finish.wait()

    transport.cancel_pending_touch.side_effect = cancel
    session = ControlSession("phone", transport)
    await session.execute(cmd(session, 1, "down"))
    closing = asyncio.create_task(session.close())
    await started.wait()
    with pytest.raises(Exception, match="manual control"):
        await manager.acquire_device_async("phone")
    assert not closing.done()
    finish.set()
    await closing
    assert not manager._manual_owners
    assert [c.args[0][1] for c in transport.send_control.call_args_list] == [0, 3]


@pytest.mark.anyio
async def test_native_cancel_failure_keeps_replacement_session_blocked(manager):
    transport = AsyncMock()
    transport.cancel_pending_touch.side_effect = ConnectionError("offline")
    session = ControlSession("phone", transport)
    await session.execute(cmd(session, 1, "down"))
    with pytest.raises(ConnectionError):
        await session.close()
    assert await manager.manual_recovery_required("phone")
    replacement = ControlSession("phone", AsyncMock())
    with pytest.raises(Exception, match="unknown"):
        await replacement.execute(cmd(replacement, 1, "down"))


@pytest.mark.anyio
@pytest.mark.parametrize("output", ["", "Error: unsupported CANCEL"])
async def test_native_cancel_is_device_bound_and_rejects_shell_error(
    monkeypatch, output
):
    import subprocess
    from AutoGLM_GUI import scrcpy_stream

    execute = AsyncMock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    monkeypatch.setattr(scrcpy_stream, "run_cmd_silently", execute)
    streamer = scrcpy_stream.ScrcpyStreamer("phone", control_enabled=True)
    if output:
        with pytest.raises(RuntimeError, match="not confirmed"):
            await streamer.cancel_pending_touch()
    else:
        await streamer.cancel_pending_touch()
    assert execute.call_args.args[0][-8:] == [
        "-s",
        "phone",
        "shell",
        "input",
        "motionevent",
        "CANCEL",
        "0",
        "0",
    ]
    assert execute.call_args.kwargs["timeout"] == 5


@pytest.mark.anyio
async def test_failed_write_stays_quarantined_until_explicit_recovery(manager):
    transport = AsyncMock()
    transport.send_control.side_effect = ConnectionError("gone")
    session = ControlSession("phone", transport)
    with pytest.raises(ConnectionError):
        await session.execute(cmd(session, 1, "down"))
    await session.close()
    assert await manager.manual_recovery_required("phone")
    replacement = ControlSession("phone", AsyncMock())
    with pytest.raises(Exception, match="unknown"):
        await replacement.execute(cmd(replacement, 1, "down"))
    await manager.recover_manual_control("phone")
    await replacement.execute(cmd(replacement, 2, "down"))
    await replacement.close()


@pytest.mark.anyio
async def test_rotation_rejects_old_epoch_and_releases_with_new_dimensions(manager):
    transport = AsyncMock()
    session = ControlSession("phone", transport)
    await session.execute(cmd(session, 1, "down"))
    old_move = cmd(session, 2, "move")
    session.epoch += 1
    with pytest.raises(ValueError, match="geometry changed"):
        await session.execute(old_move)
    cancel = cmd(session, 3, "cancel").model_copy(update={"width": 1200, "height": 600})
    await session.execute(cancel)
    decoded = struct.unpack(">BBQiiHHHII", transport.send_control.call_args.args[0])
    assert decoded[1:8] == (3, 0, 20, 10, 1200, 600, 0)
    assert not manager._manual_owners


@pytest.mark.anyio
async def test_rotation_without_geometry_cannot_claim_release(manager):
    session = ControlSession("phone", AsyncMock())
    await session.execute(cmd(session, 1, "down"))
    session.epoch += 1
    with pytest.raises(ValueError, match="geometry unknown"):
        await session.close()
    assert await manager.manual_recovery_required("phone")


@pytest.mark.anyio
async def test_server_serializes_concurrent_commands(manager):
    calls = []

    class Transport:
        async def send_control(self, data):
            await asyncio.sleep(0.01)
            calls.append(data[1])

    session = ControlSession("phone", Transport())
    await asyncio.gather(
        *(
            session.execute(cmd(session, i, action))
            for i, action in enumerate(["down", "move", "up"], 1)
        )
    )
    assert calls == [0, 2, 1]


@pytest.mark.anyio
async def test_ai_blocks_manual_and_invalid_coordinates_do_not_write(manager):
    from AutoGLM_GUI.phone_agent_manager import AgentState
    from types import SimpleNamespace

    manager._metadata["phone:chat"] = SimpleNamespace(
        device_id="phone", state=AgentState.BUSY
    )
    transport = AsyncMock()
    session = ControlSession("phone", transport)
    with pytest.raises(Exception, match="AI is operating"):
        await session.execute(cmd(session, 1, "down"))
    with pytest.raises(ValueError, match="outside"):
        await session.execute(cmd(session, 2, "down").model_copy(update={"x": 600}))
    transport.send_control.assert_not_called()


@pytest.mark.anyio
async def test_nonzero_adb_exit_is_not_success(monkeypatch):
    import subprocess
    import AutoGLM_GUI.adb_plus.touch as touch

    async def fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "permission denied")

    monkeypatch.setattr(touch, "run_cmd_silently", fail)
    for execute in (
        touch.touch_down_async,
        touch.touch_move_async,
        touch.touch_up_async,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            await execute(1, 2, "phone")


@pytest.mark.anyio
async def test_control_socket_forward_is_unique_and_shutdown_closes_writer(monkeypatch):
    import subprocess
    from AutoGLM_GUI.scrcpy_stream import ScrcpyStreamer
    import AutoGLM_GUI.scrcpy_stream as module

    calls = []

    async def run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "12345\n", "")

    monkeypatch.setattr(module, "run_cmd_silently", run)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    stream = ScrcpyStreamer("phone", control_enabled=True)
    await stream._setup_port_forward()
    assert calls[0][-2] == "tcp:0"
    assert calls[0][-1].startswith("localabstract:scrcpy_")
    assert stream.port == 12345
    assert stream._build_server_options().control
    from unittest.mock import Mock

    stream.control_writer = Mock()
    writer = stream.control_writer
    writer.is_closing.return_value = False
    writer.drain = AsyncMock()
    await stream.send_control(b"input")
    writer.write.assert_called_once_with(b"input")
    stream.stop()
    writer.close.assert_called_once()


@pytest.mark.anyio
async def test_disconnect_during_startup_does_not_leave_a_stream(monkeypatch, manager):
    import AutoGLM_GUI.socketio_server as server
    from types import SimpleNamespace

    entered, resume = asyncio.Event(), asyncio.Event()
    instances = []

    class Stream:
        def __init__(self, **kwargs):
            self.device_id = kwargs["device_id"]
            self.stopped = False
            instances.append(self)

        async def start(self):
            entered.set()
            await resume.wait()

        async def read_video_metadata(self):
            return SimpleNamespace(
                device_name="test", width=600, height=1200, codec=0x68323634
            )

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(server, "ScrcpyStreamer", Stream)
    monkeypatch.setattr(server.sio, "emit", AsyncMock())
    await server.connect("startup-test", {})
    task = asyncio.create_task(
        server.connect_device("startup-test", {"device_id": "phone", "control": True})
    )
    await entered.wait()
    await server.disconnect("startup-test")
    resume.set()
    await task
    assert instances[0].stopped
    assert "startup-test" not in server._socket_streamers
    assert "startup-test" not in server._control_sessions


@pytest.mark.anyio
async def test_video_waits_for_browser_render_ack(monkeypatch, manager):
    import AutoGLM_GUI.socketio_server as server
    from AutoGLM_GUI.scrcpy_protocol import ScrcpyMediaStreamPacket
    from unittest.mock import Mock

    entered, resume = asyncio.Event(), asyncio.Event()
    reads = []

    class Stream:
        device_id = "phone"
        stop = Mock()

        async def send_control(self, data):
            pass

        async def iter_packets(self):
            for i in range(2):
                reads.append(i)
                yield ScrcpyMediaStreamPacket(type="data", data=b"frame")

    stream = Stream()
    session = ControlSession("phone", stream)
    server._control_sessions["ack-test"] = session
    server._socket_streamers["ack-test"] = stream

    async def call(*args, **kwargs):
        entered.set()
        await resume.wait()

    monkeypatch.setattr(server.sio, "call", call)
    task = asyncio.create_task(server._stream_packets("ack-test", stream))
    await entered.wait()
    assert reads == [0]
    resume.set()
    await task
    assert reads == [0, 1]
    stream.stop.assert_called_once()


@pytest.mark.anyio
async def test_http_and_streamed_control_share_one_owner(manager, monkeypatch):
    from AutoGLM_GUI.legacy_control import legacy_touch, _gestures, _locks

    _gestures.clear()
    _locks.clear()
    session = ControlSession("phone", AsyncMock())
    await legacy_touch("down", "phone", 1, 2, 0, AsyncMock())
    with pytest.raises(Exception, match="Another controller"):
        await session.execute(cmd(session, 1, "down"))
    await legacy_touch("up", "phone", 1, 2, 0, AsyncMock())
    await session.execute(cmd(session, 2, "down"))
    with pytest.raises(Exception, match="Another controller"):
        await legacy_touch("down", "phone", 1, 2, 0, AsyncMock())
    await session.close()


@pytest.mark.anyio
async def test_cancelled_http_request_retains_owner_until_adb_finishes(manager):
    import threading
    from AutoGLM_GUI.legacy_control import manual_operation, _operations

    entered, finish = threading.Event(), threading.Event()

    def execute():
        entered.set()
        assert finish.wait(3)

    request = asyncio.create_task(manual_operation("phone", execute))
    await asyncio.to_thread(entered.wait, 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    with pytest.raises(Exception, match="manual control"):
        await manager.acquire_device_async("phone")
    finish.set()
    await asyncio.gather(*list(_operations))
    assert not manager._manual_owners


@pytest.mark.anyio
async def test_old_finalizer_cannot_cancel_a_new_startup_token(monkeypatch):
    import AutoGLM_GUI.socketio_server as server

    token = object()
    monkeypatch.setitem(server._startup_tokens, "replacement", token)
    await server._stop_stream_for_sid("replacement", expected=object())
    assert server._startup_tokens["replacement"] is token


@pytest.mark.anyio
async def test_video_pipeline_is_bounded_and_keeps_configuration_barrier(
    manager, monkeypatch
):
    import AutoGLM_GUI.socketio_server as server
    from AutoGLM_GUI.scrcpy_protocol import ScrcpyMediaStreamPacket
    from unittest.mock import Mock

    reads = []
    acknowledgements = []
    payloads = []
    full = asyncio.Event()
    config_read = asyncio.Event()
    next_geometry = asyncio.Event()

    class Stream:
        device_id = "phone"
        stop = Mock()

        async def send_control(self, data):
            pass

        async def iter_packets(self):
            for i in range(4):
                reads.append(i)
                yield ScrcpyMediaStreamPacket(type="data", data=b"frame")
            config_read.set()
            yield ScrcpyMediaStreamPacket(type="configuration", data=b"config")
            yield ScrcpyMediaStreamPacket(type="data", data=b"new-frame")

    stream = Stream()
    session = ControlSession("phone", stream)
    server._control_sessions["window-test"] = session
    server._socket_streamers["window-test"] = stream

    async def emit(event, payload, *, to, callback=None):
        assert event == "video-data"
        payloads.append(payload)
        acknowledgements.append(callback)
        if len(acknowledgements) == 4:
            full.set()
        if payload["data"] == b"new-frame":
            next_geometry.set()

    async def call(event, payload, **kwargs):
        assert payload["type"] == "configuration"
        assert all(frame["epoch"] == 0 for frame in payloads)
        payloads.append(payload)

    monkeypatch.setattr(server.sio, "emit", emit)
    monkeypatch.setattr(server.sio, "call", call)
    task = asyncio.create_task(server._stream_packets("window-test", stream, 4))
    await asyncio.wait_for(full.wait(), 1)
    assert reads == [0, 1, 2, 3]
    assert not config_read.is_set()
    acknowledgements[0]()
    await asyncio.wait_for(config_read.wait(), 1)
    assert session.epoch == 1
    with pytest.raises(ValueError, match="Video geometry changed"):
        await session.execute(cmd(session, 1, "down").model_copy(update={"epoch": 0}))
    assert len(payloads) == 4
    for acknowledge in acknowledgements[1:]:
        acknowledge()
    await asyncio.wait_for(next_geometry.wait(), 1)
    assert [packet["epoch"] for packet in payloads] == [0, 0, 0, 0, 1, 1]
    acknowledgements[-1]()
    await asyncio.wait_for(task, 1)
    stream.stop.assert_called_once()
    assert "window-test" not in server._control_sessions


@pytest.mark.anyio
async def test_video_pipeline_cancellation_releases_session(manager, monkeypatch):
    import AutoGLM_GUI.socketio_server as server
    from AutoGLM_GUI.scrcpy_protocol import ScrcpyMediaStreamPacket
    from unittest.mock import Mock

    full = asyncio.Event()
    callbacks = []

    class Stream:
        device_id = "phone"
        stop = Mock()

        async def send_control(self, data):
            pass

        async def iter_packets(self):
            for _ in range(5):
                yield ScrcpyMediaStreamPacket(type="data", data=b"frame")

    stream = Stream()
    server._socket_streamers["cancel-window"] = stream
    server._control_sessions["cancel-window"] = ControlSession("phone", stream)

    async def emit(event, payload, *, to, callback):
        callbacks.append(callback)
        if len(callbacks) == 4:
            full.set()

    monkeypatch.setattr(server.sio, "emit", emit)
    task = asyncio.create_task(server._stream_packets("cancel-window", stream, 4))
    await asyncio.wait_for(full.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for callback in callbacks:
        callback()  # Late render ACKs after cancellation must be harmless.
    stream.stop.assert_called_once()
    assert "cancel-window" not in server._control_sessions


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("requested", "codec", "expected"),
    [(999, 0x68323634, 4), (None, 0x68323634, 1), (4, 0x68323635, 1)],
)
async def test_video_window_negotiation_preserves_legacy_clients(
    manager, monkeypatch, requested, codec, expected
):
    import AutoGLM_GUI.socketio_server as server
    from types import SimpleNamespace
    from unittest.mock import Mock

    pumped = asyncio.Event()
    windows = []

    class Stream:
        stop = Mock()

        def __init__(self, **kwargs):
            self.device_id = kwargs["device_id"]

        async def start(self):
            pass

        async def read_video_metadata(self):
            return SimpleNamespace(
                device_name="phone", width=600, height=1200, codec=codec
            )

    async def pump(sid, stream, video_window=1):
        windows.append(video_window)
        pumped.set()

    monkeypatch.setattr(server, "ScrcpyStreamer", Stream)
    monkeypatch.setattr(server, "_stream_packets", pump)
    monkeypatch.setattr(server.sio, "emit", AsyncMock())
    await server.connect("negotiated", {})
    await server.connect_device(
        "negotiated", {"device_id": "phone", "control": True, "videoWindow": requested}
    )
    await asyncio.wait_for(pumped.wait(), 1)
    assert windows == [expected]
    await server.disconnect("negotiated")


@pytest.mark.anyio
async def test_video_window_missing_render_ack_stops_the_session(manager, monkeypatch):
    import AutoGLM_GUI.socketio_server as server
    from AutoGLM_GUI.scrcpy_protocol import ScrcpyMediaStreamPacket
    from unittest.mock import Mock

    callbacks = []
    errors = []
    original_wait_for = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        if type(awaitable) is asyncio.Future:
            timeout = min(timeout, 0.01)
        return await original_wait_for(awaitable, timeout)

    class Stream:
        device_id = "phone"
        stop = Mock()

        async def send_control(self, data):
            pass

        async def iter_packets(self):
            for _ in range(4):
                yield ScrcpyMediaStreamPacket(type="data", data=b"frame")

    async def emit(event, payload, *, to, callback=None):
        if event == "video-data":
            callbacks.append(callback)
        elif event == "error":
            errors.append(payload)

    stream = Stream()
    server._socket_streamers["timeout-window"] = stream
    server._control_sessions["timeout-window"] = ControlSession("phone", stream)
    monkeypatch.setattr(server.sio, "emit", emit)
    monkeypatch.setattr(server.asyncio, "wait_for", bounded_wait)
    await server._stream_packets("timeout-window", stream, 4)
    assert len(callbacks) == 4
    assert len(errors) == 1
    assert "timeout-window" not in server._control_sessions
    stream.stop.assert_called_once()
    for callback in callbacks:
        callback()
