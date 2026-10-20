"""Explicit RMQ1 connection, observation and bounded manual control routes."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr, field_validator

from AutoGLM_GUI.device_manager import DeviceManager
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


@router.post("/gesture")
def gesture(request: GestureRequest) -> dict[str, Any]:
    try:
        return (
            DeviceManager.get_instance()
            .get_rmq1_device(request.device_id)
            .gesture(
                action_id=str(request.action_id),
                session_id=request.session_id,
                frame_id=request.frame_id,
                start=request.start,
                end=request.end,
                duration_ms=request.duration_ms,
            )
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
