"""Version and immutable build metadata shipped with the application artifact."""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

APP_VERSION = "1.1.0"
SCHEMA_REVISION = "0001_tasks"


class BuildInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = APP_VERSION
    commit_sha: str = Field(default="unknown", pattern=r"^(unknown|[0-9a-f]{40})$")


@lru_cache
def get_build_info() -> BuildInfo:
    path = Path(__file__).with_name("build_info.json")
    if not path.exists():
        return BuildInfo()
    return BuildInfo.model_validate_json(path.read_text(encoding="utf-8"))
