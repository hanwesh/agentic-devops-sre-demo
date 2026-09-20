"""The privileged demo workflow uses trusted tooling and an exact regression gate."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.demo_release import (
    EXPECTED_REGRESSION_TESTS,
    emit_evidence,
    validate_diff,
    validate_request,
    verify_regression_tests,
)
from scripts.evidence import EvidenceEvent, validate_event

SHA = "a" * 40


@pytest.mark.parametrize(
    "branch,run_id,state,issue",
    [
        ("main", "run-1", "regression", "0"),
        ("demo/run-2", "run-1", "regression", "0"),
        ("demo/../main", "../main", "healthy", "1"),
        ("demo/run-1", "run-1", "off", "1"),
        ("demo/run-1", "run-1", "healthy", "-1"),
        ("demo/run-1", "run-1", "healthy", "1;echo unsafe"),
    ],
)
def test_unapproved_demo_request_is_refused(
    branch: str, run_id: str, state: str, issue: str
) -> None:
    with pytest.raises(ValueError):
        validate_request(branch, run_id, state, issue)


def report(tmp_path: Path, *, failures: bool, extra: str = "") -> Path:
    body = "<testsuites><testsuite>"
    for name in sorted(EXPECTED_REGRESSION_TESTS):
        body += f'<testcase name="{name}" classname="tests.test_demo_scenario">'
        if failures:
            body += '<failure message="expected positive assertion failed"/>'
        body += "</testcase>"
    body += extra + "</testsuite></testsuites>"
    path = tmp_path / "junit.xml"
    path.write_text(body)
    return path


@pytest.mark.parametrize("state", ["regression", "healthy"])
def test_two_exact_unchanged_tests_are_required(tmp_path: Path, state: str) -> None:
    path = report(tmp_path, failures=state == "regression")
    verify_regression_tests(path, state)
    with pytest.raises(ValueError, match="do not match"):
        verify_regression_tests(
            path, "healthy" if state == "regression" else "regression"
        )


@pytest.mark.parametrize(
    "extra",
    [
        '<testcase name="unexpected"><failure/></testcase>',
        '<testcase name="collection_error"><error/></testcase>',
        '<testcase name="skipped"><skipped/></testcase>',
    ],
)
def test_unrelated_regression_failures_are_not_ignored(
    tmp_path: Path, extra: str
) -> None:
    with pytest.raises(ValueError, match="exactly"):
        verify_regression_tests(
            report(tmp_path, failures=True, extra=extra), "regression"
        )


@pytest.mark.parametrize(
    "changed",
    [
        "src/routes/tasks.py",
        "src/config.py",
        "tests/test_demo_scenario.py",
        "requirements.lock",
    ],
)
def test_demo_cannot_disable_guards_or_weaken_positive_tests(changed: str) -> None:
    def respond(candidate: Path, *args: str) -> str:
        if args[0] == "rev-parse":
            return SHA
        if args[0] == "diff":
            return changed
        return "100644 blob existing"

    with patch("scripts.demo_release.git", side_effect=respond):
        with pytest.raises(ValueError, match="protected"):
            validate_diff(Path("candidate"), "b" * 40, SHA)


def test_recovery_evidence_comes_from_the_original_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    check = {
        "status": "passed",
        "environment": "demo",
        "commit_sha": SHA,
        "demo_run_id": "run-1",
        "endpoint_check": {
            "method": "GET",
            "endpoint": "/api/tasks?filter=broken",
            "observed_at": datetime.now(UTC).isoformat(),
            "status_code": 200,
            "response_environment": "demo",
            "response_commit_sha": SHA,
            "response_run_id": "run-1",
        },
    }
    source = tmp_path / "smoke.json"
    output = tmp_path / "event.json"
    source.write_text(json.dumps(check))
    emit_evidence(
        output,
        kind="recovery",
        status="succeeded",
        sha=SHA,
        run_id="run-1",
        check_file=source,
    )
    event = validate_event(output.read_text())
    assert isinstance(event, EvidenceEvent)
    assert event.endpoint_check is not None
    assert event.endpoint_check.status_code == 200
    check["endpoint_check"]["endpoint"] = "/health"
    source.write_text(json.dumps(check))
    with pytest.raises(ValueError):
        emit_evidence(
            output,
            kind="recovery",
            status="succeeded",
            sha=SHA,
            run_id="run-1",
            check_file=source,
        )


def test_recovery_without_endpoint_evidence_is_never_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    with pytest.raises(ValueError, match="matching original-endpoint"):
        emit_evidence(
            tmp_path / "event.json",
            kind="recovery",
            status="succeeded",
            sha=SHA,
            run_id="run-1",
            check_file=None,
        )
