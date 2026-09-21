"""Preflight contract tests never authenticate or contact Azure/GitHub."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import preflight
from src.version import APP_VERSION, SCHEMA_REVISION


@pytest.fixture
def configuration() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "environment": "staging",
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "azure_client_id": "33333333-3333-3333-3333-333333333333",
        "resource_group": "rg-sre-demo-example",
        "location": "eastus2",
        "webapp_name": "example-task-api",
        "database_name": "taskdb_staging",
        "migrator_username": "task_staging_migrator",
        "runtime_database_secret_uri": (
            "https://staging-example.vault.azure.net/secrets/runtime-database/"
            + "a" * 32
        ),
        "log_analytics_workspace_id": "44444444-4444-4444-4444-444444444444",
        "expected_commit": "a" * 40,
        "github_repository": "example/demo",
        "github_branch": "main",
        "expected_oidc_subject": "repo:example@123/demo@456:environment:staging",
        "runner_group": "trusted-staging",
        "runner_label": "sre-private-staging",
        "attestations": dict.fromkeys(preflight.BASE_ATTESTATIONS, "change-123"),
    }


def no_io(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Offline preflight attempted I/O")


def test_offline_never_calls_subprocess_or_network(
    configuration: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", no_io)
    monkeypatch.setattr(httpx, "Client", no_io)
    report = preflight.run_preflight(configuration)
    assert report["status"] == "configuration_ready"
    assert report["mode"] == "offline"
    assert report["selected_live_checks"] == []
    assert not any(
        check["evidence_kind"] in {"live_api", "live_http"}
        for check in report["checks"]
    )
    attested = [
        check
        for check in report["checks"]
        if check["evidence_kind"] == "operator_attestation"
    ]
    assert attested and all(check["status"] == "attested" for check in attested)
    assert "change-123" not in json.dumps(report)
    assert "staging-example.vault.azure.net" not in json.dumps(report)


@pytest.mark.parametrize(
    "missing",
    [
        "subscription_id",
        "tenant_id",
        "azure_client_id",
        "database_name",
        "runtime_database_secret_uri",
        "runner_label",
        "expected_commit",
    ],
)
def test_missing_configuration_blocks_without_live_io(
    configuration: dict[str, Any], missing: str
) -> None:
    del configuration[missing]
    report = preflight.run_preflight(
        configuration, live=["azure"], command_runner=no_io
    )
    assert report["status"] == "blocked"
    assert report["checks"][0]["evidence_kind"] == "configuration"


def test_missing_attestation_is_a_prerequisite_failure(
    configuration: dict[str, Any],
) -> None:
    del configuration["attestations"]["runner_private_network"]
    report = preflight.run_preflight(
        configuration, live=["azure"], command_runner=no_io
    )
    assert report["status"] == "blocked"
    failed = [item for item in report["checks"] if item["status"] == "failed"]
    assert failed[0]["id"] == "runner_private_network"
    assert failed[0]["evidence_kind"] == "operator_attestation"


@pytest.mark.parametrize(
    "updates",
    [
        {"database_name": "postgres"},
        {"migrator_username": "bootstrapadmin"},
        {"resource_group": "shared"},
        {"resource_group": "rg-sre-demo-production"},
        {"demo_scenario_enabled": True, "demo_run_id": "test"},
        {"runtime_database_secret_uri": "postgresql://runtime:do-not-log@private/db"},
        {"runtime_database_secret_uri": "https://example.com/secrets/not-a-vault"},
        {"runner_label": "ubuntu-latest"},
        {"expected_commit": "main"},
    ],
)
def test_unsafe_configuration_is_rejected_without_values_in_report(
    configuration: dict[str, Any], updates: dict[str, Any]
) -> None:
    configuration.update(updates)
    report = preflight.run_preflight(configuration, command_runner=no_io)
    assert report["status"] == "blocked"
    assert "do-not-log" not in json.dumps(report)


def test_required_environment_is_checked_without_printing_secret(
    configuration: dict[str, Any],
) -> None:
    env = {
        "AZURE_CLIENT_ID": configuration["azure_client_id"],
        "AZURE_TENANT_ID": configuration["tenant_id"],
        "AZURE_SUBSCRIPTION_ID": configuration["subscription_id"],
        "DATABASE_SSL_REQUIRED": "true",
        "MIGRATION_DATABASE_URL": (
            "postgresql+asyncpg://task_staging_migrator:do-not-log@"
            "example-task-api-pg.postgres.database.azure.com:5432/"
            "taskdb_staging?ssl=verify-full"
        ),
    }
    report = preflight.run_preflight(
        configuration,
        required_environment=preflight.REQUIRED_ENVIRONMENT,
        environ=env,
        command_runner=no_io,
    )
    assert report["status"] == "configuration_ready"
    assert "do-not-log" not in json.dumps(report)
    del env["MIGRATION_DATABASE_URL"]
    report = preflight.run_preflight(
        configuration,
        required_environment=preflight.REQUIRED_ENVIRONMENT,
        environ=env,
        command_runner=no_io,
    )
    assert report["status"] == "blocked"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://postgres:secret@host/taskdb_staging?ssl=require",
        "postgresql+asyncpg://task_staging_migrator:secret@"
        "example-task-api-pg.postgres.database.azure.com/taskdb_production?ssl=require",
        "postgresql+asyncpg://task_staging_migrator:secret@"
        "example-task-api-pg.postgres.database.azure.com/taskdb_staging?ssl=disable",
    ],
)
def test_required_migration_credentials_must_match_target(
    configuration: dict[str, Any],
    url: str,
) -> None:
    report = preflight.run_preflight(
        configuration,
        required_environment=["MIGRATION_DATABASE_URL"],
        environ={"MIGRATION_DATABASE_URL": url},
    )
    assert report["status"] == "blocked"
    assert url not in json.dumps(report)


def test_cli_failure_and_output_do_not_leak_secrets(
    configuration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 45
        assert kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(
            command, 1, stdout="do-not-log-stdout", stderr="do-not-log-stderr"
        )

    monkeypatch.setattr(subprocess, "run", fail)
    report = preflight.run_preflight(configuration, live=["azure"])
    assert report["status"] == "blocked"
    assert "do-not-log" not in json.dumps(report)


def azure_responses(config: preflight.PreflightConfig, command: list[str]) -> Any:
    if command[:5] == ["az", "ad", "app", "federated-credential", "list"]:
        return [
            {
                "issuer": "https://token.actions.githubusercontent.com",
                "subject": config.expected_oidc_subject,
                "audiences": ["api://AzureADTokenExchange"],
            }
        ]
    if command[:3] == ["az", "account", "show"]:
        return {
            "id": str(config.subscription_id),
            "tenantId": str(config.tenant_id),
            "state": "Enabled",
        }
    if command[:3] == ["az", "provider", "show"]:
        types = {
            "Microsoft.Web": "sites",
            "Microsoft.DBforPostgreSQL": "flexibleServers",
            "Microsoft.Insights": "components",
            "Microsoft.OperationalInsights": "workspaces",
            "Microsoft.Network": "virtualNetworks",
            "Microsoft.ManagedIdentity": "userAssignedIdentities",
        }
        namespace = command[command.index("--namespace") + 1]
        return {
            "registrationState": "Registered",
            "resourceTypes": [
                {
                    "resourceType": types[namespace],
                    "locations": ["East US 2"],
                }
            ],
        }
    if command[:5] == ["az", "webapp", "config", "appsettings", "list"]:
        assert "--slot" in command and command[command.index("--slot") + 1] == "staging"
        return [
            {"name": key, "value": value}
            for key, value in {
                "ENVIRONMENT": config.environment,
                "DATABASE_NAME": config.database_name,
                "DATABASE_SSL_REQUIRED": "true",
                "DATABASE_URL": (
                    f"@Microsoft.KeyVault(SecretUri={config.runtime_database_secret_uri})"
                ),
                "DEMO_SCENARIO_ENABLED": "false",
                "DEMO_RUN_ID": "",
                "APPLICATIONINSIGHTS_CONNECTION_STRING": "telemetry-do-not-log",
            }.items()
        ]
    assert command[:4] == ["az", "rest", "--method", "get"]
    url = command[command.index("--url") + 1]
    if "/config/slotConfigNames?" in url:
        return {"properties": {"appSettingNames": sorted(preflight.STICKY_SETTINGS)}}
    if "Microsoft.DBforPostgreSQL" in url:
        return {
            "properties": {
                "network": {
                    "publicNetworkAccess": "Disabled",
                    "delegatedSubnetResourceId": "private-subnet",
                    "privateDnsZoneArmResourceId": "private-dns",
                }
            }
        }
    if "Microsoft.Insights" in url:
        return {
            "properties": {
                "WorkspaceResourceId": config.workspace_resource_id,
                "ConnectionString": "telemetry-do-not-log",
            }
        }
    if "Microsoft.OperationalInsights" in url:
        return {"properties": {"customerId": str(config.log_analytics_workspace_id)}}
    return {
        "id": config.app_resource_id,
        "identity": {"principalId": "runtime-identity"},
    }


def test_selected_azure_checks_use_only_read_commands(
    configuration: dict[str, Any],
) -> None:
    config = preflight.PreflightConfig.model_validate(configuration)
    commands: list[list[str]] = []

    def runner(command: list[str]) -> Any:
        commands.append(command)
        return azure_responses(config, command)

    report = preflight.run_preflight(
        configuration, live=["azure"], command_runner=runner
    )
    assert report["status"] == "selected_live_checks_passed"
    assert commands
    assert all(
        not ({"create", "update", "delete", "register", "set", "assign"} & set(command))
        for command in commands
    )
    assert "telemetry-do-not-log" not in json.dumps(report)


@pytest.mark.parametrize(
    "defect", ["subscription", "region", "private_network", "sticky"]
)
def test_azure_mismatch_fails_closed(
    configuration: dict[str, Any],
    defect: str,
) -> None:
    config = preflight.PreflightConfig.model_validate(configuration)

    def runner(command: list[str]) -> Any:
        response = deepcopy(azure_responses(config, command))
        if defect == "subscription" and command[:3] == ["az", "account", "show"]:
            response["id"] = "wrong-subscription"
        if defect == "region" and command[:3] == ["az", "provider", "show"]:
            response["resourceTypes"][0]["locations"] = ["West Europe"]
        if defect == "private_network" and "network" in response.get("properties", {}):
            response["properties"]["network"]["publicNetworkAccess"] = "Enabled"
        if defect == "sticky" and "appSettingNames" in response.get("properties", {}):
            response["properties"]["appSettingNames"].remove("DATABASE_URL")
        return response

    report = preflight.run_preflight(
        configuration, live=["azure"], command_runner=runner
    )
    assert report["status"] == "blocked"


def github_responses(command: list[str]) -> dict[str, Any]:
    assert command[:4] == ["gh", "api", "--method", "GET"]
    path = command[-1]
    if path == "user":
        return {"login": "operator"}
    if path.endswith("/actions/oidc/customization/sub"):
        return {"use_default": True, "use_immutable_subject": True}
    if "/environments/" in path and "deployment-branch-policies" not in path:
        return {
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [{"id": 1}],
                    "prevent_self_review": True,
                }
            ],
            "deployment_branch_policy": {
                "custom_branch_policies": True,
                "protected_branches": False,
            },
        }
    if "/deployment-branch-policies" in path:
        return {
            "total_count": 1,
            "branch_policies": [{"name": "main", "type": "branch"}],
        }
    if path.endswith("/protection"):
        return {
            "required_status_checks": {"contexts": ["test"], "strict": True},
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": True,
            },
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
        }
    return {
        "permissions": {"push": True},
        "id": 456,
        "name": "demo",
        "owner": {"login": "example", "id": 123},
    }


def test_github_checks_protections_but_does_not_claim_copilot_policy_verified(
    configuration: dict[str, Any],
) -> None:
    report = preflight.run_preflight(
        configuration, live=["github"], command_runner=github_responses
    )
    assert report["status"] == "selected_live_checks_passed"
    copilot = next(
        check
        for check in report["checks"]
        if check["id"] == "copilot_license_and_repository_policy"
    )
    assert copilot["status"] == "attested"
    assert copilot["evidence_kind"] == "operator_attestation"


@pytest.mark.parametrize("defect", ["reviewers", "branch", "checks", "self_review"])
def test_github_missing_protections_are_blockers(
    configuration: dict[str, Any],
    defect: str,
) -> None:
    def runner(command: list[str]) -> dict[str, Any]:
        response = github_responses(command)
        if "protection_rules" in response:
            if defect == "reviewers":
                response["protection_rules"] = []
            if defect == "self_review":
                response["protection_rules"][0]["prevent_self_review"] = False
        if defect == "branch" and "branch_policies" in response:
            response["branch_policies"][0]["name"] = "*"
        if defect == "checks" and "required_status_checks" in response:
            response["required_status_checks"] = {}
        return response

    report = preflight.run_preflight(
        configuration, live=["github"], command_runner=runner
    )
    assert report["status"] == "blocked"


@pytest.mark.parametrize("healthy", [True, False])
def test_readiness_only_reads_live_and_schema_endpoint(
    configuration: dict[str, Any],
    healthy: bool,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "alive" if request.url.path == "/live" else "healthy",
                "database": "healthy",
                "reason": None,
                "environment": "staging",
                "version": APP_VERSION,
                "uptime_seconds": 1.0,
                "database_name": "taskdb_staging",
                "commit_sha": configuration["expected_commit"],
                "schema_revision": SCHEMA_REVISION if healthy else "old",
                "expected_schema_revision": SCHEMA_REVISION,
                "demo_scenario_enabled": False,
                "demo_run_id": "",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        report = preflight.run_preflight(
            configuration, live=["readiness"], http_client=client, command_runner=no_io
        )
    assert report["status"] == ("selected_live_checks_passed" if healthy else "blocked")
    assert [request.url.path for request in requests] == ["/live", "/ready"]
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("samples", [0, 1])
def test_telemetry_probe_is_a_readonly_query_with_canonical_properties(
    configuration: dict[str, Any],
    samples: int,
) -> None:
    def runner(command: list[str]) -> dict[str, Any]:
        assert command[:4] == ["az", "rest", "--method", "post"]
        assert command[command.index("--url") + 1].endswith("/query")
        body = json.loads(command[command.index("--body") + 1])
        assert body["timespan"] == "PT15M"
        for key in (
            "environment",
            "deployment_sha",
            "correlation_id",
        ):
            assert key in body["query"]
        return {
            "tables": [
                {
                    "columns": [{"name": "Samples", "type": "long"}],
                    "rows": [[samples]],
                }
            ]
        }

    report = preflight.run_preflight(
        configuration, live=["telemetry"], command_runner=runner
    )
    assert report["status"] == ("selected_live_checks_passed" if samples else "blocked")


def sre_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    configuration.update(
        {
            "environment": "demo",
            "expected_oidc_subject": "repo:example@123/demo@456:environment:demo",
            "database_name": "taskdb_demo",
            "migrator_username": "task_demo_migrator",
            "sre_agent": {
                "name": "example-sre",
                "location": "eastus2",
                "model_name": "operator-selected",
                "model_provider": "operator-selected",
                "consent_reference": "review-record-not-a-token",
            },
        }
    )
    configuration["attestations"].update(
        dict.fromkeys(preflight.SRE_ATTESTATIONS, "review-123")
    )
    return configuration


@pytest.mark.parametrize("mode", ["ReadOnly", "Autonomous"])
def test_sre_arm_state_is_not_confused_with_operator_consent(
    configuration: dict[str, Any],
    mode: str,
) -> None:
    configuration = sre_configuration(configuration)

    def runner(command: list[str]) -> dict[str, Any]:
        assert command[:4] == ["az", "rest", "--method", "get"]
        assert command[command.index("--url") + 1].endswith(
            "/providers/Microsoft.App/agents/example-sre?api-version=2026-01-01"
        )
        return {
            "location": "eastus2",
            "identity": {
                "principalId": "system-identity",
                "userAssignedIdentities": {"user-identity": {}},
            },
            "properties": {
                "provisioningState": "Succeeded",
                "actionConfiguration": {
                    "mode": mode,
                    "accessLevel": "Low",
                    "identity": "user-identity",
                },
                "defaultModel": {
                    "name": "operator-selected",
                    "provider": "operator-selected",
                },
                "knowledgeGraphConfiguration": {"managedResources": []},
            },
        }

    report = preflight.run_preflight(configuration, live=["sre"], command_runner=runner)
    assert report["status"] == (
        "selected_live_checks_passed" if mode == "ReadOnly" else "blocked"
    )
    consent = next(
        check for check in report["checks"] if check["id"] == "sre_provider_consent"
    )
    assert consent["evidence_kind"] == "operator_attestation"
    assert consent["status"] == "attested"


def test_sre_check_requires_explicit_opt_in(configuration: dict[str, Any]) -> None:
    report = preflight.run_preflight(configuration, live=["sre"], command_runner=no_io)
    assert report["status"] == "blocked"


@pytest.mark.parametrize("eligible", [True, False])
def test_copilot_probe_is_read_only_and_never_assigns(
    configuration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    eligible: bool,
) -> None:
    from scripts.github_api import GitHub

    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/user":
            assert request.method == "GET"
            return httpx.Response(200, json={"type": "User", "login": "operator"})
        assert request.url.path == "/graphql" and request.method == "POST"
        payload = json.loads(request.content)
        assert "query" in payload["query"] and "mutation" not in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "suggestedActors": {
                            "nodes": [{"login": "copilot-swe-agent"}]
                            if eligible
                            else []
                        }
                    }
                }
            },
        )

    monkeypatch.setattr(
        preflight,
        "GitHub",
        lambda token: GitHub(token, transport=httpx.MockTransport(respond)),
    )
    report = preflight.run_preflight(
        configuration,
        live=["copilot"],
        environ={"COPILOT_USER_TOKEN": "mock-user-token"},
        command_runner=no_io,
    )
    assert report["status"] == (
        "selected_live_checks_passed" if eligible else "blocked"
    )
    assert len(calls) == 2


def test_cli_default_is_structured_offline_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("PREFLIGHT_CONFIG", raising=False)
    monkeypatch.setattr(subprocess, "run", no_io)
    assert preflight.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["mode"] == "offline"


def test_cli_config_and_sha_override(
    configuration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        Path, "read_text", lambda *args, **kwargs: json.dumps(configuration)
    )
    monkeypatch.setattr(subprocess, "run", no_io)
    assert preflight.main(["--config", "nonsecret.json", "--sha", "b" * 40]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_ready"
