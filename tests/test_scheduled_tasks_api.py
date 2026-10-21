"""Real scheduler API validation, pinned skills, and retired workflow rejection."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import AutoGLM_GUI.api.scheduled_tasks as api
import AutoGLM_GUI.scheduler_manager as scheduler_module
from AutoGLM_GUI.models.scheduled_task import ScheduledTask
from AutoGLM_GUI.scheduler_manager import SchedulerManager
from AutoGLM_GUI.skills import SkillManager

pytestmark = [pytest.mark.contract, pytest.mark.release_gate]


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    SchedulerManager._instance = None
    manager = SchedulerManager()
    manager._tasks_path = tmp_path / "scheduled_tasks.json"
    monkeypatch.setattr(manager, "_add_job", lambda task: None)
    monkeypatch.setattr(manager, "_remove_job", lambda task_id: None)
    monkeypatch.setattr(manager, "get_next_run_time", lambda task_id: None)
    monkeypatch.setattr(api, "scheduler_manager", manager)
    monkeypatch.setattr(
        api,
        "task_store",
        SimpleNamespace(get_latest_schedule_summary=lambda task_id: None),
    )
    monkeypatch.setattr(
        scheduler_module, "skill_manager", SkillManager(tmp_path / "skills.json")
    )
    app = FastAPI()
    app.include_router(api.router)
    yield TestClient(app)
    SchedulerManager._instance = None


def payload(**changes):
    return {
        "name": "Morning",
        "skill_id": "open_wechat_chat",
        "skill_version": 1,
        "parameters": {"contact_name": "张三"},
        "device_serialnos": ["dev-1", " dev-1 ", "dev-2"],
        "cron_expression": "0 8 * * *",
        **changes,
    }


def test_schedule_crud_and_pin_validation(client: TestClient):
    response = client.post("/api/scheduled-tasks", json=payload())
    assert response.status_code == 200
    task = response.json()
    assert task["device_serialnos"] == ["dev-1", "dev-2"]
    assert task["parameters"] == {"contact_name": "张三"}
    assert "execution_mode" not in task
    identifier = task["id"]
    assert len(client.get("/api/scheduled-tasks").json()["tasks"]) == 1
    assert client.get(f"/api/scheduled-tasks/{identifier}").status_code == 200
    assert (
        client.put(
            f"/api/scheduled-tasks/{identifier}", json={"parameters": {}}
        ).status_code
        == 400
    )
    assert (
        client.get(f"/api/scheduled-tasks/{identifier}").json()["parameters"]
        == task["parameters"]
    )
    assert (
        client.put(
            f"/api/scheduled-tasks/{identifier}",
            json={"name": "Evening", "enabled": False},
        ).status_code
        == 200
    )
    assert client.post(f"/api/scheduled-tasks/{identifier}/enable").status_code == 200
    assert client.post(f"/api/scheduled-tasks/{identifier}/disable").status_code == 200
    assert client.delete(f"/api/scheduled-tasks/{identifier}").status_code == 200
    assert client.get(f"/api/scheduled-tasks/{identifier}").status_code == 404


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"skill_id": "missing"}, 400),
        ({"skill_version": 999}, 400),
        ({"parameters": {}}, 400),
        ({"parameters": {"contact_name": True}}, 400),
        ({"cron_expression": "99 8 * * *"}, 422),
        ({"device_serialnos": []}, 422),
        ({"device_group_id": "group"}, 422),
        ({"workflow_uuid": "old"}, 422),
        ({"execution_mode": "classic"}, 422),
    ],
)
def test_reject_invalid_schedule(client: TestClient, changes, status):
    assert (
        client.post("/api/scheduled-tasks", json=payload(**changes)).status_code
        == status
    )


def test_legacy_schedule_cannot_enable_until_explicitly_replaced(client: TestClient):
    legacy = ScheduledTask.from_dict(
        {
            "id": "old",
            "name": "Old",
            "workflow_uuid": "retired",
            "cron_expression": "0 8 * * *",
            "device_serialnos": ["dev-1"],
            "enabled": True,
        }
    )
    api.scheduler_manager._tasks[legacy.id] = legacy
    assert not legacy.enabled
    response = client.get("/api/scheduled-tasks/old")
    assert "Legacy workflow" in response.json()["invalid_reason"]
    assert client.post("/api/scheduled-tasks/old/enable").status_code == 400
    assert client.post("/api/scheduled-tasks/old/disable").status_code == 200
    updated = client.put(
        "/api/scheduled-tasks/old",
        json={
            "skill_id": "open_wechat",
            "skill_version": 1,
            "parameters": {},
            "enabled": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["invalid_reason"] is None
