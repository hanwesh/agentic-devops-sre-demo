"""Shared PostgreSQL connection policy for the application and migrations."""

import ssl
from typing import Any

from sqlalchemy.engine import make_url

from src.config import settings


def connect_args(url: str) -> dict[str, Any]:
    if make_url(url).drivername != "postgresql+asyncpg":
        return {}
    options: dict[str, Any] = {
        "timeout": settings.database_connect_timeout_seconds,
        "command_timeout": 30,
        "server_settings": {"statement_timeout": "30000"},
    }
    if settings.database_ssl_required:
        options["ssl"] = ssl.create_default_context()
    return options
