"""No-device tests for hierarchy capture budgets, validation and cleanup."""

import asyncio
from contextlib import nullcontext
import subprocess
import threading
from types import SimpleNamespace

import pytest

from AutoGLM_GUI.adb import hierarchy


XML = '<?xml version="1.0"?><hierarchy rotation="0"><node text="商户"/></hierarchy>'


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(hierarchy, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(hierarchy, "trace_span", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        hierarchy, "build_adb_command", lambda serial: ["fake-adb", "-s", serial]
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("Every test must install a fake command runner")

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", unexpected)
    return clock


def completed(command: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        command, returncode, stdout=stdout, stderr="private diagnostic"
    )


def test_success_reads_exact_device_and_cleans_its_unique_file(monkeypatch, clock):
    calls: list[tuple[list[str], float]] = []

    def run(command, *, timeout):
        calls.append((command, timeout))
        return completed(
            command, stdout=XML if "exec-out" in command else "dump complete"
        )

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    assert hierarchy.get_ui_hierarchy("phone-1") == XML
    dump, read, cleanup = [command for command, _ in calls]
    assert all(
        command[:3] == ["fake-adb", "-s", "phone-1"]
        for command in (dump, read, cleanup)
    )
    path = dump[-1]
    assert path.startswith("/data/local/tmp/autoglm-ui-") and path.endswith(".xml")
    assert dump[3:] == ["shell", "uiautomator", "dump", path]
    assert read[3:] == ["exec-out", "cat", path]
    assert cleanup[3:] == ["shell", "rm", "-f", path]
    assert calls[-1][1] == 2
    hierarchy.get_ui_hierarchy("phone-1")
    assert calls[3][0][-1] != path


def test_dump_and_read_share_one_deadline(monkeypatch, clock):
    timeouts: list[float] = []

    def run(command, *, timeout):
        timeouts.append(timeout)
        if "uiautomator" in command:
            clock.now += 4.25
        elif "exec-out" in command:
            clock.now += 2
        return completed(command, stdout=XML)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    assert hierarchy.get_ui_hierarchy("phone", timeout=10) == XML
    assert timeouts == [10, 5.75, 2]


def test_exhausted_capture_budget_does_not_start_read_but_still_cleans(
    monkeypatch, clock
):
    calls = []

    def run(command, *, timeout):
        calls.append(command)
        if "uiautomator" in command:
            clock.now += 5
        return completed(command, stdout=XML)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    with pytest.raises(TimeoutError, match="timed out"):
        hierarchy.get_ui_hierarchy("phone", timeout=5)
    assert len(calls) == 2
    assert not any("exec-out" in command for command in calls)
    assert calls[-1][-4:-1] == ["shell", "rm", "-f"]


@pytest.mark.parametrize("phase", ["dump", "read"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_failed_capture_always_attempts_cleanup(monkeypatch, clock, phase, failure):
    calls = []
    private = "private hierarchy contents must not enter a runtime error"

    def run(command, *, timeout):
        calls.append(command)
        failing = "uiautomator" in command if phase == "dump" else "exec-out" in command
        if failing:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, timeout, output=private)
            if failure == "oserror":
                raise OSError("cannot execute ADB")
            return completed(command, stdout=private, returncode=1)
        return completed(command, stdout=XML)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    expected = {
        "exit": RuntimeError,
        "timeout": subprocess.TimeoutExpired,
        "oserror": OSError,
    }[failure]
    with pytest.raises(expected) as caught:
        hierarchy.get_ui_hierarchy("phone")
    assert private not in str(caught.value)
    assert calls[-1][-4:-1] == ["shell", "rm", "-f"]
    assert calls[-1][-1] == calls[0][-1]
    assert len(calls) == (2 if phase == "dump" else 3)


@pytest.mark.parametrize("cleanup_failure", ["timeout", "oserror", "nonzero"])
def test_cleanup_failure_does_not_hide_success(monkeypatch, clock, cleanup_failure):
    def run(command, *, timeout):
        if "rm" in command:
            assert timeout == 2
            if cleanup_failure == "timeout":
                raise subprocess.TimeoutExpired(command, timeout)
            if cleanup_failure == "oserror":
                raise OSError("cleanup unavailable")
            return completed(command, returncode=1)
        return completed(command, stdout=XML)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    assert hierarchy.get_ui_hierarchy("phone") == XML


def test_cleanup_error_preserves_original_capture_failure(monkeypatch, clock):
    def run(command, *, timeout):
        if "rm" in command:
            raise OSError("cleanup error")
        return completed(command, returncode=1)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    with pytest.raises(RuntimeError, match="capture unavailable"):
        hierarchy.get_ui_hierarchy("phone")


@pytest.mark.parametrize(
    "payload",
    ["", "ERROR: could not get idle state", "<node/>", "<hierarchy>" + "x" * 2_000_000],
)
def test_missing_hierarchy_or_oversized_output_is_rejected_and_cleaned(
    monkeypatch, clock, payload
):
    calls = []

    def run(command, *, timeout):
        calls.append(command)
        return completed(command, stdout=payload)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)
    with pytest.raises(ValueError, match="Invalid UI hierarchy"):
        hierarchy.get_ui_hierarchy("phone")
    assert calls[-1][-4:-1] == ["shell", "rm", "-f"]


def test_output_at_size_limit_remains_usable(monkeypatch, clock):
    prefix, suffix = "<hierarchy>", "</hierarchy>"
    payload = prefix + " " * (2_000_000 - len(prefix) - len(suffix)) + suffix
    monkeypatch.setattr(
        hierarchy,
        "run_cmd_silently_sync",
        lambda command, **kwargs: completed(command, stdout=payload),
    )
    assert hierarchy.get_ui_hierarchy("phone") == payload


@pytest.mark.parametrize("timeout", [-1, 0, 31])
def test_invalid_timeout_fails_before_any_device_command(clock, timeout):
    with pytest.raises(ValueError, match="between 1 and 30"):
        hierarchy.get_ui_hierarchy("phone", timeout=timeout)


def test_async_capture_runs_blocking_work_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    calls = []

    def capture(device_id, timeout):
        calls.append((device_id, timeout, threading.get_ident()))
        return XML

    monkeypatch.setattr(hierarchy, "get_ui_hierarchy", capture)
    assert asyncio.run(hierarchy.get_ui_hierarchy_async("phone", timeout=7)) == XML
    assert calls[0][:2] == ("phone", 7)
    assert calls[0][2] != main_thread


def test_cancelled_async_read_still_cleans_its_worker_file(monkeypatch, clock):
    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    calls = []

    def run(command, *, timeout):
        calls.append(command)
        if "exec-out" in command:
            started.set()
            if not release.wait(timeout=2):
                raise AssertionError("test did not release the worker")
        if "rm" in command:
            cleaned.set()
        return completed(command, stdout=XML)

    monkeypatch.setattr(hierarchy, "run_cmd_silently_sync", run)

    async def check():
        capture = asyncio.create_task(hierarchy.get_ui_hierarchy_async("phone"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            capture.cancel()
            with pytest.raises(asyncio.CancelledError):
                await capture
        finally:
            release.set()
        assert await asyncio.to_thread(cleaned.wait, 1)

    asyncio.run(check())
    assert calls[-1][-4:-1] == ["shell", "rm", "-f"]
    assert calls[-1][-1] == calls[0][-1]
