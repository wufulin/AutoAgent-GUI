"""Validated skill catalog API."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from AutoGLM_GUI.skills import (
    SkillConflictError,
    SkillDefinition,
    SkillError,
    SkillStorageError,
    skill_manager,
)

router = APIRouter()


class SkillListResponse(BaseModel):
    skills: list[SkillDefinition]


class SkillEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    expected_enabled: bool = Field(strict=True)
    enabled: bool = Field(strict=True)


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    skill: SkillDefinition


def _error(exc: Exception) -> HTTPException:
    code = (
        503
        if isinstance(exc, SkillStorageError)
        else 409
        if isinstance(exc, SkillConflictError)
        else 400
    )
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/api/skills", response_model=SkillListResponse)
def list_skills() -> SkillListResponse:
    try:
        return SkillListResponse(skills=skill_manager.list_skills())
    except SkillStorageError as exc:
        raise _error(exc) from exc


@router.get("/api/skills/{skill_id}", response_model=SkillDefinition)
def get_skill(
    skill_id: str, version: int | None = Query(default=None, ge=1)
) -> SkillDefinition:
    try:
        skill = skill_manager.get_skill(skill_id, version)
    except SkillStorageError as exc:
        raise _error(exc) from exc
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.post("/api/skills", response_model=SkillDefinition, status_code=201)
def create_skill(request: SkillDefinition) -> SkillDefinition:
    try:
        return skill_manager.create_skill(request)
    except (SkillError, SkillStorageError) as exc:
        raise _error(exc) from exc


@router.put("/api/skills/{skill_id}", response_model=SkillDefinition)
def update_skill(skill_id: str, request: SkillUpdate) -> SkillDefinition:
    try:
        return skill_manager.update_skill(
            skill_id, request.skill, request.expected_version
        )
    except (SkillError, SkillStorageError) as exc:
        raise _error(exc) from exc


@router.patch("/api/skills/{skill_id}/enabled", response_model=SkillDefinition)
def set_skill_enabled(skill_id: str, request: SkillEnabledUpdate) -> SkillDefinition:
    try:
        return skill_manager.set_enabled(
            skill_id,
            request.enabled,
            request.expected_version,
            request.expected_enabled,
        )
    except (SkillError, SkillStorageError) as exc:
        raise _error(exc) from exc


@router.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str, expected_version: int = Query(ge=1)) -> dict[str, bool]:
    try:
        deleted = skill_manager.delete_skill(skill_id, expected_version)
    except (SkillError, SkillStorageError) as exc:
        raise _error(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"success": True}
