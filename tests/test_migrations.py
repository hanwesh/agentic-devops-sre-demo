"""PostgreSQL coverage uses only explicitly authorized ephemeral test databases."""

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from subprocess import CompletedProcess
from unittest.mock import patch

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from scripts.migrate import migrate, validate_target
from src.database import get_db
from src.main import app
from src.models import Base
from src.version import SCHEMA_REVISION
from tests.conftest import upgrade_schema


@pytest.fixture
async def postgres_engine() -> AsyncGenerator[AsyncEngine, None]:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip(
            "TEST_POSTGRES_URL is not set; CI runs the PostgreSQL service suite"
        )
    if os.environ.get("ALLOW_EPHEMERAL_POSTGRES") != "true":
        pytest.fail("Set ALLOW_EPHEMERAL_POSTGRES=true for the disposable test server")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg":
        pytest.fail("TEST_POSTGRES_URL must use postgresql+asyncpg")
    database = f"sre_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(
        parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    engine = create_async_engine(parsed.set(database=database), hide_parameters=True)
    try:
        yield engine
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_postgres_upgrade_and_crud(postgres_engine: AsyncEngine) -> None:
    async with postgres_engine.begin() as connection:
        assert not await connection.run_sync(
            lambda conn: inspect(conn).has_table("tasks")
        )
        await connection.run_sync(upgrade_schema)
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == SCHEMA_REVISION
        columns = await connection.run_sync(
            lambda conn: inspect(conn).get_columns("tasks")
        )
        assert {column["name"] for column in columns} == {
            "id",
            "title",
            "description",
            "status",
            "created_at",
            "updated_at",
        }
        assert (
            await connection.run_sync(
                lambda conn: compare_metadata(
                    MigrationContext.configure(conn), Base.metadata
                )
            )
            == []
        )
        await connection.run_sync(upgrade_schema)

    async def real_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(postgres_engine, expire_on_commit=False) as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = real_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://postgres-test"
        ) as client:
            assert (await client.get("/ready")).status_code == 200
            response = await client.post(
                "/api/tasks", json={"title": "migrated schema"}
            )
            assert response.status_code == 201
            task_id = response.json()["id"]
            found = await client.get(f"/api/tasks/{task_id}")
            assert found.status_code == 200
            assert found.json()["title"] == "migrated schema"
            updated = await client.put(
                f"/api/tasks/{task_id}", json={"status": "completed"}
            )
            assert updated.status_code == 200
            assert updated.json()["status"] == "completed"
            listing = await client.get("/api/tasks?status=completed")
            assert listing.json()["items"][0]["id"] == task_id
            assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 204
            assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_migrations_serialize(postgres_engine: AsyncEngine) -> None:
    async def upgrade() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            env={
                **os.environ,
                "DATABASE_URL": postgres_engine.url.render_as_string(
                    hide_password=False
                ),
                "ENVIRONMENT": "test",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()

    await asyncio.gather(upgrade(), upgrade())
    async with postgres_engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM alembic_version"))
        ).scalar_one() == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scoped_migrator_grants_runtime_crud_only(
    postgres_engine: AsyncEngine,
) -> None:
    suffix = uuid.uuid4().hex
    migrator = f"sre_migrator_{suffix}"
    runtime = f"sre_runtime_{suffix}"
    database = postgres_engine.url.database
    assert database is not None and database.startswith("sre_test_")
    password = "ephemeral-ci-only"
    runtime_engine = create_async_engine(
        postgres_engine.url.set(username=runtime, password=password)
    )
    async with postgres_engine.begin() as connection:
        for role in (migrator, runtime):
            await connection.execute(
                text(
                    f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                    f"NOINHERIT NOREPLICATION PASSWORD '{password}'"
                )
            )
        await connection.execute(
            text(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC')
        )
        await connection.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
        await connection.execute(
            text(f'GRANT CONNECT ON DATABASE "{database}" TO "{migrator}", "{runtime}"')
        )
        await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{runtime}"'))
        await connection.execute(
            text(f'GRANT USAGE, CREATE ON SCHEMA public TO "{migrator}"')
        )
    try:
        migrator_url = postgres_engine.url.set(username=migrator, password=password)
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "scripts.migrate",
                "--database",
                database,
                "--user",
                migrator,
                "--runtime-user",
                runtime,
                env={
                    **os.environ,
                    "ENVIRONMENT": "test",
                    "MIGRATION_DATABASE_URL": migrator_url.render_as_string(
                        hide_password=False
                    ),
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, error = await asyncio.wait_for(process.communicate(), timeout=60)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, error.decode()
        task_id = uuid.uuid4()
        async with runtime_engine.begin() as connection:
            assert (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            ).scalar_one() == SCHEMA_REVISION
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, title, status, created_at, updated_at) "
                    "VALUES (:id, 'scoped runtime', 'pending', now(), now())"
                ),
                {"id": task_id},
            )
            assert (
                await connection.execute(
                    text("SELECT title FROM tasks WHERE id=:id"), {"id": task_id}
                )
            ).scalar_one() == "scoped runtime"
            await connection.execute(
                text("UPDATE tasks SET status='completed' WHERE id=:id"),
                {"id": task_id},
            )
            assert (
                await connection.execute(
                    text("SELECT status FROM tasks WHERE id=:id"), {"id": task_id}
                )
            ).scalar_one() == "completed"
            await connection.execute(
                text("DELETE FROM tasks WHERE id=:id"), {"id": task_id}
            )
        for forbidden in (
            "UPDATE alembic_version SET version_num='forged'",
            "CREATE TABLE forbidden_runtime_ddl (id integer)",
        ):
            with pytest.raises(ProgrammingError):
                async with runtime_engine.begin() as connection:
                    await connection.execute(text(forbidden))
    finally:
        await runtime_engine.dispose()
        async with postgres_engine.begin() as connection:
            for role in (runtime, migrator):
                await connection.execute(text(f'DROP OWNED BY "{role}"'))
                await connection.execute(text(f'DROP ROLE "{role}"'))


@pytest.mark.parametrize(
    ("url", "database", "user"),
    [
        ("invalid", "staging", "migrator"),
        ("sqlite:///test", "staging", "migrator"),
        ("postgresql+asyncpg://migrator@localhost/production", "staging", "migrator"),
        ("postgresql+asyncpg://postgres@localhost/staging", "staging", "postgres"),
        ("postgresql+asyncpg://runtime@localhost/staging", "staging", "migrator"),
    ],
)
def test_migrations_refuse_unapproved_target(
    url: str, database: str, user: str
) -> None:
    with pytest.raises(ValueError):
        validate_target(url, database, user)


def test_migration_failure_blocks_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://migrator:local-test@localhost/staging",
    )
    with patch(
        "scripts.migrate.subprocess.run", return_value=CompletedProcess([], 1)
    ) as run:
        with pytest.raises(RuntimeError, match="must not proceed"):
            migrate("staging", "migrator", "runtime")
    assert "local-test" not in str(run.call_args.args)
    assert run.call_args.kwargs["env"]["DATABASE_URL"].endswith("@localhost/staging")


def test_failed_runtime_grants_block_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL", "postgresql+asyncpg://migrator@localhost/staging"
    )
    with (
        patch("scripts.migrate.subprocess.run", return_value=CompletedProcess([], 0)),
        patch("scripts.migrate.grant_runtime", new=AsyncMock(side_effect=OSError)),
        pytest.raises(RuntimeError, match="Runtime grants failed"),
    ):
        migrate("staging", "migrator", "runtime")
