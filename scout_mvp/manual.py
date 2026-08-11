from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from .collection import validate_run
from .ids import run_id
from .storage import JsonlStore


Clock = Callable[[], str | datetime]
InvocationIdFactory = Callable[[], str]
DEFAULT_PROFILE_ID = "example-profile"


def _default_clock() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_invocation_id() -> str:
    return uuid.uuid4().hex


def _timestamp(clock: Any) -> str:
    value = clock() if callable(clock) else clock.now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value:
        raise ValueError("clock must return a non-empty timestamp string or datetime")
    return value


def run_manual(
    root: str | Path,
    *,
    profile_id: str = DEFAULT_PROFILE_ID,
    clock: Clock | Any = _default_clock,
    invocation_id_factory: InvocationIdFactory = _default_invocation_id,
    store: JsonlStore | None = None,
    run_path: str = "runs.jsonl",
) -> dict[str, Any]:
    """Create and persist the Step-1 zero-card manual RunV1 envelope."""
    jsonl_store = store if store is not None else JsonlStore(root)
    started_at = _timestamp(clock)
    finished_at = _timestamp(clock)
    invocation_id = invocation_id_factory()
    run: dict[str, Any] = {
        "id": run_id(profile_id, started_at, invocation_id),
        "invocation_id": invocation_id,
        "profile_id": profile_id,
        "trigger": "MANUAL",
        "status": "SUCCESS",
        "started_at": started_at,
        "finished_at": finished_at,
        "source_ids": [],
        "observation_ids": [],
        "event_ids": [],
        "card_ids": [],
        "counts": {
            "sources": 0,
            "observations": 0,
            "events": 0,
            "cards": 0,
            "errors": 0,
        },
        "errors": [],
        "network_calls": 0,
    }
    validate_run(run)
    jsonl_store.append(run_path, [run], kind="RunV1")
    return run


def main(
    argv: list[str] | None = None,
    *,
    clock: Clock | Any = _default_clock,
    invocation_id_factory: InvocationIdFactory = _default_invocation_id,
    stdout: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="scout-mvp")
    parser.add_argument("--root", required=True, help="absolute JSONL storage root")
    parser.add_argument("--profile-id", required=True, help="runtime profile identifier")
    parser.add_argument("--run-path", default="runs.jsonl", help="safe path under --root")
    args = parser.parse_args(argv)

    run = run_manual(
        args.root,
        profile_id=args.profile_id,
        clock=clock,
        invocation_id_factory=invocation_id_factory,
        run_path=args.run_path,
    )
    output = stdout if stdout is not None else sys.stdout
    output.write(json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    output.write("\n")
    return 0


manual_run = run_manual


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "manual_run", "run_manual"]
