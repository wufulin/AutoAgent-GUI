"""Physical-device exclusion and unknown writes survive context destruction."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from AutoGLM_GUI.config import AgentConfig, ModelConfig
from AutoGLM_GUI.device_manager import DeviceManager
from AutoGLM_GUI.devices.async_adapter import AsyncDeviceAdapter
from AutoGLM_GUI.exceptions import DeviceBusyError
from AutoGLM_GUI.phone_agent_manager import AgentMetadata, AgentState, PhoneAgentManager


class BlockingDevice:
    device_id = "usb"

    def __init__(self):
        self.started = threading.Event()
        self.allow_finish = threading.Event()
        self.calls = 0

    def tap(self, x, y, delay=None):
        self.calls += 1
        self.started.set()
        assert self.allow_finish.wait(3)


def install_agent(manager, key, device_id, adapter=None):
    manager._agents[key] = SimpleNamespace(device=adapter, reset=lambda: None)
    manager._metadata[key] = AgentMetadata(
        device_id=device_id,
        state=AgentState.IDLE,
        agent_config=AgentConfig(device_id=device_id),
        model_config=ModelConfig(),
    )


def test_contexts_and_transport_aliases_share_one_phone(monkeypatch):
    monkeypatch.setattr(
        DeviceManager,
        "_instance",
        SimpleNamespace(canonical_device_id=lambda device_id: "serial"),
    )
    manager = PhoneAgentManager()
    install_agent(manager, "usb", "usb")
    install_agent(manager, "wifi:layered", "wifi")

    async def run():
        await manager.acquire_device_async("usb")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_device_async("wifi", context="layered")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_manual_control("wifi", "browser")
        await manager.release_device_async("usb")
        await manager.acquire_device_lease("wifi", "task")
        await manager.acquire_device_lease("usb", "task")
        await manager.validate_device_lease("usb", "task")
        assert await manager.get_state_async("usb") == AgentState.BUSY
        with pytest.raises(DeviceBusyError):
            await manager.acquire_device_async("usb")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_manual_control("usb", "browser")
        await manager.release_device_lease("usb", "wrong-owner")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_device_lease("usb", "other-task")
        await manager.release_device_lease("wifi", "task")
        with pytest.raises(DeviceBusyError):
            await manager.validate_device_lease("usb", "task")
        assert manager._device_leases == {}
        await manager.acquire_manual_control("usb", "browser")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_device_lease("wifi", "task")
        await manager.release_manual_control("wifi", "browser")

    asyncio.run(run())


def test_cancelled_thread_write_is_drained_and_quarantined_after_destroy(monkeypatch):
    monkeypatch.setattr(DeviceManager, "_instance", None)
    manager = PhoneAgentManager()
    raw = BlockingDevice()
    adapter = AsyncDeviceAdapter(raw)
    install_agent(manager, "usb:layered", "usb", adapter)

    async def run():
        await manager.acquire_device_lease("usb", "task")
        tap = asyncio.create_task(adapter.tap(1, 1))
        assert await asyncio.to_thread(raw.started.wait, 1)
        tap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tap
        # Even event-loop shutdown cancelling the adapter's inner Task cannot
        # mark its still-running worker thread as drained.
        for pending_task in list(adapter._write_tasks):
            pending_task.cancel()
        await asyncio.sleep(0)
        assert adapter.pending_writes and adapter.outcome_unknown
        assert not await adapter.drain(timeout=0)
        with pytest.raises(RuntimeError):
            await adapter.tap(2, 2)
        assert await manager.dispose_device_lease("usb", "task", context="layered")
        assert await manager.device_recovery_required("usb")
        with pytest.raises(DeviceBusyError):
            await manager.recover_manual_control("usb")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_manual_control("usb", "browser")
        raw.allow_finish.set()
        assert await adapter.drain(timeout=1)
        # Drain proves the call returned, not that the action's business effect
        # is known. Only explicit recovery permits another controller.
        assert await manager.device_recovery_required("usb")
        with pytest.raises(DeviceBusyError):
            await manager.acquire_device_lease("usb", "next-task")
        await manager.recover_manual_control("usb")
        await manager.acquire_device_lease("usb", "next-task")
        await manager.release_device_lease("usb", "next-task")
        assert not adapter.outcome_unknown
        assert raw.calls == 1

    try:
        asyncio.run(run())
    finally:
        raw.allow_finish.set()


def test_native_async_write_continues_after_awaiter_cancelled():
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()

        class Device:
            device_id = "native"

            async def tap(self, *args):
                started.set()
                await finish.wait()

        adapter = AsyncDeviceAdapter(Device())
        operation = asyncio.create_task(adapter.tap(1, 1))
        await started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert adapter.pending_writes
        finish.set()
        assert await adapter.drain(1)
        assert adapter.outcome_unknown

    asyncio.run(run())


def test_active_write_is_not_quarantined_by_status_poll(monkeypatch):
    monkeypatch.setattr(DeviceManager, "_instance", None)
    manager = PhoneAgentManager()
    raw = BlockingDevice()
    adapter = AsyncDeviceAdapter(raw)
    install_agent(manager, "usb:layered", "usb", adapter)

    async def run():
        await manager.acquire_device_lease("usb", "task")
        tap = asyncio.create_task(adapter.tap(1, 1))
        assert await asyncio.to_thread(raw.started.wait, 1)
        assert adapter.pending_writes
        assert not await manager.device_recovery_required("usb")
        raw.allow_finish.set()
        await tap
        await manager.release_device_lease("usb", "task")
        assert not await manager.device_recovery_required("usb")

    try:
        asyncio.run(run())
    finally:
        raw.allow_finish.set()


def test_configuration_update_excludes_new_owners_without_holding_manager_lock(
    monkeypatch,
):
    monkeypatch.setattr(DeviceManager, "_instance", None)
    manager = PhoneAgentManager()
    install_agent(manager, "usb", "usb")

    async def run():
        await manager.acquire_device_lease("usb", "task")
        with pytest.raises(DeviceBusyError), manager.configuration_update():
            pytest.fail("Config writes must not start while a device is leased")
        await manager.release_device_lease("usb", "task")
        with manager.configuration_update():
            with pytest.raises(DeviceBusyError):
                await manager.acquire_device_lease("usb", "task")
            with pytest.raises(DeviceBusyError):
                await manager.acquire_device_async("usb")
            with pytest.raises(DeviceBusyError):
                await manager.acquire_manual_control("usb", "browser")
            # The guard is a reservation, so lifecycle operations can acquire
            # the manager lock themselves without deadlocking.
            assert await manager.destroy_all_agents_async() == 1
        await manager.acquire_device_lease("usb", "task")
        await manager.release_device_lease("usb", "task")

    asyncio.run(run())


def test_dispose_lease_requires_owner_and_removes_only_its_context(monkeypatch):
    monkeypatch.setattr(DeviceManager, "_instance", None)
    manager = PhoneAgentManager()
    install_agent(manager, "usb:old-task", "usb")
    install_agent(manager, "usb:other-context", "usb")
    manager._agent_configs["usb:old-task"] = (ModelConfig(), AgentConfig())

    async def run():
        await manager.acquire_device_lease("usb", "old-owner")
        assert not await manager.dispose_device_lease(
            "usb", "wrong-owner", context="old-task"
        )
        assert "usb:old-task" in manager._agents
        assert "usb:old-task" in manager._agent_configs
        await manager.validate_device_lease("usb", "old-owner")
        blocked = asyncio.Event()

        async def successor():
            while True:
                try:
                    await manager.acquire_device_lease("usb", "new-owner")
                except DeviceBusyError:
                    blocked.set()
                    await asyncio.sleep(0)
                else:
                    # Successful acquisition must never observe half-cleaned
                    # state from the preceding task.
                    assert "usb:old-task" not in manager._agents
                    assert "usb:old-task" not in manager._metadata
                    assert "usb:old-task" not in manager._agent_configs
                    return

        next_task = asyncio.create_task(successor())
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert await manager.dispose_device_lease(
            "usb", "old-owner", context="old-task"
        )
        await asyncio.wait_for(next_task, timeout=1)
        assert "usb:other-context" in manager._agents
        assert "usb:other-context" in manager._metadata
        assert not await manager.dispose_device_lease(
            "usb", "old-owner", context="other-context"
        )
        assert "usb:other-context" in manager._agents
        await manager.validate_device_lease("usb", "new-owner")
        await manager.release_device_lease("usb", "new-owner")

    asyncio.run(run())
