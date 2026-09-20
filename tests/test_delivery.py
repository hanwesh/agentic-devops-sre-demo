"""Release gates check real response contracts and never clean up unrelated tasks."""

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from scripts.package import build_package, verify_package
from scripts.promote import PromotionFailed, promote
from scripts.smoke import CheckFailure, Target, run_checks
from src.version import APP_VERSION, SCHEMA_REVISION

SHA = "a" * 40
OLD_SHA = "b" * 40
TASK_ID = "12345678-1234-1234-1234-123456789abc"


def healthy(environment: str = "staging", sha: str = SHA) -> dict[str, object]:
    return {
        "status": "healthy",
        "database": "healthy",
        "environment": environment,
        "version": APP_VERSION,
        "commit_sha": sha,
        "uptime_seconds": 10.0,
        "schema_revision": SCHEMA_REVISION,
        "expected_schema_revision": SCHEMA_REVISION,
        "reason": None,
        "demo_scenario_enabled": environment == "demo",
        "demo_run_id": "run-1" if environment == "demo" else "",
        "database_name": environment,
        "database_role": "runtime",
    }


def list_body() -> dict[str, object]:
    return {"items": [], "total": 0, "page": 1, "per_page": 20}


def list_response(environment: str = "staging", sha: str = SHA) -> httpx.Response:
    return httpx.Response(
        200,
        json=list_body(),
        headers={
            "X-Deployment-SHA": sha,
            "X-Environment": environment,
            "X-Demo-Run-ID": "run-1" if environment == "demo" else "",
            "X-Demo-Scenario-Enabled": "true" if environment == "demo" else "false",
        },
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://app-staging.azurewebsites.net",
        "https://app.azurewebsites.net",
        "https://app-staging.azurewebsites.net.evil.example",
        "https://user:password@app-staging.azurewebsites.net",
        "https://app-staging.azurewebsites.net/?secret=1",
    ],
)
def test_smoke_refuses_ambiguous_staging_targets(url: str) -> None:
    with pytest.raises(ValueError):
        Target(url, "staging", SHA)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "degraded"),
        ("environment", "production"),
        ("database", "unhealthy"),
        ("schema_revision", "wrong"),
        ("expected_schema_revision", "wrong"),
        ("commit_sha", OLD_SHA),
        ("version", "wrong"),
        ("reason", "missing"),
        ("demo_scenario_enabled", True),
    ],
)
def test_http_200_is_not_sufficient(field: str, value: object) -> None:
    body = {**healthy(), field: value}
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CheckFailure, match="after 3 attempts"):
            run_checks(
                client,
                Target("https://app-staging.azurewebsites.net", "staging", SHA),
                attempts=3,
                delay=0,
            )
    assert calls == 3


@pytest.mark.parametrize("fail_update", [False, True])
def test_crud_cleans_only_its_record_even_after_failure(fail_update: bool) -> None:
    saved: dict[str, object] = {}
    deleted: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return httpx.Response(200, json=healthy())
        if request.method == "POST":
            saved.update(json.loads(request.content))
            saved.update(
                {
                    "id": TASK_ID,
                    "created_at": "2026-09-20T01:00:00Z",
                    "updated_at": "2026-09-20T01:00:00Z",
                }
            )
            return httpx.Response(201, json=saved)
        if request.method == "PUT":
            if fail_update:
                return httpx.Response(500, json={"detail": "failed update"})
            saved["status"] = "completed"
        if request.method == "DELETE":
            deleted.append(request.url.path)
            saved.clear()
            return httpx.Response(204)
        if request.url.path == "/api/tasks":
            return list_response()
        return httpx.Response(200 if saved else 404, json=saved)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        target = Target(
            "https://app-staging.azurewebsites.net",
            "staging",
            SHA,
            database_name="staging",
        )
        if fail_update:
            with pytest.raises(CheckFailure, match="Unexpected HTTP 500"):
                run_checks(client, target, crud=True, attempts=1, delay=0)
        else:
            report = run_checks(client, target, crud=True, attempts=1, delay=0)
            assert report["cleanup"] == "verified_deleted"
            assert report["created_task_id"] == TASK_ID
    assert not saved
    assert deleted == [f"/api/tasks/{TASK_ID}"]


def test_no_production_crud() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: pytest.fail(
                "Production CRUD must be refused before network I/O"
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="read-only"):
            run_checks(
                client,
                Target("https://app.azurewebsites.net", "production", SHA),
                crud=True,
            )


def test_staging_identity_cannot_conceal_a_production_database() -> None:
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={**healthy(), "database_name": "production"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CheckFailure, match="approved target"):
            run_checks(
                client,
                Target(
                    "https://app-staging.azurewebsites.net",
                    "staging",
                    SHA,
                    database_name="staging",
                ),
                crud=True,
                attempts=1,
                delay=0,
            )
    assert methods == ["GET"]


def test_recovery_checks_the_original_endpoint() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/ready":
            return httpx.Response(200, json=healthy("demo"))
        return httpx.Response(500, json={"detail": "still broken"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CheckFailure, match="Unexpected HTTP 500"):
            run_checks(
                client,
                Target("https://app-demo.azurewebsites.net", "demo", SHA, "run-1"),
                endpoint="/api/tasks?filter=broken",
                attempts=1,
                delay=0,
            )
    assert seen[-1].endswith("/api/tasks?filter=broken")


def test_smoke_rejects_missing_json_schema() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "healthy"})
        )
    ) as client:
        with pytest.raises(CheckFailure, match="contract"):
            run_checks(
                client,
                Target("https://app-staging.azurewebsites.net", "staging", SHA),
                attempts=1,
            )


def test_package_is_commit_bound_and_deterministic(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    build_package(source, first, SHA)
    build_package(source, second, SHA)
    assert first.read_bytes() == second.read_bytes()
    verify_package(first, SHA)
    with pytest.raises(ValueError, match="gated commit"):
        verify_package(first, OLD_SHA)
    with zipfile.ZipFile(first) as package:
        assert b"fastapi==" in package.read("requirements.txt")
        assert "alembic/versions/0001_tasks.py" in package.namelist()


@pytest.mark.parametrize("migration_fails", [False, True])
def test_migration_failure_prevents_swap_and_app_failure_rolls_back(
    migration_fails: bool,
) -> None:
    swaps: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            if len(swaps) == 1:
                return httpx.Response(503, json={"status": "degraded"})
            return httpx.Response(200, json=healthy("production", OLD_SHA))
        return list_response("production", OLD_SHA)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch("scripts.promote.swap", side_effect=lambda *args: swaps.append("swap")),
        patch(
            "scripts.promote.migrate",
            side_effect=RuntimeError("migration failed") if migration_fails else None,
        ),
        pytest.raises(PromotionFailed) as error,
    ):
        promote(
            client,
            app="example-app",
            resource_group="demo-rg",
            commit_sha=SHA,
            database="production",
            staging_database="staging",
            user="migrator",
            runtime_user="runtime",
            attempts=1,
            delay=0,
        )
    if migration_fails:
        assert not swaps
        assert error.value.report["status"] == "migration_failed_no_swap"
    else:
        assert len(swaps) == 2
        assert error.value.report["rollback"] == "verified_previous_application"
        assert error.value.report["rollback_verification"]["commit_sha"] == OLD_SHA


def test_swap_uncertainty_never_blindly_swaps_again() -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=healthy("production", OLD_SHA))
            )
        ) as client,
        patch("scripts.promote.migrate"),
        patch("scripts.promote.swap", side_effect=OSError("connection lost")) as swap,
        pytest.raises(PromotionFailed) as error,
    ):
        promote(
            client,
            app="example-app",
            resource_group="demo-rg",
            commit_sha=SHA,
            database="production",
            staging_database="staging",
            user="migrator",
            runtime_user="runtime",
        )
    assert swap.call_count == 1
    assert "unknown_operator_reconciliation" in error.value.report["status"]


@pytest.mark.parametrize("staging_restored", [True, False])
def test_swap_verifies_restored_staging_without_mutations(
    staging_restored: bool,
) -> None:
    swapped = False
    methods: list[str] = []

    def swap(*args: str) -> None:
        nonlocal swapped
        swapped = True

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.host == "example-app-staging.azurewebsites.net":
            environment = "staging" if staging_restored else "production"
            sha = OLD_SHA
        else:
            environment = "production"
            sha = SHA if swapped else OLD_SHA
        if request.url.path == "/ready":
            return httpx.Response(200, json=healthy(environment, sha))
        return list_response(environment, sha)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch("scripts.promote.migrate"),
        patch("scripts.promote.swap", side_effect=swap) as swap_mock,
    ):
        options = dict(
            app="example-app",
            resource_group="demo-rg",
            commit_sha=SHA,
            database="production",
            staging_database="staging",
            user="migrator",
            runtime_user="runtime",
            attempts=1,
            delay=0,
        )
        if staging_restored:
            report = promote(client, **options)
            assert report["status"] == "verified"
            assert report["staging_restoration"]["environment"] == "staging"
            assert report["staging_restoration"]["database_name"] == "staging"
            assert report["staging_restoration"]["commit_sha"] == OLD_SHA
        else:
            with pytest.raises(PromotionFailed) as error:
                promote(client, **options)
            assert error.value.report["status"] == (
                "production_verified_staging_restore_failed"
            )
        assert swap_mock.call_count == 1
    assert set(methods) == {"GET"}


def test_unknown_create_result_retains_marker_for_safe_reconciliation() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return httpx.Response(200, json=healthy())
        assert request.method == "POST"
        raise httpx.ReadTimeout("unknown create result")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(CheckFailure, match="reconcile smoke marker sre-smoke:"):
            run_checks(
                client,
                Target(
                    "https://app-staging.azurewebsites.net",
                    "staging",
                    SHA,
                    database_name="staging",
                ),
                crud=True,
                attempts=1,
                delay=0,
            )
