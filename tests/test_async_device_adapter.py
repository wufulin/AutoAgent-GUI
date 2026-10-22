"""Observation boundaries follow physical dispatch and write uncertainty."""

import asyncio
import threading

import pytest

from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.devices.async_adapter import AsyncDeviceAdapter
from AutoGLM_GUI.observation import StaleObservationError


class _AsyncDevice:
    device_id = "observation-test"

    def __init__(self):
        self.actions = []
        self.captures = 0

    async def get_screenshot(self, timeout=10):
        self.captures += 1
        return Screenshot("encoded", 240, 400)

    async def tap(self, x, y, delay=None):
        self.actions.append(("tap", x, y))

    async def detect_and_set_adb_keyboard(self):
        self.actions.append(("keyboard",))
        return "original-ime"

    async def clear_text(self):
        self.actions.append(("clear",))

    async def type_text(self, text):
        self.actions.append(("type", text))

    async def restore_keyboard(self, ime):
        self.actions.append(("restore", ime))


def test_write_invalidates_prior_observation_and_advances_action_sequence():
    async def run():
        device = _AsyncDevice()
        adapter = AsyncDeviceAdapter(device)
        before = await adapter.get_observation("before")
        await adapter.tap(10, 20)
        assert not adapter.observations.is_current(before)
        assert adapter.observations.recent(10) is None
        after = await adapter.get_observation("after")
        assert after.action_sequence == before.action_sequence + 1
        assert after.generation == before.generation + 2
        assert adapter.observations.is_current(after)

    asyncio.run(run())


def test_each_keyboard_write_has_its_own_barrier_and_can_continue_normally():
    async def run():
        device = _AsyncDevice()
        adapter = AsyncDeviceAdapter(device)
        before = await adapter.get_observation("before")
        ime = await adapter.detect_and_set_adb_keyboard()
        await adapter.clear_text()
        await adapter.type_text("古茗")
        await adapter.restore_keyboard(ime)
        after = await adapter.get_observation("after")
        assert after.action_sequence == 4
        assert after.generation == before.generation + 8
        assert after.session_id == before.session_id
        assert not adapter.outcome_unknown
        assert not adapter.pending_writes
        assert device.actions[-1] == ("restore", "original-ime")

    asyncio.run(run())


def test_pending_and_cancelled_writes_allow_only_raw_recovery_screenshots():
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        class Device(_AsyncDevice):
            async def tap(self, *args):
                started.set()
                await finish.wait()

        device = Device()
        adapter = AsyncDeviceAdapter(device)
        previous = await adapter.get_observation("before")
        task = asyncio.create_task(adapter.tap(1, 1))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        with pytest.raises(StaleObservationError, match="not ready"):
            await adapter.get_observation("during")
        assert (await adapter.get_screenshot()).width == 240
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.pending_writes
        assert adapter.outcome_unknown
        assert adapter.observations.recent(100) is None
        with pytest.raises(StaleObservationError):
            await adapter.get_observation("cancelled")
        with pytest.raises(RuntimeError, match="still in flight"):
            adapter.recover()
        finish.set()
        assert await adapter.drain(1)
        assert adapter.outcome_unknown
        assert (await adapter.get_screenshot()).width == 240
        with pytest.raises(StaleObservationError):
            await adapter.get_observation("unknown")
        with pytest.raises(RuntimeError, match="recovery"):
            await adapter.tap(2, 2)
        adapter.recover()
        final = await adapter.get_observation("recovered", fresh=True)
        assert final.session_id != previous.session_id
        assert not adapter.outcome_unknown
        assert not adapter.observations.is_current(previous)

    asyncio.run(run())


def test_late_physical_completion_invalidates_recovery_time_observation_cache():
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        class Device(_AsyncDevice):
            async def tap(self, *args):
                started.set()
                await finish.wait()

        adapter = AsyncDeviceAdapter(Device())
        operation = asyncio.create_task(adapter.tap(1, 1))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        # A low-level observer may capture for diagnosis, but this must never
        # survive the late physical completion as reusable action evidence.
        diagnosis = await adapter.observations.capture("diagnosis")
        finish.set()
        assert await adapter.drain(1)
        await asyncio.sleep(0)
        assert not adapter.observations.is_current(diagnosis)
        assert adapter.observations.recent(100) is None
        assert adapter.outcome_unknown

    asyncio.run(run())


def test_failed_write_invalidates_evidence_and_requires_explicit_recovery():
    async def run():
        class Device(_AsyncDevice):
            async def tap(self, *args):
                raise ConnectionError("lost connection")

        adapter = AsyncDeviceAdapter(Device())
        old = await adapter.get_observation("before")
        with pytest.raises(ConnectionError):
            await adapter.tap(1, 1)
        assert adapter.outcome_unknown
        assert adapter.observations.recent(100) is None
        assert not adapter.observations.is_current(old)
        with pytest.raises(StaleObservationError):
            await adapter.get_observation("unknown")

    asyncio.run(run())


def test_session_reset_rejects_inflight_evidence_without_clearing_uncertainty():
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        class Device(_AsyncDevice):
            async def get_screenshot(self, timeout=10):
                started.set()
                await finish.wait()
                return await super().get_screenshot(timeout)

        adapter = AsyncDeviceAdapter(Device())
        old_broker = adapter.observations
        pending = asyncio.create_task(adapter.get_observation("old-session"))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        adapter._outcome_unknown = True
        adapter.reset_observations()
        assert adapter.outcome_unknown
        assert adapter.observations.session_id != old_broker.session_id
        finish.set()
        with pytest.raises(StaleObservationError):
            await pending
        assert adapter.observations.recent(100) is None
        assert old_broker.recent(100) is None

    asyncio.run(run())


def test_capture_started_before_dispatch_cannot_be_used_after_write():
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        class Device(_AsyncDevice):
            async def get_screenshot(self, timeout=10):
                started.set()
                await finish.wait()
                return await super().get_screenshot(timeout)

        adapter = AsyncDeviceAdapter(Device())
        pending = asyncio.create_task(adapter.get_observation("before-dispatch"))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await adapter.tap(10, 20)
        finish.set()
        with pytest.raises(StaleObservationError):
            await pending

    asyncio.run(run())


def test_sync_device_observation_hooks_run_on_event_loop_thread(monkeypatch):
    async def run():
        action_threads, hook_threads = [], []

        class Device:
            device_id = "sync-test"

            def tap(self, *args):
                action_threads.append(threading.get_ident())

        adapter = AsyncDeviceAdapter(Device())
        broker = adapter.observations
        start, end = broker.action_started, broker.action_completed

        def on_start():
            hook_threads.append(threading.get_ident())
            return start()

        def on_end():
            hook_threads.append(threading.get_ident())
            return end()

        monkeypatch.setattr(broker, "action_started", on_start)
        monkeypatch.setattr(broker, "action_completed", on_end)
        await adapter.tap(1, 1)
        loop_thread = threading.get_ident()
        assert hook_threads == [loop_thread, loop_thread]
        assert action_threads and action_threads[0] != loop_thread

    asyncio.run(run())


def test_observation_consumers_share_capture_without_cancel_propagation():
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        class Device(_AsyncDevice):
            async def get_screenshot(self, timeout=10):
                self.captures += 1
                started.set()
                await finish.wait()
                return Screenshot("encoded", 240, 400)

        device = Device()
        adapter = AsyncDeviceAdapter(device)
        first = asyncio.create_task(adapter.get_observation("decision"))
        second = asyncio.create_task(adapter.get_observation("replay"))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        finish.set()
        result = await second
        assert device.captures == 1
        assert adapter.observations.is_current(result)

    asyncio.run(run())


@pytest.mark.parametrize("missing", [True, False])
def test_hierarchy_capability_requires_a_callable_device_method(missing):
    async def run():
        device = _AsyncDevice()
        if not missing:
            device.get_ui_hierarchy = None
        adapter = AsyncDeviceAdapter(device)
        assert not adapter.supports_ui_hierarchy
        with pytest.raises(NotImplementedError, match="does not support"):
            await adapter.get_ui_hierarchy()
        assert adapter.observations.action_sequence == 0

    asyncio.run(run())


@pytest.mark.parametrize("native_async", [True, False])
def test_hierarchy_read_supports_sync_and_async_without_becoming_an_action(
    native_async,
):
    async def run():
        timeouts = []
        device = _AsyncDevice()

        def read(timeout):
            timeouts.append(timeout)
            return '<hierarchy><node text="古茗" /></hierarchy>'

        async def async_read(timeout):
            return read(timeout)

        device.get_ui_hierarchy = async_read if native_async else read
        adapter = AsyncDeviceAdapter(device)
        before = await adapter.get_observation("before")
        assert adapter.supports_ui_hierarchy
        assert 'text="古茗"' in await adapter.get_ui_hierarchy(timeout=3)
        assert timeouts == [3]
        assert adapter.observations.action_sequence == 0
        assert adapter.observations.is_current(before)

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["write", "reset"])
def test_hierarchy_read_rejects_results_crossing_a_state_boundary(boundary):
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        class Device(_AsyncDevice):
            async def get_ui_hierarchy(self, timeout=10):
                started.set()
                await finish.wait()
                return "<hierarchy />"

        adapter = AsyncDeviceAdapter(Device())
        pending = asyncio.create_task(adapter.get_ui_hierarchy())
        await asyncio.wait_for(started.wait(), timeout=5.0)
        if boundary == "write":
            await adapter.tap(1, 1)
        else:
            adapter.reset_observations()
        finish.set()
        with pytest.raises(StaleObservationError, match="changed during"):
            await pending

    asyncio.run(run())


def test_hierarchy_read_refuses_unknown_device_without_invoking_provider():
    async def run():
        class Device(_AsyncDevice):
            calls = 0

            async def get_ui_hierarchy(self, timeout=10):
                self.calls += 1
                return "<hierarchy />"

        device = Device()
        adapter = AsyncDeviceAdapter(device)
        adapter._outcome_unknown = True
        with pytest.raises(StaleObservationError, match="not ready"):
            await adapter.get_ui_hierarchy()
        assert device.calls == 0

    asyncio.run(run())
