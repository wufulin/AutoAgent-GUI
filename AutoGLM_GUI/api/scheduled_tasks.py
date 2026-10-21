"""Scheduled tasks API routes."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from AutoGLM_GUI.models.scheduled_task import ScheduledTask
from AutoGLM_GUI.scheduler_manager import scheduler_manager
from AutoGLM_GUI.schemas import (
    ScheduledTaskCreate,
    ScheduledTaskListResponse,
    ScheduledTaskResponse,
    ScheduledTaskUpdate,
)
from AutoGLM_GUI.task_store import task_store
from AutoGLM_GUI.skills import SkillError, SkillStorageError

router = APIRouter()


def _task_to_response(task: ScheduledTask) -> ScheduledTaskResponse:
    next_run = scheduler_manager.get_next_run_time(task.id)
    latest_summary = task_store.get_latest_schedule_summary(task.id)
    return ScheduledTaskResponse(
        id=task.id,
        name=task.name,
        skill_id=task.skill_id,
        skill_version=task.skill_version,
        parameters=task.parameters,
        invalid_reason=task.invalid_reason,
        device_serialnos=task.device_serialnos,
        device_group_id=task.device_group_id,
        cron_expression=task.cron_expression,
        enabled=task.enabled,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        last_run_time=(
            latest_summary["last_run_time"]
            if latest_summary
            else task.last_run_time.isoformat()
            if task.last_run_time
            else None
        ),
        last_run_success=(
            latest_summary["last_run_success"]
            if latest_summary
            else task.last_run_success
        ),
        last_run_status=(
            latest_summary["last_run_status"]
            if latest_summary
            else task.last_run_status
        ),
        last_run_success_count=(
            latest_summary["last_run_success_count"]
            if latest_summary
            else task.last_run_success_count
        ),
        last_run_total_count=(
            latest_summary["last_run_total_count"]
            if latest_summary
            else task.last_run_total_count
        ),
        last_run_message=(
            latest_summary["last_run_message"]
            if latest_summary
            else task.last_run_message
        ),
        next_run_time=next_run.isoformat() if next_run else None,
    )


@router.get("/api/scheduled-tasks", response_model=ScheduledTaskListResponse)
def list_scheduled_tasks() -> ScheduledTaskListResponse:
    tasks = scheduler_manager.list_tasks()
    return ScheduledTaskListResponse(tasks=[_task_to_response(t) for t in tasks])


@router.post("/api/scheduled-tasks", response_model=ScheduledTaskResponse)
def create_scheduled_task(request: ScheduledTaskCreate) -> ScheduledTaskResponse:
    try:
        task = scheduler_manager.create_task(**request.model_dump())
    except (SkillError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (SkillStorageError, OSError) as exc:
        raise HTTPException(
            status_code=503, detail="Schedule storage unavailable"
        ) from exc
    return _task_to_response(task)


@router.get("/api/scheduled-tasks/{task_id}", response_model=ScheduledTaskResponse)
def get_scheduled_task(task_id: str) -> ScheduledTaskResponse:
    task = scheduler_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task)


@router.put("/api/scheduled-tasks/{task_id}", response_model=ScheduledTaskResponse)
def update_scheduled_task(
    task_id: str, request: ScheduledTaskUpdate
) -> ScheduledTaskResponse:
    update_data = request.model_dump(exclude_unset=True)
    try:
        task = scheduler_manager.update_task(task_id, **update_data)
    except (SkillError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (SkillStorageError, OSError) as exc:
        raise HTTPException(
            status_code=503, detail="Schedule storage unavailable"
        ) from exc
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task)


@router.delete("/api/scheduled-tasks/{task_id}")
def delete_scheduled_task(task_id: str) -> dict[str, Any]:
    try:
        success = scheduler_manager.delete_task(task_id)
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="Schedule storage unavailable"
        ) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "message": "Task deleted"}


@router.post("/api/scheduled-tasks/{task_id}/enable")
def enable_scheduled_task(task_id: str) -> dict[str, Any]:
    try:
        success = scheduler_manager.set_enabled(task_id, True)
    except (SkillError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (SkillStorageError, OSError) as exc:
        raise HTTPException(
            status_code=503, detail="Schedule storage unavailable"
        ) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "message": "Task enabled"}


@router.post("/api/scheduled-tasks/{task_id}/disable")
def disable_scheduled_task(task_id: str) -> dict[str, Any]:
    try:
        success = scheduler_manager.set_enabled(task_id, False)
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="Schedule storage unavailable"
        ) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"success": True, "message": "Task disabled"}
