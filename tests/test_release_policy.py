"""Production delivery cannot confuse an attestation with verified GitHub policy."""

from copy import deepcopy

import httpx
import pytest

from scripts.release_policy import validate_environment, verify_release

ENVIRONMENT = {
    "deployment_branch_policy": {
        "protected_branches": False,
        "custom_branch_policies": True,
    },
    "protection_rules": [
        {
            "type": "required_reviewers",
            "prevent_self_review": True,
            "reviewers": [{"type": "User", "reviewer": {"login": "release-reviewer"}}],
        }
    ],
}
POLICIES = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}


def test_production_environment_policy() -> None:
    validate_environment(ENVIRONMENT, POLICIES, "main")


def test_unqueryable_admin_bypass_needs_operator_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.release_policy import main

    monkeypatch.setenv("GITHUB_TOKEN", "unit-test-token")
    monkeypatch.setenv("EPHEMERAL_RUNNER_CONFIRMED", "true")
    monkeypatch.delenv("ADMIN_BYPASS_DISABLED_CONFIRMED", raising=False)
    monkeypatch.setattr("sys.argv", ["release-policy", "--sha", "a" * 40])
    assert main() == 1
    assert "published REST environment schema cannot verify" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["self_review", "no_review", "all_branches", "tag"])
def test_unsafe_production_policy_is_refused(failure: str) -> None:
    environment = deepcopy(ENVIRONMENT)
    policies = deepcopy(POLICIES)
    if failure == "self_review":
        environment["protection_rules"][0]["prevent_self_review"] = False
    elif failure == "no_review":
        environment["protection_rules"] = []
    elif failure == "all_branches":
        policies["branch_policies"][0]["name"] = "*"
    else:
        policies["branch_policies"][0]["type"] = "tag"
    with pytest.raises(ValueError):
        validate_environment(environment, policies, "main")


@pytest.mark.parametrize("event,head", [("pull_request", "a" * 40), ("push", "b" * 40)])
def test_untrusted_or_stale_release_has_no_privileged_side_effects(
    event: str, head: str
) -> None:
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": head}})
        return httpx.Response(200, json={"default_branch": "main"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            verify_release(
                client,
                "owner/repo",
                "a" * 40,
                "refs/heads/main",
                event,
                '["self-hosted","linux","x64","sre-demo-private"]',
            )
    assert methods and set(methods) == {"GET"}


def test_unqueryable_policy_is_failure_not_manual_approval() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"message": "Forbidden"})
        )
    ) as client:
        with pytest.raises(ValueError, match="returned 403"):
            verify_release(
                client,
                "owner/repo",
                "a" * 40,
                "refs/heads/main",
                "push",
                '["self-hosted","linux","x64","sre-demo-private"]',
            )
