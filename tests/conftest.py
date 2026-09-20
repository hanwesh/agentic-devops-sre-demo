"""Isolated test databases built from the committed migrations."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from alembic.config import Config
from src.database import get_db
from src.main import app


def upgrade_schema(connection: Connection) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with test_engine.begin() as connection:
        await connection.run_sync(upgrade_schema)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        try:
            yield db_session
        except Exception:
            await db_session.rollback()
            raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
