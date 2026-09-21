"""Offline validation used by the operator-only infrastructure shell helpers."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PURPOSE = "agentic-devops-sre-demo"
SECRET_URI = re.compile(
    r"https://[a-z0-9-]{3,24}\.vault\.azure\.net/secrets/"
    r"[A-Za-z0-9-]+/[a-fA-F0-9]{32}\Z"
)
UUID = r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}"
VAULT_ID = re.compile(
    rf"/subscriptions/{UUID}/resourceGroups/[A-Za-z0-9_.()-]+/"
    r"providers/Microsoft.KeyVault/vaults/[a-zA-Z0-9-]{3,24}\Z",
    re.IGNORECASE,
)


def safe_resource_group(name: str) -> bool:
    """Require a dedicated, deliberately named workshop group."""
    return bool(
        re.fullmatch(r"rg-sre-demo-[a-z0-9][a-z0-9-]{2,42}", name)
        and not re.search(r"(?:^|-)(?:default|shared|prod|production)(?:-|$)", name)
    )


def validate_parameters(document: Any) -> list[str]:
    """Return field names/reasons, never parameter values or credentials."""
    errors: list[str] = []
    if not isinstance(document, dict) or not isinstance(
        document.get("parameters"), dict
    ):
        return ["parameters: expected an ARM deployment parameters object"]
    parameters = document["parameters"]
    if not all(isinstance(entry, dict) for entry in parameters.values()):
        return ["parameters: every parameter must be an object"]
    values = {
        name: entry.get("value")
        for name, entry in parameters.items()
        if isinstance(entry, dict)
    }
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,23}", str(values.get("baseName", ""))):
        errors.append("baseName: replace placeholder with a unique lowercase prefix")
    admin = parameters.get("postgresAdminPassword", {})
    reference = admin.get("reference", {})
    if not isinstance(reference, dict):
        return ["postgresAdminPassword: use an existing Key Vault reference"]
    vault = reference.get("keyVault", {})
    if not isinstance(vault, dict):
        return ["postgresAdminPassword: use an existing Key Vault reference"]
    if (
        "value" in admin
        or not VAULT_ID.fullmatch(str(vault.get("id", "")))
        or not isinstance(reference.get("secretName"), str)
        or not re.fullmatch(r"[A-Za-z0-9-]+", str(reference.get("secretName", "")))
    ):
        errors.append("postgresAdminPassword: use an existing Key Vault reference")
    toggles = (
        "enableDemoSlot",
        "enableDemoAlerts",
        "enableSreAgent",
        "grantSreInvestigationRoles",
    )
    for toggle in toggles:
        if toggle in values and not isinstance(values[toggle], bool):
            errors.append(f"{toggle}: must be a JSON boolean")
    demo = values.get("enableDemoSlot") is True
    sre = values.get("enableSreAgent") is True
    secrets = ["productionDatabaseSecretUri", "stagingDatabaseSecretUri"]
    if demo:
        secrets.append("demoDatabaseSecretUri")
    for field in secrets:
        if not SECRET_URI.fullmatch(str(values.get(field, ""))):
            errors.append(f"{field}: require a versioned existing Key Vault secret URI")
    identities = {str(values.get(field, "")).rsplit("/", 1)[0] for field in secrets}
    if len(identities) != len(secrets):
        errors.append("database secrets: each environment must use a different secret")
    environments = ["production", "staging"] + (["demo"] if demo else [])
    databases = [
        values.get(f"{environment}DatabaseName", f"taskdb_{environment}")
        for environment in environments
    ]
    if not all(
        isinstance(name, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name)
        and name not in {"postgres", "template0", "template1"}
        for name in databases
    ):
        errors.append("database names: require non-system lowercase SQL identifiers")
    elif len(set(databases)) != len(databases):
        errors.append("database names: environments must use distinct databases")
    if values.get("enableDemoAlerts") is True:
        if not demo:
            errors.append("enableDemoAlerts: requires enableDemoSlot")
        if not re.fullmatch(
            r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", str(values.get("alertEmailAddress", ""))
        ):
            errors.append("alertEmailAddress: required before enabling alerts")
    if sre:
        if not demo:
            errors.append("enableSreAgent: requires enableDemoSlot")
        if values.get("sreAgentLocation", "eastus2") not in {
            "australiaeast",
            "eastus2",
            "swedencentral",
        }:
            errors.append("sreAgentLocation: unsupported documented region")
        for field in ("sreAgentModelName", "sreAgentModelProvider"):
            value = values.get(field)
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,80}", value
            ):
                errors.append(f"{field}: select an available model/provider explicitly")
        reference = values.get("sreAgentConsentReference")
        if not isinstance(reference, str) or not reference.strip():
            errors.append(
                "sreAgentConsentReference: require an operator review reference"
            )
    if values.get("grantSreInvestigationRoles") is True and not sre:
        errors.append("grantSreInvestigationRoles: requires enableSreAgent")
    if any("<" in str(value) or ">" in str(value) for value in values.values()):
        errors.append("parameters: unresolved operator placeholders")
    return errors


def validate_group(
    document: Any, expected_name: str, *, disposable: bool = False
) -> list[str]:
    if not safe_resource_group(expected_name):
        return ["resource group: refuse empty, default, production, or shared name"]
    if not isinstance(document, dict) or document.get("name") != expected_name:
        return ["resource group: Azure response does not match the confirmed name"]
    tags = document.get("tags") or {}
    if tags.get("purpose") != PURPOSE or tags.get("deployment") != expected_name:
        return ["resource group: required ownership tags are missing or do not match"]
    if disposable and str(tags.get("disposable", "")).lower() != "true":
        return ["resource group: deletion requires disposable=true"]
    return []


def validate_inventory(document: Any, expected_name: str) -> list[str]:
    if not isinstance(document, list):
        return ["inventory: expected Azure resource list"]
    for resource in document:
        tags = resource.get("tags") or {}
        if tags.get("purpose") != PURPOSE or tags.get("deployment") != expected_name:
            return ["inventory: contains an unowned/shared resource; deletion refused"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["parameters", "group", "inventory", "resource-group"]
    )
    parser.add_argument("value")
    parser.add_argument("--disposable", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "parameters":
            errors = validate_parameters(json.loads(Path(args.value).read_text()))
        elif args.operation == "resource-group":
            errors = (
                []
                if safe_resource_group(args.value)
                else ["resource group: require a dedicated rg-sre-demo-<unique> name"]
            )
        elif args.operation == "group":
            errors = validate_group(
                json.load(sys.stdin), args.value, disposable=args.disposable
            )
        else:
            errors = validate_inventory(json.load(sys.stdin), args.value)
    except (OSError, ValueError, TypeError, AttributeError):
        errors = ["input: unreadable or malformed configuration"]
    print(json.dumps({"status": "blocked" if errors else "passed", "errors": errors}))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
