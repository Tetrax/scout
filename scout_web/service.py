from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database
from .ranking import rank_candidates
from .sources import (
    ENABLED_SOURCES,
    Fetcher,
    SourceError,
    collect_source,
    fetch_bounded,
)

CACHE_TTL = timedelta(minutes=30)
ERROR_RETRY_TTL = timedelta(minutes=5)
MODEL_STATUS = "DETERMINISTIC_DEGRADED"


class DiscoveryBusy(RuntimeError):
    """Another bounded discovery currently owns the cross-process lock."""


def _text(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("service time must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cached timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def collect_and_store(
    database: Database,
    *,
    now: datetime,
    fetcher: Fetcher = fetch_bounded,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Refresh fixed sources concurrently, with bounded cache and isolated errors."""
    now_utc = now.astimezone(timezone.utc)
    now_text = _text(now_utc)
    cached = database.source_cache()
    statuses: dict[str, dict[str, Any]] = {}
    due: list[str] = []
    for source_id in ENABLED_SOURCES:
        previous = cached.get(source_id)
        if not force and previous is not None and _parse(previous["next_refresh_at"]) > now_utc:
            if previous["status"] == "ERROR":
                statuses[source_id] = {
                    "status": "ERROR",
                    "item_count": int(previous["item_count"]),
                    "detail": previous["error"]
                    or "Source momentanément indisponible ou réponse refusée.",
                    "last_success_at": previous["last_success_at"],
                }
                continue
            statuses[source_id] = {
                "status": "CACHED",
                "item_count": int(previous["item_count"]),
                "last_success_at": previous["last_success_at"],
            }
            database.update_source_cache(
                source_id,
                last_attempt_at=str(previous["last_attempt_at"]),
                last_success_at=previous["last_success_at"],
                next_refresh_at=str(previous["next_refresh_at"]),
                status="CACHED",
                error=None,
                item_count=int(previous["item_count"]),
            )
        else:
            due.append(source_id)

    if due:
        with ThreadPoolExecutor(max_workers=min(4, len(due)), thread_name_prefix="scout-source") as executor:
            futures = {
                executor.submit(collect_source, source_id, now=now_utc, fetcher=fetcher): source_id
                for source_id in due
            }
            for future in as_completed(futures):
                source_id = futures[future]
                try:
                    items = future.result()
                    inserted = database.upsert_items(items)
                    status = "OK" if items else "EMPTY"
                    result = {
                        "status": status,
                        "item_count": len(items),
                        "new_items": inserted,
                        "last_success_at": now_text,
                    }
                    database.update_source_cache(
                        source_id,
                        last_attempt_at=now_text,
                        last_success_at=now_text,
                        next_refresh_at=_text(now_utc + CACHE_TTL),
                        status=status,
                        error=None,
                        item_count=len(items),
                    )
                except (SourceError, ValueError, TimeoutError, OSError):
                    result = {
                        "status": "ERROR",
                        "item_count": 0,
                        "detail": "Source momentanément indisponible ou réponse refusée.",
                    }
                    previous = cached.get(source_id)
                    database.update_source_cache(
                        source_id,
                        last_attempt_at=now_text,
                        last_success_at=(
                            None if previous is None else previous.get("last_success_at")
                        ),
                        next_refresh_at=_text(now_utc + ERROR_RETRY_TTL),
                        status="ERROR",
                        error="Source momentanément indisponible ou réponse refusée.",
                        item_count=0,
                    )
                statuses[source_id] = result

    return {source_id: statuses[source_id] for source_id in ENABLED_SOURCES}


def run_discovery(
    database: Database,
    *,
    now: datetime,
    fetcher: Fetcher = fetch_bounded,
) -> dict[str, Any]:
    """Run one manual, lock-protected collection and fact-based ranking."""
    started_at = _text(now)
    owner = uuid.uuid4().hex
    if not database.acquire_discovery_lock(owner, started_at):
        raise DiscoveryBusy("une découverte est déjà en cours")
    try:
        source_statuses = collect_and_store(database, now=now, fetcher=fetcher)
        ranked = rank_candidates(
            database.list_candidates(),
            database.list_interests(),
            database.feedback_by_topic(),
            database.seen_item_ids(),
            database.recent_source_counts(),
            now,
            seen_story_keys=database.seen_story_keys(),
        )
        failed_sources = sum(
            1 for status in source_statuses.values() if status["status"] == "ERROR"
        )
        if failed_sources == len(source_statuses) and not ranked:
            status = "FAILED"
        elif failed_sources:
            status = "PARTIAL"
        else:
            status = "SUCCESS"
        completed_at = _text(datetime.now(timezone.utc))
        run_id = f"run_{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}"
        database.save_run(
            run_id,
            started_at=started_at,
            completed_at=completed_at,
            status=status,
            model_status=MODEL_STATUS,
            source_statuses=source_statuses,
            ranked_items=ranked,
        )
        return {
            "id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": status,
            "model_status": MODEL_STATUS,
            "source_statuses": source_statuses,
            "items": [
                {
                    **entry.item,
                    "score": entry.score,
                    "reason": entry.reason,
                    "is_serendipity": entry.is_serendipity,
                }
                for entry in ranked
            ],
        }
    finally:
        database.release_discovery_lock(owner)


__all__ = [
    "CACHE_TTL",
    "MODEL_STATUS",
    "DiscoveryBusy",
    "collect_and_store",
    "run_discovery",
]
