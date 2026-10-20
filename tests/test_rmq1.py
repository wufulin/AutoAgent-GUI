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


def test_video_decodes_real_h264(monkeypatch, tmp_path):
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
    transport = FakeTransport()
    device = RMQ1Device(transport, tmp_path)
    video = FakeSocket()
    opened = []

    def websocket(path):
        opened.append(path)
        if path == "/api/ws":
            return transport.socket
        assert opened == ["/api/ws", "/api/media/ws"]
        assert transport.socket.received > 0
        assert not transport.socket.closed
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
    for getter in (manager.get_device_protocol, manager.get_async_device_protocol):
        with pytest.raises(ValueError, match="暂不支持 AI"):
            getter(serial)
    with pytest.raises(RMQ1Error, match="已连接"):
        manager.add_rmq1_device("192.168.1.20", "a" * 64, "admin", "password")
    manager.remove_rmq1_device(serial)
    assert manager.get_serial_by_device_id(serial) is None
    assert not manager.get_devices()
