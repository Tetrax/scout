from __future__ import annotations

from typing import Any

from .contracts import validate_document


def validate_run(run: dict[str, Any]) -> None:
    """Validate a collected RunV1 envelope and its collection-level invariants."""
    validate_document("RunV1", run)


__all__ = ["validate_run"]
