"""Global exception handler middleware."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from src.telemetry import request_context

logger = logging.getLogger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Catches unhandled exceptions and returns structured error responses."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            context = request_context(request)
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            logger.exception(
                "Unhandled exception on %s %s",
                request.method,
                request.url.path,
                extra={**context, "error_type": type(exc).__name__},
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "error_type": type(exc).__name__,
                    "path": str(request.url.path),
                    "correlation_id": context["correlation_id"],
                    "trace_id": context["trace_id"],
                },
            )
