from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_DIR = Path(__file__).with_name("schemas")

_SCHEMA_ALIASES = {
    "profile": "profile-v1",
    "profile-v1": "profile-v1",
    "profilev1": "profile-v1",
    "source": "source-v1",
    "source-v1": "source-v1",
    "sourcev1": "source-v1",
    "observation": "observation-v1",
    "observation-v1": "observation-v1",
    "observationv1": "observation-v1",
    "event": "event-v1",
    "event-v1": "event-v1",
    "eventv1": "event-v1",
    "factualgate": "factual-gate-v1",
    "factual-gate": "factual-gate-v1",
    "factual-gate-v1": "factual-gate-v1",
    "factualgatev1": "factual-gate-v1",
    "decision": "decision-v1",
    "decision-v1": "decision-v1",
    "decisionv1": "decision-v1",
    "card": "card-v1",
    "card-v1": "card-v1",
    "cardv1": "card-v1",
    "feedback": "feedback-v1",
    "feedback-v1": "feedback-v1",
    "feedbackv1": "feedback-v1",
    "run": "run-v1",
    "run-v1": "run-v1",
    "runv1": "run-v1",
    "weeklyreview": "weekly-review-v1",
    "weekly-review": "weekly-review-v1",
    "weekly-review-v1": "weekly-review-v1",
    "weeklyreviewv1": "weekly-review-v1",
}


class ContractValidationError(ValueError):
    """Raised when a document violates a Scout MVP V1 contract."""

    def __init__(self, kind: str, errors: list[str]):
        self.kind = kind
        self.errors = errors
        super().__init__(f"{kind} contract validation failed: {'; '.join(errors)}")


def _schema_stem(kind: str) -> str:
    normalized = kind.strip().lower().replace("_", "-")
    normalized = normalized.replace(" ", "-")
    stem = _SCHEMA_ALIASES.get(normalized)
    if stem is None:
        raise ValueError(f"unknown Scout MVP contract: {kind}")
    return stem


def load_schema(kind: str) -> dict[str, Any]:
    """Load one local Draft 2020-12 schema without remote resolution."""
    path = SCHEMA_DIR / f"{_schema_stem(kind)}.schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"unknown Scout MVP contract: {kind}") from exc


def _run_invariant_errors(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return []

    counts = document.get("counts")
    if not isinstance(counts, dict):
        return []

    count_to_ids = {
        "sources": "source_ids",
        "observations": "observation_ids",
        "events": "event_ids",
        "cards": "card_ids",
    }
    errors: list[str] = []
    for count_name, ids_name in count_to_ids.items():
        count = counts.get(count_name)
        identifiers = document.get(ids_name)
        if isinstance(count, int) and isinstance(identifiers, list) and count != len(identifiers):
            errors.append(
                f"counts.{count_name}: expected {len(identifiers)} to match {ids_name}, got {count}"
            )
    error_count = counts.get("errors")
    run_errors = document.get("errors")
    if isinstance(error_count, int) and isinstance(run_errors, list) and error_count != len(run_errors):
        errors.append(f"counts.errors: expected {len(run_errors)} to match errors, got {error_count}")
    return errors


def validate_document(kind: str, document: Any) -> None:
    """Validate *document* against a local MVP schema.

    Schemas are self-contained and the validator is intentionally created without
    a network resolver.  Errors are returned in deterministic path order through
    :class:`ContractValidationError`.
    """
    schema = load_schema(kind)
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(document),
        key=lambda item: (tuple(str(part) for part in item.path), item.message),
    ):
        location = ".".join(str(part) for part in error.path) or "$"
        errors.append(f"{location}: {error.message}")
    if not errors and _schema_stem(kind) == "run-v1":
        errors.extend(_run_invariant_errors(document))
    if errors:
        raise ContractValidationError(kind, errors)


__all__ = ["ContractValidationError", "SCHEMA_DIR", "load_schema", "validate_document"]
