"""No-network tests of fail-closed, bounded traffic and redacted run evidence."""

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from demo import traffic
from demo.traffic import TrafficConfig, TrafficRefused, run_traffic, validate_target
from src.version import APP_VERSION, SCHEMA_REVISION

COMMIT = "a" * 40
CONFIG = TrafficConfig(
    base_url="https://tasks-demo.azurewebsites.net",
    run_id="test-one",
    expected_commit=COMMIT,
    mode="healthy",
    allow_faults=True,
    requests=21,
    interval=0,
)
TASK = {
    "id": "2b66dcbb-ecec-49b7-9c88-4f335790cd62",
    "title": "Private task data must never appear in evidence",
    "description": "Private description",
    "status": "pending",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def demo_response(
    request: httpx.Request, mode: Literal["healthy", "fault"] = "healthy"
) -> httpx.Response:
    trace_id = request.headers["traceparent"].split("-")[1]
    correlation_id = request.headers["x-correlation-id"]
    headers = {
        "X-Correlation-ID": correlation_id,
        "X-Trace-ID": trace_id,
        "X-Deployment-SHA": COMMIT,
    }
    body: dict[str, Any]
    if request.url.path in {"/live", "/ready"}:
        body = {
            "status": "alive" if request.url.path == "/live" else "healthy",
            "environment": "demo",
            "demo_scenario_enabled": True,
            "demo_run_id": CONFIG.run_id,
            "commit_sha": COMMIT,
            "database": "healthy",
            "version": APP_VERSION,
            "uptime_seconds": 1.0,
            "schema_revision": SCHEMA_REVISION,
            "expected_schema_revision": SCHEMA_REVISION,
            "reason": None,
        }
    elif request.url.params.get("filter") == "broken" and mode == "fault":
        return httpx.Response(
            500,
            headers=headers,
            json={
                "detail": "Internal server error",
                "error_type": "KeyError",
                "path": "/api/tasks",
                "correlation_id": correlation_id,
                "trace_id": trace_id,
            },
        )
    else:
        body = {"items": [TASK], "total": 1, "page": 1, "per_page": 20}
    return httpx.Response(200, headers=headers, json=body)


@pytest.mark.parametrize("mode", ["healthy", "fault"])
@pytest.mark.asyncio
async def test_validated_traffic_is_bounded_get_only_and_redacts_task_data(
    mode: Literal["healthy", "fault"],
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return demo_response(request, mode)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(replace(CONFIG, mode=mode), client=client)

    assert evidence["status"] == "passed"
    assert len(requests) == CONFIG.requests + 6
    assert evidence["scenario_requests"] == CONFIG.requests
    assert evidence["observed_5xx"] == (CONFIG.requests if mode == "fault" else 0)
    assert evidence["eligible_request_count"] == CONFIG.requests + 2
    assert evidence["error_ratio"] == (
        CONFIG.requests / (CONFIG.requests + 2) if mode == "fault" else 0
    )
    assert (
        evidence["error_ratio"] > 0.05
        if mode == "fault"
        else not evidence["error_ratio"]
    )
    assert all(request.method == "GET" for request in requests)
    scenarios = [
        request for request in requests if request.url.params.get("filter") == "broken"
    ]
    assert len(scenarios) == CONFIG.requests
    assert all(request.url.path == "/api/tasks" for request in scenarios)
    assert all(event["correlation_verified"] for event in evidence["events"])
    assert all(event["deployment_verified"] for event in evidence["events"])
    assert all(event["trace_id"] for event in evidence["events"])
    assert evidence["azure_loop_verified"] is False
    assert TASK["title"] not in json.dumps(evidence)
    assert TASK["description"] not in json.dumps(evidence)


@pytest.mark.parametrize(
    "target",
    [
        "https://tasks.azurewebsites.net",
        "https://tasks-staging.azurewebsites.net",
        "http://tasks-demo.azurewebsites.net",
        "https://tasks-demo.azurewebsites.net.attacker.invalid",
        "https://user:secret@tasks-demo.azurewebsites.net",
        "https://tasks-demo.azurewebsites.net/api/tasks",
        "https://tasks-demo.azurewebsites.net?redirect=production",
        "https://tasks-demo.azurewebsites.net#ignored",
        "https://tasks-demo.azurewebsites.net:444",
        "https://tasks-demo.azurewebsites.net:",
        "https://tasks-demo\n.azurewebsites.net",
        "https://example.invalid",
        "http://localhost.attacker.invalid",
        "http://10.0.0.1",
        "http://127.0.0.1:99999",
        "file:///api/tasks",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_target_is_refused_before_any_request(target: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return demo_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(TrafficRefused):
            await run_traffic(replace(CONFIG, base_url=target), client=client)
    assert requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"allow_faults": False},
        {"run_id": "one/../../main"},
        {"expected_commit": "main"},
        {"requests": 20},
        {"requests": 101},
        {"requests": True},
        {"max_duration": 0},
        {"max_duration": 241},
        {"max_duration": float("inf")},
        {"timeout": 0},
        {"timeout": 11},
        {"timeout": float("nan")},
        {"interval": -1},
        {"interval": 6},
        {"interval": 5, "max_duration": 50},
    ],
)
@pytest.mark.asyncio
async def test_acknowledgment_and_finite_hard_bounds_are_required(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(TrafficRefused):
        await run_traffic(replace(CONFIG, **changes))


@pytest.mark.parametrize(
    ("path", "change"),
    [
        ("/live", {"environment": "production"}),
        ("/live", {"demo_scenario_enabled": False}),
        ("/live", {"demo_scenario_enabled": "true"}),
        ("/live", {"demo_run_id": "previous-run"}),
        ("/live", {"commit_sha": "b" * 40}),
        ("/live", {"status": "healthy"}),
        ("/ready", {"environment": "staging"}),
        ("/ready", {"database": "unhealthy"}),
        ("/ready", {"schema_revision": None}),
        ("/ready", {"schema_revision": "old"}),
        ("/ready", {"reason": "database_or_schema_unavailable"}),
    ],
)
@pytest.mark.asyncio
async def test_identity_or_readiness_mismatch_blocks_scenario_requests(
    path: str, change: dict[str, Any]
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = demo_response(request)
        if request.url.path == path:
            return httpx.Response(
                200, headers=response.headers, json={**response.json(), **change}
            )
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(CONFIG, client=client)
    assert evidence["status"] == "failed"
    assert all(request.url.path in {"/live", "/ready"} for request in requests)
    assert evidence["scenario_requests"] == 0


@pytest.mark.parametrize(
    ("mode", "response_mode"),
    [("healthy", "fault"), ("fault", "healthy")],
)
@pytest.mark.asyncio
async def test_unexpected_http_code_stops_at_first_scenario_request(
    mode: Literal["healthy", "fault"], response_mode: Literal["healthy", "fault"]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: demo_response(request, response_mode)
        )
    ) as client:
        evidence = await run_traffic(replace(CONFIG, mode=mode), client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == "unexpected_http_status"
    assert evidence["scenario_requests"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"items": [], "total": 0},
        {"items": [], "total": True, "page": 1, "per_page": 20},
        {"items": "not-a-list", "total": 0, "page": 1, "per_page": 20},
        {"items": [{}], "total": 1, "page": 1, "per_page": 20},
        {"items": [{**TASK, "status": []}], "total": 1, "page": 1, "per_page": 20},
        {"items": [{**TASK, "id": "invalid"}], "total": 1, "page": 1, "per_page": 20},
        {
            "items": [{**TASK, "status": "completed"}],
            "total": 1,
            "page": 1,
            "per_page": 20,
        },
    ],
)
@pytest.mark.asyncio
async def test_healthy_status_alone_cannot_pass_with_wrong_task_shape(
    body: dict[str, Any],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response = demo_response(request)
        if request.url.params.get("filter") == "broken":
            return httpx.Response(200, headers=response.headers, json=body)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(CONFIG, client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] in {"invalid_task_list_shape", "invalid_task_shape"}
    assert evidence["scenario_requests"] == 1


@pytest.mark.asyncio
async def test_fault_mode_requires_the_actual_regression_envelope() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response = demo_response(request, "fault")
        if response.status_code == 500:
            return httpx.Response(
                500,
                headers=response.headers,
                json={**response.json(), "error_type": "DatabaseError"},
            )
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(replace(CONFIG, mode="fault"), client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == "unexpected_fault_shape"
    assert evidence["scenario_requests"] == 1


@pytest.mark.parametrize(
    ("header", "value", "error"),
    [
        ("X-Correlation-ID", "different-request", "response_correlation_mismatch"),
        ("X-Deployment-SHA", "b" * 40, "response_commit_mismatch"),
        ("X-Trace-ID", None, "response_trace_missing_or_mismatched"),
        ("X-Trace-ID", "c" * 32, "response_trace_missing_or_mismatched"),
    ],
)
@pytest.mark.asyncio
async def test_each_response_must_carry_verified_safe_evidence_headers(
    header: str, value: str | None, error: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response = demo_response(request)
        if value is None:
            del response.headers[header]
        else:
            response.headers[header] = value
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(CONFIG, client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == error
    assert evidence["observed_total"] == 1


@pytest.mark.parametrize(
    "target", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
@pytest.mark.asyncio
async def test_loopback_without_exported_trace_is_explicitly_local_only(
    target: str,
) -> None:
    assert validate_target(target)[1] is True

    def respond(request: httpx.Request) -> httpx.Response:
        response = demo_response(request)
        del response.headers["X-Trace-ID"]
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(replace(CONFIG, base_url=target), client=client)
    assert evidence["status"] == "passed"
    assert evidence["scope"] == "loopback"
    assert evidence["azure_loop_verified"] is False
    assert all(event["trace_id"] is None for event in evidence["events"])


@pytest.mark.asyncio
async def test_redirect_is_not_followed_even_when_the_client_default_would_follow() -> (
    None
):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://production.invalid"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        evidence = await run_traffic(CONFIG, client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == "unexpected_http_status"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_network_error_is_not_retried_or_exported_verbatim() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Do not disclose proxy or connection secrets")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(CONFIG, client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == "http_request_failed"
    assert evidence["observed_total"] == 0
    assert "secrets" not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_total_duration_budget_cancels_a_slow_response() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return demo_response(request)

    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(replace(CONFIG, max_duration=0.02), client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == "duration_budget_exceeded"
    assert time.monotonic() - started < 1
    assert evidence["scenario_requests"] == 0


@pytest.mark.asyncio
async def test_flag_changed_during_the_run_does_not_produce_a_false_green() -> None:
    liveness_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal liveness_reads
        response = demo_response(request)
        if request.url.path == "/live":
            liveness_reads += 1
            if liveness_reads == 2:
                return httpx.Response(
                    200,
                    headers=response.headers,
                    json={**response.json(), "demo_scenario_enabled": False},
                )
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        evidence = await run_traffic(CONFIG, client=client)
    assert evidence["status"] == "failed"
    assert evidence["error"] == "deployment_identity_mismatch"
    assert evidence["scenario_requests"] == CONFIG.requests


def test_cli_requires_acknowledgment_without_issuing_requests(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = traffic.main(
        [
            "--base-url",
            CONFIG.base_url,
            "--run-id",
            CONFIG.run_id,
            "--expected-commit",
            COMMIT,
            "--mode",
            "fault",
        ]
    )
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_cli_preserves_existing_evidence_and_exits_nonzero_for_failed_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path.cwd() / f".demo-evidence-test-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        monkeypatch.chdir(root)

        async def failure(config: TrafficConfig) -> dict[str, Any]:
            return {"status": "failed", "error": "unexpected_http_status"}

        monkeypatch.setattr(traffic, "run_traffic", failure)
        arguments = [
            "--base-url",
            CONFIG.base_url,
            "--run-id",
            CONFIG.run_id,
            "--expected-commit",
            COMMIT,
            "--mode",
            "fault",
            "--allow-faults",
            "--evidence",
            "run.json",
        ]
        assert traffic.main(arguments) == 1
        first = (root / "run.json").read_text()
        assert json.loads(first)["status"] == "failed"
        assert traffic.main(arguments) == 2
        assert (root / "run.json").read_text() == first
        assert "evidence_requires_a_new_file" in capsys.readouterr().out
        with pytest.raises(TrafficRefused):
            traffic.evidence_path("../outside.json")
    finally:
        monkeypatch.chdir(root.parent)
        shutil.rmtree(root)
