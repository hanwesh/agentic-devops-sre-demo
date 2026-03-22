"""Pydantic request/response schemas."""

import uuid
from datetime import datetime

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


class HealthResponse(BaseModel):
    """Schema for health check responses."""

    status: str
    environment: str
    database: str
    uptime_seconds: float
    version: str
