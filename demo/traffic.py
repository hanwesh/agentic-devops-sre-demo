"""Send bounded, identity-checked demo traffic and emit redacted JSON evidence."""

import argparse
import asyncio
import ipaddress
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.schemas import HealthResponse, LivenessResponse
from src.version import APP_VERSION, SCHEMA_REVISION

RUN_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
TRACE_PATTERN = re.compile(r"[0-9a-f]{32}")
DEMO_HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?-demo\.azurewebsites\.net"
)
SCENARIO_PATH = "/api/tasks?filter=broken"


class TrafficRefused(ValueError):
    """A machine-readable, non-sensitive reason to stop sending requests."""


@dataclass(frozen=True)
class TrafficConfig:
    base_url: str
    run_id: str
    expected_commit: str
    mode: Literal["healthy", "fault"]
    allow_faults: bool = False
    requests: int = 30
    max_duration: float = 120
    timeout: float = 5
    interval: float = 0.25


def validate_target(base_url: str) -> tuple[str, bool]:
    """Allow only a dedicated Azure demo-slot hostname or explicit loopback."""
    try:
        parts = urlsplit(base_url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise TrafficRefused("invalid_target_url") from exc
    if (
        any(character.isspace() or ord(character) < 32 for character in base_url)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in base_url
        or "#" in base_url
        or parts.path not in {"", "/"}
        or parts.scheme not in {"http", "https"}
        or parts.netloc.endswith(":")
    ):
        raise TrafficRefused("ambiguous_target_url")
    loopback = host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if loopback:
        if port is not None and not 1 <= port <= 65535:
            raise TrafficRefused("invalid_target_port")
    elif (
        DEMO_HOST_PATTERN.fullmatch(host) is None
        or parts.scheme != "https"
        or port not in {None, 443}
    ):
        raise TrafficRefused("target_is_not_a_dedicated_demo_slot")
    return urlunsplit((parts.scheme, parts.netloc.lower(), "", "", "")), loopback


def validate_config(config: TrafficConfig) -> tuple[str, bool]:
    if not config.allow_faults:
        raise TrafficRefused("explicit_allow_faults_acknowledgment_required")
    if RUN_ID_PATTERN.fullmatch(config.run_id) is None:
        raise TrafficRefused("invalid_run_id")
    if SHA_PATTERN.fullmatch(config.expected_commit) is None:
        raise TrafficRefused("expected_commit_must_be_a_full_sha")
    if config.mode not in {"healthy", "fault"}:
        raise TrafficRefused("invalid_mode")
    if type(config.requests) is not int or not 21 <= config.requests <= 100:
        raise TrafficRefused("request_count_must_be_between_21_and_100")
    if not all(
        math.isfinite(value)
        for value in (config.max_duration, config.timeout, config.interval)
    ):
        raise TrafficRefused("request_bounds_must_be_finite")
    if not 0 < config.max_duration <= 240 or not 0 < config.timeout <= 10:
        raise TrafficRefused("duration_or_timeout_out_of_bounds")
    if not 0 <= config.interval <= 5:
        raise TrafficRefused("interval_out_of_bounds")
    if config.interval * (config.requests - 1) >= config.max_duration:
        raise TrafficRefused("intervals_exceed_duration_budget")
    return validate_target(config.base_url)


def json_object(response: httpx.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").split(";")[0] != "application/json":
        raise TrafficRefused("response_is_not_json")
    try:
        body = response.json()
    except ValueError as exc:
        raise TrafficRefused("invalid_json_response") from exc
    if not isinstance(body, dict):
        raise TrafficRefused("response_is_not_an_object")
    return body


def check_identity(body: dict[str, Any], config: TrafficConfig, ready: bool) -> None:
    try:
        (HealthResponse if ready else LivenessResponse).model_validate(body)
    except ValueError as exc:
        raise TrafficRefused("invalid_health_response_shape") from exc
    if (
        body.get("environment") != "demo"
        or body.get("demo_scenario_enabled") is not True
        or body.get("demo_run_id") != config.run_id
        or body.get("commit_sha") != config.expected_commit
        or body.get("version") != APP_VERSION
    ):
        raise TrafficRefused("deployment_identity_mismatch")
    if body.get("status") != ("healthy" if ready else "alive"):
        raise TrafficRefused("deployment_is_not_healthy")
    if ready and (
        body.get("database") != "healthy"
        or body.get("schema_revision") != SCHEMA_REVISION
        or body.get("expected_schema_revision") != SCHEMA_REVISION
        or body.get("reason") is not None
    ):
        raise TrafficRefused("database_or_schema_is_not_ready")


def check_task_list(body: dict[str, Any], *, scenario: bool) -> None:
    items = body.get("items")
    total = body.get("total")
    if (
        not isinstance(items, list)
        or type(total) is not int
        or total < len(items)
        or len(items) > 20
        or type(body.get("page")) is not int
        or body.get("page") != 1
        or type(body.get("per_page")) is not int
        or body.get("per_page") != 20
    ):
        raise TrafficRefused("invalid_task_list_shape")
    for item in items:
        if not isinstance(item, dict) or (
            not isinstance(item.get("title"), str)
            or not item["title"]
            or len(item["title"]) > 255
            or "description" not in item
            or (
                item["description"] is not None
                and not isinstance(item["description"], str)
            )
            or not isinstance(item.get("status"), str)
            or item["status"] not in {"pending", "in_progress", "completed"}
            or (scenario and item["status"] != "pending")
        ):
            raise TrafficRefused("invalid_task_shape")
        try:
            uuid.UUID(item["id"])
            datetime.fromisoformat(item["created_at"])
            datetime.fromisoformat(item["updated_at"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise TrafficRefused("invalid_task_shape") from exc


async def exercise(
    client: httpx.AsyncClient,
    config: TrafficConfig,
    target: str,
    loopback: bool,
    events: list[dict[str, Any]],
) -> None:
    async def request(path: str, kind: str, expected_status: int) -> dict[str, Any]:
        correlation_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        traceparent = f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"
        started = time.monotonic()
        response = await client.get(
            target + path,
            headers={
                "X-Correlation-ID": correlation_id,
                "traceparent": traceparent,
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
            timeout=config.timeout,
            follow_redirects=False,
        )
        returned_trace = response.headers.get("x-trace-id")
        safe_trace = (
            returned_trace
            if returned_trace and TRACE_PATTERN.fullmatch(returned_trace)
            else None
        )
        events.append(
            {
                "sequence": len(events) + 1,
                "kind": kind,
                "path": path,
                "http_status": response.status_code,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "correlation_id": correlation_id,
                "correlation_verified": (
                    response.headers.get("x-correlation-id") == correlation_id
                ),
                "sent_trace_id": trace_id,
                "trace_id": safe_trace,
                "deployment_verified": (
                    response.headers.get("x-deployment-sha") == config.expected_commit
                ),
            }
        )
        if response.status_code != expected_status:
            raise TrafficRefused("unexpected_http_status")
        if response.headers.get("x-deployment-sha") != config.expected_commit:
            raise TrafficRefused("response_commit_mismatch")
        if response.headers.get("x-correlation-id") != correlation_id:
            raise TrafficRefused("response_correlation_mismatch")
        if (returned_trace is not None and safe_trace != trace_id) or (
            not loopback and safe_trace is None
        ):
            raise TrafficRefused("response_trace_missing_or_mismatched")
        body = json_object(response)
        if expected_status == 500 and (
            body.get("detail") != "Internal server error"
            or body.get("error_type") != "KeyError"
            or body.get("path") != "/api/tasks"
            or body.get("correlation_id") != correlation_id
            or body.get("trace_id") != safe_trace
        ):
            raise TrafficRefused("unexpected_fault_shape")
        return body

    async def identity_checks() -> None:
        check_identity(await request("/live", "liveness", 200), config, ready=False)
        check_identity(await request("/ready", "readiness", 200), config, ready=True)

    await identity_checks()
    check_task_list(await request("/api/tasks", "control", 200), scenario=False)
    for index in range(config.requests):
        body = await request(
            SCENARIO_PATH, "scenario", 500 if config.mode == "fault" else 200
        )
        if config.mode == "healthy":
            check_task_list(body, scenario=True)
        if index + 1 < config.requests:
            await asyncio.sleep(config.interval)
    check_task_list(await request("/api/tasks", "control", 200), scenario=False)
    await identity_checks()


async def run_traffic(
    config: TrafficConfig, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    target, loopback = validate_config(config)
    events: list[dict[str, Any]] = []
    started = time.monotonic()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "scenario": "optional-status-filter",
        "run_id": config.run_id,
        "target": target,
        "expected_commit_sha": config.expected_commit,
        "mode": config.mode,
        "scope": "loopback" if loopback else "azure-demo-slot",
        "started_at": datetime.now(UTC).isoformat(),
        "scenario_request_limit": config.requests,
        "total_request_limit": config.requests + 6,
        "max_duration_seconds": config.max_duration,
        "azure_loop_verified": False,
        "events": events,
        "status": "passed",
    }
    try:
        async with asyncio.timeout(config.max_duration):
            if client is not None:
                await exercise(client, config, target, loopback, events)
            else:
                async with httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                ) as owned_client:
                    await exercise(owned_client, config, target, loopback, events)
    except TimeoutError:
        evidence.update(status="failed", error="duration_budget_exceeded")
    except httpx.HTTPError:
        evidence.update(status="failed", error="http_request_failed")
    except TrafficRefused as exc:
        evidence.update(status="failed", error=str(exc))
    eligible = [event for event in events if event["kind"] in {"control", "scenario"}]
    evidence.update(
        completed_at=datetime.now(UTC).isoformat(),
        duration_seconds=round(time.monotonic() - started, 3),
        observed_total=len(events),
        scenario_requests=sum(event["kind"] == "scenario" for event in events),
        observed_5xx=sum(event["http_status"] >= 500 for event in events),
        eligible_request_count=len(eligible),
        error_ratio=(
            sum(500 <= event["http_status"] < 600 for event in eligible) / len(eligible)
            if eligible
            else 0
        ),
    )
    return evidence


def evidence_path(value: str) -> Path:
    path = Path(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parent.is_dir()
        or path.exists()
        or path.is_symlink()
        or not path.resolve().is_relative_to(Path.cwd().resolve())
    ):
        raise TrafficRefused(
            "evidence_requires_a_new_file_inside_the_current_directory"
        )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--mode", choices=["healthy", "fault"], required=True)
    parser.add_argument("--allow-faults", action="store_true")
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--max-duration", type=float, default=120)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--evidence", help="New relative JSON file; never overwritten")
    args = parser.parse_args(argv)
    config = TrafficConfig(
        base_url=args.base_url,
        run_id=args.run_id,
        expected_commit=args.expected_commit,
        mode=args.mode,
        allow_faults=args.allow_faults,
        requests=args.requests,
        max_duration=args.max_duration,
        timeout=args.timeout,
        interval=args.interval,
    )
    try:
        validate_config(config)
        if args.evidence:
            path = evidence_path(args.evidence)
            with path.open("x", encoding="utf-8") as output:
                evidence = asyncio.run(run_traffic(config))
                output.write(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        else:
            evidence = asyncio.run(run_traffic(config))
    except (TrafficRefused, OSError) as exc:
        reason = (
            str(exc) if isinstance(exc, TrafficRefused) else "evidence_write_failed"
        )
        print(json.dumps({"status": "refused", "error": reason}))
        return 2
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
