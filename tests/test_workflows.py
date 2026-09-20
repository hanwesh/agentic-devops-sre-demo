"""Static delivery trust contracts supplement actionlint's syntax validation."""

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def workflow(name: str) -> dict[str, Any]:
    value = yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def test_cd_is_called_only_after_all_exact_commit_gates() -> None:
    ci = workflow("ci.yml")
    gates = {"lint", "unit", "postgres", "security", "configuration"}
    assert set(ci["jobs"]["package"]["needs"]) == gates
    assert set(ci["jobs"]["deploy"]["needs"]) == gates | {"package"}
    assert "github.event_name == 'push'" in ci["jobs"]["deploy"]["if"]
    assert "default_branch" in ci["jobs"]["deploy"]["if"]
    assert ci["jobs"]["deploy"]["with"]["commit_sha"] == "${{ github.sha }}"
    cd = workflow("cd.yml")
    assert set(cd["on"]) == {"workflow_call"}
    assert "workflow_run" not in cd["on"]
    assert cd["concurrency"]["cancel-in-progress"] == "false"
    assert cd["jobs"]["production"]["needs"] == "staging"
    assert cd["jobs"]["production"]["environment"]["name"] == "production"


def test_migrations_are_runner_executed_and_promotions_keep_evidence() -> None:
    cd = workflow("cd.yml")
    content = (WORKFLOWS / "cd.yml").read_text()
    assert "az webapp ssh" not in content
    assert "python -m scripts.migrate" in content
    assert "MIGRATION_DATABASE_URL: ${{ secrets.MIGRATION_DATABASE_URL }}" in content
    for name in ("staging", "production"):
        job = cd["jobs"][name]
        assert job["permissions"]["id-token"] == "write"
        assert job["environment"]["name"] == name
        upload = [
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/upload-artifact")
        ]
        assert upload and upload[0]["if"] == "always()"
    assert "--crud" in content
    assert "python -m scripts.promote" in content
    assert content.count('--runtime-user "$RUNTIME_USER"') == 2
    assert "--staging-database" in content
    assert "SWAP_KEY_VAULT_READY_CONFIRMED" in content


def test_cloud_agent_setup_has_one_supported_unprivileged_job() -> None:
    setup = workflow("copilot-setup-steps.yml")
    assert list(setup["jobs"]) == ["copilot-setup-steps"]
    job = setup["jobs"]["copilot-setup-steps"]
    assert set(job) <= {
        "steps",
        "permissions",
        "runs-on",
        "services",
        "snapshot",
        "timeout-minutes",
    }
    assert job["permissions"] == {"contents": "read"}
    assert "postgres" in job["services"]
    assert "secrets." not in (WORKFLOWS / "copilot-setup-steps.yml").read_text()
