"""Keep-alive must preserve certificate binding and never retry unknown writes."""

from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

from AutoGLM_GUI.devices import rmq1_transport as module


class TLSSocket:
    def __init__(self, *responses: bytes):
        self.responses = iter(responses)
        self.writes: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.writes.append(data)

    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO(next(self.responses))

    def close(self) -> None:
        self.closed = True


def response(*, close: bool = False, status: int = 200) -> bytes:
    body = b'{"result":{"ok":true}}'
    connection = "close" if close else "keep-alive"
    headers = (
        f"HTTP/1.1 {status} Status\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: {connection}\r\n\r\n"
    ).encode()
    return headers + body


def test_status_reads_reuse_pinned_tls_connection_and_close_clears_auth(monkeypatch):
    socket = TLSSocket(response(), response())
    pin = Mock(return_value=socket)
    monkeypatch.setattr(module, "tls_socket", pin)
    transport = module.RMQ1Transport("192.168.1.20", "a" * 64)
    transport._token = "fixture-token"

    assert transport.result("/api/hid") == {"ok": True}
    assert transport.result("/api/system/get_config") == {"ok": True}

    pin.assert_called_once_with("192.168.1.20", "a" * 64)
    assert len(socket.writes) == 2
    assert all(b"Cookie: auth_token=fixture-token" in data for data in socket.writes)
    assert not socket.closed
    transport.close()
    assert socket.closed
    assert transport._token == "" and transport._http is None


def test_server_closed_keepalive_rechecks_pin_before_sending_cookie(monkeypatch):
    socket = TLSSocket(response(close=True))
    pin = Mock(side_effect=[socket, module.RMQ1Error("证书已变化")])
    monkeypatch.setattr(module, "tls_socket", pin)
    transport = module.RMQ1Transport("192.168.1.20", "a" * 64)
    transport._token = "fixture-token"

    assert transport.result("/api/hid") == {"ok": True}
    with pytest.raises(module.RMQ1Error, match="证书"):
        transport.result("/api/system/get_config")

    assert pin.call_count == 2
    assert len(socket.writes) == 1
    assert socket.closed and transport._http is None


def test_lost_post_response_is_not_replayed(monkeypatch):
    socket = TLSSocket(b"")
    pin = Mock(return_value=socket)
    monkeypatch.setattr(module, "tls_socket", pin)
    transport = module.RMQ1Transport("192.168.1.20", "a" * 64)

    with pytest.raises(module.RMQ1Error, match="请求失败") as error:
        transport.login("admin", "fixture-password")

    pin.assert_called_once()
    assert sum(b"POST /api/auth/login" in data for data in socket.writes) == 1
    assert "fixture-password" not in str(error.value)
    assert socket.closed and transport._http is None


def test_failed_status_discards_connection_without_reusing_unread_body(monkeypatch):
    first = TLSSocket(response(status=401))
    second = TLSSocket(response())
    pin = Mock(side_effect=[first, second])
    monkeypatch.setattr(module, "tls_socket", pin)
    transport = module.RMQ1Transport("192.168.1.20", "a" * 64)

    with pytest.raises(module.RMQ1Error, match="登录失败"):
        transport.result("/api/hid")
    assert first.closed and transport._http is None
    assert transport.result("/api/hid") == {"ok": True}
    assert pin.call_count == 2
    transport.close()
