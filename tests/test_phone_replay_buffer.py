"""Bounded failure evidence for the maximum verified-subtask segment."""

import base64
import json

import pytest

from AutoGLM_GUI.phone_agent_trace import PhoneAgentReplay

pytestmark = [pytest.mark.contract, pytest.mark.release_gate]


@pytest.fixture
def replay_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOGLM_TRACE_FILE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("AUTOGLM_TRACE_ENABLED", "1")
    monkeypatch.setenv("AUTOGLM_TRACE_REPLAY_ENABLED", "1")
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", "on_error")
    return tmp_path / "runs" / "buffer-test"


def record_frames(replay, count):
    phases = ("observation", "pre_action", "post_action")
    for index in range(count):
        replay.record(
            phases[index % 3],
            index // 3 + 1,
            screenshot=base64.b64encode(f"frame-{index + 1}".encode()).decode(),
        )


@pytest.mark.parametrize("frame_count", [60, 65])
def test_on_error_retains_all_twenty_steps_and_keeps_latest_overflow(
    replay_environment, frame_count
):
    replay = PhoneAgentReplay("task", "buffer-test", "phone")
    record_frames(replay, frame_count)

    assert len(replay.frames) == 60
    assert not (replay_environment / "replay.jsonl").exists()

    # The final verification failed even though individual device calls returned.
    replay.finish(failed=True)

    records = [
        json.loads(line)
        for line in (replay_environment / "replay.jsonl").read_text().splitlines()
    ]
    assert [record["event_seq"] for record in records] == list(
        range(frame_count - 59, frame_count + 1)
    )
    assert all(record["phone_agent"]["subtask_failed"] for record in records)
    screenshots = [
        replay_environment / record["phone_agent"]["artifacts"]["screenshot"]["path"]
        for record in records
    ]
    assert len(set(screenshots)) == 60
    assert screenshots[0].read_bytes() == f"frame-{frame_count - 59}".encode()
    assert screenshots[-1].read_bytes() == f"frame-{frame_count}".encode()
    assert not replay.frames
    assert "base64" not in (replay_environment / "replay.jsonl").read_text()


@pytest.mark.parametrize(
    "mode,failed",
    [("on_error", False), ("off", False), ("off", True)],
)
def test_success_and_capture_off_never_persist_screenshots(
    replay_environment, monkeypatch, mode, failed
):
    monkeypatch.setenv("AUTOGLM_TRACE_CAPTURE_SCREENSHOT", mode)
    replay = PhoneAgentReplay("task", "buffer-test", "phone")
    record_frames(replay, 65)
    if mode == "off":
        assert not replay.frames
    replay.finish(failed=failed)

    content = (replay_environment / "replay.jsonl").read_text()
    records = [json.loads(line)["phone_agent"] for line in content.splitlines()]
    assert records and all(record["artifacts"] == {} for record in records)
    assert not list(replay_environment.rglob("*.png"))
    assert "base64" not in content
    assert not replay.frames
