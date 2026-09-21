"""Request logging middleware."""

import logging
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from src.config import settings
from src.telemetry import initialize_request_context, request_context

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Logs all incoming requests with timing information."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start_time = time.monotonic()
        initialize_request_context(request)

        response = await call_next(request)

        duration_ms = (time.monotonic() - start_time) * 1000
        context = request_context(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["X-Deployment-SHA"] = context["deployment_sha"] or "unknown"
        response.headers["X-Environment"] = settings.environment
        response.headers["X-Demo-Run-ID"] = settings.demo_run_id
        response.headers["X-Demo-Scenario-Enabled"] = str(
            settings.demo_scenario_enabled
        ).lower()
        if context["trace_id"] is not None:
            response.headers["X-Trace-ID"] = context["trace_id"]
        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                **context,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )

        return response
