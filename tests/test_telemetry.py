"""Offline telemetry uses real local spans, never a live exporter."""

from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import StatusCode

from src.middleware.error_handler import ErrorHandlerMiddleware
from src.middleware.logging_middleware import LoggingMiddleware


@pytest.fixture
async def fault_client() -> AsyncGenerator[AsyncClient, None]:
    app = FastAPI()
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(LoggingMiddleware)

    @app.get("/unit-test-error")
    async def fail() -> None:
        raise ValueError("offline regression test")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://unit-test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_error_has_correlated_exception_and_real_trace(
    fault_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    provider = TracerProvider()
    with provider.get_tracer("offline-test").start_as_current_span("request") as span:
        response = await fault_client.get(
            "/unit-test-error", headers={"X-Correlation-ID": "run-1-request-7"}
        )
        assert response.status_code == 500
        assert response.json()["correlation_id"] == "run-1-request-7"
        assert response.json()["trace_id"] == f"{span.get_span_context().trace_id:032x}"
        assert response.headers["x-trace-id"] == response.json()["trace_id"]
        assert span.status.status_code == StatusCode.ERROR
        assert span.events[0].name == "exception"
    error = next(record for record in caplog.records if record.exc_info is not None)
    assert error.error_type == "ValueError"
    assert error.correlation_id == "run-1-request-7"
    assert error.environment == "test"
    provider.shutdown()


@pytest.mark.asyncio
async def test_missing_tracing_is_explicit(fault_client: AsyncClient) -> None:
    response = await fault_client.get("/unit-test-error")
    assert response.status_code == 500
    assert response.json()["trace_id"] is None
    assert "x-trace-id" not in response.headers
    assert response.json()["detail"] == "Internal server error"
    assert "offline regression test" not in response.text
