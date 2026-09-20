"""Create and verify a deterministic, commit-bound App Service ZIP artifact."""

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

from src.version import APP_VERSION


def build_package(root: Path, destination: Path, commit_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("The package must identify an exact commit SHA")
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for folder in ("src", "alembic")
        for path in (root / folder).rglob("*")
        if path.is_file() and path.suffix in {".py", ".mako"}
    }
    files["alembic.ini"] = (root / "alembic.ini").read_bytes()
    files["requirements.txt"] = (root / "requirements.lock").read_bytes()
    files["src/build_info.json"] = json.dumps(
        {"version": APP_VERSION, "commit_sha": commit_sha}, sort_keys=True
    ).encode()
    manifest = {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    files["deployment-manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, data)


def verify_package(path: Path, commit_sha: str) -> None:
    with zipfile.ZipFile(path) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or any(
            name.startswith("/") or ".." in Path(name).parts for name in names
        ):
            raise ValueError("Invalid or duplicate archive paths")
        if sum(item.file_size for item in package.infolist()) > 64 * 1024 * 1024:
            raise ValueError("Application archive exceeds its size bound")
        manifest = json.loads(package.read("deployment-manifest.json"))
        if manifest["schema_version"] != 1 or manifest["commit_sha"] != commit_sha:
            raise ValueError("Artifact commit does not match the gated commit")
        expected = manifest["files"]
        if set(names) != {*expected, "deployment-manifest.json"}:
            raise ValueError("Artifact entries do not match the manifest")
        for name, digest in expected.items():
            if hashlib.sha256(package.read(name)).hexdigest() != digest:
                raise ValueError(f"Artifact digest mismatch: {name}")
        build = json.loads(package.read("src/build_info.json"))
        if build != {"version": APP_VERSION, "commit_sha": commit_sha}:
            raise ValueError("Build identity does not match the artifact")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["build", "verify"])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--path", type=Path, default=Path("dist/app.zip"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.operation == "build":
        actual = subprocess.check_output(
            ["git", "-C", str(args.root), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != args.sha:
            raise ValueError("Checkout does not match the requested release commit")
        changes = subprocess.check_output(
            [
                "git",
                "-C",
                str(args.root),
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                "src",
                "alembic",
                "alembic.ini",
                "requirements.lock",
            ],
            text=True,
        )
        if changes.strip():
            raise ValueError("Package source has tracked or untracked changes")
        build_package(args.root, args.path, args.sha)
    verify_package(args.path, args.sha)
    print(f"Verified package for {args.sha}")


if __name__ == "__main__":
    main()
