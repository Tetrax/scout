"""Step 2A deterministic one-to-one release Event resolution."""

from __future__ import annotations

import unicodedata
from typing import Any

from .contracts import validate_document
from .ids import stable_id
from .step2_sources import validate_official_release_html_url

MAX_EVENT_TITLE_CHARS = 200
MAX_RELEASE_BODY_CHARS = 1000
MAX_EVENT_SUMMARY_CHARS = 1200


class Step2EventError(ValueError):
    """Raised when an Observation cannot become an official release Event."""


def _sanitize_text(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise Step2EventError(f"release {field} must be text")
    normalized = unicodedata.normalize("NFKC", value)
    safe_chars = []
    for character in normalized:
        if ord(character) < 32 and character not in "\t\r\n":
            safe_chars.append(" ")
        else:
            safe_chars.append(character)
    return " ".join("".join(safe_chars).split())[:limit]


def release_event_id(canonical_url: str, release_tag: str) -> str:
    """Return the stable event identity for one canonical release URL/tag pair."""
    return stable_id("event", "hermes-release-v1", canonical_url, release_tag)


def _release_metadata(observation: dict[str, Any]) -> tuple[str, str, bool]:
    if observation.get("source_id") != "hermes_releases":
        raise Step2EventError("observation is outside the official Hermes release source")
    if observation.get("kind") != "RELEASE":
        raise Step2EventError("observation is not a RELEASE")
    metadata = observation.get("metadata")
    if not isinstance(metadata, dict):
        raise Step2EventError("release observation metadata is required")
    tag = metadata.get("release_tag")
    name = metadata.get("release_name")
    prerelease = metadata.get("prerelease")
    if not isinstance(tag, str) or not tag:
        raise Step2EventError("release_tag is required")
    try:
        validate_official_release_html_url(
            observation.get("canonical_url"), expected_tag=tag
        )
    except ValueError as exc:
        raise Step2EventError("observation canonical URL is not bound to its release tag") from exc
    if not isinstance(name, str) or not name:
        raise Step2EventError("release_name is required")
    if not isinstance(prerelease, bool):
        raise Step2EventError("prerelease must be boolean")
    return tag, name, prerelease


def resolve_release_event(observation: dict[str, Any]) -> dict[str, Any]:
    """Resolve exactly one collected Hermes release Observation into one Event."""
    validate_document("ObservationV1", observation)
    tag, name, _prerelease = _release_metadata(observation)

    canonical_url = observation["canonical_url"]
    title = _sanitize_text(name, field="name", limit=MAX_EVENT_TITLE_CHARS) or tag
    safe_tag = _sanitize_text(tag, field="tag", limit=MAX_EVENT_TITLE_CHARS) or tag
    safe_body = _sanitize_text(
        observation.get("text", ""), field="body", limit=MAX_RELEASE_BODY_CHARS
    )
    identity_title = f"{title} [{safe_tag}]"
    summary = identity_title if not safe_body else f"{identity_title}: {safe_body}"
    summary = summary[:MAX_EVENT_SUMMARY_CHARS]

    event = {
        "id": release_event_id(canonical_url, tag),
        "observation_ids": [observation["id"]],
        "event_type": "RELEASE",
        "title": title,
        "summary": summary,
        "canonical_url": canonical_url,
        "first_seen_at": observation["observed_at"],
        "last_seen_at": observation["observed_at"],
        "material_change": True,
        "material_change_reasons": ["OFFICIAL_RELEASE"],
        "provenance": {
            "source_urls": [canonical_url],
            "observation_ids": [observation["id"]],
            "source_ids": [observation["source_id"]],
            "resolution": "hermes-release-one-to-one-v1",
        },
    }
    validate_document("EventV1", event)
    return event


# Alias with a verb matching the Observation -> Event pipeline wording.
observation_to_event = resolve_release_event


__all__ = [
    "MAX_EVENT_SUMMARY_CHARS",
    "MAX_EVENT_TITLE_CHARS",
    "MAX_RELEASE_BODY_CHARS",
    "Step2EventError",
    "observation_to_event",
    "release_event_id",
    "resolve_release_event",
]
