"""Application configuration via environment variables."""

import re
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings; credentials are supplied by the environment."""

    app_name: str = "Agentic DevOps SRE Demo"
    environment: Literal["development", "test", "demo", "staging", "production"] = (
        "development"
    )
    debug: bool = False
    database_url: str = Field(
        default="postgresql+asyncpg://localhost/tasksdb", repr=False
    )
    database_ssl_required: bool = False
    database_name: str = ""
    database_connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    readiness_timeout_seconds: float = Field(default=5, gt=0, le=30)
    applicationinsights_connection_string: str = Field(default="", repr=False)
    demo_scenario_enabled: bool = False
    demo_run_id: str = ""
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @model_validator(mode="after")
    def isolate_demo(self) -> Self:
        if self.demo_run_id and not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,47}", self.demo_run_id
        ):
            raise ValueError("DEMO_RUN_ID must be a 1-48 character lowercase slug")
        if self.demo_scenario_enabled and (
            self.environment != "demo" or not self.demo_run_id
        ):
            raise ValueError(
                "Demo scenarios require ENVIRONMENT=demo and an explicit DEMO_RUN_ID"
            )
        if self.debug and self.environment in {"production", "staging", "demo"}:
            raise ValueError("DEBUG must be disabled on deployed environments")
        return self


settings = Settings()
