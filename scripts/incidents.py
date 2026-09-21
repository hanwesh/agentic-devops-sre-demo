"""Strict, public-data-only incident contract shared by the demo tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

ENDPOINT = "/api/tasks?filter=broken"
REQUIRED_LABELS = frozenset(
    {"sre-incident", "source:azure-sre-agent", "environment:demo"}
)
MAX_DOCUMENT_BYTES = 16_384
SHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RunID = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")]
Actor = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?$")
]
UUID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
RESOURCE_PREFIX = (
    rf"/subscriptions/{UUID_PATTERN}/resourceGroups/[A-Za-z0-9_.()-]{{1,90}}"
    r"/providers/"
)
INSIGHTS_RESOURCE_PATTERN = (
    "^" + RESOURCE_PREFIX + r"Microsoft\.Insights/components/[A-Za-z0-9_.()-]{1,90}$"
)
ALERT_RESOURCE_PATTERN = (
    rf"^/subscriptions/{UUID_PATTERN}"
    rf"/providers/Microsoft\.AlertsManagement/alerts/{UUID_PATTERN}$"
)


class ContractError(ValueError):
    """An input is not an approved, bounded incident/evidence document."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON keys are not permitted")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError("non-finite JSON numbers are not permitted")


def parse_json(document: str) -> Any:
    if len(document.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ContractError("document exceeds the size limit")
    try:
        return json.loads(
            document, object_pairs_hook=_unique_keys, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as exc:
        raise ContractError("document is not unambiguous bounded JSON") from exc


def utc_timestamp(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must include UTC (Z or +00:00)")
    if value > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("timestamps cannot be in the future")
    return value


class Telemetry(StrictModel):
    application_insights_resource_id: Annotated[
        str, StringConstraints(pattern=INSIGHTS_RESOURCE_PATTERN)
    ]
    azure_alert_id: Annotated[str, StringConstraints(pattern=ALERT_RESOURCE_PATTERN)]
    operation_ids: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]],
        Field(min_length=1, max_length=10),
    ]
    sample_count: Annotated[int, Field(strict=True, ge=20, le=10_000_000)]
    request_count: Annotated[int, Field(strict=True, ge=20, le=10_000_000)]
    failed_request_count: Annotated[int, Field(strict=True, ge=0, le=10_000_000)]
    p95_ms: Annotated[float, Field(strict=True, ge=0, le=3_600_000)]
    exception_types: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$"),
            ]
        ],
        Field(max_length=10),
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.sample_count > self.request_count:
            raise ValueError("sample_count exceeds represented request_count")
        if self.failed_request_count > self.request_count:
            raise ValueError("failed_request_count exceeds request_count")
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must be unique")
        return self


def incident_fingerprint(
    environment: str, scenario_run_id: str, endpoint: str, symptom: str
) -> str:
    identity = "|".join((environment, scenario_run_id, endpoint, symptom))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class Incident(StrictModel):
    schema_version: Literal[1]
    source: Literal["azure-sre-agent"]
    fingerprint: Digest
    scenario_run_id: RunID
    severity: Literal["critical", "high", "medium", "low"]
    environment: Literal["demo"] = "demo"
    method: Literal["GET"]
    endpoint: Literal["/api/tasks?filter=broken"]
    symptom: Literal["http_5xx", "latency"]
    commit_sha: SHA
    observed_start: AwareDatetime
    observed_end: AwareDatetime
    telemetry: Telemetry

    _utc = field_validator("observed_start", "observed_end")(utc_timestamp)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        expected = incident_fingerprint(
            self.environment, self.scenario_run_id, self.endpoint, self.symptom
        )
        if self.fingerprint != expected:
            raise ValueError("fingerprint does not match incident identity")
        if self.observed_end - self.observed_start != timedelta(minutes=5):
            raise ValueError("the observation window must be exactly five minutes")
        if self.symptom == "http_5xx":
            if (
                self.telemetry.failed_request_count / self.telemetry.request_count
                <= 0.05
            ):
                raise ValueError("the observed 5xx ratio must exceed five percent")
        elif self.telemetry.p95_ms <= 3000:
            raise ValueError("the observed p95 must exceed 3000 ms")
        return self

    @property
    def base_branch(self) -> str:
        return f"demo/{self.scenario_run_id}"

    @property
    def title(self) -> str:
        return f"[SRE][demo][{self.severity}] {self.symptom} {self.scenario_run_id}"

    @property
    def document_digest(self) -> str:
        data = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate_incident(document: str) -> Incident:
    try:
        return Incident.model_validate(parse_json(document))
    except ValidationError as exc:
        # Do not print rejected values: they can contain secrets or instructions.
        raise ContractError("incident JSON does not satisfy the v1 contract") from exc


def render_incident(incident: Incident) -> str:
    return (
        f"<!-- sre-incident:v1 fingerprint={incident.fingerprint} -->\n"
        f"```json\n{incident.model_dump_json(indent=2)}\n```\n"
    )


def parse_incident(body: str) -> Incident:
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ContractError("issue body exceeds the size limit")
    match = re.fullmatch(
        r"(?:### Structured incident\n\n)?"
        r"<!-- sre-incident:v1 fingerprint=([0-9a-f]{64}) -->\n"
        r"```json\n(.+)\n```\n?",
        body,
        re.DOTALL,
    )
    if not match:
        raise ContractError("issue must contain only the exact incident envelope")
    incident = validate_incident(match[2])
    if match[1] != incident.fingerprint:
        raise ContractError("incident marker and fingerprint disagree")
    return incident


def read_document(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ContractError("document exceeds the size limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("document must be UTF-8") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render", "title"))
    parser.add_argument("document", type=Path)
    args = parser.parse_args(argv)
    try:
        incident = validate_incident(read_document(args.document))
    except (ContractError, OSError):
        print(
            "Invalid or unreadable incident document; nothing was published.",
            file=sys.stderr,
        )
        return 1
    if args.command == "render":
        print(render_incident(incident), end="")
    elif args.command == "title":
        print(incident.title)
    else:
        print(incident.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
