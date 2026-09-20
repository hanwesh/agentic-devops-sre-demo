"""Exercise branch operations only in disposable repositories under the checkout."""

import os
import runpy
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from scripts.demo_branch import (
    HEALTHY_FUNCTION,
    HELPER_PATH,
    REGRESSION_FUNCTION,
    DemoBranchError,
    git,
    introduce,
    reset,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def git_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, str]]:
    # Do not use tmp_path: operator tests must never write outside the checkout.
    root = PROJECT_ROOT / f".demo-branch-test-{uuid.uuid4().hex}"
    root.mkdir()
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    try:
        (root / "src").mkdir()
        (root / "src/__init__.py").write_text("", encoding="utf-8")
        (root / "tests").mkdir()
        (root / HELPER_PATH).write_text(
            "from collections.abc import Mapping\n\n\n" + HEALTHY_FUNCTION,
            encoding="utf-8",
        )
        shutil.copyfile(
            PROJECT_ROOT / "tests/test_demo_scenario.py",
            root / "tests/test_demo_scenario.py",
        )
        (root / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            'markers = ["demo_regression: healthy contract"]\n',
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n", encoding="utf-8"
        )
        git(root, "init", "--quiet", "--initial-branch=main")
        git(root, "config", "user.name", "Demo branch test")
        git(root, "config", "user.email", "demo-test@example.invalid")
        git(root, "config", "commit.gpgsign", "false")
        git(root, "add", ".")
        git(
            root,
            "commit",
            "--quiet",
            "-m",
            "test: approved fixture\n\n"
            "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
        )
        yield root, git(root, "rev-parse", "HEAD").strip()
    finally:
        shutil.rmtree(root)


def introduce_run(root: Path, base: str, run_id: str = "test-one") -> str:
    result = introduce(
        root, run_id=run_id, base=base, environment="demo", acknowledged=True
    )
    return result.commit_sha


def positive_test(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--capture=sys",
            "-p",
            "no:cacheprovider",
            "tests/test_demo_scenario.py::test_demo_status_defaults_when_status_is_missing",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_introduction_breaks_unchanged_positive_contract_and_reset_restores_it(
    git_repository: tuple[Path, str],
) -> None:
    root, base = git_repository
    original_tests = (root / "tests/test_demo_scenario.py").read_bytes()
    baseline = positive_test(root)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    regression = introduce_run(root, base)

    assert git(root, "symbolic-ref", "--short", "HEAD").strip() == "demo/test-one"
    assert git(root, "diff", "--name-only", base, regression).strip() == HELPER_PATH
    assert (root / "tests/test_demo_scenario.py").read_bytes() == original_tests
    assert REGRESSION_FUNCTION in (root / HELPER_PATH).read_text()
    for command in (
        ["ruff", "check", "--no-cache", HELPER_PATH],
        ["mypy", "--strict", "--cache-dir", ".mypy_cache", HELPER_PATH],
    ):
        gate = subprocess.run(
            [sys.executable, "-m", *command],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert gate.returncode == 0, gate.stdout + gate.stderr
    failure = positive_test(root)
    assert failure.returncode == 1, failure.stdout + failure.stderr
    assert "KeyError: 'status'" in failure.stdout
    assert "1 failed" in failure.stdout

    restored = reset(root, run_id="test-one", environment="demo", acknowledged=True)

    assert restored.scenario_state == "healthy"
    assert restored.changed is True
    assert restored.branch == "demo/test-one"
    assert git(root, "rev-parse", "HEAD^").strip() == regression
    assert git(root, "diff", base, "HEAD", "--", HELPER_PATH) == ""
    assert (root / "tests/test_demo_scenario.py").read_bytes() == original_tests
    recovered = positive_test(root)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    repeated = reset(root, run_id="test-one", environment="demo", acknowledged=True)
    assert repeated.changed is False
    assert repeated.commit_sha == restored.commit_sha


@pytest.mark.asyncio
async def test_introduced_branch_code_fails_and_restored_code_recovers_same_endpoint(
    git_repository: tuple[Path, str],
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import settings
    from src.routes import tasks

    root, base = git_repository
    introduce_run(root, base)
    monkeypatch.setattr(settings, "environment", "demo")
    monkeypatch.setattr(settings, "demo_scenario_enabled", True)
    monkeypatch.setattr(settings, "demo_run_id", "test-one")
    monkeypatch.setattr(
        tasks,
        "resolve_demo_status",
        runpy.run_path(str(root / HELPER_PATH))["resolve_demo_status"],
    )

    control = await client.get("/api/tasks")
    broken = await client.get("/api/tasks?filter=broken")

    assert control.status_code == 200
    assert control.json()["items"] == []
    assert broken.status_code == 500
    assert broken.json()["error_type"] == "KeyError"
    assert broken.json()["path"] == "/api/tasks"
    reset(root, run_id="test-one", environment="demo", acknowledged=True)
    monkeypatch.setattr(
        tasks,
        "resolve_demo_status",
        runpy.run_path(str(root / HELPER_PATH))["resolve_demo_status"],
    )

    recovered = await client.get("/api/tasks?filter=broken")

    assert settings.demo_scenario_enabled is True
    assert settings.demo_run_id == "test-one"
    assert recovered.status_code == 200
    assert recovered.json() == {"items": [], "total": 0, "page": 1, "per_page": 20}


@pytest.mark.parametrize("staged", [False, True])
def test_introduction_refuses_dirty_worktree_or_index(
    git_repository: tuple[Path, str], staged: bool
) -> None:
    root, base = git_repository
    (root / "precious.txt").write_text("Uncommitted work\n")
    if staged:
        git(root, "add", "precious.txt")

    with pytest.raises(DemoBranchError, match="must be clean"):
        introduce_run(root, base)

    assert git(root, "rev-parse", "HEAD").strip() == base
    assert git(root, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert (root / "precious.txt").read_text() == "Uncommitted work\n"


@pytest.mark.parametrize(
    "run_id",
    ["../main", "main/run", "run;touch-owned", "--orphan", "Run", "a" * 49, "a-"],
)
def test_unsafe_run_ids_never_change_git(
    git_repository: tuple[Path, str], run_id: str
) -> None:
    root, base = git_repository
    with pytest.raises(DemoBranchError, match="Run ID"):
        introduce_run(root, base, run_id)
    assert git(root, "rev-parse", "HEAD").strip() == base
    assert git(root, "branch", "--format=%(refname:short)").strip() == "main"


@pytest.mark.parametrize(
    ("environment", "acknowledged"),
    [("production", True), ("staging", True), ("demo", False)],
)
def test_explicit_nonproduction_acknowledgment_required(
    git_repository: tuple[Path, str], environment: str, acknowledged: bool
) -> None:
    root, base = git_repository
    with pytest.raises(DemoBranchError, match="Require"):
        introduce(
            root,
            run_id="test-one",
            base=base,
            environment=environment,
            acknowledged=acknowledged,
        )
    assert git(root, "rev-parse", "HEAD").strip() == base


@pytest.mark.parametrize("base_ref", ["main", "HEAD", "f41af65", "--help", "a" * 40])
def test_base_must_be_an_existing_full_commit(
    git_repository: tuple[Path, str], base_ref: str
) -> None:
    root, base = git_repository
    with pytest.raises(DemoBranchError):
        introduce_run(root, base_ref)
    assert git(root, "rev-parse", "HEAD").strip() == base
    assert git(root, "branch", "--format=%(refname:short)").strip() == "main"


@pytest.mark.parametrize("remote_tracking", [False, True])
def test_existing_run_is_refused_without_replacing_history(
    git_repository: tuple[Path, str], remote_tracking: bool
) -> None:
    root, base = git_repository
    prefix = "refs/remotes/origin" if remote_tracking else "refs/heads"
    git(root, "update-ref", f"{prefix}/demo/test-one", base)
    with pytest.raises(DemoBranchError, match="already exists"):
        introduce_run(root, base)
    assert git(root, "rev-parse", "HEAD").strip() == base
    assert git(root, "rev-parse", f"{prefix}/demo/test-one").strip() == base


def test_reset_refuses_any_branch_other_than_matching_disposable_run(
    git_repository: tuple[Path, str],
) -> None:
    root, base = git_repository
    with pytest.raises(DemoBranchError, match="only on the current"):
        reset(root, run_id="test-one", environment="demo", acknowledged=True)
    assert git(root, "rev-parse", "HEAD").strip() == base


def test_new_explicit_run_uses_approved_base_not_previous_regression(
    git_repository: tuple[Path, str],
) -> None:
    root, base = git_repository
    first = introduce_run(root, base, "first-run")
    second = introduce_run(root, base, "second-run")
    assert git(root, "rev-parse", f"{first}^").strip() == base
    assert git(root, "rev-parse", f"{second}^").strip() == base
    assert git(root, "rev-parse", "demo/first-run").strip() == first
    assert first != second


def test_unknown_helper_is_refused_before_creating_branch(
    git_repository: tuple[Path, str],
) -> None:
    root, _ = git_repository
    (root / HELPER_PATH).write_text(
        "def resolve_demo_status(filters):\n    return None\n"
    )
    git(root, "add", HELPER_PATH)
    git(
        root,
        "commit",
        "--quiet",
        "-m",
        "test: changed helper\n\n"
        "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
    )
    base = git(root, "rev-parse", "HEAD").strip()
    with pytest.raises(DemoBranchError, match="exact known"):
        introduce_run(root, base)
    assert git(root, "branch", "--format=%(refname:short)").strip() == "main"
    assert git(root, "rev-parse", "HEAD").strip() == base
