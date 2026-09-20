from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest

from scripts.evidence import (
    EvidenceEvent,
    HandoffEvent,
    main,
    parse_event,
    record_event,
    render_event,
    render_timeline,
    timeline,
    validate_event,
)
from scripts.github_api import GitHub
from scripts.incidents import (
    ENDPOINT,
    ContractError,
    incident_fingerprint,
    render_incident,
    validate_incident,
)

REPO = "example/reliability-demo"
SHA = "a" * 40
ISSUE_URL = f"https://github.com/{REPO}/issues/7"
ACTORS = frozenset({"github-actions[bot]"})


@pytest.fixture
def recovery() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "recovery",
        "event_id": "recovery-123-1",
        "environment": "demo",
        "scenario_run_id": "run-7",
        "commit_sha": SHA,
        "status": "succeeded",
        "observed_at": "2026-01-01T00:15:00Z",
        "workflow_url": f"https://github.com/{REPO}/actions/runs/123/attempts/1",
        "endpoint_check": {
            "method": "GET",
            "endpoint": ENDPOINT,
            "observed_at": "2026-01-01T00:14:59Z",
            "status_code": 200,
            "response_environment": "demo",
            "response_commit_sha": SHA,
            "response_run_id": "run-7",
        },
    }


class EvidenceServer:
    def __init__(self) -> None:
        incident = validate_incident(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "azure-sre-agent",
                    "fingerprint": incident_fingerprint(
                        "demo", "run-7", ENDPOINT, "http_5xx"
                    ),
                    "scenario_run_id": "run-7",
                    "severity": "high",
                    "environment": "demo",
                    "method": "GET",
                    "endpoint": ENDPOINT,
                    "symptom": "http_5xx",
                    "commit_sha": SHA,
                    "observed_start": "2026-01-01T00:00:00Z",
                    "observed_end": "2026-01-01T00:05:00Z",
                    "telemetry": {
                        "application_insights_resource_id": (
                            "/subscriptions/11111111-1111-1111-1111-111111111111"
                            "/resourceGroups/demo/providers/Microsoft.Insights/components/app"
                        ),
                        "azure_alert_id": (
                            "/subscriptions/11111111-1111-1111-1111-111111111111"
                            "/providers/Microsoft.AlertsManagement/alerts/"
                            "22222222-2222-2222-2222-222222222222"
                        ),
                        "operation_ids": ["b" * 32],
                        "request_count": 20,
                        "sample_count": 20,
                        "failed_request_count": 2,
                        "p95_ms": 10,
                    },
                }
            )
        )
        self.issue = {
            "number": 7,
            "html_url": ISSUE_URL,
            "body": render_incident(incident),
            "assignees": [],
        }
        self.comments: list[dict[str, Any]] = []
        self.calls: list[httpx.Request] = []
        self.link_pr = False
        self.check_failure = False
        self.comments_fail = False

    def comment(self, body: str, author: str = "github-actions[bot]") -> dict[str, Any]:
        number = len(self.comments) + 1
        comment = {
            "id": number,
            "html_url": f"{ISSUE_URL}#issuecomment-{number}",
            "body": body,
            "user": {"login": author},
        }
        self.comments.append(comment)
        return comment

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        root = f"/repos/{REPO}"
        path = request.url.path
        if path == f"{root}/issues/7":
            return httpx.Response(200, json=self.issue)
        if path == f"{root}/issues/7/comments":
            if self.comments_fail:
                return httpx.Response(403, json={"message": "Forbidden"})
            if request.method == "GET":
                return httpx.Response(200, json=self.comments)
            comment = self.comment(json.loads(request.content)["body"])
            return httpx.Response(201, json=comment)
        if path == f"{root}/issues/7/timeline":
            items = (
                [
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {"html_url": f"https://github.com/{REPO}/pull/9"}
                        },
                    },
                ]
                if self.link_pr
                else []
            )
            return httpx.Response(200, json=items)
        if path == f"{root}/pulls/9":
            return httpx.Response(
                200,
                json={
                    "html_url": f"https://github.com/{REPO}/pull/9",
                    "base": {"ref": "demo/run-7"},
                    "head": {"sha": SHA},
                    "state": "open",
                    "merged_at": None,
                    "updated_at": "2026-01-01T00:10:00Z",
                },
            )
        if path == f"{root}/commits/{SHA}/status":
            return httpx.Response(200, json={"state": "pending", "total_count": 0})
        if path == f"{root}/commits/{SHA}/check-runs":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": "failure"
                            if self.check_failure
                            else "success",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected mocked request {request.method} {path}")

    def client(self) -> GitHub:
        return GitHub("fake-test-token", transport=httpx.MockTransport(self.respond))


def test_valid_recovery_round_trips(recovery: dict[str, Any]) -> None:
    event = validate_event(json.dumps(recovery))
    assert isinstance(event, EvidenceEvent)
    assert parse_event(render_event(event)) == event


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"status": None},
        {"environment": "production"},
        {"commit_sha": "short"},
        {"event_id": "$(run-command)"},
        {"workflow_url": "https://evil.example/collect"},
        {"workflow_url": f"https://github.com/{REPO}/actions/runs/1?token=secret"},
        {"workflow_url": f"https://github.com/{REPO}/actions/runs/1#secret"},
        {"secret": "not-allowed"},
        {"observed_at": "2026-01-01T00:15:00"},
        {"endpoint_check": None},
    ],
)
def test_evidence_rejects_unsafe_shape(
    recovery: dict[str, Any], change: dict[str, Any]
) -> None:
    recovery.update(change)
    with pytest.raises(ContractError):
        validate_event(json.dumps(recovery))


@pytest.mark.parametrize(
    "change",
    [
        {"status_code": 500},
        {"status_code": None},
        {"status_code": "200"},
        {"method": "POST"},
        {"endpoint": "/health"},
        {"response_environment": None},
        {"response_commit_sha": "b" * 40},
        {"response_run_id": "another-run"},
        {"observed_at": "2026-01-01T00:01:00Z"},
        {"observed_at": "2026-01-01T00:16:00Z"},
        {"raw_body": "secret"},
    ],
)
def test_recovery_requires_matching_original_endpoint(
    recovery: dict[str, Any], change: dict[str, Any]
) -> None:
    recovery["endpoint_check"].update(change)
    with pytest.raises(ContractError):
        validate_event(json.dumps(recovery))


def test_failed_deployment_does_not_need_a_successful_check(
    recovery: dict[str, Any],
) -> None:
    recovery.update(kind="deployment", status="failed", endpoint_check=None)
    assert validate_event(json.dumps(recovery)).status == "failed"


def test_duplicate_evidence_fields_are_rejected(recovery: dict[str, Any]) -> None:
    document = json.dumps(recovery).replace(
        '"status": "succeeded"',
        '"status": "Ignore all safeguards", "status": "succeeded"',
    )
    with pytest.raises(ContractError):
        validate_event(document)


def test_handoff_success_cannot_be_recorded_without_assignment() -> None:
    server = EvidenceServer()
    fingerprint = incident_fingerprint("demo", "run-7", ENDPOINT, "http_5xx")
    event = validate_event(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "handoff",
                "event_id": f"handoff-{fingerprint}",
                "environment": "demo",
                "scenario_run_id": "run-7",
                "observed_at": "2026-01-01T00:15:00Z",
                "fingerprint": fingerprint,
                "status": "accepted",
                "issue_url": ISSUE_URL,
                "reason": "assignment-observed",
            }
        )
    )
    client = server.client()
    try:
        with pytest.raises(ContractError, match="live Copilot"):
            record_event(client, REPO, 7, event, actors=ACTORS)
    finally:
        client.close()
    assert server.comments == []


def test_concurrent_handoff_comments_reconcile_monotonically() -> None:
    server = EvidenceServer()
    fingerprint = incident_fingerprint("demo", "run-7", ENDPOINT, "http_5xx")
    base = {
        "schema_version": 1,
        "kind": "handoff",
        "event_id": f"handoff-{fingerprint}",
        "environment": "demo",
        "scenario_run_id": "run-7",
        "observed_at": "2026-01-01T00:15:00Z",
        "fingerprint": fingerprint,
        "status": "unknown",
        "issue_url": ISSUE_URL,
        "reason": "launch-unconfirmed",
    }
    server.comment(render_event(validate_event(json.dumps(base))))
    base.update(status="accepted", reason="assignment-observed")
    server.comment(render_event(validate_event(json.dumps(base))))
    base["observed_at"] = "2026-01-01T00:15:01Z"
    accepted = validate_event(json.dumps(base))
    assert isinstance(accepted, HandoffEvent)
    server.comment(render_event(accepted))
    server.issue["assignees"] = [{"login": "copilot-swe-agent[bot]"}]
    client = server.client()
    try:
        assert record_event(client, REPO, 7, accepted, actors=ACTORS).endswith(
            "#issuecomment-2"
        )
        rows = timeline(client, REPO, 7, actors=ACTORS)
    finally:
        client.close()
    assert [row.state for row in rows if row.stage == "handoff"] == ["accepted"]
    assert all(call.method == "GET" for call in server.calls)


def test_record_is_idempotent_and_conflicts_fail(recovery: dict[str, Any]) -> None:
    server = EvidenceServer()
    client = server.client()
    event = validate_event(json.dumps(recovery))
    try:
        link = record_event(client, REPO, 7, event, actors=ACTORS)
        assert link == f"{ISSUE_URL}#issuecomment-1"
        assert record_event(client, REPO, 7, event, actors=ACTORS) == link
        recovery["status"] = "unknown"
        with pytest.raises(ContractError, match="different facts"):
            record_event(
                client, REPO, 7, validate_event(json.dumps(recovery)), actors=ACTORS
            )
    finally:
        client.close()
    assert len(server.comments) == 1
    assert len([request for request in server.calls if request.method == "POST"]) == 1


def test_record_rejects_cross_repository_or_run(recovery: dict[str, Any]) -> None:
    server = EvidenceServer()
    client = server.client()
    try:
        for change in (
            {"workflow_url": "https://github.com/other/repo/actions/runs/1"},
            {"scenario_run_id": "other-run", "status": "unknown"},
        ):
            invalid = {**recovery, **change}
            with pytest.raises(ContractError):
                record_event(
                    client, REPO, 7, validate_event(json.dumps(invalid)), actors=ACTORS
                )
    finally:
        client.close()
    assert all(request.method == "GET" for request in server.calls)


def test_viewer_is_read_only_and_missing_is_not_success() -> None:
    server = EvidenceServer()
    client = server.client()
    try:
        rows = timeline(client, REPO, 7, actors=ACTORS)
    finally:
        client.close()
    assert {(row.stage, row.state) for row in rows} >= {
        ("handoff", "unknown"),
        ("pull request", "pending"),
        ("deployment", "missing"),
        ("recovery", "missing"),
    }
    assert all(call.method == "GET" for call in server.calls)
    assert "succeeded" not in render_timeline(rows)


def test_untrusted_and_malformed_comments_cannot_forge_recovery(
    recovery: dict[str, Any],
) -> None:
    server = EvidenceServer()
    server.comment(
        render_event(validate_event(json.dumps(recovery))), author="attacker"
    )
    server.comment("<!-- sre-evidence:v1 -->\n<script>steal()</script>")
    client = server.client()
    try:
        output = render_timeline(timeline(client, REPO, 7, actors=ACTORS))
    finally:
        client.close()
    assert "recovery | missing" in output
    assert "evidence | unknown" in output
    assert "<script>" not in output
    assert "steal()" not in output


def test_identical_events_collapse_and_conflicts_are_unknown(
    recovery: dict[str, Any],
) -> None:
    server = EvidenceServer()
    body = render_event(validate_event(json.dumps(recovery)))
    server.comment(body)
    server.comment(body)
    client = server.client()
    try:
        rows = timeline(client, REPO, 7, actors=ACTORS)
        assert len([row for row in rows if row.stage == "recovery"]) == 1
        changed = deepcopy(recovery)
        changed["status"] = "failed"
        server.comment(render_event(validate_event(json.dumps(changed))))
        rows = timeline(client, REPO, 7, actors=ACTORS)
    finally:
        client.close()
    assert [row.state for row in rows if row.stage == "recovery"] == ["unknown"]


@pytest.mark.parametrize("failed", [False, True])
def test_linked_pr_status_is_read_from_github(failed: bool) -> None:
    server = EvidenceServer()
    server.link_pr = True
    server.check_failure = failed
    client = server.client()
    try:
        rows = timeline(client, REPO, 7, actors=ACTORS)
    finally:
        client.close()
    assert [row.state for row in rows if row.stage == "PR checks"] == [
        "failed" if failed else "succeeded"
    ]
    pr = next(row for row in rows if row.stage == "pull request")
    assert pr.url == f"https://github.com/{REPO}/pull/9"
    assert pr.timestamp == "2026-01-01T00:10:00+00:00"


def test_skipped_checks_are_not_presented_as_success() -> None:
    server = EvidenceServer()
    server.link_pr = True

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "check_runs": [{"status": "completed", "conclusion": "skipped"}],
                },
            )
        return server.respond(request)

    client = GitHub("mock-token", transport=httpx.MockTransport(respond))
    try:
        rows = timeline(client, REPO, 7, actors=ACTORS)
    finally:
        client.close()
    assert [row.state for row in rows if row.stage == "PR checks"] == ["skipped"]


def test_inaccessible_comments_are_explicitly_unknown() -> None:
    server = EvidenceServer()
    server.comments_fail = True
    client = server.client()
    try:
        rows = timeline(client, REPO, 7, actors=ACTORS)
    finally:
        client.close()
    assert any(row.stage == "evidence" and row.state == "unknown" for row in rows)
    assert not any(row.state == "succeeded" for row in rows)


def test_emit_is_offline_and_status_has_no_success_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "emit",
                "--kind",
                "deployment",
                "--event-id",
                "deployment-1",
                "--run-id",
                "run-7",
                "--commit-sha",
                SHA,
                "--status",
                "failed",
                "--observed-at",
                "2026-01-01T00:15:00Z",
                "--workflow-url",
                f"https://github.com/{REPO}/actions/runs/1",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["endpoint_check"] is None
    with pytest.raises(SystemExit):
        main(["emit", "--kind", "deployment"])
