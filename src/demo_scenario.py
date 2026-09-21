"""Healthy optional-filter behavior used only by the opt-in demo scenario."""

from collections.abc import Mapping


def resolve_demo_status(filters: Mapping[str, str]) -> str:
    """Use pending tasks when the caller omits the optional status filter."""
    return filters.get("status", "pending")
