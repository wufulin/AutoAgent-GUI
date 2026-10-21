"""RMQ1 contracts: no real phone/network required."""

from __future__ import annotations

import json
import struct
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from AutoGLM_GUI.api import rmq1 as api
from AutoGLM_GUI.device_manager import DeviceManager
from AutoGLM_GUI.devices.rmq1 import Geometry, RMQ1Device
from AutoGLM_GUI.devices.rmq1_transport import (
    RMQ1Error,
    RMQ1Transport,
    tls_socket,
    validate_host,
)


class FakeSocket:
    def __init__(self, output="usb"):
        self.output = output
        self.sent = []
        self.closed = False
        self.fail = False
        self.received = 0

    def send(self, value):
        self.sent.append(value)
        if self.fail:
            raise OSError("private transport details")

    def ping(self):
        return SimpleNamespace(wait=lambda timeout: True)

    def recv(self, **kwargs):
        self.received += 1
        if self.received == 1:
            return json.dumps(
                {
                    "event_type": "hid",
                    "event": {
                        "connected": True,
                        "mouse": {
                            "online": True,
                            "absolute": True,
                            "outputs": {"active": self.output},
                        },
                    },
                }
            )
        return json.dumps({"event_type": "pong", "event": {}})

    def close(self):
        self.closed = True


class FakeTransport:
    host = "192.168.1.20"
    fingerprint = "a" * 64

    def __init__(self, output="usb"):
        self.socket = FakeSocket(output)
        self.output = output
        self.hid_ok = True
        self.config = {
            "remote_device_settings": {
                "type": "iphone_16_15_15pro",
                "ratio": [2556, 1179],
            },
            "mobile_orientation": "portrait",
        }

    def result(self, path):
        if path == "/api/system/get_config":
            return {"config": self.config}
        return {
            "connected": self.hid_ok,
            "mouse": {
                "online": True,
                "absolute": True,
                "outputs": {"active": self.output},
            },
        }

    def websocket(self, path):
        return self.socket

    def close(self):
        pass


def make_device(tmp_path, output="usb"):
    device = RMQ1Device(FakeTransport(output), tmp_path)
    device.check_hid()
    device._latest = (time.monotonic(), "image", 1920, 1080)
    device._control = device.transport.socket
    return device


@pytest.fixture
def device(tmp_path):
    return make_device(tmp_path)


def gesture_args(device):
    frame = device.observe()
    return {
        "action_id": str(uuid4()),
        "session_id": frame["session_id"],
        "frame_id": frame["frame_id"],
        "start": (0.5, 0.5),
        "end": None,
        "duration_ms": 50,
    }


@pytest.mark.parametrize(
    "host",
    [
        "https://192.168.1.1",
        "127.0.0.1",
        "8.8.8.8",
        "::1",
        "localhost",
        "169.254.169.254",
    ],
)
def test_only_literal_private_ipv4(host):
    with pytest.raises(RMQ1Error):
        validate_host(host)


def test_certificate_mismatch_closes_socket_before_http(monkeypatch):
    secured = Mock()
    secured.getpeercert.return_value = b"certificate"
    raw = Mock()
    context = Mock()
    context.wrap_socket.return_value = secured
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: raw)
    monkeypatch.setattr("ssl.SSLContext", lambda *a: context)
    with pytest.raises(RMQ1Error, match="证书"):
        tls_socket("192.168.1.20", "a" * 64)
    secured.close.assert_called_once()
    secured.send.assert_not_called()


def test_password_is_only_sent_after_pin_verification(monkeypatch):
    def reject(*args):
        raise RMQ1Error("证书已变化")

    monkeypatch.setattr("AutoGLM_GUI.devices.rmq1_transport.tls_socket", reject)
    request = Mock()
    monkeypatch.setattr("http.client.HTTPSConnection.request", request)
    transport = RMQ1Transport("192.168.1.20", "a" * 64)
    with pytest.raises(RMQ1Error):
        transport.login("admin", "test-password")
    request.assert_not_called()


def test_geometry_removes_black_bars_and_rotates():
    portrait = Geometry(100, 200, "portrait")
    assert portrait.map_point(0.5, 0.5, 1920, 1080) == (0.5, 0.5)
    with pytest.raises(RMQ1Error, match="黑边"):
        portrait.map_point(0.05, 0.5, 1920, 1080)
    landscape = Geometry(200, 100, "landscape_system_portrait")
    assert landscape.map_point(0.25, 0.75, 200, 100) == (0.25, 0.25)
    with pytest.raises(RMQ1Error):
        Geometry.parse({"remote_device_settings": {"type": "computer_or_other"}})


@pytest.mark.parametrize(
    "phone, capture, expected",
    [
        ((100, 200), (1920, 1080), (0.359375, 0, 0.28125, 1)),
        ((200, 100), (1080, 1920), (0, 0.359375, 1, 0.28125)),
        ((100, 200), (100, 200), (0, 0, 1, 1)),
    ],
)
def test_observation_exposes_phone_region_without_changing_input_coordinates(
    device, phone, capture, expected
):
    device.transport.config["remote_device_settings"]["ratio"] = list(reversed(phone))
    at, image, _, _ = device._latest
    device._latest = (at, image, *capture)
    observation = device.observe()
    rect = observation["content_rect"]
    assert tuple(rect[k] for k in ("x", "y", "width", "height")) == pytest.approx(
        expected
    )
    assert (observation["width"], observation["height"]) == capture
    assert observation["image"] == image
    geometry = device.geometry()
    for x, y in ((0.1, 0.2), (0.9, 0.8), (0.5, 0.5)):
        raw = (rect["x"] + x * rect["width"], rect["y"] + y * rect["height"])
        assert geometry.map_point(*raw, *capture) == pytest.approx((x, y))


def test_tap_deduplicates_original_action_and_releases(device):
    args = gesture_args(device)
    first = device.gesture(**args)
    assert first["success"] and first["receipt_scope"] == "kvm_transport"
    assert device.transport.socket.sent == [
        json.dumps({"event_type": "ping", "event": {}}),
        struct.pack(">Bhh", 3, 0, 0),
        b"\x02\x01left",
        b"\x02\x00left",
        json.dumps({"event_type": "ping", "event": {}}),
    ]
    assert device.gesture(**args) == first
    assert len(device.transport.socket.sent) == 5
    assert not device.recovery_required
    assert not device.transport.socket.closed
    with pytest.raises(RMQ1Error, match="ID"):
        device.gesture(**{**args, "end": (0.5, 0.6)})


@pytest.mark.parametrize("end", [None, (0.5, 0.7)])
def test_hybrid_gesture_positions_before_press_and_releases(tmp_path, monkeypatch, end):
    device = make_device(tmp_path, "usb_hybrid")
    events = []
    send = device.transport.socket.send

    def record_send(value):
        events.append(value)
        send(value)

    device.transport.socket.send = record_send
    monkeypatch.setattr(time, "sleep", lambda seconds: events.append(("wait", seconds)))
    args = {**gesture_args(device), "end": end}
    result = device.gesture(**args)
    assert result["success"] and not result["recovery_required"]
    move = events.index(struct.pack(">Bhh", 3, 0, 0))
    down = events.index(b"\x02\x01left")
    up = events.index(b"\x02\x00left")
    assert move < events.index(("wait", 0.08)) < down < up
    assert json.dumps({"event_type": "ping", "event": {}}) in events[move + 1 : down]
    count = len(events)
    assert device.gesture(**args) == result
    assert len(events) == count
    assert not device.recovery_required


@pytest.mark.parametrize("output", ["usb_rel", "usb_touch", "", "future"])
def test_unsupported_mouse_output_is_rejected(tmp_path, output):
    transport = FakeTransport(output)
    with pytest.raises(RMQ1Error, match="鼠标"):
        RMQ1Device(transport, tmp_path).start()
    assert transport.socket.sent == []


@pytest.mark.parametrize(
    "initial,changed", [("usb", "usb_hybrid"), ("usb_hybrid", "usb")]
)
def test_changed_mouse_mode_invalidates_session(tmp_path, initial, changed):
    device = make_device(tmp_path, initial)
    args = gesture_args(device)
    device.transport.output = changed
    with pytest.raises(RMQ1Error, match="模式已变化"):
        device.gesture(**args)
    assert device.transport.socket.sent == []
    device.transport.output = initial
    with pytest.raises(RMQ1Error, match="失效"):
        device.gesture(**args)


@pytest.mark.parametrize(
    "change", ["stale", "geometry", "resolution", "hid", "ws_mode"]
)
def test_hybrid_state_change_during_settle_never_presses(tmp_path, monkeypatch, change):
    device = make_device(tmp_path, "usb_hybrid")
    args = gesture_args(device)

    def change_state(seconds):
        assert seconds == 0.08
        if change == "stale":
            device._latest = (time.monotonic() - 4, "image", 1920, 1080)
        elif change == "geometry":
            device.transport.config["mobile_orientation"] = "landscape"
        elif change == "resolution":
            device._latest = (time.monotonic(), "image", 1080, 1920)
        elif change == "hid":
            device.transport.hid_ok = False
        else:
            # HTTP remains unchanged; the retained control channel reports the
            # new mode before DOWN, including a supported-to-supported change.
            device.transport.socket.output = "usb"
            device.transport.socket.received = 0

    monkeypatch.setattr(time, "sleep", change_state)
    result = device.gesture(**args)
    assert not result["success"] and result["recovery_required"]
    assert b"\x02\x01left" not in device.transport.socket.sent
    assert device.recovery_required
    count = len(device.transport.socket.sent)
    assert device.gesture(**args) == result
    assert len(device.transport.socket.sent) == count


@pytest.mark.parametrize("invalid", ["stale", "rotation", "session", "hid", "blackbar"])
def test_rejected_gestures_send_no_input(device, invalid):
    args = gesture_args(device)
    if invalid == "stale":
        device._observed.clear()
    elif invalid == "rotation":
        device.transport.config["mobile_orientation"] = "landscape"
    elif invalid == "session":
        args["session_id"] = "0" * 32
    elif invalid == "hid":
        device.transport.hid_ok = False
    else:
        args["start"] = (0.01, 0.01)
    with pytest.raises(RMQ1Error):
        device.gesture(**args)
    assert device.transport.socket.sent == []


def test_unknown_is_persistent_and_never_replayed(device, tmp_path):
    args = gesture_args(device)
    send = device.transport.socket.send

    def fail_input(value):
        if isinstance(value, bytes):
            device.transport.socket.fail = True
        send(value)

    device.transport.socket.send = fail_input
    result = device.gesture(**args)
    assert not result["success"]
    assert result["recovery_required"]
    count = len(device.transport.socket.sent)
    assert device.gesture(**args) == result
    assert len(device.transport.socket.sent) == count
    replacement = RMQ1Device(FakeTransport(), tmp_path)
    assert replacement.recovery_required
    with pytest.raises(RMQ1Error):
        replacement.gesture(**args)
    assert "private transport" not in json.dumps(result)
    replacement.recover(replacement.session_id)
    assert not replacement.recovery_required


def test_failed_marker_write_sends_nothing(device, monkeypatch):
    args = gesture_args(device)
    monkeypatch.setattr("os.open", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        device.gesture(**args)
    assert all(isinstance(p, str) for p in device.transport.socket.sent)
    assert not device.transport.socket.closed


def test_windows_gesture_syncs_file_without_opening_directory(device, monkeypatch):
    import os

    args = gesture_args(device)
    opened = []
    sync = Mock(wraps=os.fsync)

    def windows_open(path, *args):
        opened.append(path)
        if path == device._marker.parent:
            raise PermissionError("Windows cannot open a directory")
        return os.open(path, *args)

    # Replace only this module's OS adapter; pathlib/pytest retain the host OS.
    monkeypatch.setattr(
        "AutoGLM_GUI.devices.rmq1.os",
        SimpleNamespace(
            **(vars(os) | {"name": "nt", "open": windows_open, "fsync": sync})
        ),
    )
    assert device.gesture(**args)["success"]
    assert opened == [device._marker]
    sync.assert_called_once()
    assert not device.recovery_required


def test_concurrent_gesture_is_rejected(device):
    args = gesture_args(device)
    with device._operation:
        with pytest.raises(RMQ1Error, match="另一个操作"):
            device.gesture(**args)
    assert device.transport.socket.sent == []


def test_swipe_keeps_button_down_until_final_point(device):
    args = gesture_args(device)
    args.update(start=(0.5, 0.7), end=(0.5, 0.3), duration_ms=90)
    assert device.gesture(**args)["success"]
    packets = device.transport.socket.sent
    assert packets[2] == b"\x02\x01left"
    assert packets[-2] == b"\x02\x00left"
    moves = [
        struct.unpack(">Bhh", p) for p in packets if isinstance(p, bytes) and p[0] == 3
    ]
    assert len(moves) == 4
    assert moves[0][2] > moves[-1][2]
    assert all(a[2] > b[2] for a, b in zip(moves, moves[1:]))
    with pytest.raises(RMQ1Error, match="操作后的新画面"):
        device.observe()


def test_release_failure_quarantines_until_explicit_recovery(device):
    args = gesture_args(device)
    original = device.transport.socket.send

    def send(value):
        if value == b"\x02\x00left":
            raise OSError("release lost")
        original(value)

    device.transport.socket.send = send
    result = device.gesture(**args)
    assert not result["success"]
    assert device.recovery_required
    assert device.transport.socket.closed
    device._latest = (time.monotonic(), "image", 1920, 1080)
    with pytest.raises(RMQ1Error, match="结果未知"):
        device.gesture(**gesture_args(device))


def test_resolution_change_rejects_old_frame(device):
    args = gesture_args(device)
    device._latest = (time.monotonic(), "new-image", 1080, 1920)
    with pytest.raises(RMQ1Error, match="变化"):
        device.gesture(**args)
    assert not device.transport.socket.sent


def test_control_kickout_sends_no_input(device):
    args = gesture_args(device)
    device.transport.socket.recv = lambda **kwargs: json.dumps(
        {"event_type": "kickout"}
    )
    with pytest.raises(RMQ1Error, match="占用"):
        device.gesture(**args)
    assert all(isinstance(p, str) for p in device.transport.socket.sent)


def test_partial_hid_status_preserves_initial_state(device):
    args = gesture_args(device)
    initial = device.transport.socket.recv()
    messages = iter(
        [
            initial,
            json.dumps({"event_type": "hid", "event": {"mouse": {"online": True}}}),
            json.dumps({"event_type": "pong", "event": {}}),
            json.dumps({"event_type": "pong", "event": {}}),
        ]
    )
    device.transport.socket.recv = lambda **kwargs: next(messages)
    assert device.gesture(**args)["success"]
    assert not device.recovery_required


def test_close_invalidates_old_frames(device):
    args = gesture_args(device)
    device.close()
    with pytest.raises(RMQ1Error):
        device.gesture(**args)
    assert device.transport.socket.sent == []


def test_control_heartbeat_detects_kickout_and_stops_input(device):
    device._stop = Mock()
    device._stop.is_set.return_value = False
    device._stop.wait.return_value = False
    device.transport.socket.recv = lambda **kwargs: json.dumps(
        {"event_type": "kickout"}
    )
    device._keep_control()
    assert not device.online
    assert device.transport.socket.closed
    assert all(isinstance(p, str) for p in device.transport.socket.sent)


@pytest.mark.parametrize("cold_hid", [False, True])
@pytest.mark.parametrize("output", ["usb", "usb_hybrid"])
def test_video_decodes_real_h264(monkeypatch, tmp_path, cold_hid, output):
    """Cold start requires the stream session before opening H.264 media."""
    import av
    from PIL import Image
    from fractions import Fraction

    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = 64, 128
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
    frame = av.VideoFrame.from_image(Image.new("RGB", (64, 128), "red"))
    packets = encoder.encode(frame) + encoder.encode(None)
    transport = FakeTransport(output)
    device = RMQ1Device(transport, tmp_path)
    if cold_hid:
        original_result = transport.result

        def result(path):
            value = original_result(path)
            if path == "/api/hid":
                value["mouse"]["online"] = False
            return value

        transport.result = result
        ready = json.loads(transport.socket.recv())
        offline = json.loads(json.dumps(ready))
        offline["event"]["mouse"]["online"] = False
        updates = iter(
            [
                json.dumps(offline),
                json.dumps({"event_type": "hid", "event": {"mouse": {"online": True}}}),
            ]
        )
        original_recv = transport.socket.recv
        transport.socket.recv = lambda **kwargs: next(updates, None) or original_recv(
            **kwargs
        )
    video = FakeSocket()
    opened = []

    def websocket(path):
        opened.append(path)
        if path == "/api/ws":
            return transport.socket
        assert opened == ["/api/ws", "/api/media/ws"]
        assert transport.socket.received > 0
        assert not transport.socket.closed
        assert device._hid_state["mouse"]["online"] is True
        return video

    transport.websocket = websocket
    queue = iter(
        [
            json.dumps(
                {
                    "event_type": "media",
                    "event": {"video": {"h264": {"profile_level_id": "42e01f"}}},
                }
            ),
            *[b"\x01\x01" + bytes(p) for p in packets],
        ]
    )

    def recv(**kwargs):
        value = next(queue, None)
        if value is None:
            device._stop.wait(5)
            return b"\xff"
        return value

    video.recv = recv
    device.start()
    assert device._latest is not None
    assert device._latest[2:] == (64, 128)
    assert not device._issue
    assert not transport.socket.closed
    device.close()
    assert video.closed and transport.socket.closed
    assert not device._worker.is_alive()
    assert not device._control_worker.is_alive()


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "mode_change", "kickout"])
def test_startup_hid_failure_never_opens_video_or_sends_input(tmp_path, failure):
    transport = FakeTransport("usb_hybrid")
    device = RMQ1Device(transport, tmp_path)
    initial = json.loads(transport.socket.recv())
    initial["event"]["mouse"]["online"] = False
    followup = {
        "disconnect": {"event_type": "hid", "event": {"connected": False}},
        "mode_change": {
            "event_type": "hid",
            "event": {"mouse": {"outputs": {"active": "usb"}, "online": True}},
        },
        "kickout": {"event_type": "kickout", "event": {}},
    }
    messages = iter([initial, followup.get(failure)])

    def recv(**kwargs):
        value = next(messages, None)
        if value is None:
            raise TimeoutError
        return json.dumps(value)

    transport.socket.recv = recv
    opened = []

    def websocket(path):
        opened.append(path)
        return transport.socket

    transport.websocket = websocket
    device._marker.write_text("existing-unknown-action")
    try:
        with pytest.raises(RMQ1Error):
            device.start()
        assert opened == ["/api/ws"]
        assert transport.socket.sent == []
        assert not device.online
        assert device._marker.read_text() == "existing-unknown-action"
    finally:
        device.close()
    assert transport.socket.closed


def test_online_hid_is_still_required_for_every_gesture(device):
    args = gesture_args(device)
    original = device.transport.result

    def result(path):
        value = original(path)
        if path == "/api/hid":
            value["mouse"]["online"] = False
        return value

    device.transport.result = result
    with pytest.raises(RMQ1Error, match="HID 未就绪"):
        device.gesture(**args)
    assert device.transport.socket.sent == []
    assert not device.recovery_required


def test_api_validates_coordinates_and_redacts_unexpected_error(device, monkeypatch):
    manager = SimpleNamespace(
        get_rmq1_device=lambda device_id: device,
        add_rmq1_device=Mock(side_effect=OSError("secret-password")),
    )
    monkeypatch.setattr(DeviceManager, "get_instance", lambda: manager)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        args = {**gesture_args(device), "device_id": device.device_id}
        assert (
            client.post("/api/rmq1/gesture", json={**args, "start": [2, 0]}).status_code
            == 422
        )
        response = client.post(
            "/api/rmq1/connect",
            json={
                "host": "192.168.1.20",
                "fingerprint": "a" * 64,
                "password": "secret-password",
            },
        )
        assert response.status_code == 409
        assert "secret-password" not in response.text
        assert (
            client.post(
                "/api/rmq1/recover",
                json={
                    "device_id": device.device_id,
                    "session_id": device.session_id,
                    "confirmed_idle": False,
                },
            ).status_code
            == 422
        )


def test_manager_preserves_rmq1_and_blocks_android_dispatch(monkeypatch, tmp_path):
    from AutoGLM_GUI.device_metadata_manager import DeviceMetadataManager

    monkeypatch.setattr(
        DeviceMetadataManager, "get_instance", lambda: DeviceMetadataManager(tmp_path)
    )
    monkeypatch.setattr(RMQ1Transport, "login", lambda *a: None)
    monkeypatch.setattr(
        RMQ1Device,
        "start",
        lambda self: setattr(self, "_latest", (time.monotonic(), "image", 1920, 1080)),
    )
    manager = DeviceManager()
    serial = manager.add_rmq1_device("192.168.1.20", "a" * 64, "admin", "password")
    manager._adb_conn.list_devices = lambda: []
    manager._enable_mdns_discovery = False
    manager._poll_devices()
    item = manager.get_devices()[0]
    assert item.to_dict()["platform"] == "ios"
    assert item.connection_type.value == "rmq1"
    assert item.status == "device"
    with pytest.raises(ValueError, match="暂不支持 AI"):
        manager.get_device_protocol(serial)
    from AutoGLM_GUI.devices.rmq1_ai import Rmq1AIAdapter

    adapter = manager.get_async_device_protocol(serial)
    assert isinstance(adapter, Rmq1AIAdapter)
    assert adapter.lease_owner == ""  # Creation alone never grants input authority.
    with pytest.raises(RMQ1Error, match="已连接"):
        manager.add_rmq1_device("192.168.1.20", "a" * 64, "admin", "password")
    manager.remove_rmq1_device(serial)
    assert manager.get_serial_by_device_id(serial) is None
    assert not manager.get_devices()


def live_gesture(device, **overrides):
    from AutoGLM_GUI.devices.rmq1_live import LiveGesture
    import queue

    messages = queue.Queue()
    frame = device.observe()
    args = dict(
        action_id=str(uuid4()),
        session_id=device.session_id,
        frame_id=frame["frame_id"],
        point=(0.5, 0.5),
        notify=messages.put,
    )
    args.update(overrides)
    return LiveGesture(device, **args), messages


def test_live_quick_up_preserves_down_minimum_and_latest_final_point(device):
    events = []
    send = device.transport.socket.send

    def record(value):
        events.append((time.monotonic(), value))
        send(value)

    device.transport.socket.send = record
    live, messages = live_gesture(device)
    live.start()
    # UP arrives before asynchronous validation/DOWN has completed.
    live.update((0.5, 0.6), finish=True)
    assert messages.get(timeout=2)["type"] == "started"
    result = messages.get(timeout=2)
    live.join()
    down = next(at for at, value in events if value == b"\x02\x01left")
    up = next(at for at, value in events if value == b"\x02\x00left")
    assert up - down >= 0.05
    assert result["outcome"] == "completed"
    moves = [
        struct.unpack(">Bhh", value)
        for _, value in events
        if isinstance(value, bytes) and value[0] == 3
    ]
    assert moves[-1][2] == round(0.6 * 65535 - 32768)
    assert not device.recovery_required


def test_live_moves_coalesce_without_losing_up(device):
    live, messages = live_gesture(device)
    live.start()
    assert messages.get(timeout=2)["type"] == "started"
    with live._condition:
        for i in range(300):
            live.update((0.5, 0.3 + i / 1000))
        live.update((0.5, 0.7), finish=True)
    assert messages.get(timeout=2)["success"]
    live.join()
    packets = device.transport.socket.sent
    moves = [p for p in packets if isinstance(p, bytes) and p[0] == 3]
    assert len(moves) == 2
    assert struct.unpack(">Bhh", moves[-1])[2] == round(0.7 * 65535 - 32768)
    assert packets.count(b"\x02\x01left") == packets.count(b"\x02\x00left") == 1


@pytest.mark.parametrize("reason", ["cancel", "deadline", "shutdown"])
def test_live_cancellation_and_deadline_release_original_channel(device, reason):
    live, messages = live_gesture(device)
    if reason == "deadline":
        live.MAX_HOLD_SECONDS = 0.08
    live.start()
    assert messages.get(timeout=2)["type"] == "started"
    if reason == "cancel":
        live.cancel()
    elif reason == "shutdown":
        device._stop.set()
    result = messages.get(timeout=2)
    live.join()
    assert result["cancelled"] and result["outcome"] == "completed"
    assert result["receipt_scope"] == "kvm_transport"
    assert device.transport.socket.sent.count(b"\x02\x00left") == 1
    assert not device.recovery_required


def test_live_unknown_is_not_replayed_and_marker_precedes_hid(device):
    live, messages = live_gesture(device)
    send = device.transport.socket.send

    def fail(value):
        if isinstance(value, bytes):
            assert device._marker.read_text() == live.action_id
        if value == b"\x02\x00left":
            raise OSError("private details")
        send(value)

    device.transport.socket.send = fail
    live.start()
    assert messages.get(timeout=2)["type"] == "started"
    live.update((0.5, 0.5), finish=True)
    result = messages.get(timeout=2)
    live.join()
    assert result["outcome"] == "unknown" and device.recovery_required
    assert "private" not in str(result)
    count = len(device.transport.socket.sent)
    from AutoGLM_GUI.devices.rmq1_live import LiveGesture

    retry = LiveGesture(
        device,
        action_id=live.action_id,
        session_id=live.session_id,
        frame_id=live.frame_id,
        point=live.point,
        notify=messages.put,
    )
    retry.start()
    assert messages.get(timeout=2) == result
    retry.join()
    assert len(device.transport.socket.sent) == count


def test_live_hybrid_settle_revalidates_before_down(tmp_path):
    device = make_device(tmp_path, "usb_hybrid")
    live, messages = live_gesture(device)
    send = device.transport.socket.send

    def change_after_motion(value):
        send(value)
        if isinstance(value, bytes) and value[0] == 3:
            device.transport.config["mobile_orientation"] = (
                "portrait_system_landscape_flipped"
            )

    device.transport.socket.send = change_after_motion
    live.start()
    result = messages.get(timeout=2)
    live.join()
    assert result["outcome"] == "unknown"
    assert b"\x02\x01left" not in device.transport.socket.sent
    assert device.recovery_required


def test_preview_jpeg_is_latest_only_and_requires_render_ack(device):
    from PIL import Image
    import base64
    import io

    device._latest = (time.monotonic(), Image.new("RGB", (64, 128), "red"), 64, 128)
    payload, frame = device.preview(0)
    assert payload["mime"] == "image/jpeg"
    assert Image.open(io.BytesIO(base64.b64decode(payload["image"]))).format == "JPEG"
    assert payload["frame_id"] not in device._observed
    device.acknowledge_preview(payload["frame_id"], frame)
    assert payload["frame_id"] in device._observed
    assert device.preview(frame.at, timeout=0.01) is None
    latest_at = time.monotonic()
    device._latest = (latest_at, Image.new("RGB", (64, 128), "blue"), 64, 128)
    newer, newer_frame = device.preview(frame.at)
    assert newer_frame.at == latest_at and newer["image"] != payload["image"]
    png = device.observe()
    assert Image.open(io.BytesIO(base64.b64decode(png["image"]))).format == "PNG"


def test_live_preview_does_not_report_active_marker_as_unknown(device):
    live, messages = live_gesture(device)
    live.start()
    assert messages.get(timeout=2)["type"] == "started"
    payload, _ = device.preview(0)
    assert payload["input_active"] and not payload["recovery_required"]
    live.cancel()
    assert messages.get(timeout=2)["success"]
    live.join()


def test_live_same_size_orientation_change_stops_further_motion(device):
    live, messages = live_gesture(device)
    live.start()
    assert messages.get(timeout=2)["type"] == "started"
    device.transport.config["mobile_orientation"] = "portrait_system_landscape_flipped"
    device._preview_geometry = None
    payload, _ = device.preview(0)
    assert payload["geometry_id"].endswith("portrait_system_landscape_flipped")
    result = messages.get(timeout=2)
    live.join()
    assert result["outcome"] == "unknown" and device.recovery_required


def rmq1_client(device, monkeypatch):
    manager = SimpleNamespace(get_rmq1_device=lambda device_id: device)
    monkeypatch.setattr(DeviceManager, "get_instance", lambda: manager)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_control_socket_disconnect_releases_input(device, monkeypatch):
    args = gesture_args(device)
    with rmq1_client(device, monkeypatch) as client:
        with client.websocket_connect(
            "/api/rmq1/control?device_id=" + device.device_id
        ) as websocket:
            websocket.send_json(
                dict(
                    type="down",
                    action_id=args["action_id"],
                    session_id=args["session_id"],
                    frame_id=args["frame_id"],
                    point=args["start"],
                )
            )
            assert websocket.receive_json()["type"] == "started"
        # Context exit waits for endpoint cleanup; no hardware input remains.
    assert b"\x02\x00left" in device.transport.socket.sent
    assert not device.recovery_required


def test_preview_socket_ack_backpressure_authorizes_only_displayed_frame(
    device, monkeypatch
):
    with rmq1_client(device, monkeypatch) as client:
        with client.websocket_connect(
            "/api/rmq1/preview?device_id=" + device.device_id
        ) as websocket:
            first = websocket.receive_json()
            assert first["frame_id"] not in device._observed
            device._latest = (time.monotonic(), "skipped", 1920, 1080)
            device._latest = (time.monotonic(), "latest", 1920, 1080)
            websocket.send_json({"type": "ack", "frame_id": first["frame_id"]})
            assert websocket.receive_json() == {
                "type": "ready",
                "frame_id": first["frame_id"],
            }
            latest = websocket.receive_json()
            assert latest["image"] == "latest"
            assert first["frame_id"] in device._observed
            assert latest["frame_id"] not in device._observed


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", "https://testserver"]
)
def test_control_socket_rejects_cross_origin_before_device_lookup(
    device, monkeypatch, origin
):
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("AUTOGLM_CORS_ORIGINS", "http://localhost:3000")
    with rmq1_client(device, monkeypatch) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "/api/rmq1/control?device_id=" + device.device_id,
                headers={"origin": origin},
            ):
                pass
        assert error.value.code == 1008
    assert not device.transport.socket.sent


@pytest.mark.parametrize(
    "origin", ["http://testserver", "http://localhost:3000", "http://127.0.0.1:3000"]
)
def test_control_socket_accepts_same_origin_and_local_vite(device, monkeypatch, origin):
    monkeypatch.delenv("AUTOGLM_CORS_ORIGINS", raising=False)
    with rmq1_client(device, monkeypatch) as client:
        with client.websocket_connect(
            "/api/rmq1/control?device_id=" + device.device_id,
            headers={"origin": origin},
        ) as websocket:
            websocket.send_text("{}")
            assert websocket.receive_json()["type"] == "error"
    assert not device.transport.socket.sent


def test_live_watchdog_interrupts_stuck_original_socket_without_replay(device):
    import threading

    live, messages = live_gesture(device)
    live.MAX_HOLD_SECONDS = 0.06
    released = threading.Event()
    raw = SimpleNamespace(shutdown=lambda how: released.set(), close=lambda: None)
    device.transport.socket.socket = raw
    send = device.transport.socket.send

    def blocked(value):
        if value == b"\x02\x01left":
            assert released.wait(2), "independent watchdog failed to unblock send"
            raise OSError("send interrupted")
        send(value)

    device.transport.socket.send = blocked
    live.start()
    result = messages.get(timeout=2)
    live.join()
    assert released.is_set()
    assert result["outcome"] == "unknown" and device.recovery_required
    assert live.done.is_set()


def test_duplicate_live_down_cannot_report_terminal_while_pointer_is_held(
    device, monkeypatch
):
    args = gesture_args(device)
    message = dict(
        type="down",
        action_id=args["action_id"],
        session_id=args["session_id"],
        frame_id=args["frame_id"],
        point=args["start"],
    )
    with rmq1_client(device, monkeypatch) as client:
        with client.websocket_connect(
            "/api/rmq1/control?device_id=" + device.device_id
        ) as websocket:
            websocket.send_json(message)
            assert websocket.receive_json()["type"] == "started"
            websocket.send_json(message)
            websocket.send_json(
                {"type": "up", "action_id": args["action_id"], "point": [0.5, 0.6]}
            )
            result = websocket.receive_json()
            assert result["type"] == "result" and result["outcome"] == "completed"
    assert device.transport.socket.sent.count(b"\x02\x01left") == 1
    assert device.transport.socket.sent.count(b"\x02\x00left") == 1


def test_http_gesture_rejection_preserves_409_and_unknown_recovery_state(
    device, monkeypatch
):
    args = {**gesture_args(device), "device_id": device.device_id}
    device._marker.write_text("prior-unknown")
    with rmq1_client(device, monkeypatch) as client:
        response = client.post("/api/rmq1/gesture", json=args)
    assert response.status_code == 409
    assert response.json()["outcome"] == "rejected"
    assert response.json()["recovery_required"]
    assert isinstance(response.json()["detail"], str)
    assert not device.transport.socket.sent


def test_control_invalid_message_cancels_active_pointer_before_closing(
    device, monkeypatch
):
    args = gesture_args(device)
    with rmq1_client(device, monkeypatch) as client:
        with client.websocket_connect(
            "/api/rmq1/control?device_id=" + device.device_id
        ) as websocket:
            websocket.send_json(
                dict(
                    type="down",
                    action_id=args["action_id"],
                    session_id=args["session_id"],
                    frame_id=args["frame_id"],
                    point=args["start"],
                )
            )
            assert websocket.receive_json()["type"] == "started"
            websocket.send_text("x" * 4097)
            assert websocket.receive_json()["type"] == "error"
    assert b"\x02\x00left" in device.transport.socket.sent
    assert not device.recovery_required


def test_recovery_cannot_queue_behind_input_and_clear_a_later_unknown(device):
    import threading

    live, messages = live_gesture(device)
    send = device.transport.socket.send

    def fail_release(value):
        if value == b"\x02\x00left":
            raise OSError("release lost after recovery confirmation")
        send(value)

    device.transport.socket.send = fail_release
    live.start()
    assert messages.get(timeout=2)["type"] == "started"
    recovered = threading.Event()
    errors = []

    def request_recovery():
        try:
            device.recover(device.session_id)
        except RMQ1Error as error:
            errors.append(str(error))
        finally:
            recovered.set()

    confirmation = threading.Thread(target=request_recovery, daemon=True)
    confirmation.start()
    rejected_while_active = recovered.wait(0.2)
    live.update((0.5, 0.5), finish=True)
    result = messages.get(timeout=2)
    live.join()
    confirmation.join(timeout=1)
    assert rejected_while_active, "recovery must reject, never queue behind input"
    assert errors and "正在执行" in errors[0]
    assert result["outcome"] == "unknown" and device.recovery_required
    assert device._marker.read_text() == live.action_id

    # A fresh confirmation after the failed gesture may recover while idle;
    # recovery itself must never send or replay any HID report.
    sent_count = len(device.transport.socket.sent)
    device.recover(device.session_id)
    assert not device.recovery_required
    assert len(device.transport.socket.sent) == sent_count


def test_unknown_http_receipt_can_be_read_without_replaying_or_losing_quarantine(
    device, monkeypatch
):
    from types import SimpleNamespace
    from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

    manager = PhoneAgentManager()
    monkeypatch.setattr(PhoneAgentManager, "get_instance", lambda: manager)
    monkeypatch.setattr(
        DeviceManager,
        "get_instance",
        lambda: SimpleNamespace(get_rmq1_device=lambda _: device),
    )
    app = FastAPI()
    app.include_router(api.router)
    args = {"device_id": device.device_id, **gesture_args(device)}
    original_send = device.transport.socket.send

    def fail_release(value):
        original_send(value)
        if value == b"\x02\x00left":
            raise OSError("fixture release failed")

    monkeypatch.setattr(device.transport.socket, "send", fail_release)
    with TestClient(app) as client:
        first = client.post("/api/rmq1/gesture", json=args)
        assert first.json()["outcome"] == "unknown"
        sent = len(device.transport.socket.sent)
        again = client.post("/api/rmq1/gesture", json=args)
        assert again.json() == first.json()
        assert len(device.transport.socket.sent) == sent
        assert device.device_id in manager._manual_uncertain


def test_ai_lease_blocks_both_rmq1_manual_endpoints_without_hid(device, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from AutoGLM_GUI.phone_agent_manager import PhoneAgentManager

    manager = PhoneAgentManager()
    monkeypatch.setattr(PhoneAgentManager, "get_instance", lambda: manager)
    monkeypatch.setattr(
        DeviceManager,
        "get_instance",
        lambda: SimpleNamespace(get_rmq1_device=lambda _: device),
    )
    asyncio.run(manager.acquire_device_lease(device.device_id, "task"))
    app = FastAPI()
    app.include_router(api.router)
    args = {"device_id": device.device_id, **gesture_args(device)}
    with TestClient(app) as client:
        assert client.post("/api/rmq1/gesture", json=args).status_code == 409
        with client.websocket_connect(
            "/api/rmq1/control?device_id=" + device.device_id,
            headers={"origin": "http://testserver"},
        ) as ws:
            ws.send_json(
                {
                    "type": "down",
                    "action_id": args["action_id"],
                    "session_id": args["session_id"],
                    "frame_id": args["frame_id"],
                    "point": [0.5, 0.5],
                }
            )
            result = ws.receive_json()
            assert result["outcome"] == "rejected"
    assert not device.transport.socket.sent
