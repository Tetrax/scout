"""Manual Step 2 end-to-end orchestration and local JSONL persistence."""

from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence, TypeAlias

from .contracts import validate_document
from .ids import run_id
from .step2_candidates import build_triage_candidate
from .step2_events import resolve_release_event
from .step2_gate import build_factual_gate
from .step2_sources import HERMES_RELEASES_SOURCE, collect_hermes_releases
from .step2_transaction import (
    COMMITTED,
    Step2Transaction,
    commit_prepared_transaction,
    prepare_step2_transaction,
    reconcile_committed_transaction,
    recover_step2_transaction,
)
from .storage import JsonlStore
from .triage import (
    DEFAULT_PROFILE_CONTEXT,
    DEFAULT_PROFILE_ID,
    MODEL,
    rank_and_build_cards,
    run_sol_triage,
)


Clock: TypeAlias = Callable[[], str | datetime]
InvocationIdFactory: TypeAlias = Callable[[], str]
Fetcher: TypeAlias = Callable[[str], tuple[bytes, int]]
ModelRunner: TypeAlias = Callable[..., Any]


class Step2RunError(RuntimeError):
    """Raised after a failed manual run has been persisted fail-closed."""

    def __init__(
        self,
        message: str,
        *,
        run: dict[str, Any],
        state_root: Path,
        cause: Exception | None = None,
        audit_persistence_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.run = run
        self.state_root = state_root
        self.cause = cause
        self.audit_persistence_error = audit_persistence_error


@dataclass
class Step2RunResult:
    """In-memory result of one validated and persisted manual Step 2 run."""

    state_root: Path
    run: dict[str, Any]
    source: dict[str, Any]
    observations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    gates: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    cards: list[dict[str, Any]]
    model_session_id: str | None


def _result_from_committed_transaction(transaction: Step2Transaction) -> Step2RunResult:
    gates_by_event = {gate["event_id"]: gate for gate in transaction.gates}
    candidates: list[dict[str, Any]] = []
    for event in transaction.events:
        gate = gates_by_event.get(event["id"])
        if gate is None:
            raise ValueError("committed transaction has no gate for an event")
        candidate = build_triage_candidate(event, gate)
        if candidate is not None:
            candidates.append(candidate)
    return Step2RunResult(
        state_root=transaction.root,
        run=transaction.run,
        source=transaction.source,
        observations=transaction.observations,
        events=transaction.events,
        gates=transaction.gates,
        candidates=candidates,
        decisions=transaction.decisions,
        cards=transaction.cards,
        model_session_id=None,
    )


def default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_invocation_id() -> str:
    return uuid.uuid4().hex


def _timestamp(clock: Clock | Any) -> str:
    value = clock() if callable(clock) else clock.now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value:
        raise ValueError("clock must return a non-empty timestamp string or datetime")
    return value


def _validate_artifact_uniqueness(
    *,
    source: dict[str, Any],
    observations: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    gates: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]] = (),
    decisions: Sequence[dict[str, Any]],
    cards: Sequence[dict[str, Any]],
) -> None:
    """Reject duplicate artifact identities and ambiguous cross-links."""
    seen_ids: dict[str, str] = {}

    def check_ids(kind: str, records: Sequence[dict[str, Any]]) -> None:
        for record in records:
            identifier = record.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError(f"{kind} identity is missing")
            previous = seen_ids.get(identifier)
            if previous is not None:
                raise ValueError(f"duplicate id {identifier!r} across {previous} and {kind}")
            seen_ids[identifier] = kind

    check_ids("source", [source])
    check_ids("observation", observations)
    check_ids("event", events)
    check_ids("gate", gates)
    check_ids("decision", decisions)
    check_ids("card", cards)

    source_ids = {source["id"]}
    observation_ids = {item["id"] for item in observations}
    event_ids = {item["id"] for item in events}
    gate_ids = {item["id"] for item in gates}
    candidate_event_ids: set[str] = set()
    candidate_gate_ids: set[str] = set()
    decision_event_ids: set[str] = set()
    decision_gate_ids: set[str] = set()
    card_event_ids: set[str] = set()
    card_decision_ids: set[str] = set()
    card_gate_ids: set[str] = set()

    def unique_links(values: Sequence[Any], label: str) -> None:
        linked: set[Any] = set()
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} link is missing")
            if value in linked:
                raise ValueError(f"duplicate {label} link {value!r}")
            linked.add(value)

    observation_keys: set[tuple[str, str]] = set()
    for observation in observations:
        source_id = observation.get("source_id")
        external_id = observation.get("external_id")
        if source_id not in source_ids:
            raise ValueError("observation source link is unknown")
        key = (source_id, external_id)
        if key in observation_keys:
            raise ValueError("duplicate observation linked identity")
        observation_keys.add(key)

    event_observation_links: list[str] = []
    for event in events:
        links = event.get("observation_ids")
        if not isinstance(links, list):
            raise ValueError("event observation links are missing")
        unique_links(links, "event observation")
        if any(identifier not in observation_ids for identifier in links):
            raise ValueError("event observation link is unknown")
        event_observation_links.extend(links)
    unique_links(event_observation_links, "event observation")

    for gate in gates:
        event_id = gate.get("event_id")
        if event_id not in event_ids:
            raise ValueError("gate event link is unknown")
        if event_id in {item.get("event_id") for item in gates[: gates.index(gate)]}:
            raise ValueError("duplicate gate event link")
        locked_facts = gate.get("locked_facts")
        if not isinstance(locked_facts, list):
            raise ValueError("gate locked facts are missing")
        for fact in locked_facts:
            if not isinstance(fact, dict):
                raise ValueError("gate locked fact is invalid")
            links = fact.get("observation_ids")
            if not isinstance(links, list) or any(identifier not in observation_ids for identifier in links):
                raise ValueError("gate observation link is unknown")

    candidate_by_event: dict[str, dict[str, Any]] = {}
    candidate_by_gate: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        event_id = candidate.get("event_id")
        gate_id = candidate.get("factual_gate_id")
        if event_id in candidate_event_ids:
            raise ValueError("duplicate candidate event link")
        if gate_id in candidate_gate_ids:
            raise ValueError("duplicate candidate gate link")
        if event_id not in event_ids or gate_id not in gate_ids:
            raise ValueError("candidate link is unknown")
        candidate_event_ids.add(event_id)
        candidate_gate_ids.add(gate_id)
        candidate_by_event[event_id] = candidate
        candidate_by_gate[gate_id] = candidate

    decision_by_event: dict[str, dict[str, Any]] = {}
    decision_by_id: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        event_id = decision.get("event_id")
        gate_id = decision.get("factual_gate_id")
        if event_id in decision_event_ids:
            raise ValueError("duplicate decision event link")
        if gate_id in decision_gate_ids:
            raise ValueError("duplicate decision gate link")
        candidate = candidate_by_event.get(event_id)
        if candidate is None or candidate.get("factual_gate_id") != gate_id:
            raise ValueError("decision link is unknown or inconsistent")
        decision_event_ids.add(event_id)
        decision_gate_ids.add(gate_id)
        decision_by_event[event_id] = decision
        decision_by_id[decision["id"]] = decision

    for card in cards:
        event_id = card.get("event_id")
        decision_id = card.get("decision_id")
        gate_id = card.get("factual_gate_id")
        if event_id in card_event_ids:
            raise ValueError("duplicate card event link")
        if decision_id in card_decision_ids:
            raise ValueError("duplicate card decision link")
        if gate_id in card_gate_ids:
            raise ValueError("duplicate card gate link")
        decision = decision_by_event.get(event_id)
        if decision is None or decision.get("id") != decision_id or decision.get("factual_gate_id") != gate_id:
            raise ValueError("card link is unknown or inconsistent")
        card_event_ids.add(event_id)
        card_decision_ids.add(decision_id)
        card_gate_ids.add(gate_id)


def _run_document(
    *,
    profile_id: str,
    run_identifier: str,
    invocation_id: str,
    started_at: str,
    finished_at: str,
    status: str,
    source: dict[str, Any],
    observations: list[dict[str, Any]],
    events: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    errors: list[str],
    network_calls: int,
    model_session_id: str | None,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "id": run_identifier,
        "invocation_id": invocation_id,
        "profile_id": profile_id,
        "trigger": "MANUAL",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "source_ids": [source["id"]],
        "observation_ids": [item["id"] for item in observations],
        "event_ids": [item["id"] for item in events],
        "card_ids": [item["id"] for item in cards],
        "counts": {
            "sources": 1,
            "observations": len(observations),
            "events": len(events),
            "cards": len(cards),
            "errors": len(errors),
        },
        "errors": list(errors),
        "network_calls": network_calls,
        "model": MODEL,
    }
    if model_session_id is not None:
        session_hash = hashlib.sha256(model_session_id.encode("utf-8")).hexdigest()
        run["notes"] = f"model_session_sha256:{session_hash}"
    validate_document("RunV1", run)
    return run


def _failed_run_error(
    *,
    store: JsonlStore,
    profile_id: str,
    run_identifier: str,
    invocation_id: str,
    started_at: str,
    source: dict[str, Any],
    observations: list[dict[str, Any]],
    events: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    network_calls: int,
    error_code: str,
    cause: Exception,
    clock: Clock | Any,
    finished_at: str | None = None,
) -> Step2RunError:
    """Atomically publish one coherent failed Run without replacing its cause."""
    run = _run_document(
        profile_id=profile_id,
        run_identifier=run_identifier,
        invocation_id=invocation_id,
        started_at=started_at,
        finished_at=finished_at if finished_at is not None else _timestamp(clock),
        status="FAILED",
        source=source,
        observations=observations,
        events=events,
        cards=[],
        errors=[error_code],
        network_calls=network_calls,
        model_session_id=None,
    )
    audit_error: Exception | None = None
    try:
        prepare_step2_transaction(
            store,
            run,
            source,
            observations,
            events,
            gates,
            [],
            [],
        )
        commit_prepared_transaction(store.root, run_identifier)
    except Exception as exc:
        audit_error = exc
    return Step2RunError(
        f"Step 2 manual run failed during {error_code.removesuffix('_failed')}",
        run=run,
        state_root=store.root,
        cause=cause,
        audit_persistence_error=audit_error,
    )


def run_step2(
    state_root: str | Path,
    *,
    profile_id: str = DEFAULT_PROFILE_ID,
    profile_context: Sequence[str] = DEFAULT_PROFILE_CONTEXT,
    clock: Clock | Any,
    invocation_id_factory: InvocationIdFactory,
    fetcher: Fetcher,
    model_runner: ModelRunner | None,
    transaction_crash_hook: Callable[[str], Any] | None = None,
) -> Step2RunResult:
    """Run the frozen Step-2 pipeline once, without delivery or scheduling.

    ``network_calls`` counts attempted external operations at the two enforced
    boundaries: one collector GET attempt, plus one no-tool Sol invocation
    attempt only when at least one candidate is eligible.
    """
    store = JsonlStore(state_root)
    started_at = _timestamp(clock)
    invocation_id = invocation_id_factory()
    run_identifier = run_id(profile_id, started_at, invocation_id)
    source = deepcopy(HERMES_RELEASES_SOURCE)
    validate_document("SourceV1", source)

    recovery_status = recover_step2_transaction(store.root, run_identifier)
    if recovery_status == COMMITTED:
        recovered = reconcile_committed_transaction(store.root, run_identifier)
        if recovered.run["status"] == "FAILED":
            raise Step2RunError(
                "Step 2 manual run was already committed as FAILED",
                run=recovered.run,
                state_root=store.root,
            )
        return _result_from_committed_transaction(recovered)

    try:
        observations = collect_hermes_releases(fetcher=fetcher, observed_at=started_at)
    except Exception as exc:
        run_error = _failed_run_error(
            store=store,
            profile_id=profile_id,
            run_identifier=run_identifier,
            invocation_id=invocation_id,
            started_at=started_at,
            source=source,
            observations=[],
            events=[],
            gates=[],
            network_calls=1,
            error_code="collector_failed",
            cause=exc,
            clock=clock,
        )
        raise run_error from None

    events: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    try:
        for observation in observations:
            validate_document("ObservationV1", observation)
        for observation in observations:
            event = resolve_release_event(observation)
            events.append(event)
            gate = build_factual_gate(event, observation)
            gates.append(gate)
            candidate = build_triage_candidate(event, gate)
            if candidate is not None:
                candidates.append(candidate)
        _validate_artifact_uniqueness(
            source=source,
            observations=observations,
            events=events,
            gates=gates,
            candidates=candidates,
            decisions=[],
            cards=[],
        )
    except Exception as exc:
        run_error = _failed_run_error(
            store=store,
            profile_id=profile_id,
            run_identifier=run_identifier,
            invocation_id=invocation_id,
            started_at=started_at,
            source=source,
            observations=list(observations),
            events=events,
            gates=gates,
            network_calls=1,
            error_code="deterministic_stage_failed",
            cause=exc,
            clock=clock,
        )
        raise run_error from None

    decisions: list[dict[str, Any]] = []
    model_session_id: str | None = None
    network_calls = 1
    if candidates:
        network_calls = 2
        try:
            triage_result = run_sol_triage(
                candidates,
                profile_id=profile_id,
                profile_context=profile_context,
                runner=model_runner,
            )
            if not isinstance(triage_result, dict):
                raise ValueError("model triage result must be an object")
            model_session_id = triage_result.get("session_id")
            if model_session_id is not None and not isinstance(model_session_id, str):
                raise ValueError("model session ID must be text")
            raw_decisions = triage_result.get("decisions")
            if not isinstance(raw_decisions, list):
                raise ValueError("model triage decisions must be a list")
            decisions = list(raw_decisions)
            for decision in decisions:
                validate_document("DecisionV1", decision)
        except Exception as exc:
            run_error = _failed_run_error(
                store=store,
                profile_id=profile_id,
                run_identifier=run_identifier,
                invocation_id=invocation_id,
                started_at=started_at,
                source=source,
                observations=list(observations),
                events=events,
                gates=gates,
                network_calls=2,
                error_code="model_triage_failed",
                cause=exc,
                clock=clock,
            )
            raise run_error from None

    finished_at = _timestamp(clock)
    try:
        cards = rank_and_build_cards(
            run_identifier,
            candidates,
            decisions,
            finished_at,
            profile_id=profile_id,
        )
        run = _run_document(
            profile_id=profile_id,
            run_identifier=run_identifier,
            invocation_id=invocation_id,
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCESS",
            source=source,
            observations=list(observations),
            events=events,
            cards=cards,
            errors=[],
            network_calls=network_calls,
            model_session_id=model_session_id,
        )
    except Exception as exc:
        run_error = _failed_run_error(
            store=store,
            profile_id=profile_id,
            run_identifier=run_identifier,
            invocation_id=invocation_id,
            started_at=started_at,
            source=source,
            observations=list(observations),
            events=events,
            gates=gates,
            network_calls=network_calls,
            error_code="card_ranking_failed",
            cause=exc,
            clock=clock,
            finished_at=finished_at,
        )
        raise run_error from None

    try:
        prepare_step2_transaction(
            store,
            run,
            source,
            list(observations),
            events,
            gates,
            decisions,
            cards,
        )
        commit_prepared_transaction(
            store.root,
            run_identifier,
            crash_hook=transaction_crash_hook,
        )
    except Exception as exc:
        raise Step2RunError(
            "Step 2 manual run publication is incomplete and requires reconciliation",
            run=run,
            state_root=store.root,
            cause=exc,
            audit_persistence_error=exc,
        ) from None

    return Step2RunResult(
        state_root=store.root,
        run=run,
        source=source,
        observations=list(observations),
        events=events,
        gates=gates,
        candidates=candidates,
        decisions=decisions,
        cards=cards,
        model_session_id=model_session_id,
    )


__all__ = [
    "Clock",
    "Fetcher",
    "InvocationIdFactory",
    "ModelRunner",
    "Step2RunError",
    "Step2RunResult",
    "default_clock",
    "default_invocation_id",
    "run_step2",
]
