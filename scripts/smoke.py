"""Bounded, identity-aware smoke checks. Only staging/demo checks may create data."""

import argparse
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from src.schemas import HealthResponse, TaskListResponse, TaskResponse
from src.version import APP_VERSION, SCHEMA_REVISION

ALLOWED_ENDPOINTS = {"/api/tasks", "/api/tasks?filter=broken"}


class CheckFailure(RuntimeError):
    """An observed failure, with a safe message suitable for release evidence."""


@dataclass(frozen=True)
class Target:
    url: str
    environment: str
    commit_sha: str
    demo_run_id: str = ""
    allow_loopback: bool = False
    version: str = APP_VERSION
    database_name: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("The target must be an origin URL without credentials")
        if self.environment not in {"production", "staging", "demo"}:
            raise ValueError("A deployed environment must be explicit")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_sha):
            raise ValueError("An exact deployed commit SHA is required")
        if self.allow_loopback and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("Loopback checks require HTTP or HTTPS")
            return
        suffix = "" if self.environment == "production" else f"-{self.environment}"
        if (
            parsed.scheme != "https"
            or parsed.port not in {None, 443}
            or not re.fullmatch(
                rf"[a-z0-9-]+{suffix}\.azurewebsites\.net", parsed.hostname or ""
            )
        ):
            raise ValueError("Target must be the explicit HTTPS Azure environment host")
        if self.environment == "production" and (parsed.hostname or "").endswith(
            ("-staging.azurewebsites.net", "-demo.azurewebsites.net")
        ):
            raise ValueError("Production checks cannot target a non-production slot")


def json_body(response: httpx.Response, status: int) -> Any:
    if response.status_code != status:
        raise CheckFailure(f"Unexpected HTTP {response.status_code}; expected {status}")
    try:
        return response.json()
    except ValueError as exc:
        raise CheckFailure("Response is not valid JSON") from exc


def check_readiness(client: httpx.Client, target: Target) -> HealthResponse:
    body = json_body(client.get(f"{target.url}/ready"), 200)
    try:
        health = HealthResponse.model_validate(body, strict=True)
    except ValidationError as exc:
        raise CheckFailure("Readiness JSON does not match the health contract") from exc
    if (
        health.status != "healthy"
        or health.database != "healthy"
        or health.reason is not None
        or health.environment != target.environment
        or health.commit_sha != target.commit_sha
        or health.version != target.version
        or health.schema_revision != SCHEMA_REVISION
        or health.expected_schema_revision != SCHEMA_REVISION
    ):
        raise CheckFailure("Readiness identity, schema, version, or status mismatch")
    if target.environment != "demo" and health.demo_scenario_enabled:
        raise CheckFailure("Scenario unexpectedly enabled outside the demo environment")
    if target.database_name and health.database_name != target.database_name:
        raise CheckFailure("Observed database does not match the approved target")
    if target.demo_run_id and (
        health.demo_run_id != target.demo_run_id or not health.demo_scenario_enabled
    ):
        raise CheckFailure("Demo run identity or scenario configuration mismatch")
    return health


def wait_ready(
    client: httpx.Client,
    target: Target,
    *,
    attempts: int = 12,
    delay: float = 5,
) -> HealthResponse:
    if not 1 <= attempts <= 20 or not 0 <= delay <= 10:
        raise ValueError("Readiness retries must be bounded to 1-20, with delay <=10s")
    last_failure = "Readiness was not attempted"
    for attempt in range(attempts):
        try:
            return check_readiness(client, target)
        except (CheckFailure, httpx.TransportError) as exc:
            last_failure = (
                str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
            )
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise CheckFailure(f"Readiness failed after {attempts} attempts: {last_failure}")


def check_endpoint(
    client: httpx.Client, target: Target, endpoint: str
) -> dict[str, Any]:
    if endpoint not in ALLOWED_ENDPOINTS:
        raise ValueError("Only the documented task GET endpoints are permitted")
    if endpoint.endswith("filter=broken") and (
        target.environment != "demo" or not target.demo_run_id
    ):
        raise ValueError("Scenario verification requires an explicit demo run")
    response = client.get(f"{target.url}{endpoint}")
    try:
        tasks = TaskListResponse.model_validate(json_body(response, 200))
    except ValidationError as exc:
        raise CheckFailure(
            "Task endpoint returned an invalid task-list schema"
        ) from exc
    if (
        tasks.page != 1
        or tasks.per_page != 20
        or len(tasks.items) > 20
        or tasks.total < len(tasks.items)
    ):
        raise CheckFailure("Task endpoint pagination/count contract is invalid")
    if endpoint.endswith("filter=broken") and any(
        item.status != "pending" for item in tasks.items
    ):
        raise CheckFailure("Original endpoint no longer applies the default status")
    if (
        response.headers.get("x-deployment-sha") != target.commit_sha
        or response.headers.get("x-environment") != target.environment
    ):
        raise CheckFailure("Original endpoint responded from a different deployment")
    if target.demo_run_id and (
        response.headers.get("x-demo-run-id") != target.demo_run_id
        or response.headers.get("x-demo-scenario-enabled") != "true"
    ):
        raise CheckFailure("Original endpoint scenario identity does not match")
    return {
        "correlation_id": response.headers.get("x-correlation-id"),
        "trace_id": response.headers.get("x-trace-id"),
        "endpoint_check": {
            "method": "GET",
            "endpoint": endpoint,
            "observed_at": datetime.now(UTC).isoformat(),
            "status_code": response.status_code,
            "response_environment": response.headers["x-environment"],
            "response_commit_sha": response.headers["x-deployment-sha"],
            "response_run_id": response.headers.get("x-demo-run-id", ""),
        },
    }


def _task(response: httpx.Response, status: int) -> TaskResponse:
    try:
        return TaskResponse.model_validate(json_body(response, status))
    except ValidationError as exc:
        raise CheckFailure("CRUD response does not match the task schema") from exc


def check_crud(client: httpx.Client, target: Target) -> str:
    if target.environment not in {"staging", "demo"}:
        raise ValueError("CRUD smoke checks are forbidden against production")
    marker = f"sre-smoke:{uuid.uuid4().hex}"
    task_id: uuid.UUID | None = None
    try:
        try:
            created = _task(
                client.post(
                    f"{target.url}/api/tasks",
                    json={"title": marker, "description": marker, "status": "pending"},
                ),
                201,
            )
        except (CheckFailure, httpx.TransportError) as exc:
            raise CheckFailure(
                f"Create outcome uncertain; reconcile smoke marker {marker} "
                f"only in the approved {target.environment} database"
            ) from exc
        if created.title != marker or created.description != marker:
            raise CheckFailure(f"Created task ownership is ambiguous; marker {marker}")
        task_id = created.id
        path = f"{target.url}/api/tasks/{task_id}"
        fetched = _task(client.get(path), 200)
        if fetched.id != task_id or fetched.title != marker:
            raise CheckFailure("Created task cannot be read back correctly")
        updated = _task(client.put(path, json={"status": "completed"}), 200)
        if updated.id != task_id or updated.status != "completed":
            raise CheckFailure("Task update did not persist the requested status")
        persisted = _task(client.get(path), 200)
        if persisted.status != "completed":
            raise CheckFailure("Task update was not committed")
    finally:
        if task_id is not None:
            path = f"{target.url}/api/tasks/{task_id}"
            response = client.get(path)
            if response.status_code != 404:
                owned = _task(response, 200)
                if owned.id != task_id or owned.description != marker:
                    raise CheckFailure(
                        f"Refusing cleanup of a non-owned task {task_id}"
                    )
                deleted = client.delete(path)
                if deleted.status_code != 204 or client.get(path).status_code != 404:
                    raise CheckFailure(f"Cleanup failed for smoke task {task_id}")
    return str(task_id)


def run_checks(
    client: httpx.Client,
    target: Target,
    *,
    crud: bool = False,
    endpoint: str = "/api/tasks",
    attempts: int = 12,
    delay: float = 5,
) -> dict[str, Any]:
    if crud and target.environment == "production":
        raise ValueError("Production verification is read-only")
    if crud and not target.database_name:
        raise ValueError(
            "CRUD checks require an independently configured database name"
        )
    if endpoint not in ALLOWED_ENDPOINTS:
        raise ValueError("Unsupported original endpoint")
    wait_ready(client, target, attempts=attempts, delay=delay)
    record_id = check_crud(client, target) if crud else None
    correlation = check_endpoint(client, target, endpoint)
    return {
        "schema_version": 1,
        "status": "passed",
        "environment": target.environment,
        "commit_sha": target.commit_sha,
        "version": target.version,
        "schema_revision": SCHEMA_REVISION,
        "database_name": target.database_name or None,
        "demo_run_id": target.demo_run_id,
        "checked_at": datetime.now(UTC).isoformat(),
        "endpoint": endpoint,
        "crud": "passed" if crud else "not_requested",
        "cleanup": "verified_deleted" if crud else "not_requested",
        "created_task_id": record_id,
        **correlation,
    }


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    content = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(content, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--environment", choices=["staging", "production", "demo"], required=True
    )
    parser.add_argument("--sha", required=True)
    parser.add_argument("--demo-run-id", default="")
    parser.add_argument("--database", default="")
    parser.add_argument("--crud", action="store_true")
    parser.add_argument(
        "--endpoint", choices=sorted(ALLOWED_ENDPOINTS), default="/api/tasks"
    )
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--allow-loopback", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        target = Target(
            args.url.rstrip("/"),
            args.environment,
            args.sha,
            args.demo_run_id,
            args.allow_loopback,
            database_name=args.database,
        )
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            report = run_checks(
                client,
                target,
                crud=args.crud,
                endpoint=args.endpoint,
                attempts=args.attempts,
                delay=args.delay,
            )
    except (CheckFailure, ValueError, httpx.TransportError) as exc:
        reason = (
            str(exc)
            if not isinstance(exc, httpx.TransportError)
            else type(exc).__name__
        )
        report = {
            "schema_version": 1,
            "status": "failed",
            "environment": args.environment,
            "commit_sha": args.sha,
            "checked_at": datetime.now(UTC).isoformat(),
            "endpoint": args.endpoint,
            "reason": reason,
        }
        write_report(args.output, report)
        print("Smoke verification failed; do not promote", file=sys.stderr)
        return 1
    write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
