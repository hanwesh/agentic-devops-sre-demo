"""Health contracts, actual schema failures, and recovery after transient failures."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, settings
from src.schemas import HealthResponse, LivenessResponse
from src.version import APP_VERSION, SCHEMA_REVISION


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_readiness(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == body.database == "healthy"
    assert body.schema_revision == body.expected_schema_revision == SCHEMA_REVISION
    assert body.reason is None
    assert body.version == APP_VERSION
    assert body.uptime_seconds >= 0
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-correlation-id"]) == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["tasks", "alembic_version"])
async def test_missing_schema_is_not_ready(
    client: AsyncClient, db_session: AsyncSession, table: str
) -> None:
    await db_session.execute(text(f"DROP TABLE {table}"))
    response = await client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unhealthy"
    assert body["reason"] == "database_or_schema_unavailable"
    assert "DROP" not in response.text

    live = await client.get("/live")
    assert live.status_code == 200
    assert LivenessResponse.model_validate(live.json()).status == "alive"


@pytest.mark.asyncio
async def test_wrong_revision_is_not_ready(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(text("UPDATE alembic_version SET version_num = 'future'"))
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["reason"] == "schema_not_current"


@pytest.mark.asyncio
async def test_missing_application_column_is_not_ready(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(text("ALTER TABLE tasks RENAME COLUMN title TO missing"))
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unhealthy"


@pytest.mark.asyncio
async def test_readiness_recovers_after_database_error(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = db_session.execute
    with monkeypatch.context() as patch:
        patch.setattr(
            db_session,
            "execute",
            AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("offline"))),
        )
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "unhealthy"
    assert db_session.execute == original
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_readiness_has_bounded_timeout(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_query(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(settings, "readiness_timeout_seconds", 0.01)
    monkeypatch.setattr(db_session, "execute", slow_query)
    async with asyncio.timeout(0.5):
        response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_or_schema_unavailable"


@pytest.mark.asyncio
async def test_deployed_app_requires_build_identity(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["reason"] == "deployment_metadata_missing"


@pytest.mark.asyncio
async def test_correlation_header_validation(client: AsyncClient) -> None:
    response = await client.get("/live", headers={"X-Correlation-ID": "demo-123"})
    assert response.headers["x-correlation-id"] == "demo-123"
    response = await client.get("/live", headers={"X-Correlation-ID": "unsafe value"})
    assert response.headers["x-correlation-id"] != "unsafe value"


@pytest.mark.asyncio
async def test_root_endpoint(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["readiness"] == "/ready"
    assert response.json()["liveness"] == "/live"


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
def test_demo_configuration_requires_isolation(environment: str) -> None:
    with pytest.raises(ValueError, match="ENVIRONMENT=demo"):
        Settings(
            _env_file=None,
            environment=environment,  # type: ignore[arg-type]
            demo_scenario_enabled=True,
            demo_run_id="run-1",
        )


def test_demo_requires_run_id() -> None:
    with pytest.raises(ValueError, match="DEMO_RUN_ID"):
        Settings(_env_file=None, environment="demo", demo_scenario_enabled=True)
