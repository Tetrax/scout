"""Minimal local Step 2 transaction staging and recovery core.

This module deliberately implements one fixed Scout Step 2 transaction shape.  It
is not a general transaction engine: the only accepted files are the V1 JSONL
artifacts produced by Step 2, a single RunV1 document, and one control record.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TypeAlias

from .contracts import validate_document
from .ids import card_id, observation_id, run_id as deterministic_run_id, stable_id
from .step2_candidates import build_triage_candidate
from .step2_events import resolve_release_event
from .step2_gate import build_factual_gate
from .step2_sources import (
    HERMES_RELEASES_SOURCE,
    OFFICIAL_RELEASE_API_URL,
    validate_official_release_html_url,
)
from .storage import JsonlStore
from .triage import rank_and_build_cards


PathLike: TypeAlias = str | os.PathLike[str]
CrashHook: TypeAlias = Callable[[str], Any]

STAGING_DIRNAME = ".staging"
INDEX_FILENAME = "runs.jsonl"
TRANSACTION_FILENAME = "transaction.jsonl"
TRANSACTION_VERSION = 1
PREPARED = "PREPARED"

AFTER_VALIDATE_BEFORE_RENAME = "AFTER_VALIDATE_BEFORE_RENAME"
AFTER_RENAME_BEFORE_INDEX = "AFTER_RENAME_BEFORE_INDEX"
AFTER_INDEX = "AFTER_INDEX"

COMMITTED = "COMMITTED"
NOT_FOUND = "NOT_FOUND"
INCOMPLETE_STAGING = "INCOMPLETE_STAGING"

_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
_RENAME_NOREPLACE = 1


def _rename_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """Linux atomic directory publish that fails if the destination exists."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise Step2TransactionIntegrityError("atomic no-replace rename is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
        src_dir_fd,
        os.fsencode(src),
        dst_dir_fd,
        os.fsencode(dst),
        _RENAME_NOREPLACE,
    ) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise Step2TransactionCollisionError("committed run appeared during atomic rename")
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise Step2TransactionIntegrityError("atomic no-replace rename is unsupported")
    raise OSError(error_number, os.strerror(error_number))


class Step2TransactionError(ValueError):
    """Base class for fail-closed transaction errors."""

    code = "TRANSACTION_ERROR"

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class Step2TransactionValidationError(Step2TransactionError):
    """The input or an on-disk transaction violates the fixed Step 2 shape."""

    code = "INVALID_TRANSACTION"


class Step2TransactionCollisionError(Step2TransactionError):
    """A same-ID final/staging/index record is not the exact expected record."""

    code = "TRANSACTION_COLLISION"


class Step2TransactionIntegrityError(Step2TransactionError):
    """A durable write or read-back could not be reconciled safely."""

    code = "TRANSACTION_INTEGRITY_UNKNOWN"


class UnsafeTransactionPathError(Step2TransactionError):
    """A transaction path contains a symlink, wrong type, or wrong owner."""

    code = "UNSAFE_TRANSACTION_PATH"


class IncompleteStagingError(Step2TransactionError):
    """A recognizable staging directory is not a complete prepared transaction."""

    code = INCOMPLETE_STAGING


class TransactionNotFoundError(Step2TransactionError):
    """The requested transaction location does not exist."""

    code = "NOT_FOUND"


# Short aliases make the typed failure categories convenient to callers without
# creating a second exception hierarchy.
Step2TransactionIncompleteError = IncompleteStagingError
Step2TransactionNotFound = TransactionNotFoundError


@dataclass
class Step2Transaction:
    """Loaded fixed-shape Step 2 transaction data."""

    root: Path
    directory: Path
    run_id: str
    state: str
    manifest: dict[str, Any]
    run: dict[str, Any]
    source: dict[str, Any]
    observations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    gates: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    cards: list[dict[str, Any]]
    location: str

    @property
    def transaction(self) -> dict[str, Any]:
        """Return the control record under its natural public name."""
        return self.manifest

    @property
    def artifacts(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "sources.jsonl": [self.source],
            "observations.jsonl": self.observations,
            "events.jsonl": self.events,
            "factual-gates.jsonl": self.gates,
            "decisions.jsonl": self.decisions,
            "cards.jsonl": self.cards,
            "run.jsonl": [self.run],
        }

    def __getitem__(self, key: str) -> Any:
        values = {
            "root": self.root,
            "directory": self.directory,
            "run_id": self.run_id,
            "state": self.state,
            "manifest": self.manifest,
            "transaction": self.manifest,
            "run": self.run,
            "source": self.source,
            "observations": self.observations,
            "events": self.events,
            "gates": self.gates,
            "decisions": self.decisions,
            "cards": self.cards,
            "location": self.location,
        }
        try:
            return values[key]
        except KeyError as exc:
            raise KeyError(key) from exc


@dataclass(frozen=True)
class _ArtifactSpec:
    filename: str
    kind: str
    argument: str
    required: bool = False


# Keep this order stable: the manifest is an exact, deterministic inventory and
# mirrors the accepted Step 2 artifact specification.
_ARTIFACT_SPECS = (
    _ArtifactSpec("sources.jsonl", "SourceV1", "source", True),
    _ArtifactSpec("observations.jsonl", "ObservationV1", "observations"),
    _ArtifactSpec("events.jsonl", "EventV1", "events"),
    _ArtifactSpec("factual-gates.jsonl", "FactualGateV1", "gates"),
    _ArtifactSpec("decisions.jsonl", "DecisionV1", "decisions"),
    _ArtifactSpec("cards.jsonl", "CardV1", "cards"),
    _ArtifactSpec("run.jsonl", "RunV1", "run", True),
)
_ARTIFACT_BY_FILENAME = {spec.filename: spec for spec in _ARTIFACT_SPECS}
_ALLOWED_FILENAMES = frozenset((TRANSACTION_FILENAME, *_ARTIFACT_BY_FILENAME))


@contextlib.contextmanager
def _root_descriptor(store: JsonlStore, *, create: bool) -> Iterator[int | None]:
    """Open the store root through JsonlStore's no-follow descriptor traversal."""

    with store._root_descriptor(create=create) as root_fd:  # type: ignore[attr-defined]
        yield root_fd


def _root_from(store_or_root: JsonlStore | PathLike) -> tuple[JsonlStore, Path]:
    if isinstance(store_or_root, JsonlStore):
        return store_or_root, store_or_root.root
    store = JsonlStore(store_or_root)
    return store, store.root


def _validate_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise Step2TransactionValidationError("run ID is not a safe single path component")
    return run_id


def _canonical_jsonl(records: Sequence[dict[str, Any]]) -> bytes:
    lines: list[bytes] = []
    for record in records:
        try:
            text = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise Step2TransactionValidationError("artifact is not JSON serializable") from exc
        lines.append((text + "\n").encode("utf-8"))
    return b"".join(lines)


def _same_json(value: Any, expected: Any) -> bool:
    return value == expected


def _require_list(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise Step2TransactionValidationError(f"{name} must be a list")
    if any(not isinstance(item, dict) for item in value):
        raise Step2TransactionValidationError(f"{name} must contain objects")
    return value


def _identity_map(kind: str, records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise Step2TransactionValidationError(f"{kind} identity is missing")
        if identifier in result:
            raise Step2TransactionValidationError(f"duplicate {kind} ID")
        result[identifier] = record
    return result


def _unique_links(values: Any, label: str) -> list[str]:
    if not isinstance(values, list):
        raise Step2TransactionValidationError(f"{label} links are missing")
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise Step2TransactionValidationError(f"{label} link is missing")
        if value in seen:
            raise Step2TransactionValidationError(f"duplicate {label} link")
        seen.add(value)
    return values


def _validate_cross_links(
    *,
    run: dict[str, Any],
    source: dict[str, Any],
    observations: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    gates: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    cards: Sequence[dict[str, Any]],
) -> None:
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id:
        raise Step2TransactionValidationError("source identity is missing")

    observation_by_id = _identity_map("observation", observations)
    event_by_id = _identity_map("event", events)
    gate_by_id = _identity_map("gate", gates)
    decision_by_id = _identity_map("decision", decisions)
    card_by_id = _identity_map("card", cards)

    all_ids: dict[str, str] = {}
    for kind, records in (
        ("source", [source]),
        ("observation", observations),
        ("event", events),
        ("gate", gates),
        ("decision", decisions),
        ("card", cards),
    ):
        for record in records:
            identifier = record["id"]
            previous = all_ids.get(identifier)
            if previous is not None:
                raise Step2TransactionValidationError(
                    f"duplicate artifact ID across {previous} and {kind}"
                )
            all_ids[identifier] = kind

    observation_keys: set[tuple[str, str]] = set()
    for observation in observations:
        if observation.get("source_id") != source_id:
            raise Step2TransactionValidationError("observation source link is unknown")
        provenance = observation.get("provenance")
        metadata = observation.get("metadata")
        if (
            observation.get("source_url") != OFFICIAL_RELEASE_API_URL
            or not isinstance(provenance, dict)
            or provenance.get("source_url") != OFFICIAL_RELEASE_API_URL
            or not isinstance(metadata, dict)
        ):
            raise Step2TransactionValidationError(
                "observation is not bound to the official release API"
            )
        if (
            not isinstance(metadata.get("release_tag"), str)
            or not metadata["release_tag"]
            or not isinstance(metadata.get("release_name"), str)
            or not metadata["release_name"]
            or not isinstance(metadata.get("prerelease"), bool)
        ):
            raise Step2TransactionValidationError(
                "observation release metadata is incomplete"
            )
        try:
            validate_official_release_html_url(
                observation.get("canonical_url"),
                expected_tag=metadata.get("release_tag"),
            )
        except ValueError as exc:
            raise Step2TransactionValidationError(
                "observation is outside the official release route"
            ) from exc
        external_id = observation.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            raise Step2TransactionValidationError("observation external identity is missing")
        if observation.get("id") != observation_id(source_id, external_id):
            raise Step2TransactionValidationError(
                "observation identity is not deterministic"
            )
        key = (source_id, external_id)
        if key in observation_keys:
            raise Step2TransactionValidationError("duplicate observation linked identity")
        observation_keys.add(key)

    linked_observations: set[str] = set()
    for event in events:
        links = _unique_links(event.get("observation_ids"), "event observation")
        if any(identifier not in observation_by_id for identifier in links):
            raise Step2TransactionValidationError("event observation link is unknown")
        if len(links) != 1:
            raise Step2TransactionValidationError(
                "release event must resolve exactly one observation"
            )
        try:
            expected_event = resolve_release_event(observation_by_id[links[0]])
        except ValueError as exc:
            raise Step2TransactionValidationError(
                "observation cannot produce a deterministic event"
            ) from exc
        if event != expected_event:
            raise Step2TransactionValidationError(
                "event does not match deterministic event resolution"
            )
        if linked_observations.intersection(links):
            raise Step2TransactionValidationError("observation is linked by multiple events")
        linked_observations.update(links)
        canonical_urls = {observation_by_id[item]["canonical_url"] for item in links}
        provenance = event.get("provenance")
        if (
            len(canonical_urls) != 1
            or event.get("canonical_url") not in canonical_urls
            or not isinstance(provenance, dict)
            or provenance.get("source_urls") != [event.get("canonical_url")]
            or provenance.get("observation_ids") != links
            or provenance.get("source_ids") != [source_id]
        ):
            raise Step2TransactionValidationError(
                "event provenance is not the exact official release provenance"
            )
    if run.get("status") == "SUCCESS" and linked_observations != set(observation_by_id):
        raise Step2TransactionValidationError("every observation must be linked by one event")

    gate_by_event: dict[str, str] = {}
    for gate in gates:
        gate_id = gate.get("id")
        event_id = gate.get("event_id")
        if not isinstance(gate_id, str) or not gate_id:
            raise Step2TransactionValidationError("gate identity is missing")
        if event_id not in event_by_id:
            raise Step2TransactionValidationError("gate event link is unknown")
        if event_id in gate_by_event:
            raise Step2TransactionValidationError("duplicate gate event link")
        gate_by_event[event_id] = gate_id
        event = event_by_id[event_id]
        event_observation_ids = event.get("observation_ids")
        if not isinstance(event_observation_ids, list) or len(event_observation_ids) != 1:
            raise Step2TransactionValidationError(
                "authoritative gate requires one release observation"
            )
        try:
            expected_gate = build_factual_gate(
                event, observation_by_id[event_observation_ids[0]]
            )
        except ValueError as exc:
            raise Step2TransactionValidationError(
                "deterministic inputs cannot produce an authoritative gate"
            ) from exc
        if gate != expected_gate:
            raise Step2TransactionValidationError(
                "gate does not match authoritative gate recomputation"
            )
        if gate.get("source_urls") != [event_by_id[event_id].get("canonical_url")]:
            raise Step2TransactionValidationError(
                "gate URLs are not the exact official release URL"
            )
        locked_facts = gate.get("locked_facts")
        if not isinstance(locked_facts, list):
            raise Step2TransactionValidationError("gate locked facts are missing")
        for fact in locked_facts:
            if not isinstance(fact, dict):
                raise Step2TransactionValidationError("gate locked fact is invalid")
            fact_links = _unique_links(fact.get("observation_ids"), "gate observation")
            if any(identifier not in observation_by_id for identifier in fact_links):
                raise Step2TransactionValidationError("gate observation link is unknown")
    if run.get("status") == "SUCCESS" and set(gate_by_event) != set(event_by_id):
        raise Step2TransactionValidationError("every event must have exactly one gate")

    decision_by_event: dict[str, dict[str, Any]] = {}
    decision_by_gate: dict[str, str] = {}
    for decision in decisions:
        decision_id = decision.get("id")
        event_id = decision.get("event_id")
        gate_id = decision.get("factual_gate_id")
        if decision_id not in decision_by_id:
            raise Step2TransactionValidationError("decision identity is missing")
        if event_id not in event_by_id or gate_id not in gate_by_id:
            raise Step2TransactionValidationError("decision link is unknown")
        if gate_by_event.get(event_id) != gate_id:
            raise Step2TransactionValidationError("decision event and gate links disagree")
        gate = gate_by_id[gate_id]
        candidate = build_triage_candidate(event_by_id[event_id], gate)
        if candidate is None:
            raise Step2TransactionValidationError(
                "decision cannot reference a non-eligible gate"
            )
        if decision.get("factual_draft") != candidate["summary"]:
            raise Step2TransactionValidationError(
                "decision factual draft does not match locked facts"
            )
        if decision.get("model") != "gpt-5.6-sol":
            raise Step2TransactionValidationError("decision model is not the fixed Step 2 model")
        if decision_id != stable_id("decision", event_id, gate_id, decision["model"]):
            raise Step2TransactionValidationError(
                "decision identity is not deterministic"
            )
        if decision.get("gate_action") != gate.get("gate_action"):
            raise Step2TransactionValidationError(
                "decision gate action does not match the authoritative gate"
            )
        expected_urls = [OFFICIAL_RELEASE_API_URL, event_by_id[event_id]["canonical_url"]]
        if decision.get("source_urls") != expected_urls:
            raise Step2TransactionValidationError(
                "decision URLs are not the exact official release routes"
            )
        if event_id in decision_by_event or gate_id in decision_by_gate:
            raise Step2TransactionValidationError("duplicate decision link")
        decision_by_event[event_id] = decision
        decision_by_gate[gate_id] = decision_id

    for event_id, gate_id in gate_by_event.items():
        gate_action = gate_by_id[gate_id].get("gate_action")
        has_decision = event_id in decision_by_event
        if (
            run.get("status") == "SUCCESS"
            and gate_action in {"ELIGIBLE", "MUST_SHOW"}
            and not has_decision
        ):
            raise Step2TransactionValidationError(
                "every eligible gate must have exactly one decision"
            )
        if gate_action not in {"ELIGIBLE", "MUST_SHOW"} and has_decision:
            raise Step2TransactionValidationError(
                "non-eligible gate must not have a decision"
            )

    card_event_ids: set[str] = set()
    card_decision_ids: set[str] = set()
    card_gate_ids: set[str] = set()
    for card in cards:
        if card.get("run_id") != run["id"]:
            raise Step2TransactionValidationError("card run ID does not match transaction run")
        event_id = card.get("event_id")
        decision_id = card.get("decision_id")
        gate_id = card.get("factual_gate_id")
        if event_id not in event_by_id or decision_id not in decision_by_id or gate_id not in gate_by_id:
            raise Step2TransactionValidationError("card link is unknown")
        if card.get("id") != card_id(run["id"], event_id):
            raise Step2TransactionValidationError("card identity is not deterministic")
        if (
            event_id in card_event_ids
            or decision_id in card_decision_ids
            or gate_id in card_gate_ids
        ):
            raise Step2TransactionValidationError("duplicate card link")
        decision = decision_by_event.get(event_id)
        if (
            decision is None
            or decision.get("id") != decision_id
            or decision.get("factual_gate_id") != gate_id
        ):
            raise Step2TransactionValidationError("card links are inconsistent")
        if decision.get("decision") != "SHOW":
            raise Step2TransactionValidationError("card requires a SHOW decision")
        expected_urls = [OFFICIAL_RELEASE_API_URL, event_by_id[event_id]["canonical_url"]]
        source_links = card.get("source_links")
        if (
            not isinstance(source_links, list)
            or [item.get("url") for item in source_links if isinstance(item, dict)]
            != expected_urls
            or any(
                item.get("source_id") != source_id
                for item in source_links
                if isinstance(item, dict)
            )
            or [item.get("access") for item in source_links if isinstance(item, dict)]
            != ["COLLECTED", "CITED_NOT_COLLECTED"]
        ):
            raise Step2TransactionValidationError(
                "card links are not the exact official release routes"
            )
        card_event_ids.add(event_id)
        card_decision_ids.add(decision_id)
        card_gate_ids.add(gate_id)

    if run.get("status") == "FAILED" and cards:
        raise Step2TransactionValidationError("failed run must not publish cards")
    if run.get("status") == "SUCCESS":
        candidates = []
        for event in events:
            candidate = build_triage_candidate(
                event, gate_by_id[gate_by_event[event["id"]]]
            )
            if candidate is not None:
                candidates.append(candidate)
        expected_cards = rank_and_build_cards(
            run["id"],
            candidates,
            decisions,
            run["finished_at"],
            profile_id=run["profile_id"],
        )
        if list(cards) != expected_cards:
            raise Step2TransactionValidationError(
                "cards do not match deterministic card ranking output"
            )

    expected_source_ids = [source_id]
    expected_observation_ids = [item["id"] for item in observations]
    expected_event_ids = [item["id"] for item in events]
    expected_card_ids = [item["id"] for item in cards]
    if run.get("source_ids") != expected_source_ids:
        raise Step2TransactionValidationError("run source IDs do not match artifacts")
    if run.get("observation_ids") != expected_observation_ids:
        raise Step2TransactionValidationError("run observation IDs do not match artifacts")
    if run.get("event_ids") != expected_event_ids:
        raise Step2TransactionValidationError("run event IDs do not match artifacts")
    if run.get("card_ids") != expected_card_ids:
        raise Step2TransactionValidationError("run card IDs do not match artifacts")

    counts = run.get("counts")
    if not isinstance(counts, dict):
        raise Step2TransactionValidationError("run counts are missing")
    expected_counts = {
        "sources": 1,
        "observations": len(observations),
        "events": len(events),
        "cards": len(cards),
    }
    for name, expected in expected_counts.items():
        if counts.get(name) != expected:
            raise Step2TransactionValidationError(f"run count {name} does not match artifacts")
    errors = run.get("errors")
    if not isinstance(errors, list) or counts.get("errors") != len(errors):
        raise Step2TransactionValidationError("run error count does not match errors")


def _validate_inputs(
    *,
    run: dict[str, Any],
    source: dict[str, Any],
    observations: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    gates: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    cards: Sequence[dict[str, Any]],
) -> str:
    if not isinstance(run, dict) or not isinstance(source, dict):
        raise Step2TransactionValidationError("run and source must be objects")
    if source != HERMES_RELEASES_SOURCE:
        raise Step2TransactionValidationError(
            "transaction source is not the fixed official Hermes source"
        )
    try:
        validate_document("SourceV1", source)
        for kind, records in (
            ("ObservationV1", observations),
            ("EventV1", events),
            ("FactualGateV1", gates),
            ("DecisionV1", decisions),
            ("CardV1", cards),
        ):
            for record in records:
                validate_document(kind, record)
        validate_document("RunV1", run)
    except Exception as exc:
        if isinstance(exc, Step2TransactionError):
            raise
        raise Step2TransactionValidationError("Step 2 artifact contract validation failed") from exc

    run_identifier = _validate_run_id(run.get("id"))
    if run.get("status") not in {"SUCCESS", "FAILED"}:
        raise Step2TransactionValidationError(
            "transaction publication requires a terminal run status"
        )
    if run.get("trigger") != "MANUAL" or run.get("model") != "gpt-5.6-sol":
        raise Step2TransactionValidationError("run is outside the fixed Step 2 scope")
    try:
        expected_run_id = deterministic_run_id(
            run["profile_id"], run["started_at"], run["invocation_id"]
        )
    except (KeyError, ValueError) as exc:
        raise Step2TransactionValidationError("run identity inputs are invalid") from exc
    if run_identifier != expected_run_id:
        raise Step2TransactionValidationError("run identity is not deterministic")
    if source.get("id") is None:
        raise Step2TransactionValidationError("source ID is missing")
    _validate_cross_links(
        run=run,
        source=source,
        observations=observations,
        events=events,
        gates=gates,
        decisions=decisions,
        cards=cards,
    )
    return run_identifier


def _records_by_filename(
    *,
    run: dict[str, Any],
    source: dict[str, Any],
    observations: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    gates: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    cards: Sequence[dict[str, Any]],
) -> dict[str, tuple[_ArtifactSpec, list[dict[str, Any]], bytes]]:
    values: Mapping[str, list[dict[str, Any]]] = {
        "source": [source],
        "observations": list(observations),
        "events": list(events),
        "gates": list(gates),
        "decisions": list(decisions),
        "cards": list(cards),
        "run": [run],
    }
    result: dict[str, tuple[_ArtifactSpec, list[dict[str, Any]], bytes]] = {}
    for spec in _ARTIFACT_SPECS:
        records = values[spec.argument]
        if records or spec.required:
            result[spec.filename] = (spec, records, _canonical_jsonl(records))
    return result


def _manifest_for_payloads(
    run_id: str,
    run: dict[str, Any],
    payloads: Mapping[str, tuple[_ArtifactSpec, list[dict[str, Any]], bytes]],
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    for spec in _ARTIFACT_SPECS:
        entry = payloads.get(spec.filename)
        if entry is None:
            continue
        actual_spec, records, payload = entry
        artifacts.append(
            {
                "filename": actual_spec.filename,
                "kind": actual_spec.kind,
                "count": len(records),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {
        "version": TRANSACTION_VERSION,
        "run_id": run_id,
        "state": PREPARED,
        "prepared_at": run["finished_at"],
        "artifacts": artifacts,
    }


def _check_directory_fd(fd: int, label: str, *, tighten: bool) -> None:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise UnsafeTransactionPathError(f"could not inspect {label}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeTransactionPathError(f"{label} is not a directory")
    if info.st_uid != os.geteuid():
        raise UnsafeTransactionPathError(f"{label} is not owned by the current user")
    if tighten:
        os.fchmod(fd, 0o700)
    elif stat.S_IMODE(info.st_mode) != 0o700:
        raise UnsafeTransactionPathError(f"{label} is not private")


def _open_private_child(parent_fd: int, name: str, *, create: bool, label: str) -> int:
    try:
        fd = JsonlStore._open_directory_at(parent_fd, name, create=create)  # type: ignore[attr-defined]
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise UnsafeTransactionPathError(f"unsafe {label}") from exc
    try:
        _check_directory_fd(fd, label, tighten=create)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _child_stat(parent_fd: int, name: str, *, label: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafeTransactionPathError(f"could not inspect {label}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeTransactionPathError(f"symlink {label} is not allowed")
    return info


def _open_regular_at(directory_fd: int, name: str, *, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | _NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise UnsafeTransactionPathError(f"could not open {label}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeTransactionPathError(f"{label} is not a regular file")
        if info.st_uid != os.geteuid():
            raise UnsafeTransactionPathError(f"{label} is not owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise UnsafeTransactionPathError(f"{label} is not mode 0600")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _read_fd(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise Step2TransactionIntegrityError("transaction file changed while being read")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _read_regular_bytes(directory_fd: int, name: str, *, label: str) -> bytes:
    fd, info = _open_regular_at(directory_fd, name, label=label)
    try:
        return _read_fd(fd, info.st_size)
    finally:
        os.close(fd)


def _inspect_entries(directory_fd: int, *, label: str) -> dict[str, os.stat_result]:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise UnsafeTransactionPathError(f"could not list {label}") from exc
    entries: dict[str, os.stat_result] = {}
    for name in names:
        info = _child_stat(directory_fd, name, label=f"{label}/{name}")
        assert info is not None
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeTransactionPathError(f"nested or non-file entry in {label}")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise UnsafeTransactionPathError(f"unsafe file permissions in {label}")
        entries[name] = info
    return entries


def _location_relative(location: str, run_id: str, filename: str) -> str:
    if location == "staging":
        return f"{STAGING_DIRNAME}/{run_id}/{filename}"
    if location == "committed":
        return f"{run_id}/{filename}"
    raise AssertionError(location)


def _location_directory_path(root: Path, run_id: str, location: str) -> Path:
    if location == "staging":
        return root / STAGING_DIRNAME / run_id
    if location == "committed":
        return root / run_id
    raise AssertionError(location)


def _load_location(
    root: Path,
    run_id: str,
    *,
    location: str,
) -> Step2Transaction:
    run_id = _validate_run_id(run_id)
    store = JsonlStore(root)
    with _root_descriptor(store, create=False) as root_fd:
        if root_fd is None:
            raise TransactionNotFoundError("state root is absent", run_id=run_id)
        _check_directory_fd(root_fd, "state root", tighten=False)
        parent_fd: int | None = None
        directory_fd: int | None = None
        try:
            if location == "staging":
                try:
                    parent_fd = _open_private_child(
                        root_fd,
                        STAGING_DIRNAME,
                        create=False,
                        label="staging parent",
                    )
                except FileNotFoundError as exc:
                    raise TransactionNotFoundError("staging parent is absent", run_id=run_id) from exc
            else:
                parent_fd = os.dup(root_fd)
            try:
                directory_fd = _open_private_child(
                    parent_fd,
                    run_id,
                    create=False,
                    label=f"{location} transaction directory",
                )
            except FileNotFoundError as exc:
                raise TransactionNotFoundError(
                    f"{location} transaction is absent", run_id=run_id
                ) from exc

            entries = _inspect_entries(directory_fd, label=f"{location}/{run_id}")
            if TRANSACTION_FILENAME not in entries:
                raise Step2TransactionValidationError(
                    "transaction control file is missing", run_id=run_id
                )
            expected_mandatory = {
                spec.filename for spec in _ARTIFACT_SPECS if spec.required
            }
            missing = expected_mandatory.difference(entries)
            if missing:
                raise Step2TransactionValidationError(
                    f"mandatory transaction files are missing: {sorted(missing)}", run_id=run_id
                )
            unknown = set(entries).difference(_ALLOWED_FILENAMES)
            if unknown:
                raise Step2TransactionValidationError(
                    f"unknown transaction files: {sorted(unknown)}", run_id=run_id
                )
            for filename, info in entries.items():
                if info.st_size == 0:
                    raise Step2TransactionValidationError(
                        f"empty transaction file: {filename}", run_id=run_id
                    )

            control_bytes = _read_regular_bytes(
                directory_fd, TRANSACTION_FILENAME, label="transaction control file"
            )
            try:
                control_records = store.read(
                    _location_relative(location, run_id, TRANSACTION_FILENAME)
                )
            except Exception as exc:
                raise Step2TransactionValidationError(
                    "transaction control JSONL is malformed", run_id=run_id
                ) from exc
            if len(control_records) != 1:
                raise Step2TransactionValidationError(
                    "transaction control JSONL must contain exactly one record", run_id=run_id
                )
            manifest = control_records[0]
            if set(manifest) != {"version", "run_id", "state", "prepared_at", "artifacts"}:
                raise Step2TransactionValidationError("transaction manifest shape is wrong", run_id=run_id)
            if manifest.get("version") != TRANSACTION_VERSION:
                raise Step2TransactionValidationError("transaction manifest version is wrong", run_id=run_id)
            if manifest.get("run_id") != run_id or manifest.get("state") != PREPARED:
                raise Step2TransactionValidationError("transaction manifest identity/state is wrong", run_id=run_id)
            if not isinstance(manifest.get("prepared_at"), str) or not manifest["prepared_at"]:
                raise Step2TransactionValidationError("transaction prepared_at is wrong", run_id=run_id)
            if control_bytes != _canonical_jsonl([manifest]):
                raise Step2TransactionValidationError("transaction manifest bytes are not canonical", run_id=run_id)

            artifact_entries = manifest.get("artifacts")
            if not isinstance(artifact_entries, list):
                raise Step2TransactionValidationError("transaction artifact inventory is missing", run_id=run_id)
            present_specs = [spec for spec in _ARTIFACT_SPECS if spec.filename in entries]
            if [item.get("filename") for item in artifact_entries if isinstance(item, dict)] != [
                spec.filename for spec in present_specs
            ]:
                raise Step2TransactionValidationError("transaction artifact inventory is not exact", run_id=run_id)
            if len(artifact_entries) != len(present_specs):
                raise Step2TransactionValidationError("transaction artifact inventory has duplicates", run_id=run_id)

            records: dict[str, list[dict[str, Any]]] = {}
            for item, spec in zip(artifact_entries, present_specs):
                if not isinstance(item, dict) or set(item) != {"filename", "kind", "count", "sha256"}:
                    raise Step2TransactionValidationError("transaction inventory entry shape is wrong", run_id=run_id)
                if item.get("filename") != spec.filename or item.get("kind") != spec.kind:
                    raise Step2TransactionValidationError("transaction inventory entry identity is wrong", run_id=run_id)
                count = item.get("count")
                digest = item.get("sha256")
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise Step2TransactionValidationError("transaction inventory count is wrong", run_id=run_id)
                if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                    raise Step2TransactionValidationError("transaction inventory hash is wrong", run_id=run_id)
                payload = _read_regular_bytes(directory_fd, spec.filename, label=spec.filename)
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise Step2TransactionValidationError(
                        f"transaction hash mismatch: {spec.filename}", run_id=run_id
                    )
                try:
                    documents = store.read(
                        _location_relative(location, run_id, spec.filename), kind=spec.kind
                    )
                except Exception as exc:
                    raise Step2TransactionValidationError(
                        f"malformed {spec.filename}", run_id=run_id
                    ) from exc
                if len(documents) != count:
                    raise Step2TransactionValidationError(
                        f"transaction count mismatch: {spec.filename}", run_id=run_id
                    )
                records[spec.filename] = documents

            source_records = records.get("sources.jsonl", [])
            run_records = records.get("run.jsonl", [])
            if len(source_records) != 1 or len(run_records) != 1:
                raise Step2TransactionValidationError(
                    "sources.jsonl and run.jsonl must contain exactly one record", run_id=run_id
                )
            source = source_records[0]
            run = run_records[0]
            observations = records.get("observations.jsonl", [])
            events = records.get("events.jsonl", [])
            gates = records.get("factual-gates.jsonl", [])
            decisions = records.get("decisions.jsonl", [])
            cards = records.get("cards.jsonl", [])
            if manifest.get("prepared_at") != run.get("finished_at"):
                raise Step2TransactionValidationError(
                    "transaction prepared_at does not match run.finished_at", run_id=run_id
                )
            loaded_id = _validate_inputs(
                run=run,
                source=source,
                observations=observations,
                events=events,
                gates=gates,
                decisions=decisions,
                cards=cards,
            )
            if loaded_id != run_id:
                raise Step2TransactionValidationError("run ID does not match directory", run_id=run_id)
            return Step2Transaction(
                root=root,
                directory=_location_directory_path(root, run_id, location),
                run_id=run_id,
                state=manifest["state"],
                manifest=manifest,
                run=run,
                source=source,
                observations=observations,
                events=events,
                gates=gates,
                decisions=decisions,
                cards=cards,
                location=location,
            )
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if parent_fd is not None:
                os.close(parent_fd)


def _try_load_location(root: Path, run_id: str, *, location: str) -> Step2Transaction | None:
    try:
        return _load_location(root, run_id, location=location)
    except TransactionNotFoundError:
        return None


def _same_transaction(
    loaded: Step2Transaction,
    *,
    run: dict[str, Any],
    source: dict[str, Any],
    observations: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    gates: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    cards: Sequence[dict[str, Any]],
) -> bool:
    return (
        loaded.run == run
        and loaded.source == source
        and loaded.observations == list(observations)
        and loaded.events == list(events)
        and loaded.gates == list(gates)
        and loaded.decisions == list(decisions)
        and loaded.cards == list(cards)
    )


def _invoke_hook(crash_hook: CrashHook | None, point: str) -> None:
    if crash_hook is not None:
        crash_hook(point)


def prepare_step2_transaction(
    store_or_root: JsonlStore | PathLike,
    run: dict[str, Any],
    source: dict[str, Any],
    observations: list[dict[str, Any]],
    events: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    cards: list[dict[str, Any]],
) -> Step2Transaction:
    """Validate all Step 2 inputs, then write one complete private staging unit."""

    observations = _require_list(observations, "observations")
    events = _require_list(events, "events")
    gates = _require_list(gates, "gates")
    decisions = _require_list(decisions, "decisions")
    cards = _require_list(cards, "cards")
    run_id = _validate_inputs(
        run=run,
        source=source,
        observations=observations,
        events=events,
        gates=gates,
        decisions=decisions,
        cards=cards,
    )
    store, root = _root_from(store_or_root)
    payloads = _records_by_filename(
        run=run,
        source=source,
        observations=observations,
        events=events,
        gates=gates,
        decisions=decisions,
        cards=cards,
    )
    manifest = _manifest_for_payloads(run_id, run, payloads)

    # A valid final directory is already committed.  It is safe to make a retry
    # idempotent, but never safe to overwrite a same-ID different run.
    final = _try_load_location(root, run_id, location="committed")
    if final is not None:
        if not _same_transaction(
            final,
            run=run,
            source=source,
            observations=observations,
            events=events,
            gates=gates,
            decisions=decisions,
            cards=cards,
        ) or final.manifest != manifest:
            raise Step2TransactionCollisionError("committed run ID contains different content", run_id=run_id)
        return final

    with _root_descriptor(store, create=True) as root_fd:
        assert root_fd is not None
        _check_directory_fd(root_fd, "state root", tighten=True)
        staging_fd = _open_private_child(
            root_fd, STAGING_DIRNAME, create=True, label="staging parent"
        )
        try:
            stage_info = _child_stat(
                staging_fd, run_id, label=f"staging/{run_id}"
            )
            if stage_info is not None and not stat.S_ISDIR(stage_info.st_mode):
                raise IncompleteStagingError("staging run entry is not a directory", run_id=run_id)
            if stage_info is not None:
                # Close descriptors before the full JsonlStore read path, which
                # independently re-checks every component with O_NOFOLLOW.
                pass
        finally:
            os.close(staging_fd)

    try:
        existing = _try_load_location(root, run_id, location="staging")
    except Step2TransactionValidationError as exc:
        raise IncompleteStagingError(
            "existing staging transaction is incomplete or invalid", run_id=run_id
        ) from exc
    if existing is not None:
        if not _same_transaction(
            existing,
            run=run,
            source=source,
            observations=observations,
            events=events,
            gates=gates,
            decisions=decisions,
            cards=cards,
        ) or existing.manifest != manifest:
            raise IncompleteStagingError(
                "existing staging transaction does not match requested content", run_id=run_id
            )
        return existing
    # If a staging directory exists but is malformed, _try_load_location would
    # have raised.  The only remaining case is a new directory.

    with _root_descriptor(store, create=False) as root_fd:
        assert root_fd is not None
        _check_directory_fd(root_fd, "state root", tighten=False)
        staging_fd = _open_private_child(
            root_fd, STAGING_DIRNAME, create=False, label="staging parent"
        )
        try:
            try:
                stage_fd = _open_private_child(
                    staging_fd, run_id, create=True, label="staging run directory"
                )
            except Exception:
                raise
            try:
                _check_directory_fd(stage_fd, "staging run directory", tighten=True)
            finally:
                os.close(stage_fd)
        finally:
            os.close(staging_fd)

    # Files are written only below .staging/<run_id>; transaction.jsonl is last.
    for filename, (spec, records, _payload) in payloads.items():
        store.append(
            _location_relative("staging", run_id, filename),
            records,
            kind=spec.kind,
        )
    store.append(
        _location_relative("staging", run_id, TRANSACTION_FILENAME),
        [manifest],
    )

    with _root_descriptor(store, create=False) as root_fd:
        assert root_fd is not None
        _check_directory_fd(root_fd, "state root", tighten=False)
        staging_fd = _open_private_child(
            root_fd, STAGING_DIRNAME, create=False, label="staging parent"
        )
        try:
            stage_fd = _open_private_child(
                staging_fd, run_id, create=False, label="staging run directory"
            )
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            os.fsync(staging_fd)
            os.fsync(root_fd)
        finally:
            os.close(staging_fd)
    return _load_location(root, run_id, location="staging")


def validate_prepared_transaction(root: PathLike, run_id: str) -> Step2Transaction:
    """Load and strictly validate one complete prepared staging transaction."""

    _validate_run_id(run_id)
    _store, root_path = _root_from(root)
    loaded = _load_location(root_path, run_id, location="staging")
    return loaded


def _fsync_root(root: Path, *, create: bool = False) -> None:
    store = JsonlStore(root)
    with _root_descriptor(store, create=create) as root_fd:
        if root_fd is None:
            raise TransactionNotFoundError("state root is absent")
        _check_directory_fd(root_fd, "state root", tighten=False)
        os.fsync(root_fd)


def reconcile_committed_transaction(
    root: PathLike,
    run_id: str,
    crash_hook: CrashHook | None = None,
) -> Step2Transaction:
    """Reconcile the derived top-level run index for a committed directory."""

    _validate_run_id(run_id)
    _store, root_path = _root_from(root)
    loaded = _load_location(root_path, run_id, location="committed")
    store = JsonlStore(root_path)
    try:
        existing = store.read(INDEX_FILENAME, kind="RunV1")
    except Exception as exc:
        raise Step2TransactionIntegrityError("top-level run index is malformed", run_id=run_id) from exc

    matches = [item for item in existing if item.get("id") == run_id]
    if len(matches) > 1:
        raise Step2TransactionCollisionError("top-level run index contains duplicate run ID", run_id=run_id)
    if matches and matches[0] != loaded.run:
        raise Step2TransactionCollisionError("top-level run index contains mismatched run", run_id=run_id)

    if not matches:
        try:
            store.append(INDEX_FILENAME, [loaded.run], kind="RunV1")
        except Exception as append_error:
            try:
                after = store.read(INDEX_FILENAME, kind="RunV1")
            except Exception as verification_error:
                raise Step2TransactionIntegrityError(
                    "could not verify ambiguous run index append", run_id=run_id
                ) from append_error
            after_matches = [item for item in after if item.get("id") == run_id]
            if len(after_matches) == 1 and after_matches[0] == loaded.run:
                pass
            else:
                raise
        else:
            try:
                after = store.read(INDEX_FILENAME, kind="RunV1")
            except Exception as verification_error:
                raise Step2TransactionIntegrityError(
                    "could not verify run index append", run_id=run_id
                ) from verification_error
            after_matches = [item for item in after if item.get("id") == run_id]
            if len(after_matches) != 1 or after_matches[0] != loaded.run:
                raise Step2TransactionIntegrityError("run index append did not reconcile", run_id=run_id)

    _fsync_root(root_path)
    _invoke_hook(crash_hook, AFTER_INDEX)
    return loaded


def commit_prepared_transaction(
    root: PathLike,
    run_id: str,
    crash_hook: CrashHook | None = None,
) -> Step2Transaction:
    """Atomically publish a validated staging directory, then reconcile its index."""

    _validate_run_id(run_id)
    _store, root_path = _root_from(root)

    # A rename may already have happened before a process crash.  A valid final
    # directory is authoritative and must not be overwritten or require staging.
    final = _try_load_location(root_path, run_id, location="committed")
    if final is not None:
        return reconcile_committed_transaction(root_path, run_id, crash_hook=crash_hook)

    prepared = validate_prepared_transaction(root_path, run_id)
    _invoke_hook(crash_hook, AFTER_VALIDATE_BEFORE_RENAME)

    store = JsonlStore(root_path)
    with _root_descriptor(store, create=False) as root_fd:
        assert root_fd is not None
        _check_directory_fd(root_fd, "state root", tighten=False)
        staging_fd = _open_private_child(
            root_fd, STAGING_DIRNAME, create=False, label="staging parent"
        )
        try:
            stage_fd = _open_private_child(
                staging_fd, run_id, create=False, label="staging run directory"
            )
            try:
                # Recheck final immediately before rename.  os.rename is used
                # descriptor-relatively and no destination is ever overwritten by
                # this code path.
                final_info = _child_stat(root_fd, run_id, label=f"committed/{run_id}")
                if final_info is not None:
                    raise Step2TransactionCollisionError(
                        "committed run appeared before atomic rename", run_id=run_id
                    )
                os.fsync(stage_fd)
                os.fsync(staging_fd)
                os.fsync(root_fd)
                _rename_noreplace(staging_fd, run_id, root_fd, run_id)
                os.fsync(root_fd)
            finally:
                os.close(stage_fd)
        finally:
            os.close(staging_fd)

    _invoke_hook(crash_hook, AFTER_RENAME_BEFORE_INDEX)
    return reconcile_committed_transaction(root_path, run_id, crash_hook=crash_hook)


def recover_step2_transaction(root: PathLike, run_id: str) -> str:
    """Recover a run deterministically without deleting an incomplete stage."""

    _validate_run_id(run_id)
    _store, root_path = _root_from(root)
    final = _try_load_location(root_path, run_id, location="committed")
    if final is not None:
        reconcile_committed_transaction(root_path, run_id)
        return COMMITTED
    try:
        prepared = _try_load_location(root_path, run_id, location="staging")
    except UnsafeTransactionPathError:
        raise
    except Step2TransactionError as exc:
        raise IncompleteStagingError(
            "staging transaction is incomplete or invalid", run_id=run_id
        ) from exc
    if prepared is None:
        return NOT_FOUND
    commit_prepared_transaction(root_path, run_id)
    return COMMITTED


def discard_incomplete_staging(root: PathLike, run_id: str) -> bool:
    """Explicitly delete only a known incomplete fixed-shape staging directory."""

    _validate_run_id(run_id)
    _store, root_path = _root_from(root)
    committed = _try_load_location(root_path, run_id, location="committed")
    if committed is not None:
        raise Step2TransactionCollisionError(
            "refusing to discard a committed transaction", run_id=run_id
        )

    store = JsonlStore(root_path)
    with _root_descriptor(store, create=False) as root_fd:
        if root_fd is None:
            return False
        _check_directory_fd(root_fd, "state root", tighten=False)
        try:
            staging_fd = _open_private_child(
                root_fd, STAGING_DIRNAME, create=False, label="staging parent"
            )
        except FileNotFoundError:
            return False
        try:
            stage_info = _child_stat(staging_fd, run_id, label=f"staging/{run_id}")
            if stage_info is None:
                return False
            if not stat.S_ISDIR(stage_info.st_mode):
                raise UnsafeTransactionPathError("staging run entry is not a directory", run_id=run_id)
            try:
                _load_location(root_path, run_id, location="staging")
            except TransactionNotFoundError:
                raise
            except Step2TransactionError as exc:
                if not isinstance(exc, (IncompleteStagingError, UnsafeTransactionPathError)):
                    # The explicit cleanup path is allowed to remove a malformed
                    # fixed-shape stage, but never an unknown or unsafe tree.
                    if exc.code not in {"INVALID_TRANSACTION", INCOMPLETE_STAGING}:
                        raise
            else:
                raise Step2TransactionCollisionError(
                    "refusing to discard a complete prepared transaction", run_id=run_id
                )

            stage_fd = _open_private_child(
                staging_fd, run_id, create=False, label="staging run directory"
            )
            try:
                stage_entries = _inspect_entries(stage_fd, label=f"staging/{run_id}")
                unknown = set(stage_entries).difference(_ALLOWED_FILENAMES)
                if unknown:
                    raise UnsafeTransactionPathError(
                        f"refusing to delete unknown staging files: {sorted(unknown)}", run_id=run_id
                    )
                for filename in sorted(stage_entries):
                    os.unlink(filename, dir_fd=stage_fd)
                os.rmdir(run_id, dir_fd=staging_fd)
                os.fsync(staging_fd)
                os.fsync(root_fd)
                return True
            finally:
                os.close(stage_fd)
        finally:
            os.close(staging_fd)


__all__ = [
    "AFTER_INDEX",
    "AFTER_RENAME_BEFORE_INDEX",
    "AFTER_VALIDATE_BEFORE_RENAME",
    "COMMITTED",
    "INCOMPLETE_STAGING",
    "NOT_FOUND",
    "IncompleteStagingError",
    "PREPARED",
    "Step2Transaction",
    "Step2TransactionCollisionError",
    "Step2TransactionError",
    "Step2TransactionIncompleteError",
    "Step2TransactionIntegrityError",
    "Step2TransactionNotFound",
    "Step2TransactionValidationError",
    "TransactionNotFoundError",
    "UnsafeTransactionPathError",
    "commit_prepared_transaction",
    "discard_incomplete_staging",
    "prepare_step2_transaction",
    "reconcile_committed_transaction",
    "recover_step2_transaction",
    "validate_prepared_transaction",
]
