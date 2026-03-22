"""Health check route handler."""

import logging
import time

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_db
from src.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_start_time = time.time()
APP_VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse)
async def health_check(
    db: AsyncSession = Depends(get_db),
) -> HealthResponse:
    """Detailed health check including database connectivity."""
    db_status = "healthy"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error("Database health check failed: %s", e)
        db_status = "unhealthy"

    uptime = time.time() - _start_time

    return HealthResponse(
        status="healthy" if db_status == "healthy" else "degraded",
        environment=settings.environment,
        database=db_status,
        uptime_seconds=round(uptime, 2),
        version=APP_VERSION,
    )
