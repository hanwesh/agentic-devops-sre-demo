"""Validate artifacts, append issue evidence, or read an incident timeline."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from scripts.github_api import GitHub, GitHubError, validate_repository
from scripts.incidents import (
    ENDPOINT,
    MAX_DOCUMENT_BYTES,
    SHA,
    Actor,
    ContractError,
    Digest,
    RunID,
    StrictModel,
    parse_incident,
    parse_json,
    read_document,
    utc_timestamp,
)

EventID = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")]
WorkflowURL = Annotated[
    str,
    StringConstraints(
        max_length=240,
        pattern=(
            r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
            r"/actions/runs/[1-9][0-9]{0,19}(?:/attempts/[1-9][0-9]{0,3})?$"
        ),
    ),
]
IssueURL = Annotated[
    str,
    StringConstraints(
        max_length=240,
        pattern=(
            r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
            r"/issues/[1-9][0-9]{0,19}$"
        ),
    ),
]


class EndpointCheck(StrictModel):
    method: Literal["GET"]
    endpoint: Literal["/api/tasks?filter=broken"]
    observed_at: AwareDatetime
    status_code: Annotated[int, Field(strict=True, ge=100, le=599)] | None
    response_environment: Literal["demo"] | None = None
    response_commit_sha: SHA | None = None
    response_run_id: RunID | None = None

    _utc = field_validator("observed_at")(utc_timestamp)


class EventBase(StrictModel):
    schema_version: Literal[1]
    event_id: EventID
    environment: Literal["demo"] = "demo"
    scenario_run_id: RunID
    observed_at: AwareDatetime

    _utc = field_validator("observed_at")(utc_timestamp)


class EvidenceEvent(EventBase):
    kind: Literal["deployment", "recovery"]
    commit_sha: SHA
    status: Literal["pending", "succeeded", "failed", "unknown"]
    workflow_url: WorkflowURL
    endpoint_check: EndpointCheck | None = None

    @model_validator(mode="after")
    def verify_recovery_claim(self) -> Self:
        check = self.endpoint_check
        if check is not None and not (
            timedelta(0)
            <= self.observed_at - check.observed_at
            <= timedelta(minutes=10)
        ):
            raise ValueError(
                "endpoint check must precede the event by at most ten minutes"
            )
        if self.kind == "recovery" and self.status == "succeeded":
            if (
                check is None
                or check.status_code != 200
                or check.response_environment != self.environment
                or check.response_commit_sha != self.commit_sha
                or check.response_run_id != self.scenario_run_id
            ):
                raise ValueError("recovery requires a matching original-endpoint check")
        return self


class HandoffEvent(EventBase):
    kind: Literal["handoff"]
    fingerprint: Digest
    status: Literal["accepted", "failed", "unknown"]
    issue_url: IssueURL
    reason: Literal[
        "assignment-observed",
        "launch-unconfirmed",
        "assignment-rejected",
        "reservation-owned-by-another-issue",
    ]

    @model_validator(mode="after")
    def consistent_assignment(self) -> Self:
        if self.event_id != f"handoff-{self.fingerprint}":
            raise ValueError("handoff event_id must bind the fingerprint")
        if (self.status == "accepted") != (self.reason == "assignment-observed"):
            raise ValueError("accepted handoff requires assignment-observed evidence")
        return self


Event = EvidenceEvent | HandoffEvent
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(
    Annotated[Event, Field(discriminator="kind")]
)


def validate_event(document: str) -> Event:
    if len(document.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ContractError("evidence document exceeds the size limit")
    try:
        return EVENT_ADAPTER.validate_python(parse_json(document))
    except ValidationError as exc:
        raise ContractError("evidence JSON does not satisfy the v1 contract") from exc


def render_event(event: Event) -> str:
    return (
        f"<!-- sre-evidence:v1 event={event.event_id} -->\n"
        f"```json\n{event.model_dump_json(indent=2)}\n```\n"
    )


def parse_event(body: str) -> Event:
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ContractError("evidence comment exceeds the size limit")
    match = re.fullmatch(
        r"<!-- sre-evidence:v1 event=([a-z0-9][a-z0-9-]{0,79}) -->\n"
        r"```json\n(.+)\n```\n?",
        body,
        re.DOTALL,
    )
    if not match:
        raise ContractError("invalid evidence envelope")
    event = validate_event(match[2])
    if match[1] != event.event_id:
        raise ContractError("event marker and event_id disagree")
    return event


def issue_url(repository: str, number: int, value: Any) -> str:
    expected = f"https://github.com/{repository}/issues/{number}"
    if not isinstance(value, str) or value != expected:
        raise ContractError("GitHub issue URL does not match the requested issue")
    return value


def comment_url(repository: str, number: int, comment: dict[str, Any]) -> str:
    comment_id = comment.get("id")
    value = comment.get("html_url")
    if type(comment_id) is not int or comment_id < 1:
        raise ContractError("GitHub returned an invalid comment ID")
    expected = (
        f"https://github.com/{repository}/issues/{number}#issuecomment-{comment_id}"
    )
    if value != expected:
        raise ContractError("GitHub returned an invalid comment URL")
    return expected


def trusted_comments(
    api: GitHub, path: str, actors: frozenset[str]
) -> list[dict[str, Any]]:
    if not actors:
        raise ContractError("at least one exact evidence author must be configured")
    return [
        comment
        for comment in api.pages(f"{path}/comments")
        if isinstance(comment.get("user"), dict)
        and comment["user"].get("login") in actors
    ]


def same_handoff(left: Event, right: Event) -> bool:
    return (
        isinstance(left, HandoffEvent)
        and isinstance(right, HandoffEvent)
        and left.model_dump(exclude={"observed_at", "status", "reason"})
        == right.model_dump(exclude={"observed_at", "status", "reason"})
    )


def record_event(
    api: GitHub,
    repository: str,
    number: int,
    event: Event,
    *,
    actors: frozenset[str],
) -> str:
    """Append once logically; a reused ID with different facts fails closed."""
    validate_repository(repository)
    if number < 1:
        raise ContractError("issue number must be positive")
    path = f"/repos/{repository}/issues/{number}"
    issue = api.object("GET", path)
    url = issue_url(repository, number, issue.get("html_url"))
    incident = parse_incident(issue.get("body") or "")
    if (
        "pull_request" in issue
        or incident.environment != event.environment
        or incident.scenario_run_id != event.scenario_run_id
    ):
        raise ContractError("evidence does not describe the requested incident")
    if isinstance(event, EvidenceEvent):
        if not event.workflow_url.startswith(
            f"https://github.com/{repository}/actions/runs/"
        ):
            raise ContractError("workflow URL must belong to this repository")
    elif event.fingerprint != incident.fingerprint or event.issue_url != url:
        raise ContractError("handoff evidence does not match the incident")
    elif event.status == "accepted" and not any(
        actor.get("login") == "copilot-swe-agent[bot]"
        for actor in issue.get("assignees", [])
    ):
        raise ContractError("accepted handoff requires live Copilot assignee proof")

    existing: list[tuple[Event, dict[str, Any]]] = []
    marker = f"<!-- sre-evidence:v1 event={event.event_id} -->"
    for comment in trusted_comments(api, path, actors):
        body = comment.get("body") or ""
        if body.startswith(marker):
            existing.append((parse_event(body), comment))
    if existing:
        if any(
            old != existing[0][0] and not same_handoff(old, existing[0][0])
            for old, _ in existing
        ):
            raise ContractError(
                "conflicting stored events require manual reconciliation"
            )
        existing.sort(
            key=lambda item: (item[0].status != "accepted", item[0].observed_at)
        )
        old, comment = existing[0]
        if old == event or (
            isinstance(old, HandoffEvent)
            and isinstance(event, HandoffEvent)
            and (old.status == event.status or old.status == "accepted")
            and old.fingerprint == event.fingerprint
        ):
            return comment_url(repository, number, comment)
        if not (
            isinstance(old, HandoffEvent)
            and isinstance(event, HandoffEvent)
            and old.status != "accepted"
            and event.status == "accepted"
        ):
            raise ContractError("event_id already exists with different facts")
        comment_url(repository, number, comment)
        result = api.object(
            "PATCH",
            f"/repos/{repository}/issues/comments/{comment['id']}",
            payload={"body": render_event(event)},
        )
    else:
        result = api.object(
            "POST",
            f"{path}/comments",
            payload={"body": render_event(event)},
            expected=(201,),
        )
    if (result.get("user") or {}).get("login") not in actors:
        raise ContractError("evidence was posted by an unconfigured author")
    return comment_url(repository, number, result)


@dataclass(frozen=True)
class TimelineRow:
    stage: str
    state: str
    timestamp: str = "-"
    url: str = "-"
    detail: str = "-"


def safe_time(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return TypeAdapter(AwareDatetime).validate_python(value).isoformat()
    except ValidationError:
        return "-"


def pull_request_rows(
    api: GitHub, repository: str, number: int, base_branch: str
) -> list[TimelineRow]:
    rows: list[TimelineRow] = []
    pr_numbers: set[int] = set()
    for item in api.pages(f"/repos/{repository}/issues/{number}/timeline"):
        source = (item.get("source") or {}).get("issue") or {}
        url = source.get("html_url")
        if item.get("event") != "cross-referenced" or not isinstance(url, str):
            continue
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/pull/([1-9][0-9]*)",
            url,
        )
        if match:
            pr_numbers.add(int(match[1]))
    for pr_number in sorted(pr_numbers):
        pr = api.object("GET", f"/repos/{repository}/pulls/{pr_number}")
        if (pr.get("base") or {}).get("ref") != base_branch:
            continue
        url = pr.get("html_url")
        if url != f"https://github.com/{repository}/pull/{pr_number}":
            raise ContractError("linked PR URL does not match its repository")
        state = (
            "merged"
            if pr.get("merged_at")
            else "pending"
            if pr.get("state") == "open"
            else "closed"
        )
        rows.append(
            TimelineRow(
                "pull request",
                state,
                safe_time(pr.get("merged_at") or pr.get("updated_at")),
                url,
                "human review required" if state == "pending" else "-",
            )
        )
        sha = (pr.get("head") or {}).get("sha", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ContractError("linked PR has no valid head SHA")
        statuses = api.object("GET", f"/repos/{repository}/commits/{sha}/status")
        checks = api.object("GET", f"/repos/{repository}/commits/{sha}/check-runs")
        check_runs = checks.get("check_runs", [])
        count = statuses.get("total_count", 0) + checks.get("total_count", 0)
        if len(check_runs) != checks.get("total_count"):
            state = "unknown"
        elif not count:
            state = "missing"
        elif statuses.get("state") in {"failure", "error"} or any(
            check.get("conclusion") in {"failure", "timed_out", "cancelled"}
            for check in check_runs
        ):
            state = "failed"
        elif any(check.get("status") != "completed" for check in check_runs) or (
            statuses.get("total_count") and statuses.get("state") == "pending"
        ):
            state = "pending"
        elif all(
            check.get("conclusion") in {"success", "neutral", "skipped"}
            for check in check_runs
        ) and (not statuses.get("total_count") or statuses.get("state") == "success"):
            state = (
                "succeeded"
                if statuses.get("total_count")
                or any(check.get("conclusion") == "success" for check in check_runs)
                else "skipped"
            )
        else:
            state = "unknown"
        rows.append(TimelineRow("PR checks", state, url=url))
    return rows or [TimelineRow("pull request", "pending", detail="no linked demo PR")]


def timeline(
    api: GitHub, repository: str, number: int, *, actors: frozenset[str]
) -> list[TimelineRow]:
    validate_repository(repository)
    if number < 1:
        raise ContractError("issue number must be positive")
    path = f"/repos/{repository}/issues/{number}"
    issue = api.object("GET", path)
    url = issue_url(repository, number, issue.get("html_url"))
    incident = parse_incident(issue.get("body") or "")
    rows = [TimelineRow("incident", "observed", incident.observed_end.isoformat(), url)]
    events: dict[str, tuple[Event, str]] = {}
    conflicts: set[str] = set()
    try:
        comments = trusted_comments(api, path, actors)
        for comment in comments:
            body = comment.get("body") or ""
            if not body.startswith("<!-- sre-evidence:"):
                continue
            try:
                event = parse_event(body)
                link = comment_url(repository, number, comment)
                if (
                    event.environment != incident.environment
                    or event.scenario_run_id != incident.scenario_run_id
                ):
                    raise ContractError("event belongs to a different incident")
                if isinstance(event, HandoffEvent):
                    if (
                        event.fingerprint != incident.fingerprint
                        or event.issue_url != url
                    ):
                        raise ContractError("handoff belongs to a different incident")
                elif not event.workflow_url.startswith(
                    f"https://github.com/{repository}/actions/runs/"
                ):
                    raise ContractError("workflow belongs to a different repository")
                if event.event_id in events and events[event.event_id][0] != event:
                    previous = events[event.event_id][0]
                    if same_handoff(previous, event):
                        if event.status == "accepted" and previous.status != "accepted":
                            events[event.event_id] = (event, link)
                    else:
                        conflicts.add(event.event_id)
                else:
                    events[event.event_id] = (event, link)
            except ContractError:
                rows.append(TimelineRow("evidence", "unknown", detail="invalid record"))
    except GitHubError:
        rows.append(TimelineRow("evidence", "unknown", detail="comments unavailable"))

    seen: set[str] = set()
    for event_id, (event, link) in sorted(
        events.items(), key=lambda item: item[1][0].observed_at
    ):
        seen.add(event.kind)
        state = "unknown" if event_id in conflicts else event.status
        detail = "conflicting event ID" if event_id in conflicts else "-"
        if isinstance(event, EvidenceEvent):
            detail = (
                f"GET {ENDPOINT}: {event.endpoint_check.status_code}"
                if event.endpoint_check
                else "endpoint check missing"
            )
            link = event.workflow_url
        rows.append(
            TimelineRow(event.kind, state, event.observed_at.isoformat(), link, detail)
        )
    if "handoff" not in seen:
        assigned = any(
            actor.get("login") == "copilot-swe-agent[bot]"
            for actor in issue.get("assignees", [])
        )
        rows.append(
            TimelineRow(
                "handoff",
                "accepted" if assigned else "unknown",
                url=url,
                detail="live assignee proof"
                if assigned
                else "no accepted launch evidence",
            )
        )
    try:
        rows.extend(pull_request_rows(api, repository, number, incident.base_branch))
    except (GitHubError, ContractError):
        rows.append(TimelineRow("pull request", "unknown", detail="status unavailable"))
    for kind in ("deployment", "recovery"):
        if kind not in seen:
            rows.append(
                TimelineRow(kind, "missing", detail="no trusted event recorded")
            )
    return rows


def render_timeline(rows: list[TimelineRow]) -> str:
    lines = [
        "Stage | State | Observed (UTC) | Evidence | Detail",
        "--- | --- | --- | --- | ---",
    ]
    for row in rows:
        lines.append(
            " | ".join((row.stage, row.state, row.timestamp, row.url, row.detail))
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a JSON artifact offline")
    validate.add_argument("document", type=Path)
    emit = commands.add_parser(
        "emit", help="emit validated pipeline JSON; no API writes"
    )
    emit.add_argument("--kind", choices=("deployment", "recovery"), required=True)
    emit.add_argument("--event-id", required=True)
    emit.add_argument("--environment", default="demo")
    emit.add_argument("--run-id", required=True)
    emit.add_argument("--commit-sha", required=True)
    emit.add_argument(
        "--status", choices=("pending", "succeeded", "failed", "unknown"), required=True
    )
    emit.add_argument("--observed-at", required=True)
    emit.add_argument("--workflow-url", required=True)
    emit.add_argument("--check-file", type=Path)
    for name in ("record", "show"):
        command = commands.add_parser(name)
        if name == "record":
            command.add_argument("document", type=Path)
        command.add_argument("--repo", required=True)
        command.add_argument("--issue", type=int, required=True)
        command.add_argument("--trusted-actor", action="append", default=[])
    args = parser.parse_args(argv)
    api: GitHub | None = None
    event: Event
    try:
        if args.command == "emit":
            check = (
                EndpointCheck.model_validate(parse_json(read_document(args.check_file)))
                if args.check_file
                else None
            )
            event = EvidenceEvent(
                schema_version=1,
                kind=args.kind,
                event_id=args.event_id,
                environment=args.environment,
                scenario_run_id=args.run_id,
                commit_sha=args.commit_sha,
                status=args.status,
                observed_at=args.observed_at,
                workflow_url=args.workflow_url,
                endpoint_check=check,
            )
            print(event.model_dump_json(indent=2))
        elif args.command == "validate":
            print(
                validate_event(read_document(args.document)).model_dump_json(indent=2)
            )
        else:
            actors = frozenset(args.trusted_actor or ["github-actions[bot]"])
            for actor in actors:
                TypeAdapter(Actor).validate_python(actor)
            api = GitHub(
                os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
            )
            if args.command == "record":
                event = validate_event(read_document(args.document))
                print(record_event(api, args.repo, args.issue, event, actors=actors))
            else:
                print(
                    render_timeline(timeline(api, args.repo, args.issue, actors=actors))
                )
    except (ContractError, GitHubError, ValidationError, OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ContractError | GitHubError)
            else "invalid input"
        )
        print(
            f"Evidence unavailable: {message}. No success is inferred.", file=sys.stderr
        )
        return 1
    finally:
        if api is not None:
            api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
