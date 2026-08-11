"""Minimal manual CLI for one frozen Step-2 run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

# Allow ``python scripts/run_step2.py`` from a checkout without installing the
# package; no runtime state or configuration is discovered implicitly.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_mvp.step2_run import (  # noqa: E402
    default_clock,
    default_invocation_id,
    run_step2,
)
from scout_mvp.step2_sources import urllib_fetch  # noqa: E402


_CONFIG_FIELDS = {"profile_context", "profile_id", "state_root"}
_MAX_CONFIG_BYTES = 64 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate config key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _load_config(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError("--config must be an absolute path")
    payload = path.read_bytes()
    if len(payload) > _MAX_CONFIG_BYTES:
        raise ValueError("config exceeds the 64 KiB limit")
    try:
        config = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("config must be strict UTF-8 JSON") from exc
    if not isinstance(config, dict) or set(config) != _CONFIG_FIELDS:
        raise ValueError("config fields must be exactly profile_context, profile_id, state_root")
    state_root = config["state_root"]
    profile_id = config["profile_id"]
    profile_context = config["profile_context"]
    if not isinstance(state_root, str) or not Path(state_root).is_absolute():
        raise ValueError("config state_root must be an absolute path")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("config profile_id must be non-empty text")
    if (
        not isinstance(profile_context, list)
        or not profile_context
        or any(not isinstance(item, str) or not item.strip() for item in profile_context)
    ):
        raise ValueError("config profile_context must contain non-empty text entries")
    return config


def _summary(result: Any) -> dict[str, Any]:
    state_root = Path(result.state_root)
    run = result.run
    run_identifier = run["id"]
    return {
        "run_id": run_identifier,
        "status": run["status"],
        "counts": run["counts"],
        "model_session_id": result.model_session_id,
        "card_paths": (
            [str(state_root / run_identifier / "cards.jsonl")]
            if result.cards
            else []
        ),
        "decision_paths": (
            [str(state_root / run_identifier / "decisions.jsonl")]
            if result.decisions
            else []
        ),
    }


def main(
    argv: list[str] | None = None,
    *,
    run_fn: Any = run_step2,
    stdout: TextIO | None = None,
    clock: Any = default_clock,
    invocation_id_factory: Any = default_invocation_id,
    fetcher: Any = urllib_fetch,
    model_runner: Any = None,
) -> int:
    parser = argparse.ArgumentParser(prog="scout-step2")
    parser.add_argument("--config", required=True, help="absolute path to private runtime JSON")
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    result = run_fn(
        config["state_root"],
        profile_id=config["profile_id"],
        profile_context=config["profile_context"],
        clock=clock,
        invocation_id_factory=invocation_id_factory,
        fetcher=fetcher,
        model_runner=model_runner,
    )
    output = stdout if stdout is not None else sys.stdout
    output.write(json.dumps(_summary(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
