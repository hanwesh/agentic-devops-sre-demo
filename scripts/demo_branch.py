"""Create or restore a disposable, local-only demo regression branch."""

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

HELPER_PATH = "src/demo_scenario.py"
HEALTHY_FUNCTION = '''def resolve_demo_status(filters: Mapping[str, str]) -> str:
    """Use pending tasks when the caller omits the optional status filter."""
    return filters.get("status", "pending")
'''
REGRESSION_FUNCTION = HEALTHY_FUNCTION.replace(
    'return filters.get("status", "pending")', 'return filters["status"]'
)
RUN_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
COAUTHOR = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"


class DemoBranchError(ValueError):
    """An unsafe or ambiguous local branch operation was refused."""


@dataclass(frozen=True)
class BranchResult:
    branch: str
    run_id: str
    commit_sha: str
    scenario_state: str
    changed: bool
    base_sha: str | None = None


def git(repo: Path, *arguments: str) -> str:
    """Run Git without a shell, network authentication, or interactive input."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DemoBranchError(
            "Git operation failed; inspect the local worktree. "
            "No destructive rollback, push, or deployment was attempted."
        ) from exc
    return result.stdout


def validate_operator(run_id: str, environment: str, acknowledged: bool) -> None:
    if environment != "demo" or not acknowledged:
        raise DemoBranchError(
            "Require --environment demo and --acknowledge-disposable-demo"
        )
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise DemoBranchError(
            "Run ID must be 1-48 lowercase letters, digits, or hyphens, "
            "starting and ending with a letter or digit"
        )


def clean_repository(repo: Path) -> Path:
    root = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    if git(root, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise DemoBranchError(
            "Worktree and index must be clean, including untracked files"
        )
    for operation in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-merge",
        "rebase-apply",
    ):
        location = Path(git(root, "rev-parse", "--git-path", operation).strip())
        if not location.is_absolute():
            location = root / location
        if location.exists():
            raise DemoBranchError("Finish the existing Git operation first")
    return root


def replace_known_function(source: str, before: str, after: str) -> str:
    if source.count(before) != 1 or after in source:
        raise DemoBranchError("Helper differs from the exact known scenario; refusing")
    return source.replace(before, after, 1)


def commit_change(repo: Path, source: str, message: str, parent: str) -> str:
    git(repo, "var", "GIT_AUTHOR_IDENT")
    git(repo, "var", "GIT_COMMITTER_IDENT")
    (repo / HELPER_PATH).write_text(source, encoding="utf-8")
    git(repo, "add", "--", HELPER_PATH)
    git(repo, "commit", "--only", "-m", f"{message}\n\n{COAUTHOR}", "--", HELPER_PATH)
    commit = git(repo, "rev-parse", "HEAD").strip()
    changed = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    if (
        changed.strip() != HELPER_PATH
        or git(repo, "rev-parse", "HEAD^").strip() != parent
        or git(repo, "show", f"HEAD:{HELPER_PATH}") != source
        or git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip()
    ):
        raise DemoBranchError("Commit verification failed; inspect without resetting")
    return commit


def introduce(
    repo: Path,
    *,
    run_id: str,
    base: str,
    environment: str,
    acknowledged: bool,
) -> BranchResult:
    """Commit exactly one fallback removal on a new branch from an approved SHA."""
    validate_operator(run_id, environment, acknowledged)
    if SHA_PATTERN.fullmatch(base) is None:
        raise DemoBranchError(
            "--base must be the full, approved 40-character commit SHA"
        )
    root = clean_repository(repo)
    branch = f"demo/{run_id}"
    refs = git(
        root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"
    )
    if any(ref.endswith(f"/{branch}") for ref in refs.splitlines()):
        raise DemoBranchError(
            "This run branch already exists; use a new explicit run ID"
        )
    if git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip() != base:
        raise DemoBranchError("The approved base must resolve to that exact commit")
    source = replace_known_function(
        git(root, "show", f"{base}:{HELPER_PATH}"),
        HEALTHY_FUNCTION,
        REGRESSION_FUNCTION,
    )
    git(root, "var", "GIT_AUTHOR_IDENT")
    git(root, "var", "GIT_COMMITTER_IDENT")
    git(root, "switch", "--create", branch, base)
    commit = commit_change(
        root,
        source,
        (
            f"demo: introduce optional-filter regression for {run_id}\n\n"
            f"Demo-Run-Id: {run_id}\nDemo-Base-SHA: {base}\n"
            "Demo-Scenario-State: regression"
        ),
        base,
    )
    return BranchResult(branch, run_id, commit, "regression", True, base)


def reset(
    repo: Path,
    *,
    run_id: str,
    environment: str,
    acknowledged: bool,
) -> BranchResult:
    """Restore the known fallback with a forward commit, never a history reset."""
    validate_operator(run_id, environment, acknowledged)
    root = clean_repository(repo)
    branch = f"demo/{run_id}"
    if git(root, "symbolic-ref", "--short", "HEAD").strip() != branch:
        raise DemoBranchError(f"Reset is allowed only on the current {branch} branch")
    parent = git(root, "rev-parse", "HEAD").strip()
    source = git(root, "show", f"HEAD:{HELPER_PATH}")
    if source.count(HEALTHY_FUNCTION) == 1 and REGRESSION_FUNCTION not in source:
        return BranchResult(branch, run_id, parent, "healthy", False)
    restored = replace_known_function(source, REGRESSION_FUNCTION, HEALTHY_FUNCTION)
    commit = commit_change(
        root,
        restored,
        (
            f"demo: restore optional-filter fallback for {run_id}\n\n"
            f"Demo-Run-Id: {run_id}\nDemo-Scenario-State: healthy"
        ),
        parent,
    )
    return BranchResult(branch, run_id, commit, "healthy", True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("introduce", "reset"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--run-id", required=True)
        subparser.add_argument("--environment", choices=["demo"], required=True)
        subparser.add_argument(
            "--acknowledge-disposable-demo", action="store_true", required=True
        )
        if command == "introduce":
            subparser.add_argument(
                "--base",
                required=True,
                help="Full approved healthy commit SHA, not a ref",
            )
    args = parser.parse_args(argv)
    try:
        common = {
            "run_id": args.run_id,
            "environment": args.environment,
            "acknowledged": args.acknowledge_disposable_demo,
        }
        if args.command == "introduce":
            result = introduce(Path.cwd(), base=args.base, **common)
        else:
            result = reset(Path.cwd(), **common)
    except DemoBranchError as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}))
        return 2
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
