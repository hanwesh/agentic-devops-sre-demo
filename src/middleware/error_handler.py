"""Global exception handler middleware."""

import logging
import traceback

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Catches unhandled exceptions and returns structured error responses."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            logger.error(
                "Unhandled exception on %s %s: %s\n%s",
                request.method,
                request.url.path,
                exc,
                traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "error_type": type(exc).__name__,
                    "path": str(request.url.path),
                },
            )
