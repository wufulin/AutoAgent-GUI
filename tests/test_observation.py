"""Observation identity, concurrent capture sharing, and freshness boundaries."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from AutoGLM_GUI.device_protocol import Screenshot
from AutoGLM_GUI.observation import (
    InvalidObservationError,
    ObservationBroker,
    StaleObservationError,
)


def _shot(width: int = 240, height: int = 400) -> Screenshot:
    return Screenshot("encoded-image", width, height)


class _Device:
    def __init__(self):
        self.requests: list[asyncio.Future[Screenshot]] = []
        self.started = asyncio.Event()

    async def get_screenshot(self, timeout: int = 10) -> Screenshot:
        request = asyncio.get_running_loop().create_future()
        self.requests.append(request)
        self.started.set()
        return await request

    async def wait_for_calls(self, count: int) -> None:
        async def wait():
            while len(self.requests) < count:
                await self.started.wait()
                self.started.clear()

        await asyncio.wait_for(wait(), timeout=1)


class _Clock:
    now = 10.0

    def __call__(self) -> float:
        return self.now


async def _capture(broker, device, shot=None, *, fresh=False):
    call = asyncio.create_task(broker.capture("test", fresh=fresh))
    await device.wait_for_calls(len(device.requests) + 1)
    device.requests[-1].set_result(shot or _shot())
    return await call


def test_concurrent_consumers_share_identity_but_completed_capture_is_not_automatic():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        first = asyncio.create_task(broker.capture("decision"))
        second = asyncio.create_task(broker.capture("replay"))
        await device.wait_for_calls(1)
        assert len(device.requests) == 1
        device.requests[0].set_result(_shot())
        a, b = await asyncio.gather(first, second)
        assert a is b
        assert broker.recent(1) is a
        c = await _capture(broker, device)
        assert len(device.requests) == 2
        assert c.observation_id != a.observation_id
        assert broker.recent(1, consume=True) is c
        assert broker.recent(1) is None

    asyncio.run(run())


def test_observation_and_retained_pixels_are_immutable():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        source = _shot()
        observation = await _capture(broker, device, source)
        source.width = 999
        exported = observation.screenshot
        exported.base64_data = "changed"
        exported.width = 999
        assert observation.screenshot == _shot()
        with pytest.raises(FrozenInstanceError):
            observation.purpose = "changed"

    asyncio.run(run())


def test_invalidation_rejects_inflight_and_starts_a_new_generation():
    async def run():
        device = _Device()
        broker = ObservationBroker(device, session_id="session")
        old = asyncio.create_task(broker.capture("before-reset"))
        await device.wait_for_calls(1)
        broker.invalidate()
        new = asyncio.create_task(broker.capture("after-reset"))
        await device.wait_for_calls(2)
        device.requests[0].set_result(_shot())
        with pytest.raises(StaleObservationError):
            await old
        device.requests[1].set_result(_shot())
        result = await new
        assert result.session_id == "session"
        assert result.generation == 1
        assert broker.is_current(result)

    asyncio.run(run())


def test_action_start_and_completion_are_independent_capture_barriers():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        before = await _capture(broker, device)
        assert broker.action_started() == 1
        assert broker.recent(100) is None
        assert not broker.is_current(before)
        during = asyncio.create_task(broker.capture("during-action"))
        await device.wait_for_calls(2)
        broker.action_completed()
        device.requests[1].set_result(_shot())
        with pytest.raises(StaleObservationError):
            await during
        after = await _capture(broker, device)
        assert after.action_sequence == 1
        assert after.generation == 2

    asyncio.run(run())


def test_cancelling_one_consumer_does_not_cancel_the_shared_capture():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        first = asyncio.create_task(broker.capture("decision"))
        second = asyncio.create_task(broker.capture("replay"))
        await device.wait_for_calls(1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not device.requests[0].cancelled()
        device.requests[0].set_result(_shot())
        result = await second
        assert broker.recent(1) is result
        assert len(device.requests) == 1

    asyncio.run(run())


def test_cancelling_last_consumer_retires_capture_and_previous_recent_evidence():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        await _capture(broker, device)
        cancelled = asyncio.create_task(broker.capture("cancelled"))
        await device.wait_for_calls(2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await asyncio.sleep(0)
        assert device.requests[1].cancelled()
        assert broker.recent(100) is None
        result = await _capture(broker, device)
        assert broker.recent(1) is result

    asyncio.run(run())


def test_fresh_capture_is_independent_and_breaks_old_inflight_sharing():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        old = asyncio.create_task(broker.capture("decision"))
        await device.wait_for_calls(1)
        final = asyncio.create_task(broker.capture("verification", fresh=True))
        await device.wait_for_calls(2)
        next_normal = asyncio.create_task(broker.capture("another-consumer"))
        await device.wait_for_calls(3)
        for request in device.requests:
            request.set_result(_shot())
        results = await asyncio.gather(old, final, next_normal)
        assert len({result.observation_id for result in results}) == 3
        assert broker.recent(1) is results[2]

    asyncio.run(run())


def test_parallel_fresh_verifiers_never_share_a_capture():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        first = asyncio.create_task(broker.capture("verify-a", fresh=True))
        second = asyncio.create_task(broker.capture("verify-b", fresh=True))
        await device.wait_for_calls(2)
        device.requests[1].set_result(_shot())
        b = await second
        device.requests[0].set_result(_shot())
        a = await first
        assert a.observation_id != b.observation_id
        assert broker.recent(1) is b

    asyncio.run(run())


def test_freshness_includes_time_spent_acquiring_pixels():
    async def run():
        device, clock = _Device(), _Clock()
        broker = ObservationBroker(device, clock=clock)
        pending = asyncio.create_task(broker.capture("slow-capture"))
        await device.wait_for_calls(1)
        clock.now += 2
        device.requests[0].set_result(_shot())
        result = await pending
        assert result.capture_started_at == 10
        assert result.captured_at == 12
        assert result.age_seconds(clock.now) == 2
        assert not broker.is_current(result, max_age_seconds=1)
        assert broker.recent(1) is None

    asyncio.run(run())


def test_geometry_change_invalidates_previous_geometry():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        portrait = await _capture(broker, device)
        landscape = await _capture(broker, device, _shot(400, 240))
        assert portrait.geometry_id != landscape.geometry_id
        assert not broker.is_current(portrait)
        assert broker.recent(1) is landscape

    asyncio.run(run())


def test_slow_older_capture_cannot_restore_obsolete_geometry():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        old = asyncio.create_task(broker.capture("old"))
        await device.wait_for_calls(1)
        new = asyncio.create_task(broker.capture("new", fresh=True))
        await device.wait_for_calls(2)
        device.requests[1].set_result(_shot(400, 240))
        result = await new
        device.requests[0].set_result(_shot())
        with pytest.raises(StaleObservationError):
            await old
        assert broker.is_current(result)
        assert broker.geometry_id == result.geometry_id

    asyncio.run(run())


@pytest.mark.parametrize(
    "shot",
    [
        Screenshot("", 240, 400),
        Screenshot("image", 0, 400),
        Screenshot("x", 1, 1, True),
    ],
)
def test_sensitive_or_invalid_capture_clears_recent_evidence(shot):
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        await _capture(broker, device)
        with pytest.raises(InvalidObservationError):
            await _capture(broker, device, shot)
        assert broker.recent(100) is None
        result = await _capture(broker, device)
        assert broker.is_current(result)

    asyncio.run(run())


def test_failed_capture_does_not_leave_reusable_evidence():
    async def run():
        device = _Device()
        broker = ObservationBroker(device)
        await _capture(broker, device)
        failure = asyncio.create_task(broker.capture("failure"))
        await device.wait_for_calls(2)
        device.requests[1].set_exception(ConnectionError("disconnected"))
        with pytest.raises(ConnectionError):
            await failure
        assert broker.recent(100) is None
        await _capture(broker, device)
        assert len(device.requests) == 3

    asyncio.run(run())
