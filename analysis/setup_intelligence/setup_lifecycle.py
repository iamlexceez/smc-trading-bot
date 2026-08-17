"""Setup lifecycle management for Setup Intelligence V2."""
from __future__ import annotations


def update_lifecycle(setup, status: str) -> None:
    setup.execution_state = status
