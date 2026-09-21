"""Trusted control-plane helpers for the opt-in, non-production demo workflow."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

import httpx

from scripts.evidence import EndpointCheck, EvidenceEvent
from scripts.release_policy import get_json, validate_environment

EXPECTED_REGRESSION_TESTS = {
    "test_demo_status_defaults_when_status_is_missing",
    "test_demo_filter_returns_task_list",
}


def validate_request(branch: str, run_id: str, state: str, issue: str) -> None:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?", run_id):
        raise ValueError("Invalid demo run ID")
    if branch != f"demo/{run_id}" or state not in {"regression", "healthy"}:
        raise ValueError("Only a matching disposable demo branch is permitted")
    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", issue):
        raise ValueError(
            "Issue must be zero (no incident yet) or a positive issue number"
        )


def prepare(client: httpx.Client, repository: str, branch: str, ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    prefix = f"/repos/{repository}"
    default = get_json(client, prefix).get("default_branch")
    if not isinstance(default, str) or ref != f"refs/heads/{default}":
        raise ValueError("Dispatch the demo workflow from the trusted default branch")
    environment = get_json(client, f"{prefix}/environments/demo")
    policies = get_json(
        client, f"{prefix}/environments/demo/deployment-branch-policies?per_page=100"
    )
    validate_environment(environment, policies, default, require_review=True)
    head = get_json(client, f"{prefix}/git/ref/heads/{branch}")
    sha = head.get("object", {}).get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Demo branch did not resolve to an immutable commit")
    return sha


def git(candidate: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(candidate), *arguments], text=True, timeout=30
    ).strip()


def validate_diff(candidate: Path, trusted_sha: str, target_sha: str) -> None:
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (trusted_sha, target_sha)):
        raise ValueError("Both trusted tooling and candidate must be exact SHAs")
    if git(candidate, "rev-parse", "HEAD") != target_sha:
        raise ValueError("Candidate checkout differs from the approved SHA")
    changed = git(
        candidate, "diff", "--name-only", trusted_sha, target_sha
    ).splitlines()
    for path in changed:
        if path == "src/demo_scenario.py":
            continue
        # New regression tests are allowed; existing safety tests cannot be weakened.
        if path.startswith("tests/") and path.endswith(".py"):
            existing = git(candidate, "ls-tree", trusted_sha, "--", path)
            if not existing:
                continue
        if path.startswith(("docs/", "demo/")) and path.endswith(".md"):
            continue
        raise ValueError(f"Demo branch changes protected code/configuration: {path}")
    entry = git(candidate, "ls-tree", target_sha, "--", "src/demo_scenario.py")
    if not entry.startswith("100644 blob "):
        raise ValueError("Demo helper must be a regular source file")


def verify_regression_tests(path: Path, state: str) -> None:
    document = path.read_bytes()
    if len(document) > 1_000_000 or b"<!DOCTYPE" in document or b"<!ENTITY" in document:
        raise ValueError("Unexpected regression report format or size")
    root = ElementTree.fromstring(document)
    cases = list(root.iter("testcase"))
    if (
        len(cases) != 2
        or {case.attrib.get("name") for case in cases} != EXPECTED_REGRESSION_TESTS
        or any(
            case.attrib.get("classname") != "tests.test_demo_scenario"
            or case.find("error") is not None
            or case.find("skipped") is not None
            for case in cases
        )
    ):
        raise ValueError("Expected exactly the two unchanged positive regression tests")
    failures = sum(case.find("failure") is not None for case in cases)
    if state not in {"healthy", "regression"} or failures != (
        2 if state == "regression" else 0
    ):
        raise ValueError("Regression test results do not match the approved scenario")


def emit_evidence(
    path: Path,
    *,
    kind: Literal["deployment", "recovery"],
    status: Literal["pending", "succeeded", "failed", "unknown"],
    sha: str,
    run_id: str,
    check_file: Path | None,
) -> None:
    check: EndpointCheck | None = None
    if check_file is not None:
        report = json.loads(check_file.read_text())
        if (
            report.get("status") != "passed"
            or report.get("environment") != "demo"
            or report.get("commit_sha") != sha
            or report.get("demo_run_id") != run_id
        ):
            raise ValueError("Smoke artifact does not establish recovery of this run")
        check = EndpointCheck.model_validate(report.get("endpoint_check"))
    event = EvidenceEvent(
        schema_version=1,
        kind=kind,
        event_id=(
            f"{kind}-{os.environ['GITHUB_RUN_ID']}-"
            f"{os.environ['GITHUB_RUN_ATTEMPT']}-{status}"
        ),
        environment="demo",
        scenario_run_id=run_id,
        commit_sha=sha,
        status=status,
        observed_at=datetime.now(UTC),
        workflow_url=(
            f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        ),
        endpoint_check=check,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(event.model_dump_json(indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("prepare")
    plan.add_argument("--branch", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--state", choices=["regression", "healthy"], required=True)
    plan.add_argument("--issue", default="0")
    diff = commands.add_parser("verify-diff")
    diff.add_argument("--candidate", type=Path, required=True)
    diff.add_argument("--trusted-sha", required=True)
    diff.add_argument("--target-sha", required=True)
    tests = commands.add_parser("verify-tests")
    tests.add_argument("--junit", type=Path, required=True)
    tests.add_argument("--state", choices=["regression", "healthy"], required=True)
    evidence = commands.add_parser("evidence")
    evidence.add_argument("--kind", choices=["deployment", "recovery"], required=True)
    evidence.add_argument(
        "--status", choices=["pending", "succeeded", "failed", "unknown"], required=True
    )
    evidence.add_argument("--sha", required=True)
    evidence.add_argument("--run-id", required=True)
    evidence.add_argument("--check-file", type=Path)
    evidence.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            validate_request(args.branch, args.run_id, args.state, args.issue)
            if os.environ.get("EPHEMERAL_RUNNER_CONFIRMED") != "true":
                raise ValueError("Ephemeral runner isolation must be configured")
            if os.environ.get("ADMIN_BYPASS_DISABLED_CONFIRMED") != "true":
                raise ValueError(
                    "Disabled administrator bypass requires operator confirmation; "
                    "it is not exposed by the published REST environment schema"
                )
            labels = json.loads(os.environ.get("AZURE_RUNNER_LABELS", ""))
            if (
                not isinstance(labels, list)
                or not {"self-hosted", "linux", "x64"}.issubset(labels)
                or len(labels) < 4
            ):
                raise ValueError("A dedicated private-network runner is required")
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise ValueError("Missing read-only GitHub authentication")
            with httpx.Client(
                timeout=10,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as client:
                sha = prepare(
                    client,
                    os.environ["GITHUB_REPOSITORY"],
                    args.branch,
                    os.environ["GITHUB_REF"],
                )
            with Path(os.environ["GITHUB_OUTPUT"]).open(
                "a", encoding="utf-8"
            ) as output:
                output.write(f"target_sha={sha}\n")
            print(
                f"Validated demo candidate; human deployment approval is pending: {sha}"
            )
        elif args.command == "verify-diff":
            validate_diff(args.candidate, args.trusted_sha, args.target_sha)
        elif args.command == "verify-tests":
            verify_regression_tests(args.junit, args.state)
        else:
            emit_evidence(
                args.output,
                kind=args.kind,
                status=args.status,
                sha=args.sha,
                run_id=args.run_id,
                check_file=args.check_file,
            )
    except (
        ValueError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        httpx.HTTPError,
        ElementTree.ParseError,
    ) as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(
            f"Demo release refused: {detail}. Inspect the failed step; "
            "no recovery is inferred.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
