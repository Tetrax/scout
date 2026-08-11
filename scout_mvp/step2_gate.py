"""Step 2A authoritative, deterministic Factual Gate for Hermes releases."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import ContractValidationError, validate_document
from .ids import observation_id, stable_id
from .step2_events import release_event_id
from .step2_sources import (
    HERMES_RELEASES_SOURCE,
    validate_official_release_html_url,
)

MAX_RELEASE_AGE = timedelta(days=45)
MAX_ALLOWED_FUTURE = timedelta(hours=24)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class Step2GateError(ValueError):
    """Raised when a Gate cannot be represented as a valid FactualGateV1."""


def factual_gate_id(event_id: str, observation_id: str) -> str:
    """Return the deterministic gate identity for one Event/Observation pair."""
    return stable_id("factual_gate", event_id, observation_id)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fact(metadata: Any, name: str, fallback: Any = None) -> Any:
    return metadata.get(name, fallback) if isinstance(metadata, dict) else fallback


def _release_url(observation: dict[str, Any]) -> str:
    url = observation.get("canonical_url")
    metadata = observation.get("metadata")
    expected_tag = _fact(metadata, "release_tag")
    try:
        return validate_official_release_html_url(url, expected_tag=expected_tag)
    except ValueError as exc:
        raise Step2GateError("a valid official Hermes release URL is required for FactualGateV1") from exc


def _contract_valid(event: dict[str, Any], observation: dict[str, Any]) -> bool:
    try:
        validate_document("ObservationV1", observation)
        validate_document("EventV1", event)
    except ContractValidationError:
        return False
    return True


def _provenance_is_valid(event: dict[str, Any], observation: dict[str, Any]) -> bool:
    observation_identifier = observation.get("id")
    release_url = observation.get("canonical_url")
    event_provenance = event.get("provenance")
    observation_provenance = observation.get("provenance")
    if not isinstance(observation_identifier, str) or not isinstance(release_url, str):
        return False
    if observation.get("source_id") != HERMES_RELEASES_SOURCE["id"]:
        return False
    if observation.get("kind") != "RELEASE":
        return False
    metadata = observation.get("metadata")
    if not isinstance(metadata, dict):
        return False
    release_tag = metadata.get("release_tag")
    release_name = metadata.get("release_name")
    prerelease = metadata.get("prerelease")
    if not isinstance(release_tag, str) or not release_tag:
        return False
    if not isinstance(release_name, str) or not release_name:
        return False
    if not isinstance(prerelease, bool):
        return False
    if observation.get("source_url", HERMES_RELEASES_SOURCE["url"]) != HERMES_RELEASES_SOURCE[
        "url"
    ]:
        return False
    if (
        observation_id(HERMES_RELEASES_SOURCE["id"], str(observation.get("external_id", "")))
        != observation_identifier
    ):
        return False
    try:
        validate_official_release_html_url(release_url)
    except ValueError:
        return False
    if event.get("id") != release_event_id(release_url, release_tag):
        return False
    if event.get("id") is None or event.get("observation_ids") != [observation_identifier]:
        return False
    if event.get("event_type") != "RELEASE" or event.get("material_change") is not True:
        return False
    if event.get("canonical_url") != release_url:
        return False
    if event.get("first_seen_at") != observation.get("observed_at"):
        return False
    if event.get("last_seen_at") != observation.get("observed_at"):
        return False
    if not isinstance(event_provenance, dict):
        return False
    if event_provenance.get("source_urls") != [release_url]:
        return False
    if event_provenance.get("observation_ids") != [observation_identifier]:
        return False
    if event_provenance.get("source_ids") != [HERMES_RELEASES_SOURCE["id"]]:
        return False
    if event_provenance.get("resolution") != "hermes-release-one-to-one-v1":
        return False
    if not isinstance(observation_provenance, dict):
        return False
    if observation_provenance.get("source_url") != HERMES_RELEASES_SOURCE["url"]:
        return False
    if observation_provenance.get("retrieved_at") != observation.get("retrieved_at"):
        return False
    if observation_provenance.get("response_status") != 200:
        return False
    if observation_provenance.get("read_only") is not True:
        return False
    if not isinstance(observation_provenance.get("collector"), str):
        return False
    if not _SHA256_RE.fullmatch(str(observation_provenance.get("content_sha256", ""))):
        return False
    return True


def _freshness(observation: dict[str, Any]) -> tuple[str, str]:
    observed = _timestamp(observation.get("observed_at"))
    published = _timestamp(observation.get("published_at"))
    if observed is None or published is None:
        return "UNKNOWN", "published_at_or_observed_at_invalid"
    if published > observed + MAX_ALLOWED_FUTURE:
        return "UNKNOWN", "published_at_too_far_in_the_future"
    if published < observed - MAX_RELEASE_AGE:
        return "STALE", "published_at_older_than_45_days"
    return "CURRENT", "published_at_within_45_days"


def _locked_facts(observation: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = observation.get("metadata")
    observation_id = observation.get("id")
    facts = (
        ("release_tag", _fact(metadata, "release_tag")),
        ("release_name", _fact(metadata, "release_name")),
        ("published_at", observation.get("published_at")),
        ("canonical_url", observation.get("canonical_url")),
        ("prerelease", _fact(metadata, "prerelease")),
    )
    return [
        {"kind": kind, "value": value, "observation_ids": [observation_id]}
        for kind, value in facts
    ]


def build_factual_gate(
    event: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    """Build a Gate without accepting any model-supplied factual fields."""
    if not isinstance(event, dict) or not isinstance(observation, dict):
        raise Step2GateError("event and observation must be objects")
    event_identifier = event.get("id")
    observation_identifier = observation.get("id")
    if not isinstance(event_identifier, str) or not isinstance(observation_identifier, str):
        raise Step2GateError("event and observation IDs are required")

    contract_ok = _contract_valid(event, observation)
    provenance_ok = contract_ok and _provenance_is_valid(event, observation)
    release_url = _release_url(observation)
    gate = {
        "id": factual_gate_id(event_identifier, observation_identifier),
        "event_id": event_identifier,
        "provenance_status": "VALID" if provenance_ok else "INVALID",
        "evidence_access": "COLLECTED" if provenance_ok else "UNKNOWN",
        "freshness": "UNKNOWN",
        "material_change": "YES",
        "critical_policy": "NORMAL",
        "contradiction_status": "NONE" if provenance_ok else "UNKNOWN",
        "gate_action": "ELIGIBLE" if provenance_ok else "BLOCK",
        "locked_facts": _locked_facts(observation),
        "source_urls": [release_url],
    }

    if provenance_ok:
        freshness, reason = _freshness(observation)
        gate["freshness"] = freshness
        if freshness == "CURRENT":
            gate["gate_action"] = "ELIGIBLE"
        elif freshness == "STALE":
            gate["gate_action"] = "HOLD"
        else:
            gate["gate_action"] = "REVIEW"
        gate["gate_reasons"] = [reason]
    else:
        gate["gate_reasons"] = [
            "invalid_contract_or_release_provenance",
        ]

    validate_document("FactualGateV1", gate)
    return gate


# Descriptive aliases for callers that name the operation as evaluation.
evaluate_factual_gate = build_factual_gate
build_gate = build_factual_gate


__all__ = [
    "MAX_ALLOWED_FUTURE",
    "MAX_RELEASE_AGE",
    "Step2GateError",
    "build_factual_gate",
    "build_gate",
    "evaluate_factual_gate",
    "factual_gate_id",
]
