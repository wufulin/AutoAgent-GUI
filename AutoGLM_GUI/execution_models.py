"""Validated routing choices shared by chat, skills and scheduled execution."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal["auto", "direct", "planner", "skill"] = "auto"
    skill_id: str | None = Field(default=None, max_length=80)
    skill_version: int | None = Field(default=None, ge=1, strict=True)
    parameters: dict[str, str | int | bool] = Field(default_factory=dict, max_length=30)
    success_criteria: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    max_steps: int = Field(default=60, ge=1, le=200, strict=True)
    max_seconds: int = Field(default=300, ge=10, le=1800, strict=True)

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if self.route == "skill":
            if not self.skill_id or self.skill_version is None:
                raise ValueError("skill route requires skill_id and skill_version")
        elif self.skill_id or self.skill_version is not None or self.parameters:
            raise ValueError("skill parameters require the skill route")
        if self.route == "direct" and not self.success_criteria:
            raise ValueError("direct route requires explicit success_criteria")
        for item in self.success_criteria + self.constraints:
            if not item.strip() or len(item) > 2000:
                raise ValueError(
                    "criteria and constraints must contain 1..2000 characters"
                )
        for key, value in self.parameters.items():
            if len(key) > 80 or (isinstance(value, str) and len(value) > 2000):
                raise ValueError("skill parameter is too long")
        return self
