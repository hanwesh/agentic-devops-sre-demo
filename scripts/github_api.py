"""Small fixed-origin GitHub REST client; never retry a write automatically."""

from __future__ import annotations

import re
from typing import Any

import httpx

API_VERSION = "2022-11-28"
REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
MAX_RESPONSE_BYTES = 4_000_000


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def validate_repository(repository: str) -> str:
    if not re.fullmatch(REPOSITORY_PATTERN, repository):
        raise ValueError("repository must be an owner/repository name")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise ValueError("invalid repository name")
    return repository


class GitHub:
    def __init__(
        self, token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not token or token != token.strip():
            raise ValueError("a nonempty GitHub token is required")
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("GitHub requests must use fixed-origin relative paths")
        try:
            response = self.client.request(method, path, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise GitHubError(
                f"GitHub {method} transport failure; outcome unknown"
            ) from exc
        if response.status_code not in expected:
            raise GitHubError(
                f"GitHub {method} returned HTTP {response.status_code}",
                response.status_code,
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise GitHubError("GitHub response exceeded the size limit")
        return response

    def object(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            method, path, payload=payload, expected=expected, headers=headers
        )
        try:
            result = response.json()
        except ValueError as exc:
            raise GitHubError("GitHub returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise GitHubError("GitHub returned an unexpected object")
        return result

    def optional_object(self, path: str) -> dict[str, Any] | None:
        try:
            return self.object("GET", path)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def pages(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 21):
            response = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            try:
                batch = response.json()
            except ValueError as exc:
                raise GitHubError("GitHub returned invalid JSON") from exc
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise GitHubError("GitHub returned an unexpected list")
            results.extend(batch)
            if len(batch) < 100:
                return results
        raise GitHubError("GitHub pagination limit reached; evidence is incomplete")
