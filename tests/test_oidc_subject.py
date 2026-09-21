"""Federation never guesses a legacy subject or requests/logs an OIDC token."""

import pytest

from scripts.oidc_subject import environment_subject

REPOSITORY = {"name": "demo", "id": 456, "owner": {"login": "example", "id": 123}}


@pytest.mark.parametrize("environment", ["production", "staging", "demo"])
@pytest.mark.parametrize("immutable", [True, False])
def test_environment_subject_uses_actual_repository_settings(
    environment: str, immutable: bool
) -> None:
    result = environment_subject(
        REPOSITORY,
        {"use_default": True, "use_immutable_subject": immutable},
        environment,
    )
    prefix = "example@123/demo@456" if immutable else "example/demo"
    assert result == f"repo:{prefix}:environment:{environment}"


@pytest.mark.parametrize(
    "settings",
    [
        {"use_default": True},
        {"use_default": True, "use_immutable_subject": "false"},
        {
            "use_default": False,
            "include_claim_keys": ["job_workflow_ref"],
            "use_immutable_subject": True,
        },
    ],
)
def test_missing_or_unsupported_claim_templates_fail_closed(settings: dict) -> None:
    with pytest.raises(ValueError):
        environment_subject(REPOSITORY, settings, "production")


def test_explicit_repo_context_template() -> None:
    assert (
        environment_subject(
            REPOSITORY,
            {
                "use_default": False,
                "include_claim_keys": ["repo", "context"],
                "use_immutable_subject": True,
            },
            "staging",
        )
        == "repo:example@123/demo@456:environment:staging"
    )


def test_repository_reported_prefix_must_agree_with_identity() -> None:
    with pytest.raises(ValueError, match="sub_claim_prefix"):
        environment_subject(
            REPOSITORY,
            {
                "use_default": True,
                "use_immutable_subject": True,
                "sub_claim_prefix": "repo:example/demo",
            },
            "production",
        )
