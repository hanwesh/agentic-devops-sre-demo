"""Liveness and schema-aware readiness checks."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_db
from src.models import Task
from src.schemas import HealthResponse, LivenessResponse
from src.version import SCHEMA_REVISION, get_build_info

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])
_start_time = time.monotonic()


def process_identity() -> LivenessResponse:
    build = get_build_info()
    return LivenessResponse(
        environment=settings.environment,
        uptime_seconds=round(time.monotonic() - _start_time, 2),
        version=build.version,
        commit_sha=build.commit_sha,
        demo_scenario_enabled=settings.demo_scenario_enabled,
        demo_run_id=settings.demo_run_id,
    )


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    """A running process is live even while its database is unavailable."""
    return process_identity()


@router.get("/health", response_model=HealthResponse)
@router.get("/ready", response_model=HealthResponse)
async def health_check(
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> HealthResponse:
    """Fail closed on missing migrations, columns, or build identity."""
    identity = process_identity()
    revision: str | None = None
    database_name: str | None = None
    database_role: str | None = None
    reason: str | None = None
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            revisions = await db.execute(
                text("SELECT version_num FROM alembic_version")
            )
            found = list(revisions.scalars())
            revision = found[0] if len(found) == 1 else None
            await db.execute(select(Task).limit(0))
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                database_name, database_role = (
                    await db.execute(text("SELECT current_database(), current_user"))
                ).one()
        if revision != SCHEMA_REVISION:
            reason = "schema_not_current"
        elif (
            settings.environment not in {"development", "test"}
            and identity.commit_sha == "unknown"
        ):
            reason = "deployment_metadata_missing"
        elif settings.environment not in {"development", "test"} and (
            not settings.database_name or database_name != settings.database_name
        ):
            reason = "database_identity_mismatch"
    except (SQLAlchemyError, TimeoutError, OSError) as exc:
        logger.exception(
            "Readiness database/schema check failed",
            extra={
                "environment": settings.environment,
                "error_type": type(exc).__name__,
            },
        )
        await db.rollback()
        reason = "database_or_schema_unavailable"

    if reason is not None:
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        **identity.model_dump(exclude={"status"}),
        status="healthy" if reason is None else "degraded",
        database="healthy"
        if reason != "database_or_schema_unavailable"
        else "unhealthy",
        schema_revision=revision,
        expected_schema_revision=SCHEMA_REVISION,
        database_name=database_name,
        database_role=database_role,
        reason=reason,
    )
