"""Read-only checks for trusted release provenance and GitHub environment protection."""

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.parse import quote

import httpx


def validate_environment(
    data: dict[str, Any],
    policies: dict[str, Any],
    branch: str,
    require_review: bool = True,
) -> None:
    policy = data.get("deployment_branch_policy")
    if (
        not isinstance(policy, dict)
        or policy.get("custom_branch_policies") is not True
        or policy.get("protected_branches") is not False
    ):
        raise ValueError("Environment must allow only explicitly selected branches")
    branches = policies.get("branch_policies", [])
    if (
        not isinstance(branches, list)
        or len(branches) != 1
        or policies.get("total_count") != 1
        or branches[0].get("name") != branch
        or branches[0].get("type", "branch") != "branch"
    ):
        raise ValueError(f"Environment must permit only branch {branch}")
    if require_review:
        rules = data.get("protection_rules", [])
        reviewers = [rule for rule in rules if rule.get("type") == "required_reviewers"]
        if (
            not reviewers
            or not reviewers[0].get("reviewers")
            or reviewers[0].get("prevent_self_review") is not True
        ):
            raise ValueError(
                "Approved deployment requires reviewers and prevention of self-review"
            )


def get_json(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.get(f"https://api.github.com{path}")
    if response.status_code != 200:
        raise ValueError(
            f"Cannot verify GitHub policy: GET {path} returned {response.status_code}"
        )
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("GitHub returned an unexpected JSON shape")
    return value


def verify_release(
    client: httpx.Client,
    repository: str,
    sha: str,
    ref: str,
    event: str,
    runner_labels: str,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release requires an immutable commit SHA")
    labels = json.loads(runner_labels)
    if (
        not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
        or not {"self-hosted", "linux", "x64"}.issubset(labels)
        or len(labels) < 4
    ):
        raise ValueError(
            "AZURE_RUNNER_LABELS must select an ephemeral Linux x64 "
            "private-network runner"
        )
    prefix = f"/repos/{repository}"
    branch = get_json(client, prefix).get("default_branch")
    if not isinstance(branch, str) or event != "push" or ref != f"refs/heads/{branch}":
        raise ValueError("Only a default-branch push can authorize production delivery")
    head = get_json(client, f"{prefix}/git/ref/heads/{quote(branch, safe='')}")
    if head.get("object", {}).get("sha") != sha:
        raise ValueError(
            "Stale release: the gated commit is no longer the default head"
        )
    for name in ("staging", "production"):
        environment = get_json(client, f"{prefix}/environments/{name}")
        policies = get_json(
            client,
            f"{prefix}/environments/{name}/deployment-branch-policies?per_page=100",
        )
        validate_environment(
            environment, policies, branch, require_review=name == "production"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("GITHUB_TOKEN is required for read-only policy checks")
        if os.environ.get("EPHEMERAL_RUNNER_CONFIRMED") != "true":
            raise ValueError(
                "Operator must attest ephemeral single-use runner isolation"
            )
        if os.environ.get("ADMIN_BYPASS_DISABLED_CONFIRMED") != "true":
            raise ValueError(
                "Operator must confirm environment administrator bypass is disabled; "
                "the published REST environment schema cannot verify this setting"
            )
        if os.environ.get("SWAP_KEY_VAULT_READY_CONFIRMED") != "true":
            raise ValueError(
                "Operator must verify both slot identities can resolve target runtime "
                "Key Vault references during swap and rollback warmup"
            )
        with httpx.Client(
            timeout=10,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        ) as client:
            verify_release(
                client,
                os.environ.get("GITHUB_REPOSITORY", ""),
                args.sha,
                os.environ.get("GITHUB_REF", ""),
                os.environ.get("GITHUB_EVENT_NAME", ""),
                os.environ.get("AZURE_RUNNER_LABELS", ""),
            )
    except (ValueError, httpx.HTTPError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Release preflight failed: {message}", file=sys.stderr)
        return 1
    print(
        "Default head, reviewers and branch policies verified via read-only API. "
        "Runner isolation, disabled administrator bypass and swap Key Vault "
        "resolution are operator attestations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
