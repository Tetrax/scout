from __future__ import annotations

import hashlib


def _validate_identity_parts(kind: str, identity_parts: tuple[str, ...]) -> None:
    for part in (kind, *identity_parts):
        if not isinstance(part, str) or not part or "\x00" in part:
            raise ValueError("identity parts must be non-empty strings without NUL bytes")


def stable_id(kind: str, *identity_parts: str) -> str:
    """Return a deterministic, type-namespaced SHA-256 identifier."""
    _validate_identity_parts(kind, identity_parts)
    payload = "\x00".join((kind, *identity_parts)).encode("utf-8")
    return f"{kind}-{hashlib.sha256(payload).hexdigest()}"


def observation_id(source_id: str, external_id: str) -> str:
    return stable_id("observation", source_id, external_id)


def event_id(canonical_identity: str) -> str:
    return stable_id("event", canonical_identity)


def card_id(run_identifier: str, event_identifier: str) -> str:
    return stable_id("card", run_identifier, event_identifier)


def feedback_id(card_identifier: str, label: str) -> str:
    return stable_id("feedback", card_identifier, label)


def run_id(profile_id: str, started_at: str, invocation_id: str) -> str:
    return stable_id("run", profile_id, started_at, invocation_id)


__all__ = [
    "card_id",
    "event_id",
    "feedback_id",
    "observation_id",
    "run_id",
    "stable_id",
]
