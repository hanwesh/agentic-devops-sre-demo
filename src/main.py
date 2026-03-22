"""FastAPI application entry point."""

import logging

from fastapi import FastAPI

from src.config import settings
from src.middleware.error_handler import ErrorHandlerMiddleware
from src.middleware.logging_middleware import LoggingMiddleware
from src.routes.health import router as health_router
from src.routes.tasks import router as tasks_router

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Optionally configure Azure Monitor / Application Insights
if settings.applicationinsights_connection_string:
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=settings.applicationinsights_connection_string,
        )
        logger.info("Azure Monitor OpenTelemetry configured")
    except ImportError:
        logger.warning(
            "azure-monitor-opentelemetry not installed; "
            "Application Insights telemetry disabled"
        )

app = FastAPI(
    title=settings.app_name,
    description=(
        "A demo application for showcasing the Agentic DevOps & SRE loop: "
        "Azure SRE Agent → GitHub Issues → Copilot Coding Agent → CI/CD."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Middleware (order matters — outermost first)
app.add_middleware(ErrorHandlerMiddleware)
app.add_middleware(LoggingMiddleware)

# Routes
app.include_router(health_router)
app.include_router(tasks_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Redirect to API documentation."""
    return {
        "message": f"Welcome to {settings.app_name}",
        "docs": "/docs",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
