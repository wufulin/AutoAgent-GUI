"""Scheduled task manager with APScheduler."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Self
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from AutoGLM_GUI.logger import logger
from AutoGLM_GUI.models.scheduled_task import ScheduledTask
from AutoGLM_GUI.skills import SkillError, SkillStorageError, skill_manager


class SchedulerManager:
    _instance: Self | None = None

    def __new__(cls: type[Self]) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._tasks_path = Path.home() / ".config" / "autoglm" / "scheduled_tasks.json"
        self._scheduler = AsyncIOScheduler()
        self._tasks: dict[str, ScheduledTask] = {}
        self._file_mtime: float | None = None
        self._lock = RLock()

    async def start(self) -> None:
        self._load_tasks()
        for task in self._tasks.values():
            if task.enabled:
                self._add_job(task)
        self._scheduler.start()
        logger.info(f"SchedulerManager started with {len(self._tasks)} task(s)")

    async def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("SchedulerManager shutdown")

    def create_task(
        self,
        name: str,
        skill_id: str,
        skill_version: int,
        parameters: dict[str, str | int | bool],
        device_serialnos: list[str] | None,
        cron_expression: str,
        enabled: bool = True,
        device_group_id: str | None = None,
    ) -> ScheduledTask:
        with self._lock:
            skill_manager.compile_skill(skill_id, skill_version, parameters)
            CronTrigger.from_crontab(cron_expression)
            task = ScheduledTask(
                name=name,
                skill_id=skill_id,
                skill_version=skill_version,
                parameters=dict(parameters),
                device_serialnos=device_serialnos or [],
                device_group_id=device_group_id,
                cron_expression=cron_expression,
                enabled=enabled,
            )
            self._tasks[task.id] = task
            try:
                self._save_tasks()
            except OSError:
                self._tasks.pop(task.id)
                raise

            if enabled:
                self._add_job(task)

            logger.info(f"Created scheduled task: {name} (id={task.id})")
            return task

    def update_task(self, task_id: str, **kwargs: Any) -> ScheduledTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            from AutoGLM_GUI.schemas import ScheduledTaskCreate

            data = {key: getattr(task, key) for key in ScheduledTaskCreate.model_fields}
            data.update(kwargs)
            validated = ScheduledTaskCreate.model_validate(data)
            skill_manager.compile_skill(
                validated.skill_id, validated.skill_version, validated.parameters
            )
            updated = replace(
                task,
                **validated.model_dump(),
                invalid_reason=None,
                updated_at=datetime.now(tz=timezone.utc),
            )
            self._tasks[task_id] = updated
            try:
                self._save_tasks()
            except OSError:
                self._tasks[task_id] = task
                raise
            if task.enabled:
                self._remove_job(task_id)
            if updated.enabled:
                self._add_job(updated)
            task = updated

            logger.info(f"Updated scheduled task: {task.name} (id={task_id})")
            return task

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if not task:
                return False

            try:
                self._save_tasks()
            except OSError:
                self._tasks[task_id] = task
                raise
            self._remove_job(task_id)
            logger.info(f"Deleted scheduled task: {task.name} (id={task_id})")
            return True

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            if enabled:
                self.update_task(task_id, enabled=True)
            else:
                task = self._tasks[task_id]
                self._tasks[task_id] = replace(
                    task, enabled=False, updated_at=datetime.now(tz=timezone.utc)
                )
                try:
                    self._save_tasks()
                except OSError:
                    self._tasks[task_id] = task
                    raise
                self._remove_job(task_id)
            return True

    def get_next_run_time(self, task_id: str) -> datetime | None:
        job = self._scheduler.get_job(task_id)
        if job and job.next_run_time:
            return job.next_run_time.replace(tzinfo=None)
        return None

    def _add_job(self, task: ScheduledTask) -> None:
        if task.invalid_reason:
            return
        try:
            parts = task.cron_expression.split()
            if len(parts) != 5:
                logger.error(f"Invalid cron expression: {task.cron_expression}")
                return

            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )

            self._scheduler.add_job(
                self._execute_task,
                trigger=trigger,
                id=task.id,
                args=[task.id],
                replace_existing=True,
            )
            logger.debug(f"Added job for task: {task.name}")
        except Exception as e:
            logger.error(f"Failed to add job for task {task.name}: {e}")

    def _remove_job(self, task_id: str) -> None:
        try:
            if self._scheduler.get_job(task_id):
                self._scheduler.remove_job(task_id)
                logger.debug(f"Removed job: {task_id}")
        except Exception as e:
            logger.warning(f"Failed to remove job {task_id}: {e}")

    def _resolve_device_serialnos(self, task: ScheduledTask) -> list[str]:
        """解析任务的目标设备列表.

        如果指定了 device_group_id，则从分组获取设备列表；
        否则使用 device_serialnos 字段。
        """
        if task.device_group_id:
            from AutoGLM_GUI.device_group_manager import device_group_manager
            from AutoGLM_GUI.device_manager import DeviceManager

            device_manager = DeviceManager.get_instance()

            # 获取分组内的所有设备
            if task.device_group_id == "default":
                # 默认分组：获取所有未分配到其他分组的设备
                assignments = device_group_manager.get_all_assignments()
                assigned_serials = {
                    s for s, gid in assignments.items() if gid != "default"
                }
                managed_devices = device_manager.get_devices()
                return [
                    d.serial
                    for d in managed_devices
                    if d.serial not in assigned_serials
                ]
            else:
                # 其他分组：从分配中获取
                return device_group_manager.get_devices_in_group(task.device_group_id)
        else:
            return task.device_serialnos

    async def _execute_task(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            logger.warning(f"Task {task_id} not found for execution")
            return

        if not task.enabled:
            return

        # 解析目标设备列表
        device_serialnos = self._resolve_device_serialnos(task)

        logger.info(
            f"Executing scheduled task: {task.name} on {len(device_serialnos)} device(s)"
        )

        from AutoGLM_GUI.device_manager import DeviceManager
        from AutoGLM_GUI.task_manager import task_manager
        from AutoGLM_GUI.task_store import TaskStatus, task_store

        try:
            if task.invalid_reason:
                raise SkillError(task.invalid_reason)
            compiled = skill_manager.compile_skill(
                task.skill_id, task.skill_version, task.parameters
            )
        except (SkillError, SkillStorageError) as exc:
            self._record_run(
                task=task,
                status="failure",
                message=str(exc),
                success_count=0,
                total_count=len(device_serialnos),
            )
            return

        device_manager = DeviceManager.get_instance()

        total_count = len(device_serialnos)
        if total_count == 0:
            self._record_run(
                task=task,
                status="failure",
                message="No devices selected",
                success_count=0,
                total_count=0,
            )
            return

        online_devices = {
            device.serial: device
            for device in device_manager.get_devices()
            if device.state.value == "online"
        }

        schedule_fire_id = str(uuid4())
        created_count = 0
        executor_key = "scheduled_skill"
        for serialno in device_serialnos:
            device = online_devices.get(serialno)
            if device is None:
                message = "Device offline"
                failed_task = await asyncio.to_thread(
                    task_store.create_task_run,
                    source="scheduled",
                    executor_key=executor_key,
                    status=TaskStatus.FAILED.value,
                    scheduled_task_id=task.id,
                    schedule_fire_id=schedule_fire_id,
                    device_id=serialno,
                    device_serial=serialno,
                    input_text=compiled.goal,
                    user_message={
                        "message": compiled.goal,
                        "execution": {
                            "route": "skill",
                            "skill_id": task.skill_id,
                            "skill_version": task.skill_version,
                            "parameters": task.parameters,
                            "max_steps": compiled.max_steps,
                            "max_seconds": compiled.max_seconds,
                        },
                    },
                )
                await asyncio.to_thread(
                    task_store.append_event,
                    task_id=failed_task["id"],
                    event_type="error",
                    payload={"message": message},
                )
                await asyncio.to_thread(
                    task_store.update_task_terminal,
                    task_id=failed_task["id"],
                    status=TaskStatus.FAILED.value,
                    final_message=message,
                    error_message=message,
                    step_count=0,
                )
                logger.warning(
                    f"Scheduled task {task.name} skipped offline device {serialno}"
                )
                continue

            await task_manager.enqueue_scheduled_task(
                scheduled_task_id=task.id,
                skill_id=task.skill_id,
                skill_version=task.skill_version,
                parameters=task.parameters,
                device_id=device.primary_device_id,
                device_serial=device.serial,
                input_text=compiled.goal,
                schedule_fire_id=schedule_fire_id,
                max_steps=compiled.max_steps,
                max_seconds=compiled.max_seconds,
            )
            created_count += 1

        if created_count == 0:
            self._record_run(
                task=task,
                status="failure",
                message="No online devices available",
                success_count=0,
                total_count=total_count,
            )
            return

        logger.info(
            f"Scheduled task {task.name} enqueued {created_count}/{total_count} task run(s)"
        )

    def _record_run(
        self,
        task: ScheduledTask,
        status: str,
        message: str,
        success_count: int,
        total_count: int,
    ) -> None:
        with self._lock:
            task.last_run_time = datetime.now(tz=timezone.utc)
            task.last_run_status = status
            task.last_run_success = status == "success"
            task.last_run_success_count = success_count
            task.last_run_total_count = total_count
            task.last_run_message = message[:500] if message else ""
            self._save_tasks()
            if status == "success":
                logger.info(f"Scheduled task completed: {task.name}")
            elif status == "partial":
                logger.warning(f"Scheduled task partially succeeded: {task.name}")
            else:
                logger.warning(f"Scheduled task failed: {task.name} - {message}")

    def _load_tasks(self) -> None:
        if not self._tasks_path.exists():
            return

        try:
            with open(self._tasks_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
                raise ValueError("Invalid scheduled task file")
            tasks_data = data["tasks"]
            loaded: dict[str, ScheduledTask] = {}
            for record in tasks_data:
                task = ScheduledTask.from_dict(record)
                if not task.invalid_reason:
                    try:
                        from AutoGLM_GUI.schemas import ScheduledTaskCreate

                        ScheduledTaskCreate.model_validate(
                            {
                                key: getattr(task, key)
                                for key in ScheduledTaskCreate.model_fields
                            }
                        )
                        skill_manager.compile_skill(
                            task.skill_id, task.skill_version, task.parameters
                        )
                    except (ValueError, SkillStorageError) as exc:
                        task.enabled = False
                        task.invalid_reason = str(exc)
                loaded[task.id] = task
            self._tasks = loaded
            self._file_mtime = self._tasks_path.stat().st_mtime
            logger.debug(f"Loaded {len(self._tasks)} scheduled tasks")
        except Exception as e:
            logger.warning(f"Failed to load scheduled tasks: {e}")

    def _save_tasks(self) -> None:
        self._tasks_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._tasks_path.with_suffix(".tmp")

        try:
            if self._tasks_path.exists():
                original = self._tasks_path.read_text(encoding="utf-8")
                parsed = json.loads(original)
                if not isinstance(parsed, dict) or not isinstance(
                    parsed.get("tasks"), list
                ):
                    raise ValueError("Invalid scheduled task file")
                if any("workflow_uuid" in item for item in parsed["tasks"]):
                    backup = self._tasks_path.with_suffix(".legacy.json")
                    if not backup.exists():
                        with backup.open("x", encoding="utf-8") as handle:
                            handle.write(original)
            data = {"tasks": [t.to_dict() for t in self._tasks.values()]}
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_path.replace(self._tasks_path)
            self._file_mtime = self._tasks_path.stat().st_mtime
            logger.debug(f"Saved {len(self._tasks)} scheduled tasks")
        except Exception as e:
            logger.error(f"Failed to save scheduled tasks: {e}")
            if temp_path.exists():
                temp_path.unlink()
            raise OSError("Scheduled task storage could not be saved") from e


scheduler_manager = SchedulerManager()
