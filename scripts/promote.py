"""Migrate, swap, verify, and (only when schema-compatible) roll back app content."""

import argparse
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from scripts.migrate import migrate
from scripts.smoke import CheckFailure, Target, run_checks, write_report
from src.schemas import HealthResponse
from src.version import SCHEMA_REVISION


class PromotionFailed(RuntimeError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(str(report["status"]))
        self.report = report


def swap(app: str, resource_group: str) -> None:
    subprocess.run(
        [
            "az",
            "webapp",
            "deployment",
            "slot",
            "swap",
            "--resource-group",
            resource_group,
            "--name",
            app,
            "--slot",
            "staging",
            "--target-slot",
            "production",
            "--only-show-errors",
            "--output",
            "none",
        ],
        check=True,
        timeout=600,
    )


def promote(
    client: httpx.Client,
    *,
    app: str,
    resource_group: str,
    commit_sha: str,
    database: str,
    staging_database: str,
    user: str,
    runtime_user: str,
    allow_initial_deployment: bool = False,
    attempts: int = 12,
    delay: float = 5,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]", app):
        raise ValueError("Invalid Azure application name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}", resource_group):
        raise ValueError("Invalid resource group name")
    if not staging_database or staging_database == database:
        raise ValueError(
            "Production and staging databases must be explicit and distinct"
        )
    target = Target(
        f"https://{app}.azurewebsites.net",
        "production",
        commit_sha,
        database_name=database,
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "environment": "production",
        "commit_sha": commit_sha,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "not_started",
        "rollback": "not_attempted",
    }
    previous: HealthResponse | None = None
    try:
        response = client.get(f"{target.url}/ready")
        if response.status_code != 200:
            raise CheckFailure("Previous production readiness is not healthy")
        previous = HealthResponse.model_validate(response.json())
        if (
            previous.status != "healthy"
            or previous.database != "healthy"
            or previous.environment != "production"
            or previous.database_name != database
            or previous.demo_scenario_enabled
            or previous.reason is not None
            or not re.fullmatch(r"[0-9a-f]{40}", previous.commit_sha)
        ):
            raise CheckFailure("Previous production identity is not trustworthy")
    except (CheckFailure, ValidationError, ValueError, httpx.TransportError):
        previous = None
        if not allow_initial_deployment:
            report["status"] = "blocked_missing_previous_identity"
            raise PromotionFailed(report) from None
        report["rollback"] = "unavailable_initial_deployment"
    report["previous_commit_sha"] = previous.commit_sha if previous else None

    try:
        migrate(database, user, runtime_user)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        report["status"] = "migration_failed_no_swap"
        raise PromotionFailed(report) from None
    report["migration"] = "passed"
    try:
        swap(app, resource_group)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # A failed/timed-out control-plane request may already have swapped slots.
        report["status"] = "swap_state_unknown_operator_reconciliation_required"
        raise PromotionFailed(report) from None

    try:
        report["verification"] = run_checks(
            client, target, attempts=attempts, delay=delay
        )
    except (CheckFailure, httpx.TransportError) as exc:
        report["status"] = "verification_failed"
        report["failure"] = (
            str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        )
        if previous is None or previous.schema_revision != SCHEMA_REVISION:
            report["rollback"] = "blocked_unknown_or_incompatible_schema"
            raise PromotionFailed(report) from None
        try:
            swap(app, resource_group)
            previous_target = Target(
                target.url,
                "production",
                previous.commit_sha,
                version=previous.version,
                database_name=database,
            )
            report["rollback_verification"] = run_checks(
                client, previous_target, attempts=attempts, delay=delay
            )
        except (
            CheckFailure,
            httpx.TransportError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ):
            report["rollback"] = "failed_operator_intervention_required"
            raise PromotionFailed(report) from None
        report["rollback"] = "verified_previous_application"
        raise PromotionFailed(report) from None
    if previous is not None:
        try:
            report["staging_restoration"] = run_checks(
                client,
                Target(
                    f"https://{app}-staging.azurewebsites.net",
                    "staging",
                    previous.commit_sha,
                    version=previous.version,
                    database_name=staging_database,
                ),
                attempts=attempts,
                delay=delay,
            )
        except (CheckFailure, httpx.TransportError):
            report["status"] = "production_verified_staging_restore_failed"
            raise PromotionFailed(report) from None
    else:
        report["staging_restoration"] = "unverified_initial_deployment"
    report["status"] = "verified"
    report["completed_at"] = datetime.now(UTC).isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--staging-database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--runtime-user", required=True)
    parser.add_argument("--allow-initial-deployment", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            report = promote(
                client,
                app=args.app,
                resource_group=args.resource_group,
                commit_sha=args.sha,
                database=args.database,
                staging_database=args.staging_database,
                user=args.user,
                runtime_user=args.runtime_user,
                allow_initial_deployment=args.allow_initial_deployment,
            )
    except PromotionFailed as exc:
        write_report(args.output, exc.report)
        return 1
    write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
