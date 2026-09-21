"""Render environment federation metadata from read-only GitHub OIDC settings."""

import argparse
import json
import re
from pathlib import Path
from typing import Any


def environment_subject(
    repository: dict[str, Any], settings: dict[str, Any], environment: str
) -> str:
    if environment not in {"staging", "production", "demo"}:
        raise ValueError("Select a declared deployment environment")
    owner = repository.get("owner", {})
    if not isinstance(owner, dict):
        raise ValueError("GitHub repository owner is missing")
    name = repository.get("name", "")
    login = owner.get("login", "")
    if not all(
        isinstance(part, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        for part in (login, name)
    ):
        raise ValueError("GitHub repository identity is missing")
    if settings.get("use_default") is not True and settings.get(
        "include_claim_keys"
    ) != ["repo", "context"]:
        raise ValueError(
            "Custom/inherited OIDC template requires separate operator review; "
            "only default or explicit repo/context templates are supported"
        )
    immutable = settings.get("use_immutable_subject")
    if type(immutable) is not bool:
        raise ValueError(
            "GitHub did not expose use_immutable_subject; do not infer it from dates"
        )
    if immutable:
        if (
            type(owner.get("id")) is not int
            or owner["id"] <= 0
            or type(repository.get("id")) is not int
            or repository["id"] <= 0
        ):
            raise ValueError("Immutable federation requires owner and repository IDs")
        prefix = f"{login}@{owner['id']}/{name}@{repository['id']}"
    else:
        prefix = f"{login}/{name}"
    if settings.get("sub_claim_prefix", f"repo:{prefix}") != f"repo:{prefix}":
        raise ValueError("GitHub sub_claim_prefix differs; review the subject template")
    return f"repo:{prefix}:environment:{environment}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-json", type=Path, required=True)
    parser.add_argument("--settings-json", type=Path, required=True)
    parser.add_argument(
        "--environment", choices=["staging", "production", "demo"], required=True
    )
    args = parser.parse_args()
    repository = json.loads(args.repository_json.read_text())
    settings = json.loads(args.settings_json.read_text())
    if not isinstance(repository, dict) or not isinstance(settings, dict):
        raise ValueError("Read-only GitHub responses must be JSON objects")
    print(
        json.dumps(
            {
                "name": f"github-{args.environment}",
                "issuer": "https://token.actions.githubusercontent.com",
                "subject": environment_subject(repository, settings, args.environment),
                "audiences": ["api://AzureADTokenExchange"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
