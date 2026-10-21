"""Unit tests for scheduler task execution semantics."""

from __future__ import annotations

import asyncio
import json

import pytest
from pathlib import Path
from types import SimpleNamespace

import AutoGLM_GUI.device_manager as device_manager_module
import AutoGLM_GUI.task_manager as task_manager_module
import AutoGLM_GUI.task_store as task_store_module
import AutoGLM_GUI.scheduler_manager as scheduler_module
from AutoGLM_GUI.skills import SkillManager
from AutoGLM_GUI.models.scheduled_task import ScheduledTask
from AutoGLM_GUI.scheduler_manager import SchedulerManager
from AutoGLM_GUI.task_store import TaskStatus, TaskStore


def test_scheduler_execution_counts_offline_devices_in_latest_summary(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeDeviceManager:
        @staticmethod
        def get_devices() -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    serial="online-1",
                    primary_device_id="device-online-1",
                    state=SimpleNamespace(value="online"),
                )
            ]

    class FakeTaskManager:
        def __init__(self, store: TaskStore) -> None:
            self.store = store

        async def enqueue_scheduled_task(
            self,
            *,
            scheduled_task_id: str,
            skill_id: str,
            skill_version: int,
            parameters: dict,
            device_id: str,
            device_serial: str,
            input_text: str,
            schedule_fire_id: str,
            max_steps: int,
            max_seconds: int,
        ) -> dict[str, object]:
            assert (max_steps, max_seconds) == (60, 300)
            task = self.store.create_task_run(
                source="scheduled",
                executor_key="scheduled_skill",
                scheduled_task_id=scheduled_task_id,
                schedule_fire_id=schedule_fire_id,
                device_id=device_id,
                device_serial=device_serial,
                input_text=input_text,
            )
            self.store.update_task_terminal(
                task_id=task["id"],
                status=TaskStatus.SUCCEEDED.value,
                final_message="完成",
                error_message=None,
                step_count=1,
            )
            return task

    store = TaskStore(tmp_path / "tasks.db")
    fake_task_manager = FakeTaskManager(store)

    original_create = store.create_task_run
    offline_at_creation = []

    def capture_creation(**kwargs):
        task = original_create(**kwargs)
        if kwargs["device_serial"] == "offline-1":
            offline_at_creation.append(task)
            # A concurrent worker cannot claim the offline history record.
            assert store.claim_next_queued_task("offline-1") is None
            events = store.list_task_events(task["id"])
            request = next(
                event["payload"]["execution"]
                for event in events
                if event["event_type"] == "user_message"
            )
            assert request == {
                "route": "skill",
                "skill_id": "open_wechat",
                "skill_version": 1,
                "parameters": {},
                "max_steps": 60,
                "max_seconds": 300,
            }
        return task

    monkeypatch.setattr(store, "create_task_run", capture_creation)

    SchedulerManager._instance = None
    manager = SchedulerManager()
    manager._tasks_path = tmp_path / "scheduled_tasks.json"
    manager._tasks = {
        "scheduled-1": ScheduledTask(
            id="scheduled-1",
            name="Morning",
            skill_id="open_wechat",
            device_serialnos=["online-1", "offline-1"],
            cron_expression="0 8 * * *",
            enabled=True,
        )
    }

    monkeypatch.setattr(
        scheduler_module, "skill_manager", SkillManager(tmp_path / "skills.json")
    )
    monkeypatch.setattr(
        device_manager_module.DeviceManager,
        "get_instance",
        classmethod(lambda cls: FakeDeviceManager()),
    )
    monkeypatch.setattr(task_manager_module, "task_manager", fake_task_manager)
    monkeypatch.setattr(task_store_module, "task_store", store)

    try:
        asyncio.run(manager._execute_task("scheduled-1"))

        summary = store.get_latest_schedule_summary("scheduled-1")
        tasks, total = store.list_tasks(
            source="scheduled", limit=10, offset=0, device_serial=None
        )
    finally:
        store.close()
        SchedulerManager._instance = None

    assert len(offline_at_creation) == 1
    assert offline_at_creation[0]["status"] == TaskStatus.FAILED.value
    assert total == 2
    assert summary is not None
    assert summary["last_run_status"] == "partial"
    assert summary["last_run_success_count"] == 1
    assert summary["last_run_total_count"] == 2
    assert {task["device_serial"] for task in tasks} == {"online-1", "offline-1"}
    offline_task = next(task for task in tasks if task["device_serial"] == "offline-1")
    assert offline_task["status"] == TaskStatus.FAILED.value
    assert offline_task["error_message"] == "Device offline"


def test_scheduler_pins_skill_and_routes_through_task_manager(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeDeviceManager:
        @staticmethod
        def get_devices() -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    serial="online-1",
                    primary_device_id="device-online-1",
                    state=SimpleNamespace(value="online"),
                )
            ]

    class FakeTaskManager:
        def __init__(self) -> None:
            self.enqueued: list[dict[str, object]] = []

        async def enqueue_scheduled_task(self, **kwargs) -> dict[str, object]:
            self.enqueued.append(kwargs)
            return {"id": "task-1"}

    store = TaskStore(tmp_path / "tasks.db")
    fake_task_manager = FakeTaskManager()

    SchedulerManager._instance = None
    manager = SchedulerManager()
    manager._tasks_path = tmp_path / "scheduled_tasks.json"
    manager._tasks = {
        "scheduled-1": ScheduledTask(
            id="scheduled-1",
            name="Planner",
            skill_id="open_wechat",
            device_serialnos=["online-1"],
            cron_expression="0 8 * * *",
            enabled=True,
        )
    }

    monkeypatch.setattr(
        scheduler_module, "skill_manager", SkillManager(tmp_path / "skills.json")
    )
    monkeypatch.setattr(
        device_manager_module.DeviceManager,
        "get_instance",
        classmethod(lambda cls: FakeDeviceManager()),
    )
    monkeypatch.setattr(task_manager_module, "task_manager", fake_task_manager)
    monkeypatch.setattr(task_store_module, "task_store", store)

    try:
        asyncio.run(manager._execute_task("scheduled-1"))
    finally:
        store.close()
        SchedulerManager._instance = None

    assert len(fake_task_manager.enqueued) == 1
    assert fake_task_manager.enqueued[0]["skill_id"] == "open_wechat"
    assert fake_task_manager.enqueued[0]["skill_version"] == 1
    assert fake_task_manager.enqueued[0]["parameters"] == {}


def test_legacy_load_never_schedules_and_preserves_original(
    tmp_path: Path, monkeypatch
) -> None:
    SchedulerManager._instance = None
    manager = SchedulerManager()
    manager._tasks_path = tmp_path / "scheduled_tasks.json"
    legacy = {
        "tasks": [
            {
                "id": "old",
                "name": "Old",
                "workflow_uuid": "wf",
                "enabled": True,
                "device_serialnos": ["dev"],
                "cron_expression": "0 8 * * *",
            }
        ]
    }
    original = json.dumps(legacy)
    manager._tasks_path.write_text(original)
    manager._load_tasks()
    assert manager._tasks_path.read_text() == original
    task = manager.get_task("old")
    assert task is not None and task.enabled is False
    assert task.invalid_reason is not None
    monkeypatch.setattr(manager, "_remove_job", lambda task_id: None)
    with pytest.raises(ValueError):
        manager.set_enabled("old", True)
    assert manager.set_enabled("old", False)
    assert manager._tasks_path.with_suffix(".legacy.json").read_text() == original
    SchedulerManager._instance = None


def test_schedule_persistence_failure_does_not_publish_task(
    tmp_path: Path, monkeypatch
) -> None:
    SchedulerManager._instance = None
    manager = SchedulerManager()
    manager._tasks_path = tmp_path / "scheduled_tasks.json"
    monkeypatch.setattr(
        scheduler_module, "skill_manager", SkillManager(tmp_path / "skills.json")
    )
    jobs = []
    monkeypatch.setattr(manager, "_add_job", jobs.append)

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save_tasks", fail_save)
    with pytest.raises(OSError):
        manager.create_task("task", "open_wechat", 1, {}, ["dev"], "0 8 * * *")
    assert manager.list_tasks() == []
    assert jobs == []
    SchedulerManager._instance = None
