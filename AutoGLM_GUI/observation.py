"""Immutable, generation-scoped observations shared by concurrent consumers.

The broker owns identity and freshness, not device action serialization. Callers
must bracket device writes with ``action_started`` and ``action_completed`` and
invalidate on reset, disconnect, cancellation, or an unknown action outcome.
There is deliberately no automatic reuse of completed captures.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from AutoGLM_GUI.device_protocol import FrameReference, Screenshot


class ScreenshotSource(Protocol):
    async def get_screenshot(self, timeout: int = 10) -> Screenshot: ...


class InvalidObservationError(ValueError):
    """A capture is sensitive or does not contain a usable screen."""


class StaleObservationError(RuntimeError):
    """A capture crossed a state boundary and must not authorize an action."""


@dataclass(frozen=True, slots=True)
class Observation:
    """A screen snapshot with immutable identity and monotonic timestamps.

    ``screenshot`` returns a defensive copy because the legacy Screenshot type
    is mutable. Mutating that copy cannot alter evidence retained by the broker.
    Freshness starts when capture starts, including time spent acquiring pixels.
    """

    observation_id: str
    session_id: str
    geometry_id: str
    generation: int
    action_sequence: int
    capture_started_at: float
    captured_at: float
    purpose: str
    _base64_data: str = field(repr=False)
    _width: int
    _height: int
    frame: FrameReference | None = None

    @property
    def screenshot(self) -> Screenshot:
        return Screenshot(
            self._base64_data, self._width, self._height, frame=self.frame
        )

    def age_seconds(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.capture_started_at


@dataclass
class _Capture:
    task: asyncio.Task[Observation]
    sharing_epoch: int
    capture_sequence: int
    waiters: int = 0
    abandoned: bool = False


class ObservationBroker:
    """Single-event-loop broker for one device execution session.

    Concurrent ordinary requests in the same generation share an unfinished
    capture. Every ``fresh=True`` request obtains its own capture and detaches
    the preceding shared flight; requests after that boundary cannot join it.
    Existing consumers may still receive that earlier observation if no device
    state boundary occurred, but it cannot populate the explicit recent cache.
    """

    def __init__(
        self,
        device: ScreenshotSource,
        *,
        session_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._device = device
        self._clock = clock
        self.session_id = session_id or uuid.uuid4().hex
        self._generation = 0
        self._action_sequence = 0
        self._geometry_generation = 0
        self._geometry: tuple[int, int, str | None, str | None] | None = None
        self._geometry_capture_sequence = 0
        self._capture_sequence = 0
        self._sharing_epoch = 0
        self._flight: _Capture | None = None
        self._recent: tuple[Observation, int] | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def action_sequence(self) -> int:
        return self._action_sequence

    @property
    def geometry_id(self) -> str:
        return f"{self.session_id}:{self._geometry_generation}"

    def invalidate(self) -> int:
        """Invalidate cached and in-flight evidence without cancelling callers."""
        self._generation += 1
        self._sharing_epoch += 1
        self._flight = None
        self._recent = None
        return self._generation

    def action_started(self) -> int:
        """Create a new action sequence and invalidate preceding evidence."""
        self._action_sequence += 1
        self.invalidate()
        return self._action_sequence

    def action_completed(self) -> int:
        """Barrier: subsequent evidence must start after the action returns."""
        return self.invalidate()

    def is_current(
        self, observation: Observation, *, max_age_seconds: float | None = None
    ) -> bool:
        if (
            observation.session_id != self.session_id
            or observation.generation != self._generation
            or observation.action_sequence != self._action_sequence
            or observation.geometry_id != self.geometry_id
        ):
            return False
        age = observation.age_seconds(self._clock())
        return age >= 0 and (max_age_seconds is None or age <= max_age_seconds)

    def recent(
        self, max_age_seconds: float, *, consume: bool = False
    ) -> Observation | None:
        """Explicitly request an age-bounded observation, optionally once only."""
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        candidate = self._recent
        if consume:
            self._recent = None
        if candidate is None:
            return None
        observation, _ = candidate
        if not self.is_current(observation, max_age_seconds=max_age_seconds):
            self._recent = None
            return None
        return observation

    async def capture(self, purpose: str, fresh: bool = False) -> Observation:
        """Capture a screen; final verification must use ``fresh=True``."""
        if not purpose or not purpose.strip():
            raise ValueError("purpose must be non-empty")
        if fresh:
            self._sharing_epoch += 1
            self._flight = None
            self._recent = None
        flight = None if fresh else self._flight
        if flight is None or flight.task.done():
            self._capture_sequence += 1
            sequence = self._capture_sequence
            flight = _Capture(
                task=asyncio.create_task(
                    self._collect(
                        purpose, self._generation, self._action_sequence, sequence
                    )
                ),
                sharing_epoch=self._sharing_epoch,
                capture_sequence=sequence,
            )
            # A cancelled last consumer can leave a cancellation-resistant
            # source finishing later. Always retrieve its eventual exception.
            flight.task.add_done_callback(self._consume_task_exception)
            if not fresh:
                self._flight = flight
        flight.waiters += 1
        try:
            # A consumer timing out must not cancel a capture another consumer
            # still needs. The last departing consumer retires it below.
            observation = await asyncio.shield(flight.task)
            if flight.abandoned or not self.is_current(observation):
                raise StaleObservationError("capture crossed an observation boundary")
            if flight.sharing_epoch == self._sharing_epoch and (
                self._recent is None or flight.capture_sequence >= self._recent[1]
            ):
                self._recent = observation, flight.capture_sequence
            return observation
        except asyncio.CancelledError:
            # The surviving consumer, if any, may publish its valid result.
            if flight.waiters == 1:
                self._recent = None
            raise
        except Exception:
            self._recent = None
            raise
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if not flight.task.done():
                    flight.abandoned = True
                    flight.task.cancel()
                if self._flight is flight:
                    self._flight = None

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Observation]) -> None:
        if not task.cancelled():
            task.exception()

    async def _collect(
        self, purpose: str, generation: int, action_sequence: int, sequence: int
    ) -> Observation:
        started_at = self._clock()
        screenshot = await self._device.get_screenshot()
        captured_at = self._clock()
        if generation != self._generation or action_sequence != self._action_sequence:
            raise StaleObservationError("capture crossed a device state boundary")
        if (
            screenshot.is_sensitive
            or screenshot.width <= 0
            or screenshot.height <= 0
            or not screenshot.base64_data
        ):
            raise InvalidObservationError("capture is sensitive or invalid")
        frame = getattr(screenshot, "frame", None)
        geometry = (
            screenshot.width,
            screenshot.height,
            frame.session_id if frame else None,
            frame.geometry_id if frame else None,
        )
        if geometry != self._geometry:
            # A slower older request must not restore obsolete geometry after a
            # more recent independent capture has already observed a new layout.
            if sequence < self._geometry_capture_sequence:
                raise StaleObservationError("capture has superseded geometry")
            self._geometry = geometry
            self._geometry_generation += 1
            self._recent = None
        self._geometry_capture_sequence = max(self._geometry_capture_sequence, sequence)
        return Observation(
            observation_id=uuid.uuid4().hex,
            session_id=self.session_id,
            geometry_id=self.geometry_id,
            generation=generation,
            action_sequence=action_sequence,
            capture_started_at=started_at,
            captured_at=captured_at,
            purpose=purpose,
            _base64_data=screenshot.base64_data,
            _width=screenshot.width,
            _height=screenshot.height,
            frame=frame,
        )
