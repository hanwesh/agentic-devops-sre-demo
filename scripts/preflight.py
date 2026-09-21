"""Offline prerequisite checks; live read-only probes require explicit selection."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from infrastructure.safety import SECRET_URI, safe_resource_group
from scripts.github_api import GitHub, GitHubError
from scripts.incidents import ContractError
from scripts.oidc_subject import environment_subject
from scripts.release_policy import validate_environment
from scripts.sre_handoff import check_eligibility

LIVE_CHECKS = ("azure", "github", "copilot", "readiness", "telemetry", "sre")
REQUIRED_ENVIRONMENT = (
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_ID",
    "MIGRATION_DATABASE_URL",
    "DATABASE_SSL_REQUIRED",
)
BASE_ATTESTATIONS = (
    "isolated_resource_group",
    "database_bootstrapped",
    "runtime_identity_scoped",
    "migration_identity_scoped",
    "runner_private_network",
    "key_vault_references_resolved",
    "oidc_environment_scoped",
    "github_protections_configured",
    "administrator_bypass_disabled",
    "swap_key_vault_resolution_reviewed",
    "copilot_license_and_repository_policy",
    "cost_budget_reviewed",
)
SRE_ATTESTATIONS = (
    "sre_provider_consent",
    "sre_code_and_log_connectors",
    "sre_incident_response_plan",
    "sre_user_roles",
    "sre_readonly_effective_rbac",
    "sre_active_usage_limit",
)
STICKY_SETTINGS = {
    "ENVIRONMENT",
    "DATABASE_URL",
    "DATABASE_NAME",
    "DATABASE_SSL_REQUIRED",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "OTEL_SERVICE_NAME",
    "OTEL_TRACES_SAMPLER",
    "DEMO_SCENARIO_ENABLED",
    "DEMO_RUN_ID",
}
CommandRunner = Callable[[list[str]], Any]


class SreAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z][-A-Za-z0-9]{0,30}[A-Za-z0-9]$")
    location: Literal["australiaeast", "eastus2", "swedencentral"]
    model_name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    model_provider: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    consent_reference: str = Field(min_length=1, max_length=200, repr=False)


class PreflightConfig(BaseModel):
    """Nonsecret, per-environment input; attestations are never API evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    environment: Literal["production", "staging", "demo"]
    subscription_id: UUID
    tenant_id: UUID
    azure_client_id: UUID
    resource_group: str
    location: str = Field(pattern=r"^[a-z0-9]+$")
    webapp_name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,23}$")
    database_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    migrator_username: str
    runtime_database_secret_uri: str = Field(repr=False)
    log_analytics_workspace_id: UUID
    expected_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    github_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    github_branch: str = Field(min_length=1, max_length=100)
    expected_oidc_subject: str = Field(min_length=1, max_length=512)
    runner_group: str = Field(min_length=1, max_length=100)
    runner_label: str = Field(min_length=1, max_length=100)
    demo_scenario_enabled: StrictBool = False
    demo_run_id: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]{0,47})?$")
    sre_agent: SreAgentConfig | None = None
    attestations: dict[str, str] = Field(default_factory=dict, repr=False)

    @field_validator("resource_group")
    @classmethod
    def isolated_group(cls, value: str) -> str:
        if not safe_resource_group(value):
            raise ValueError("Require a dedicated rg-sre-demo-<unique> resource group")
        return value

    @field_validator("runtime_database_secret_uri")
    @classmethod
    def secret_reference(cls, value: str) -> str:
        if not SECRET_URI.fullmatch(value):
            raise ValueError("Require a versioned existing Key Vault secret URI")
        return value

    @field_validator("github_branch", "runner_group", "runner_label")
    @classmethod
    def explicit_name(cls, value: str) -> str:
        if (
            value.strip() != value
            or any(character in value for character in "<>\r\n")
            or value in {"default", "self-hosted", "ubuntu-latest"}
        ):
            raise ValueError("Require an explicit environment-scoped value")
        return value

    @model_validator(mode="after")
    def isolated_environment(self) -> Self:
        if self.database_name in {"postgres", "template0", "template1"}:
            raise ValueError("A dedicated application database is required")
        if not self.expected_oidc_subject.endswith(f":environment:{self.environment}"):
            raise ValueError("OIDC subject must be scoped to this environment")
        if self.migrator_username != f"task_{self.environment}_migrator":
            raise ValueError("Migrator role must match the selected environment")
        if self.environment != "demo" and (
            self.demo_scenario_enabled or self.demo_run_id
        ):
            raise ValueError("Scenario settings are forbidden outside demo")
        if self.demo_scenario_enabled and not self.demo_run_id:
            raise ValueError("An enabled scenario requires an explicit demo run")
        if self.sre_agent and self.environment != "demo":
            raise ValueError(
                "This template grants SRE investigation access only to demo"
            )
        return self

    @property
    def group_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/"
            f"{self.resource_group}"
        )

    @property
    def site_id(self) -> str:
        return f"{self.group_id}/providers/Microsoft.Web/sites/{self.webapp_name}"

    @property
    def app_resource_id(self) -> str:
        suffix = (
            "" if self.environment == "production" else f"/slots/{self.environment}"
        )
        return self.site_id + suffix

    @property
    def app_url(self) -> str:
        suffix = "" if self.environment == "production" else f"-{self.environment}"
        return f"https://{self.webapp_name}{suffix}.azurewebsites.net"

    @property
    def insights_resource_id(self) -> str:
        return (
            f"{self.group_id}/providers/Microsoft.Insights/components/"
            f"{self.webapp_name}-{self.environment}-insights"
        )

    @property
    def workspace_resource_id(self) -> str:
        return (
            f"{self.group_id}/providers/Microsoft.OperationalInsights/workspaces/"
            f"{self.webapp_name}-{self.environment}-logs"
        )

    @property
    def postgres_resource_id(self) -> str:
        return (
            f"{self.group_id}/providers/Microsoft.DBforPostgreSQL/flexibleServers/"
            f"{self.webapp_name}-pg"
        )


@dataclass(frozen=True)
class Check:
    id: str
    status: Literal["passed", "failed", "attested", "not_requested"]
    evidence_kind: Literal[
        "configuration", "operator_attestation", "live_api", "live_http"
    ]
    message: str


class ProbeFailure(RuntimeError):
    """Only fixed, nonsecret messages may escape a live probe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def _command_json(command: list[str]) -> Any:
    """Execute only the fixed read commands constructed below; never a shell."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
            env={
                **os.environ,
                "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
                "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
                "GH_PAGER": "cat",
            },
        )
        if result.returncode != 0:
            raise ProbeFailure("Read-only CLI probe failed; inspect access separately")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ProbeFailure(
            "CLI unavailable, timed out, or returned invalid JSON"
        ) from exc


def _az_get(
    config: PreflightConfig, runner: CommandRunner, resource_id: str, version: str
) -> dict[str, Any]:
    response = runner(
        [
            "az",
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{resource_id}?api-version={version}",
            "--subscription",
            str(config.subscription_id),
            "--output",
            "json",
        ]
    )
    if not isinstance(response, dict):
        raise ProbeFailure("Azure returned an unexpected object")
    return response


def _azure_checks(config: PreflightConfig, runner: CommandRunner) -> None:
    account = runner(["az", "account", "show", "--output", "json"])
    _require(
        account.get("id", "").lower() == str(config.subscription_id)
        and account.get("tenantId", "").lower() == str(config.tenant_id)
        and account.get("state") == "Enabled",
        "Azure login tenant/subscription does not match the approved configuration",
    )
    credentials = runner(
        [
            "az",
            "ad",
            "app",
            "federated-credential",
            "list",
            "--id",
            str(config.azure_client_id),
            "--output",
            "json",
        ]
    )
    _require(
        isinstance(credentials, list)
        and any(
            credential.get("issuer") == "https://token.actions.githubusercontent.com"
            and credential.get("subject") == config.expected_oidc_subject
            and credential.get("audiences") == ["api://AzureADTokenExchange"]
            for credential in credentials
        ),
        "Entra environment federation does not match the approved subject",
    )
    providers = {
        "Microsoft.Web": ("sites", config.location),
        "Microsoft.DBforPostgreSQL": ("flexibleServers", config.location),
        "Microsoft.Insights": ("components", config.location),
        "Microsoft.OperationalInsights": ("workspaces", config.location),
        "Microsoft.Network": ("virtualNetworks", config.location),
        "Microsoft.ManagedIdentity": ("userAssignedIdentities", config.location),
    }
    if config.sre_agent:
        providers["Microsoft.App"] = ("agents", config.sre_agent.location)
    for namespace, (resource_type, location) in providers.items():
        provider = runner(
            [
                "az",
                "provider",
                "show",
                "--namespace",
                namespace,
                "--subscription",
                str(config.subscription_id),
                "--output",
                "json",
            ]
        )
        _require(
            provider.get("registrationState") == "Registered",
            f"{namespace} is not registered; registration is an operator action",
        )
        resource_types = [
            item
            for item in provider.get("resourceTypes", [])
            if item.get("resourceType", "").lower() == resource_type.lower()
        ]
        _require(bool(resource_types), f"{namespace} resource type is unavailable")
        locations = {
            re.sub(r"\s+", "", item).lower()
            for item in resource_types[0].get("locations", [])
        }
        _require(location in locations, f"{namespace} does not advertise the region")
        if namespace == "Microsoft.App":
            _require(
                "2026-01-01" in resource_types[0].get("apiVersions", []),
                "Subscription does not advertise the pinned SRE Agent API version",
            )
    app = _az_get(config, runner, config.app_resource_id, "2023-12-01")
    _require(
        app.get("id", "").lower() == config.app_resource_id.lower()
        and app.get("identity", {}).get("principalId"),
        "The target slot or its separate managed identity is unavailable",
    )
    postgres = _az_get(config, runner, config.postgres_resource_id, "2024-08-01")
    network = postgres.get("properties", {}).get("network", {})
    _require(
        network.get("publicNetworkAccess") == "Disabled"
        and bool(network.get("delegatedSubnetResourceId"))
        and bool(network.get("privateDnsZoneArmResourceId")),
        "PostgreSQL must use private delegated networking and private DNS",
    )
    insights = _az_get(config, runner, config.insights_resource_id, "2020-02-02")
    _require(
        insights.get("properties", {}).get("WorkspaceResourceId", "").lower()
        == config.workspace_resource_id.lower(),
        "Application Insights is not linked to the isolated environment workspace",
    )
    workspace = _az_get(config, runner, config.workspace_resource_id, "2023-09-01")
    _require(
        workspace.get("properties", {}).get("customerId", "").lower()
        == str(config.log_analytics_workspace_id),
        "Workspace ID does not match the configured environment",
    )
    sticky = _az_get(
        config, runner, config.site_id + "/config/slotConfigNames", "2023-12-01"
    )
    _require(
        STICKY_SETTINGS <= set(sticky.get("properties", {}).get("appSettingNames", [])),
        "Environment, database, telemetry, and scenario settings must be slot-sticky",
    )
    command = [
        "az",
        "webapp",
        "config",
        "appsettings",
        "list",
        "--subscription",
        str(config.subscription_id),
        "--resource-group",
        config.resource_group,
        "--name",
        config.webapp_name,
        "--output",
        "json",
    ]
    if config.environment != "production":
        command.extend(["--slot", config.environment])
    # The list operation is read-only. Its values remain in memory, never in the report.
    settings = {item["name"]: item["value"] for item in runner(command)}
    _require(
        settings.get("ENVIRONMENT") == config.environment
        and settings.get("DATABASE_NAME") == config.database_name
        and settings.get("DATABASE_SSL_REQUIRED", "").lower() == "true"
        and settings.get("DATABASE_URL")
        == f"@Microsoft.KeyVault(SecretUri={config.runtime_database_secret_uri})"
        and settings.get("DEMO_SCENARIO_ENABLED", "").lower()
        == str(config.demo_scenario_enabled).lower()
        and settings.get("DEMO_RUN_ID", "") == config.demo_run_id,
        "Live slot settings do not match the approved environment and secret reference",
    )
    _require(
        bool(settings.get("APPLICATIONINSIGHTS_CONNECTION_STRING"))
        and settings["APPLICATIONINSIGHTS_CONNECTION_STRING"]
        == insights.get("properties", {}).get("ConnectionString"),
        "The slot telemetry destination is not its own Application Insights instance",
    )


def _github_checks(config: PreflightConfig, runner: CommandRunner) -> None:
    def get(path: str) -> dict[str, Any]:
        result = runner(["gh", "api", "--method", "GET", path])
        if not isinstance(result, dict):
            raise ProbeFailure("GitHub returned an unexpected object")
        return result

    user = get("user")
    _require(bool(user.get("login")), "GitHub authentication could not be verified")
    repo = f"repos/{config.github_repository}"
    repository = get(repo)
    _require(
        environment_subject(
            repository,
            get(f"{repo}/actions/oidc/customization/sub"),
            config.environment,
        )
        == config.expected_oidc_subject,
        "OIDC subject differs from the repository's actual subject settings",
    )
    permissions = repository.get("permissions", {})
    _require(
        permissions.get("push") is True or permissions.get("admin") is True,
        "GitHub actor lacks repository write access required for the handoff",
    )
    for name in {"production", config.environment}:
        validate_environment(
            get(f"{repo}/environments/{name}"),
            get(f"{repo}/environments/{name}/deployment-branch-policies?per_page=100"),
            config.github_branch,
            require_review=name in {"production", "demo"},
        )
    branch = quote(config.github_branch, safe="")
    protection = get(f"{repo}/branches/{branch}/protection")
    status_checks = protection.get("required_status_checks") or {}
    reviews = protection.get("required_pull_request_reviews") or {}
    _require(
        bool(status_checks.get("contexts") or status_checks.get("checks"))
        and status_checks.get("strict") is True
        and reviews.get("required_approving_review_count", 0) >= 1
        and reviews.get("dismiss_stale_reviews") is True
        and (protection.get("enforce_admins") or {}).get("enabled") is True
        and not (protection.get("allow_force_pushes") or {}).get("enabled")
        and not (protection.get("allow_deletions") or {}).get("enabled"),
        "Branch protection needs strict CI, reviews, and admin enforcement",
    )


def _readiness_checks(
    config: PreflightConfig, client: httpx.Client | None = None
) -> None:
    from src.schemas import HealthResponse, LivenessResponse
    from src.version import APP_VERSION, SCHEMA_REVISION

    def check(http: httpx.Client) -> None:
        live = http.get(f"{config.app_url}/live")
        _require(live.status_code == 200, "The target process is not live")
        identity = LivenessResponse.model_validate(live.json())
        _require(
            identity.environment == config.environment
            and identity.commit_sha == config.expected_commit,
            "Liveness deployment identity does not match",
        )
        response = http.get(f"{config.app_url}/ready")
        _require(response.status_code == 200, "Database/schema readiness did not pass")
        body = response.json()
        HealthResponse.model_validate(body)
        _require(
            body.get("status") == "healthy"
            and body.get("database") == "healthy"
            and body.get("reason") is None
            and body.get("environment") == config.environment
            and body.get("database_name") == config.database_name
            and body.get("version") == APP_VERSION
            and body.get("commit_sha") == config.expected_commit
            and body.get("schema_revision") == SCHEMA_REVISION
            and body.get("expected_schema_revision") == SCHEMA_REVISION
            and body.get("demo_scenario_enabled") is config.demo_scenario_enabled
            and body.get("demo_run_id", "") == config.demo_run_id,
            "Readiness schema, commit, environment, or scenario identity mismatch",
        )

    if client is not None:
        check(client)
    else:
        with httpx.Client(timeout=10, follow_redirects=False) as http:
            check(http)


def _telemetry_checks(config: PreflightConfig, runner: CommandRunner) -> None:
    query = (
        "AppRequests | where TimeGenerated > ago(15m)"
        f" | where _ResourceId =~ '{config.insights_resource_id}'"
        ' | where tostring(Properties["environment"])'
        f" == '{config.environment}'"
        ' | where tostring(Properties["deployment_sha"])'
        f" == '{config.expected_commit}'"
        ' | where coalesce(tostring(Properties["service.name"]), AppRoleName)'
        " == 'task-api'"
        ' | where isnotempty(tostring(Properties["correlation_id"]))'
    )
    if config.demo_run_id:
        query += (
            f" | where tostring(Properties[\"demo_run_id\"]) == '{config.demo_run_id}'"
        )
    query += " | summarize Samples=count()"
    response = runner(
        [
            "az",
            "rest",
            "--method",
            "post",
            "--url",
            "https://api.loganalytics.azure.com/v1/workspaces/"
            f"{config.log_analytics_workspace_id}/query",
            "--resource",
            "https://api.loganalytics.azure.com",
            "--subscription",
            str(config.subscription_id),
            "--body",
            json.dumps({"query": query, "timespan": "PT15M"}),
            "--output",
            "json",
        ]
    )
    # POST /query is a read-only Log Analytics query, not a management mutation.
    tables = response.get("tables", [])
    _require(
        bool(tables)
        and tables[0].get("columns") == [{"name": "Samples", "type": "long"}]
        and bool(tables[0].get("rows"))
        and tables[0]["rows"][0][0] > 0,
        "No recent request telemetry with matching environment/commit/correlation",
    )


def _sre_checks(config: PreflightConfig, runner: CommandRunner) -> None:
    _require(
        config.sre_agent is not None, "SRE checks require explicit SRE configuration"
    )
    agent_config = config.sre_agent
    assert agent_config is not None
    resource_id = (
        f"{config.group_id}/providers/Microsoft.App/agents/{agent_config.name}"
    )
    agent = _az_get(config, runner, resource_id, "2026-01-01")
    properties = agent.get("properties", {})
    action = properties.get("actionConfiguration", {})
    identity = agent.get("identity", {})
    user_identities = identity.get("userAssignedIdentities", {})
    model = properties.get("defaultModel", {})
    _require(
        agent.get("location") == agent_config.location
        and properties.get("provisioningState") == "Succeeded"
        and action.get("mode") == "ReadOnly"
        and action.get("accessLevel") == "Low"
        and action.get("identity") in user_identities
        and identity.get("principalId")
        and model.get("name") == agent_config.model_name
        and model.get("provider") == agent_config.model_provider,
        "SRE requires separate identities and ReadOnly/Low mode",
    )
    _require(
        not properties.get("knowledgeGraphConfiguration", {}).get("managedResources"),
        "Do not connect the mixed production/demo resource group as an agent scope",
    )
    # Consent, connector health, user licensing, IRP filters, and effective inherited
    # RBAC are separate operator attestations, not proved by this ARM GET.


def _environment_check(
    config: PreflightConfig, key: str, environment: Mapping[str, str]
) -> bool:
    value = environment.get(key, "")
    if not value:
        return False
    expected = {
        "AZURE_CLIENT_ID": str(config.azure_client_id),
        "AZURE_TENANT_ID": str(config.tenant_id),
        "AZURE_SUBSCRIPTION_ID": str(config.subscription_id),
    }
    if key in expected:
        return value.lower() == expected[key]
    if key == "DATABASE_SSL_REQUIRED":
        return value.lower() == "true"
    if key == "MIGRATION_DATABASE_URL":
        try:
            parsed = urlsplit(value)
            ssl = parse_qs(parsed.query).get("ssl", [])
            return (
                parsed.scheme == "postgresql+asyncpg"
                and parsed.hostname
                == f"{config.webapp_name}-pg.postgres.database.azure.com"
                and unquote(parsed.username or "") == config.migrator_username
                and parsed.path == f"/{config.database_name}"
                and bool(parsed.password)
                and ssl in ([], ["verify-full"])
            )
        except ValueError:
            return False
    return False


def _copilot_check(config: PreflightConfig, environment: Mapping[str, str]) -> None:
    token = environment.get("COPILOT_USER_TOKEN", "")
    _require(
        bool(token)
        and not token.startswith("ghs_")
        and token != environment.get("GITHUB_TOKEN"),
        "Copilot eligibility requires a separate supported user token",
    )
    api = GitHub(token)
    try:
        check_eligibility(api, config.github_repository)
    except (GitHubError, ContractError) as exc:
        raise ProbeFailure(
            "Copilot is unavailable to this user or the eligibility API failed"
        ) from exc
    finally:
        api.close()


def run_preflight(
    configuration: Mapping[str, Any] | PreflightConfig,
    *,
    live: Sequence[str] = (),
    required_environment: Sequence[str] = (),
    environ: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Return a safe JSON-serializable report. With defaults, perform NO I/O."""
    checks: list[Check] = []
    selected = list(dict.fromkeys(live))
    try:
        config = PreflightConfig.model_validate(configuration)
    except ValidationError as exc:
        known_fields = PreflightConfig.model_fields
        fields = {
            str(error["loc"][0])
            for error in exc.errors(include_input=False, include_context=False)
            if error["loc"] and error["loc"][0] in known_fields
        }
        for field in sorted(fields) or ["schema"]:
            checks.append(
                Check(
                    f"configuration.{field}",
                    "failed",
                    "configuration",
                    "Required field missing/invalid; consult the nonsecret example",
                )
            )
        return _report(checks, selected)
    except (ValueError, TypeError):
        checks.append(
            Check(
                "configuration",
                "failed",
                "configuration",
                "Invalid configuration; see infrastructure/preflight.example.json",
            )
        )
        return _report(checks, selected)
    checks.append(
        Check(
            "configuration",
            "passed",
            "configuration",
            "Environment-scoped configuration is valid; no resource existence implied",
        )
    )
    if set(selected) - set(LIVE_CHECKS):
        checks.append(
            Check(
                "live.selection",
                "failed",
                "configuration",
                "Unknown live check selected",
            )
        )
    if "sre" in selected and config.sre_agent is None:
        checks.append(
            Check(
                "sre.configuration",
                "failed",
                "configuration",
                "SRE checks require explicit opt-in configuration",
            )
        )
    required = BASE_ATTESTATIONS + (SRE_ATTESTATIONS if config.sre_agent else ())
    for key in required:
        reference = config.attestations.get(key, "").strip()
        valid = bool(reference) and not any(mark in reference for mark in "<>\r\n")
        checks.append(
            Check(
                key,
                "attested" if valid else "failed",
                "operator_attestation",
                "Operator reference supplied; NOT verified API evidence"
                if valid
                else "Missing operator review/evidence reference",
            )
        )
    environment = os.environ if environ is None else environ
    for key in required_environment:
        known = key in REQUIRED_ENVIRONMENT
        passed = known and _environment_check(config, key, environment)
        checks.append(
            Check(
                f"environment.{key}" if known else "environment.unsupported",
                "passed" if passed else "failed",
                "configuration",
                "Required setting matches the environment"
                if passed
                else "Required setting missing, unsafe, mismatched, or unsupported",
            )
        )
    # Fail closed before making any live calls when configuration/prerequisites fail.
    if any(check.status == "failed" for check in checks):
        return _report(checks, selected)
    runner = command_runner or _command_json
    probes: dict[str, Callable[[], None]] = {
        "azure": lambda: _azure_checks(config, runner),
        "github": lambda: _github_checks(config, runner),
        "copilot": lambda: _copilot_check(config, environment),
        "readiness": lambda: _readiness_checks(config, http_client),
        "telemetry": lambda: _telemetry_checks(config, runner),
        "sre": lambda: _sre_checks(config, runner),
    }
    for key in LIVE_CHECKS:
        if key not in selected:
            checks.append(
                Check(
                    f"live.{key}",
                    "not_requested",
                    "configuration",
                    "No live call made; select this check explicitly",
                )
            )
            continue
        try:
            probes[key]()
        except ProbeFailure as exc:
            checks.append(
                Check(
                    f"live.{key}",
                    "failed",
                    "live_http" if key == "readiness" else "live_api",
                    str(exc),
                )
            )
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
        ):
            checks.append(
                Check(
                    f"live.{key}",
                    "failed",
                    "live_http" if key == "readiness" else "live_api",
                    "Read-only probe failed or returned an unexpected response",
                )
            )
        else:
            checks.append(
                Check(
                    f"live.{key}",
                    "passed",
                    "live_http" if key == "readiness" else "live_api",
                    "Selected read-only probe passed; documented limits still apply",
                )
            )
    return _report(checks, selected)


def _report(checks: list[Check], selected: list[str]) -> dict[str, Any]:
    failed = any(check.status == "failed" for check in checks)
    return {
        "schema_version": 1,
        "status": (
            "blocked"
            if failed
            else "selected_live_checks_passed"
            if selected
            else "configuration_ready"
        ),
        "mode": "live-read-only" if selected else "offline",
        "checked_at": datetime.now(UTC).isoformat(),
        "selected_live_checks": [
            item if item in LIVE_CHECKS else "unsupported" for item in selected
        ],
        "checks": [asdict(check) for check in checks],
        "limitations": [
            "Configuration/attestations do not prove a working cloud loop.",
            "No assignments, resource writes, migrations, or deployments run.",
            "Operator review covers Copilot, consent, RBAC, connectors, and IRP.",
            "HTTP readiness checks schema status, not migration privileges.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=os.environ.get("PREFLIGHT_CONFIG"),
        help="Nonsecret JSON file; defaults to the PREFLIGHT_CONFIG path",
    )
    parser.add_argument(
        "--live",
        action="append",
        choices=LIVE_CHECKS,
        default=[],
        help="Explicitly selected read-only probe; repeat to select more than one",
    )
    parser.add_argument(
        "--require-env",
        action="append",
        choices=REQUIRED_ENVIRONMENT,
        default=[],
        help="Offline presence/target validation of a protected job setting",
    )
    parser.add_argument("--sha", help="Override expected_commit with the packaged SHA")
    args = parser.parse_args(argv)
    try:
        if args.config is None:
            raise ValueError("Missing config")
        configuration = json.loads(args.config.read_text(encoding="utf-8"))
        if args.sha:
            configuration["expected_commit"] = args.sha
        report = run_preflight(
            configuration, live=args.live, required_environment=args.require_env
        )
    except (OSError, ValueError, TypeError):
        report = _report(
            [
                Check(
                    "configuration",
                    "failed",
                    "configuration",
                    "Provide nonsecret JSON using --config or PREFLIGHT_CONFIG",
                )
            ],
            args.live,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
