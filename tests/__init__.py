"""Prevent tests from inheriting live application connections or telemetry."""

import os

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "postgresql+asyncpg://localhost/unused_test_fixture",
        "DATABASE_NAME": "",
        "DATABASE_SSL_REQUIRED": "false",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
        "DEMO_SCENARIO_ENABLED": "false",
        "DEMO_RUN_ID": "",
    }
)
