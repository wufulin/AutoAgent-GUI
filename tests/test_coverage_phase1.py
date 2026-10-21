"""Additional unit coverage for pure parsing and file-backed managers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from AutoGLM_GUI.device_group_manager import DeviceGroupManager
from AutoGLM_GUI.history_manager import HistoryManager
from AutoGLM_GUI.models.device_group import DEFAULT_GROUP_ID, DeviceGroup
from AutoGLM_GUI.models.history import (
    ConversationRecord,
    DeviceHistory,
    MessageRecord,
    StepTimingRecord,
    TraceSummaryRecord,
)


@pytest.fixture
def device_group_manager(tmp_path: Path) -> DeviceGroupManager:
    DeviceGroupManager._instance = None
    manager = DeviceGroupManager()
    manager._groups_path = tmp_path / "device_groups.json"
    manager._groups_cache = None
    manager._assignments_cache = None
    manager._file_mtime = None
    yield manager
    DeviceGroupManager._instance = None


def test_device_group_manager_crud_assignments_and_cache(
    device_group_manager: DeviceGroupManager,
) -> None:
    groups = device_group_manager.list_groups()
    assert [group.id for group in groups] == [DEFAULT_GROUP_ID]
    assert device_group_manager.delete_group(DEFAULT_GROUP_ID) is False

    work = device_group_manager.create_group("Work")
    personal = device_group_manager.create_group("Personal")
    assert device_group_manager.get_group(work.id).name == "Work"
    assert device_group_manager.update_group(work.id, "Office").name == "Office"
    assert device_group_manager.update_group("missing", "x") is None

    assert device_group_manager.assign_device("serial-1", work.id) is True
    assert device_group_manager.assign_device("serial-2", "missing") is False
    assert device_group_manager.get_device_group("serial-1") == work.id
    assert device_group_manager.get_device_group("unknown") == DEFAULT_GROUP_ID
    assert device_group_manager.get_devices_in_group(work.id) == ["serial-1"]
    assert device_group_manager.get_all_assignments() == {"serial-1": work.id}

    assert device_group_manager.reorder_groups([DEFAULT_GROUP_ID, personal.id, work.id])
    assert device_group_manager.reorder_groups(["missing"]) is False
    assert [group.id for group in device_group_manager.list_groups()] == [
        DEFAULT_GROUP_ID,
        personal.id,
        work.id,
    ]

    assert device_group_manager.delete_group(work.id) is True
    assert device_group_manager.get_device_group("serial-1") == DEFAULT_GROUP_ID
    assert device_group_manager.get_devices_in_group(DEFAULT_GROUP_ID) == ["serial-1"]
    assert device_group_manager.delete_group("missing") is False


def test_device_group_manager_recovers_default_and_bad_json(
    device_group_manager: DeviceGroupManager,
) -> None:
    custom = DeviceGroup(id="custom", name="Custom", order=2)
    device_group_manager._groups_path.parent.mkdir(parents=True, exist_ok=True)
    device_group_manager._groups_path.write_text(
        json.dumps(
            {
                "groups": [custom.to_dict()],
                "device_assignments": {"serial": "custom"},
            }
        ),
        encoding="utf-8",
    )
    device_group_manager._groups_cache = None
    device_group_manager._assignments_cache = None
    device_group_manager._file_mtime = None

    assert [group.id for group in device_group_manager.list_groups()] == [
        DEFAULT_GROUP_ID,
        "custom",
    ]

    device_group_manager._groups_path.write_text("{bad", encoding="utf-8")
    device_group_manager._groups_cache = None
    device_group_manager._assignments_cache = None
    device_group_manager._file_mtime = None
    assert [group.id for group in device_group_manager.list_groups()] == [
        DEFAULT_GROUP_ID
    ]


@pytest.fixture
def history_manager(tmp_path: Path) -> HistoryManager:
    HistoryManager._instance = None
    manager = HistoryManager()
    manager._history_dir = tmp_path / "history"
    manager._file_cache = {}
    manager._file_mtime = {}
    yield manager
    HistoryManager._instance = None


def _conversation(record_id: str = "record-1") -> ConversationRecord:
    return ConversationRecord(
        id=record_id,
        task_text="task",
        final_message="done",
        success=True,
        steps=1,
        source="chat",
        trace_id="trace-1",
        step_timings=[
            StepTimingRecord(step=1, trace_id="trace-1", llm_duration_ms=2.5)
        ],
        trace_summary=TraceSummaryRecord(trace_id="trace-1", steps=1),
        messages=[
            MessageRecord(
                role="assistant",
                content="done",
                thinking="think",
                action={"action": "Tap"},
                step=1,
            )
        ],
    )


def test_history_manager_crud_cache_and_serial_safety(
    history_manager: HistoryManager,
) -> None:
    record = _conversation()

    assert (
        history_manager._sanitize_serialno("")
        == "ad87109bfff0765f4dd8cf4943b04d16a4070fea"
    )
    assert history_manager._sanitize_serialno("../bad") != "../bad"
    assert history_manager._sanitize_serialno("adb-1._tcp:5555") == "adb-1._tcp:5555"

    history_manager.add_record("serial-1", record)
    assert history_manager.get_total_count("serial-1") == 1
    assert history_manager.list_records("serial-1")[0].id == "record-1"
    assert history_manager.list_records("serial-1", limit=1, offset=1) == []
    assert history_manager.get_record("serial-1", "record-1").trace_id == "trace-1"
    assert history_manager.get_record("serial-1", "missing") is None

    loaded_once = history_manager._load_history("serial-1")
    loaded_twice = history_manager._load_history("serial-1")
    assert loaded_once is loaded_twice

    assert history_manager.delete_record("serial-1", "missing") is False
    assert history_manager.delete_record("serial-1", "record-1") is True
    assert history_manager.get_total_count("serial-1") == 0
    assert history_manager.clear_device_history("serial-1") is True
    assert history_manager.clear_device_history("serial-1") is False


def test_history_manager_models_and_corrupt_file(
    history_manager: HistoryManager,
) -> None:
    record = _conversation()
    history = DeviceHistory(serialno="serial-1", records=[record])
    round_tripped = DeviceHistory.from_dict(history.to_dict())

    assert round_tripped.records[0].messages[0].action == {"action": "Tap"}
    assert round_tripped.records[0].step_timings[0].llm_duration_ms == 2.5
    assert round_tripped.records[0].trace_summary.trace_id == "trace-1"

    path = history_manager._get_history_path("serial-2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad", encoding="utf-8")
    assert history_manager._load_history("serial-2").records == []


def test_history_manager_save_failure_cleans_temp_file(
    history_manager: HistoryManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = DeviceHistory(serialno="serial-1", records=[_conversation()])
    real_open = open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise OSError("write failed")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)

    assert history_manager._save_history(history) is False
    assert (
        not history_manager._get_history_path("serial-1").with_suffix(".tmp").exists()
    )
