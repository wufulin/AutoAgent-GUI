"""Task-local accounting of actual model requests and provider-reported usage.

Hooks deliberately do not request extra provider features. A request that fails or
does not return usage is counted, while its token usage remains unknown.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

ModelRole = Literal["planner", "executor", "verifier"]


@dataclass
class _ModelCall:
    role: ModelRole
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class ExecutionMetrics:
    """One execution's counters, shared by its inherited async contexts."""

    route: str
    calls_complete: bool = True
    _started_at: float = field(default_factory=time.perf_counter)
    _ended_at: float | None = None
    _calls: dict[str, _ModelCall] = field(default_factory=dict)

    def summary(
        self,
        status: str,
        stop_reason: str,
        *,
        steps: int = 0,
        subtasks: int = 0,
        continuations: int = 0,
    ) -> dict[str, Any]:
        calls = list(self._calls.values())
        input_known = self.calls_complete and all(
            call.input_tokens is not None for call in calls
        )
        output_known = self.calls_complete and all(
            call.output_tokens is not None for call in calls
        )
        ended_at = self._ended_at if self._ended_at is not None else time.perf_counter()
        return {
            "route": self.route,
            "status": status,
            "stop_reason": stop_reason,
            "elapsed_ms": round(max(0.0, ended_at - self._started_at) * 1000, 3),
            "steps": steps,
            "subtasks": subtasks,
            "continuations": continuations,
            **{
                f"{role}_calls": sum(call.role == role for call in calls)
                for role in ("planner", "executor", "verifier")
            },
            "calls_complete": self.calls_complete,
            "input_tokens": sum(call.input_tokens or 0 for call in calls)
            if input_known
            else None,
            "output_tokens": sum(call.output_tokens or 0 for call in calls)
            if output_known
            else None,
            "usage_complete": input_known and output_known,
        }


_current_metrics: ContextVar[ExecutionMetrics | None] = ContextVar(
    "execution_metrics", default=None
)


def current_execution_metrics() -> ExecutionMetrics | None:
    return _current_metrics.get()


@contextmanager
def execution_metrics(
    route: str, metrics: ExecutionMetrics | None = None
) -> Iterator[ExecutionMetrics]:
    """Install counters for a run, restoring any enclosing context on exit.

    Supplying existing counters only binds them; it does not restart/finalize their
    timer. This supports inherited runners without resetting a parent's counters.
    """
    owned = metrics is None
    counters = metrics if metrics is not None else ExecutionMetrics(route)
    token = _current_metrics.set(counters)
    try:
        yield counters
    finally:
        if owned:
            counters._ended_at = time.perf_counter()
        _current_metrics.reset(token)


def record_model_call(role: ModelRole) -> str | None:
    """Call immediately before a provider request, including failed requests.

    This counts logical provider requests, not any SDK-internal HTTP retries.
    Pass the returned ID to ``record_usage`` to correlate concurrent requests.
    """
    metrics = _current_metrics.get()
    if metrics is None:
        return None
    call_id = uuid4().hex
    metrics._calls[call_id] = _ModelCall(role=role)
    return call_id


def _token_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage_field(usage: Any, *names: str) -> int | None:
    for name in names:
        value = (
            usage.get(name)
            if isinstance(usage, Mapping)
            else getattr(usage, name, None)
        )
        count = _token_count(value)
        if count is not None:
            return count
    return None


def record_usage(
    role: ModelRole,
    usage: Any = None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    call_id: str | None = None,
) -> None:
    """Record cumulative usage once per call; repeated stream totals replace it.

    Accept OpenAI Chat Completions and Responses/Agents usage objects or dicts.
    Omitted IDs are only safe for a sequential caller: the latest matching call
    is selected. Usage without a preceding request never invents a model call.
    """
    metrics = _current_metrics.get()
    if metrics is None:
        return
    if call_id is None:
        call_id = next(
            (
                key
                for key, call in reversed(metrics._calls.items())
                if call.role == role
            ),
            None,
        )
    call = metrics._calls.get(call_id) if call_id is not None else None
    if call is None or call.role != role:
        return
    received_input = _token_count(input_tokens)
    if received_input is None:
        received_input = _usage_field(usage, "input_tokens", "prompt_tokens")
    received_output = _token_count(output_tokens)
    if received_output is None:
        received_output = _usage_field(usage, "output_tokens", "completion_tokens")
    if received_input is not None:
        call.input_tokens = received_input
    if received_output is not None:
        call.output_tokens = received_output


def mark_model_calls_unavailable() -> None:
    """Mark external/opaque executors whose provider calls cannot be observed."""
    metrics = _current_metrics.get()
    if metrics is not None:
        metrics.calls_complete = False
