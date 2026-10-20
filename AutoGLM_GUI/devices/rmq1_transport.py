"""Pinned, local-network GLKVM transport. Credentials never leave memory."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode

from websockets.sync.client import ClientConnection, connect
from websockets.typing import Origin


class RMQ1Error(ValueError):
    """A safe, user-facing connection or control error."""


def validate_host(host: str) -> str:
    try:
        ip = ipaddress.IPv4Address(host.strip())
    except ValueError:
        raise RMQ1Error("请输入 RMQ1 的局域网 IPv4 地址") from None
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not any(ip in ipaddress.ip_network(network) for network in networks):
        raise RMQ1Error("RMQ1 仅支持局域网私有地址")
    return str(ip)


def normalize_fingerprint(value: str) -> str:
    value = value.replace(":", "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise RMQ1Error("证书 SHA-256 指纹格式不正确")
    return value


def tls_socket(host: str, fingerprint: str | None = None) -> ssl.SSLSocket:
    """Check the leaf before any HTTP credentials or WebSocket handshake."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((validate_host(host), 443), timeout=5)
    try:
        secured = context.wrap_socket(raw, server_hostname=host)
        actual = hashlib.sha256(
            secured.getpeercert(binary_form=True) or b""
        ).hexdigest()
        if fingerprint and not hmac.compare_digest(actual, fingerprint):
            secured.close()
            raise RMQ1Error("RMQ1 证书已变化，请重新核对指纹")
        return secured
    except BaseException:
        raw.close()
        raise


def probe_certificate(host: str) -> str:
    try:
        with tls_socket(host) as secured:
            return hashlib.sha256(
                secured.getpeercert(binary_form=True) or b""
            ).hexdigest()
    except RMQ1Error:
        raise
    except Exception:
        raise RMQ1Error("无法连接 RMQ1，请检查地址和网络") from None


class RMQ1Transport:
    def __init__(self, host: str, fingerprint: str):
        self.host = validate_host(host)
        self.fingerprint = normalize_fingerprint(fingerprint)
        self._token = ""

    def request(
        self, path: str, *, form: dict[str, str] | None = None
    ) -> tuple[Any, str | None]:
        connection = http.client.HTTPSConnection(self.host, timeout=5)
        try:
            connection.sock = tls_socket(self.host, self.fingerprint)
            headers = {"Cookie": f"auth_token={self._token}"} if self._token else {}
            body = None
            if form is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = urlencode(form)
            connection.request(
                "POST" if form is not None else "GET", path, body, headers
            )
            response = connection.getresponse()
            if response.status in (401, 403):
                raise RMQ1Error("RMQ1 登录失败或已过期，请重新连接")
            if response.status != 200:
                raise RMQ1Error("RMQ1 服务不可用或固件不支持此接口")
            data = response.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                raise RMQ1Error("RMQ1 响应超过限制")
            return json.loads(data), response.getheader("Set-Cookie")
        except RMQ1Error:
            raise
        except Exception:
            raise RMQ1Error("RMQ1 请求失败，请检查连接") from None
        finally:
            connection.close()

    def login(self, username: str, password: str) -> None:
        value, cookie = self.request(
            "/api/auth/login",
            form={"user": username, "passwd": password, "expire": "0"},
        )
        cookies = SimpleCookie()
        if cookie:
            cookies.load(cookie)
        token = (
            cookies["auth_token"].value
            if "auth_token" in cookies
            else value.get("result", {}).get("token", value.get("token", ""))
        )
        if not isinstance(token, str) or not re.fullmatch(
            r"[a-zA-Z0-9._~-]{8,1024}", token
        ):
            raise RMQ1Error("RMQ1 登录未返回有效会话")
        self._token = token

    def result(self, path: str) -> dict[str, Any]:
        value, _ = self.request(path)
        result = value.get("result", value)
        if not isinstance(result, dict):
            raise RMQ1Error("RMQ1 响应格式不支持")
        return result

    def websocket(self, path: str) -> ClientConnection:
        secured = tls_socket(self.host, self.fingerprint)
        try:
            # The supplied socket is already TLS-protected and leaf-pinned.
            # ws:// prevents websockets from wrapping that TLS socket again.
            secured.settimeout(None)
            return connect(
                f"ws://{self.host}{path}",
                sock=secured,
                proxy=None,
                origin=Origin(f"https://{self.host}"),
                additional_headers={"Cookie": f"auth_token={self._token}"},
                open_timeout=5,
                close_timeout=2,
                max_size=4 * 1024 * 1024,
                max_queue=2,
                compression=None,
            )
        except Exception:
            secured.close()
            raise RMQ1Error("RMQ1 视频或控制通道连接失败") from None

    def close(self) -> None:
        self._token = ""
