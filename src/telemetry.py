"""Public request metadata shared by logs, exceptions, and trace spans."""

import re
import uuid

from fastapi import Request
from opentelemetry import trace

from src.config import settings
from src.version import get_build_info


def initialize_request_context(request: Request) -> None:
    supplied = request.headers.get("x-correlation-id", "")
    request.state.correlation_id = (
        supplied
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", supplied)
        else uuid.uuid4().hex
    )
    span = trace.get_current_span()
    context = span.get_span_context()
    request.state.trace_id = f"{context.trace_id:032x}" if context.is_valid else None
    for key, value in request_context(request).items():
        if value is not None:
            span.set_attribute(key, value)


def request_context(request: Request) -> dict[str, str | None]:
    return {
        "environment": settings.environment,
        "deployment_sha": get_build_info().commit_sha,
        "demo_run_id": settings.demo_run_id,
        "correlation_id": getattr(request.state, "correlation_id", None),
        "trace_id": getattr(request.state, "trace_id", None),
    }
