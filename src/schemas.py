"""Pydantic request/response schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    """Schema for creating a new task."""

    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    status: str = Field(default="pending", pattern="^(pending|in_progress|completed)$")


class TaskUpdate(BaseModel):
    """Schema for updating an existing task."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: str | None = Field(
        default=None, pattern="^(pending|in_progress|completed)$"
    )


class TaskResponse(BaseModel):
    """Schema for task responses."""

    id: uuid.UUID
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaskListResponse(BaseModel):
    """Schema for paginated task list responses."""

    items: list[TaskResponse]
    total: int
    page: int
    per_page: int


class LivenessResponse(BaseModel):
    """Process identity without a database dependency."""

    status: Literal["alive"] = "alive"
    environment: str
    uptime_seconds: float
    version: str
    commit_sha: str
    demo_scenario_enabled: bool
    demo_run_id: str


class HealthResponse(BaseModel):
    """Readiness, retaining the original /health fields."""

    status: Literal["healthy", "degraded"]
    environment: str
    database: Literal["healthy", "unhealthy"]
    uptime_seconds: float
    version: str
    commit_sha: str
    schema_revision: str | None
    expected_schema_revision: str
    database_name: str | None = None
    database_role: str | None = None
    reason: str | None = None
    demo_scenario_enabled: bool
    demo_run_id: str
