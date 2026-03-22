"""Tests for task CRUD endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_task(client: AsyncClient) -> None:
    """Create a new task and verify the response."""
    payload = {"title": "Test task", "description": "A test task", "status": "pending"}
    response = await client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test task"
    assert data["description"] == "A test task"
    assert data["status"] == "pending"
    assert "id" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_create_task_minimal(client: AsyncClient) -> None:
    """Create a task with only the required title field."""
    response = await client.post("/api/tasks", json={"title": "Minimal task"})
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Minimal task"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_task_invalid(client: AsyncClient) -> None:
    """Creating a task without title should fail."""
    response = await client.post("/api/tasks", json={})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_tasks_empty(client: AsyncClient) -> None:
    """Listing tasks when none exist returns empty list."""
    response = await client.get("/api/tasks")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_tasks_with_data(client: AsyncClient) -> None:
    """Listing tasks returns created tasks."""
    # Create two tasks
    await client.post("/api/tasks", json={"title": "Task 1"})
    await client.post("/api/tasks", json={"title": "Task 2"})

    response = await client.get("/api/tasks")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2


@pytest.mark.asyncio
async def test_get_task(client: AsyncClient) -> None:
    """Get a single task by ID."""
    create_resp = await client.post("/api/tasks", json={"title": "Get me"})
    task_id = create_resp.json()["id"]

    response = await client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Get me"


@pytest.mark.asyncio
async def test_get_task_not_found(client: AsyncClient) -> None:
    """Getting a non-existent task returns 404."""
    response = await client.get("/api/tasks/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_task(client: AsyncClient) -> None:
    """Update a task's title and status."""
    create_resp = await client.post("/api/tasks", json={"title": "Update me"})
    task_id = create_resp.json()["id"]

    response = await client.put(
        f"/api/tasks/{task_id}",
        json={"title": "Updated title", "status": "completed"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Updated title"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_delete_task(client: AsyncClient) -> None:
    """Delete a task and confirm it's gone."""
    create_resp = await client.post("/api/tasks", json={"title": "Delete me"})
    task_id = create_resp.json()["id"]

    delete_resp = await client.delete(f"/api/tasks/{task_id}")
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/tasks/{task_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_broken_filter_returns_500(client: AsyncClient) -> None:
    """The intentional 'broken' filter should trigger a 500 error."""
    response = await client.get("/api/tasks?filter=broken")
    assert response.status_code == 500
