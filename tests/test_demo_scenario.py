"""Positive contracts for the deliberately isolated optional-filter scenario."""

import pytest
from httpx import AsyncClient

from src.demo_scenario import resolve_demo_status


@pytest.fixture
def demo_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "environment", "demo")
    monkeypatch.setattr(settings, "demo_scenario_enabled", True)
    monkeypatch.setattr(settings, "demo_run_id", "test-run")


@pytest.mark.demo_regression
def test_demo_status_defaults_when_status_is_missing() -> None:
    assert resolve_demo_status({}) == "pending"


@pytest.mark.parametrize("status", ["pending", "in_progress", "completed"])
def test_demo_status_preserves_explicit_filter(status: str) -> None:
    assert resolve_demo_status({"status": status}) == status


@pytest.mark.demo_regression
@pytest.mark.asyncio
async def test_demo_filter_returns_task_list(
    client: AsyncClient, demo_environment: None
) -> None:
    pending = await client.post("/api/tasks", json={"title": "Demo pending task"})
    completed = await client.post(
        "/api/tasks", json={"title": "Demo completed task", "status": "completed"}
    )
    assert pending.status_code == 201
    assert completed.status_code == 201

    response = await client.get("/api/tasks?filter=broken")

    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["total"] == 1
    assert [task["id"] for task in data["items"]] == [pending.json()["id"]]
    assert data["items"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_demo_filter_preserves_explicit_status(
    client: AsyncClient, demo_environment: None
) -> None:
    created = await client.post(
        "/api/tasks", json={"title": "Completed task", "status": "completed"}
    )
    assert created.status_code == 201

    response = await client.get("/api/tasks?filter=broken&status=completed")

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == created.json()["id"]
    assert response.json()["total"] == 1


@pytest.mark.parametrize(
    ("environment", "enabled"),
    [
        ("development", False),
        ("staging", False),
        ("production", False),
        ("demo", False),
        ("staging", True),
        ("production", True),
    ],
)
@pytest.mark.asyncio
async def test_demo_filter_refuses_disabled_or_non_demo_environment(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    enabled: bool,
) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "demo_scenario_enabled", enabled)

    response = await client.get("/api/tasks?filter=broken")

    assert response.status_code == 403
    assert "Demo scenario is disabled" in response.json()["detail"]
    normal_response = await client.get("/api/tasks")
    assert normal_response.status_code == 200
    assert normal_response.json()["items"] == []
