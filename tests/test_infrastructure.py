"""Offline IaC, SQL, and alert contracts, not simulated Azure deployment evidence."""

import json
import math
from pathlib import Path
from typing import Any

import pytest

from infrastructure.safety import (
    PURPOSE,
    safe_resource_group,
    validate_group,
    validate_inventory,
    validate_parameters,
)

ROOT = Path(__file__).resolve().parents[1] / "infrastructure"
POLICY = json.loads((ROOT / "alerts/policy.json").read_text())
REQUEST_QUERY = (ROOT / "alerts/request-window.kql").read_text()


def valid_parameters() -> dict[str, Any]:
    return {
        "parameters": {
            "baseName": {"value": "example-demo"},
            "postgresAdminPassword": {
                "reference": {
                    "keyVault": {
                        "id": (
                            "/subscriptions/11111111-1111-1111-1111-111111111111/"
                            "resourceGroups/bootstrap-vault/providers/Microsoft.KeyVault/"
                            "vaults/bootstrap-vault"
                        )
                    },
                    "secretName": "bootstrap-password",
                }
            },
            "productionDatabaseSecretUri": {
                "value": (
                    "https://production-vault.vault.azure.net/secrets/runtime/"
                    + "a" * 32
                )
            },
            "stagingDatabaseSecretUri": {
                "value": (
                    "https://staging-vault.vault.azure.net/secrets/runtime/" + "b" * 32
                )
            },
        }
    }


def test_parameter_example_intentionally_requires_operator_values() -> None:
    example = json.loads((ROOT / "parameters.json").read_text())
    assert validate_parameters(example)
    assert not validate_parameters(valid_parameters())
    for toggle in (
        "enableDemoSlot",
        "enableDemoAlerts",
        "enableSreAgent",
        "grantSreInvestigationRoles",
    ):
        assert example["parameters"][toggle]["value"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "inline_password",
        "same_secret",
        "implicit_sre",
        "unapproved_alerts",
        "string_boolean",
    ],
)
def test_unsafe_parameter_combinations_are_rejected(mutation: str) -> None:
    config = valid_parameters()
    parameters = config["parameters"]
    if mutation == "inline_password":
        parameters["postgresAdminPassword"] = {"value": "do-not-log"}
    elif mutation == "same_secret":
        parameters["stagingDatabaseSecretUri"] = parameters[
            "productionDatabaseSecretUri"
        ]
    elif mutation == "implicit_sre":
        parameters["enableSreAgent"] = {"value": True}
    elif mutation == "unapproved_alerts":
        parameters["enableDemoAlerts"] = {"value": True}
    else:
        parameters["enableDemoSlot"] = {"value": "false"}
    errors = validate_parameters(config)
    assert errors and "do-not-log" not in str(errors)


def test_separate_versions_of_one_secret_are_not_environment_isolation() -> None:
    document = valid_parameters()
    production = document["parameters"]["productionDatabaseSecretUri"]["value"]
    document["parameters"]["stagingDatabaseSecretUri"] = {
        "value": production.rsplit("/", 1)[0] + "/" + "c" * 32
    }
    assert validate_parameters(document)


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {},
        {"parameters": []},
        {"parameters": {"postgresAdminPassword": 1}},
        {"parameters": {"postgresAdminPassword": {"reference": []}}},
    ],
)
def test_parameter_shape_errors_are_safe(document: Any) -> None:
    assert validate_parameters(document)


def test_teardown_refuses_default_shared_and_unowned_scopes() -> None:
    for name in (
        "",
        "default",
        "shared",
        "production",
        "rg-sre-demo-shared",
        "rg-sre-demo-default",
        "rg-sre-demo-production",
    ):
        assert not safe_resource_group(name)
    group = "rg-sre-demo-example"
    tags = {"purpose": PURPOSE, "deployment": group, "disposable": "true"}
    assert safe_resource_group(group)
    assert not validate_group({"name": group, "tags": tags}, group, disposable=True)
    assert validate_group({"name": group, "tags": {}}, group, disposable=True)
    assert validate_group({"name": "other", "tags": tags}, group)
    assert validate_inventory([{"tags": {}}], group)
    assert not validate_inventory([{"tags": tags}], group)
    assert validate_group(
        {
            "name": group,
            "tags": {**tags, "disposable": "false"},
        },
        group,
        disposable=True,
    )


def test_private_database_and_separate_environment_resources() -> None:
    bicep = (ROOT / "main.bicep").read_text()
    assert "publicNetworkAccess: 'Disabled'" in bicep
    assert "delegatedSubnetResourceId:" in bicep
    assert "privateDnsZoneArmResourceId:" in bicep
    assert "firewallRules" not in bicep
    assert "0.0.0.0" not in bicep.replace("--host 0.0.0.0", "")
    assert "name: databaseNames[environment]" in bicep
    for environment in ("production", "staging", "demo"):
        assert f"param {environment}DatabaseName string" in bicep
    assert "name: '${baseName}-${environment}-insights'" in bicep
    assert "name: '${baseName}-${environment}-logs'" in bicep
    assert "healthCheckPath: '/ready'" in bicep
    assert "value: '/live'" in bicep
    assert "value: 'false'" in bicep
    assert "type: 'SystemAssigned'" in bicep
    assert "postgresql+asyncpg://" not in bicep
    outputs = "\n".join(
        line for line in bicep.splitlines() if line.startswith("output ")
    )
    assert "ConnectionString" not in outputs and "Password" not in outputs
    sticky = bicep.split("name: 'slotConfigNames'", 1)[1].split("module demoAlerts", 1)[
        0
    ]
    for key in (
        "ENVIRONMENT",
        "DATABASE_URL",
        "DATABASE_NAME",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "DEMO_SCENARIO_ENABLED",
        "DEMO_RUN_ID",
    ):
        assert f"'{key}'" in sticky
    assert "COMMIT" not in sticky


def test_bootstrap_does_not_give_runtime_schema_ownership_or_admin_password() -> None:
    sql = (ROOT / "bootstrap-database.sql").read_text()
    assert "\\getenv runtime_password BOOTSTRAP_RUNTIME_PASSWORD" in sql
    assert 'REVOKE ALL ON DATABASE :"database" FROM PUBLIC' in sql
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in sql
    assert 'GRANT USAGE, CREATE ON SCHEMA public TO :"migrator_role"' in sql
    assert 'GRANT USAGE ON SCHEMA public TO :"runtime_role"' in sql
    assert "ALTER DEFAULT PRIVILEGES" not in sql
    assert "OWNER TO" not in sql
    grants = (ROOT / "grant-runtime.sql").read_text()
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tasks" in grants
    assert "GRANT SELECT ON TABLE public.alembic_version" in grants
    assert "GRANT ALL" not in grants


def test_sre_uses_published_properties_and_no_broad_write_role() -> None:
    bicep = (ROOT / "sre-agent.bicep").read_text()
    assert "'Microsoft.App/agents@2026-01-01'" in bicep
    assert "type: 'SystemAssigned,UserAssigned'" in bicep
    assert "mode: 'ReadOnly'" in bicep and "accessLevel: 'Low'" in bicep
    assert "managedResources: []" in bicep
    assert "defaultModel:" in bicep
    assert "monthlyAgentUnitLimit:" not in bicep
    assert "experimentalSettings:" not in bicep
    assert "Contributor" not in bicep
    assert "scope: demoSlot" in bicep
    assert "scope: demoInsights" in bicep
    assert "scope: demoWorkspace" in bicep
    assert "scope: resourceGroup()" not in bicep


def test_alert_contract_uses_real_request_schema_and_shared_policy() -> None:
    assert "\nAppRequests\n" in REQUEST_QUERY
    assert "let WindowStart = WindowEnd - __WINDOW_MINUTES__m;" in REQUEST_QUERY
    assert "DurationMs" in REQUEST_QUERY and "ItemCount" in REQUEST_QUERY
    assert "SampleCount = count()" in REQUEST_QUERY
    assert "TimeGenerated = max(TimeGenerated)" in REQUEST_QUERY
    assert "isnotempty(RequestPath)" in REQUEST_QUERY
    assert "RequestCount = sum(SampleWeight)" in REQUEST_QUERY
    assert "sumif(SampleWeight, StatusCode between (500 .. 599))" in REQUEST_QUERY
    assert "100.0 * todouble(ErrorCount) / todouble(RequestCount)" in REQUEST_QUERY
    assert "percentilew(DurationMs, SampleWeight, __PERCENTILE__)" in REQUEST_QUERY
    assert "SampleCount >= __MINIMUM_SAMPLES__" in REQUEST_QUERY
    assert 'trim_end(@"/+", tostring(parse_url(Url).Path))' in REQUEST_QUERY
    for field in (
        "environment",
        "deployment_sha",
        "demo_run_id",
        "correlation_id",
    ):
        assert field in REQUEST_QUERY
    assert POLICY["environment"] == "demo"
    assert POLICY["window_minutes"] == 5
    assert POLICY["minimum_samples"] == 20
    assert POLICY["excluded_paths"] == ["/live", "/ready", "/health"]
    bicep = (ROOT / "alerts.bicep").read_text()
    assert "loadJsonContent('./alerts/policy.json')" in bicep
    assert "loadTextContent('./alerts/request-window.kql')" in bicep
    assert "scheduledQueryRules@2023-12-01" in bicep
    assert "operator: 'GreaterThan'" in bicep
    assert "threshold: rule.policy.threshold" in bicep
    assert "useCommonAlertSchema: true" in bicep
    assert "windowSize: 'PT5M'" in bicep and "evaluationFrequency: 'PT1M'" in bicep
    assert "timeAggregation: 'Maximum'" in bicep
    for rule in POLICY["rules"]:
        suffix = (ROOT / f"alerts/{rule}.kql").read_text()
        assert "IncidentFingerprint" in suffix
        assert "hash_sha256(strcat(Environment" in suffix
        assert "|/api/tasks?filter=broken|" in suffix
        assert "WindowStart, WindowEnd" in suffix
        assert "DemoRunId" in suffix and "DeploymentCommit" in suffix
        assert "SampleCount, RequestCount, ErrorCount" in suffix
        assert POLICY["rules"][rule]["metric_column"] in suffix


def test_database_names_are_parameterized_but_must_be_distinct() -> None:
    document = valid_parameters()
    document["parameters"]["stagingDatabaseName"] = {"value": "workshop_preview"}
    assert not validate_parameters(document)
    document["parameters"]["productionDatabaseName"] = {"value": "workshop_preview"}
    assert validate_parameters(document)
    document["parameters"]["productionDatabaseName"] = {"value": "postgres"}
    assert validate_parameters(document)


def request_metrics(samples: list[tuple[int, float, int]]) -> dict[str, float]:
    """Small-fixture nearest-rank oracle for the KQL expressions asserted above.

    Kusto percentilew is approximate on real datasets; these are contract tests,
    not claims that Python executes KQL or predicts live ingestion.
    """
    if not samples:
        return {"sample_count": 0, "ErrorRate": 0, "P95DurationMs": 0}
    weights = [max(weight, 1) for _, _, weight in samples]
    total = sum(weights)
    errors = sum(
        weight
        for (code, _, _), weight in zip(samples, weights, strict=True)
        if 500 <= code <= 599
    )
    target = math.ceil(total * POLICY["percentile"] / 100)
    cumulative = 0
    percentile = 0.0
    for duration, weight in sorted(
        (duration, weight)
        for (_, duration, _), weight in zip(samples, weights, strict=True)
    ):
        cumulative += weight
        if cumulative >= target:
            percentile = duration
            break
    return {
        "sample_count": len(samples),
        "ErrorRate": 100.0 * errors / total,
        "P95DurationMs": percentile,
    }


def breached(signal: str, samples: list[tuple[int, float, int]]) -> bool:
    metrics = request_metrics(samples)
    rule = POLICY["rules"][signal]
    return (
        metrics["sample_count"] >= POLICY["minimum_samples"]
        and metrics[rule["metric_column"]] > rule["threshold"]
    )


@pytest.mark.parametrize(
    "samples,expected",
    [
        ([], False),
        ([(500, 100, 1)] * 19, False),
        ([(500, 100, 100)], False),
        ([(500, 100, 1)] + [(200, 100, 1)] * 19, False),  # Exactly 5%.
        ([(500, 100, 1)] * 2 + [(200, 100, 1)] * 18, True),
        ([(499, 100, 1)] * 20, False),
        ([(600, 100, 1)] * 20, False),
        ([(500, 100, 2)] + [(200, 100, 1)] * 19, True),
    ],
)
def test_error_ratio_boundaries(
    samples: list[tuple[int, float, int]], expected: bool
) -> None:
    assert breached("http-5xx", samples) is expected


@pytest.mark.parametrize(
    "samples,expected",
    [
        ([], False),
        ([(200, 4000, 1)] * 19, False),
        ([(200, 3000, 1)] * 20, False),
        ([(200, 3001, 1)] * 20, True),
        ([(200, 100, 1)] * 19 + [(200, 10000, 1)], False),
        ([(200, 100, 1)] * 19 + [(200, 10000, 2)], True),
    ],
)
def test_p95_millisecond_boundaries(
    samples: list[tuple[int, float, int]], expected: bool
) -> None:
    assert breached("latency-p95", samples) is expected
