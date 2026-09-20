"""Run approved migrations from a network-authorized runner, never over web SSH."""

import argparse
import asyncio
import os
import re
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from src.db_options import connect_args


def validate_target(url: str, database: str, username: str) -> None:
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise ValueError("MIGRATION_DATABASE_URL is not a valid database URL") from exc
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("Migrations require a postgresql+asyncpg URL")
    if not database or not username:
        raise ValueError("Expected database and migrator user are required")
    if parsed.database != database or parsed.username != username:
        raise ValueError(
            "Migration connection does not match the approved database/user"
        )
    if username in {"postgres", "azure_pg_admin"}:
        raise ValueError("Deployed migrations must not run as the server administrator")


async def grant_runtime(url: str, runtime_user: str) -> None:
    engine = create_async_engine(
        url, hide_parameters=True, connect_args=connect_args(url)
    )
    try:
        async with engine.begin() as connection:
            role = connection.dialect.identifier_preparer.quote(runtime_user)
            await connection.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.tasks TO {role}")
            )
            await connection.execute(
                text(f"GRANT SELECT ON public.alembic_version TO {role}")
            )
    finally:
        await engine.dispose()


def migrate(database: str, username: str, runtime_user: str) -> None:
    url = os.environ.get("MIGRATION_DATABASE_URL", "")
    validate_target(url, database, username)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", runtime_user) or runtime_user in {
        username,
        "postgres",
        "azure_pg_admin",
        "public",
    }:
        raise ValueError("A distinct, approved runtime role is required")
    env = {**os.environ, "DATABASE_URL": url}
    # The URL stays in the environment, never in argv or console output.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError("Alembic upgrade failed; deployment must not proceed")
    try:
        asyncio.run(grant_runtime(url, runtime_user))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        raise RuntimeError(
            "Runtime grants failed after migration; deployment must not proceed"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--runtime-user", required=True)
    args = parser.parse_args()
    try:
        migrate(args.database, args.user, args.runtime_user)
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    print("Database upgraded to the committed Alembic head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
