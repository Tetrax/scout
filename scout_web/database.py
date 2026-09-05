from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ranking import RankedItem
from .sources import CollectedItem

SCHEMA_VERSION = 1
REACTIONS = {"DISLIKE", "LOVE", "STAR"}
_REACTION_WEIGHTS = {"DISLIKE": -2.5, "LOVE": 2.0, "STAR": 4.0}
_DEFAULT_INTERESTS = (
    (
        "Fortinet & sécurité réseau",
        5.0,
        ("fortinet", "cybersecurity", "networking", "active_exploitation"),
    ),
    ("Hermes & agents IA", 4.0, ("hermes", "ai_agents")),
    (
        "DevOps & automatisation",
        3.5,
        ("devops", "automation", "developer_tools", "containers"),
    ),
    ("Cloud & Linux", 3.0, ("cloud", "linux")),
    ("Gaming & tech", 2.0, ("gaming", "technology")),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _item_id(source_id: str, external_id: str) -> str:
    digest = hashlib.sha256(f"{source_id}\0{external_id}".encode()).hexdigest()[:32]
    return f"item_{digest}"


class Database:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("database path must be absolute")

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("database schema is newer than this Scout release")
            if version < 1:
                connection.executescript(
                    """
                    CREATE TABLE interests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        weight REAL NOT NULL CHECK(weight >= 0 AND weight <= 5),
                        topics_json TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE items (
                        id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL UNIQUE,
                        published_at TEXT,
                        summary TEXT NOT NULL,
                        topics_json TEXT NOT NULL,
                        story_key TEXT NOT NULL,
                        first_collected_at TEXT NOT NULL,
                        last_collected_at TEXT NOT NULL,
                        UNIQUE(source_id, external_id)
                    );
                    CREATE INDEX items_story_key_idx ON items(story_key);
                    CREATE TABLE reactions (
                        item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
                        reaction TEXT NOT NULL CHECK(reaction IN ('DISLIKE', 'LOVE', 'STAR')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'PARTIAL', 'FAILED')),
                        model_status TEXT NOT NULL,
                        source_statuses_json TEXT NOT NULL
                    );
                    CREATE TABLE run_items (
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        item_id TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
                        position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 3),
                        score REAL NOT NULL,
                        reason TEXT NOT NULL,
                        is_serendipity INTEGER NOT NULL CHECK(is_serendipity IN (0, 1)),
                        PRIMARY KEY(run_id, item_id),
                        UNIQUE(run_id, position)
                    );
                    CREATE TABLE source_cache (
                        source_id TEXT PRIMARY KEY,
                        last_attempt_at TEXT NOT NULL,
                        last_success_at TEXT,
                        next_refresh_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error TEXT,
                        item_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE discovery_lock (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        owner TEXT NOT NULL,
                        acquired_at TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        username TEXT,
                        csrf_token TEXT NOT NULL,
                        authenticated INTEGER NOT NULL CHECK(authenticated IN (0, 1)),
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL
                    );
                    CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
                    CREATE TABLE login_attempts (
                        client_key TEXT NOT NULL,
                        attempted_at TEXT NOT NULL
                    );
                    CREATE INDEX login_attempts_client_idx
                        ON login_attempts(client_key, attempted_at);
                    PRAGMA user_version=1;
                    """
                )
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for name, weight, topics in _DEFAULT_INTERESTS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO interests(name, weight, topics_json, enabled, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (name, weight, _json(topics), now),
                )
            connection.commit()
        os.chmod(self.path, 0o600)

    def schema_version(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def journal_mode(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()

    @staticmethod
    def _decoded_item(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["topics"] = json.loads(result.pop("topics_json"))
        return result

    def list_interests(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, weight, topics_json, enabled, updated_at "
                "FROM interests ORDER BY weight DESC, id"
            ).fetchall()
        return [
            {
                **dict(row),
                "topics": json.loads(row["topics_json"]),
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]

    def replace_interests(
        self, interests: Sequence[Mapping[str, Any]], *, updated_at: str
    ) -> None:
        if not interests or len(interests) > 12:
            raise ValueError("between one and twelve interests are required")
        normalized: list[tuple[str, float, tuple[str, ...], int]] = []
        names: set[str] = set()
        for raw in interests:
            name = str(raw.get("name", "")).strip()
            topics = tuple(
                sorted(
                    {
                        str(topic).strip().casefold()
                        for topic in raw.get("topics", [])
                        if str(topic).strip()
                    }
                )
            )
            weight = float(raw.get("weight", 0))
            enabled = 1 if raw.get("enabled", True) else 0
            if not name or len(name) > 80 or name.casefold() in names:
                raise ValueError("interest names must be unique and contain 1..80 characters")
            if not topics or len(topics) > 12 or any(len(topic) > 40 for topic in topics):
                raise ValueError("each interest needs one to twelve bounded topics")
            if not 0 <= weight <= 5:
                raise ValueError("interest weight must be between zero and five")
            names.add(name.casefold())
            normalized.append((name, weight, topics, enabled))
        _utc(updated_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM interests")
            connection.executemany(
                "INSERT INTO interests(name, weight, topics_json, enabled, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(name, weight, _json(topics), enabled, updated_at) for name, weight, topics, enabled in normalized],
            )
            connection.commit()

    def upsert_items(self, items: Sequence[CollectedItem]) -> int:
        inserted = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in items:
                existing = connection.execute(
                    """
                    SELECT items.id, items.url,
                        EXISTS(
                            SELECT 1 FROM run_items WHERE run_items.item_id = items.id
                        ) AS facts_locked
                    FROM items
                    WHERE source_id=? AND external_id=?
                    """,
                    (item.source_id, item.external_id),
                ).fetchone()
                if existing is not None:
                    if existing["facts_locked"]:
                        connection.execute(
                            "UPDATE items SET last_collected_at=? WHERE id=?",
                            (item.collected_at, existing["id"]),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE items SET title=?, published_at=?, summary=?, topics_json=?,
                                story_key=?, last_collected_at=? WHERE id=?
                            """,
                            (
                                item.title,
                                item.published_at,
                                item.summary,
                                _json(item.topics),
                                item.story_key,
                                item.collected_at,
                                existing["id"],
                            ),
                        )
                    continue
                duplicate_url = connection.execute(
                    "SELECT id FROM items WHERE url=?", (item.url,)
                ).fetchone()
                if duplicate_url is not None:
                    continue
                connection.execute(
                    """
                    INSERT INTO items(
                        id, source_id, external_id, title, url, published_at, summary,
                        topics_json, story_key, first_collected_at, last_collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _item_id(item.source_id, item.external_id),
                        item.source_id,
                        item.external_id,
                        item.title,
                        item.url,
                        item.published_at,
                        item.summary,
                        _json(item.topics),
                        item.story_key,
                        item.collected_at,
                        item.collected_at,
                    ),
                )
                inserted += 1
            connection.commit()
        return inserted

    def list_candidates(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.*, reactions.reaction
                FROM items LEFT JOIN reactions ON reactions.item_id=items.id
                ORDER BY COALESCE(items.published_at, '') DESC, items.id
                """
            ).fetchall()
        return [self._decoded_item(row) for row in rows]

    def set_reaction(self, item_id: str, reaction: str | None, updated_at: str) -> None:
        _utc(updated_at)
        if reaction is not None and reaction not in REACTIONS:
            raise ValueError("reaction must be DISLIKE, LOVE, STAR or neutral")
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is None:
                raise KeyError("unknown item")
            if reaction is None:
                connection.execute("DELETE FROM reactions WHERE item_id=?", (item_id,))
            else:
                connection.execute(
                    """
                    INSERT INTO reactions(item_id, reaction, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        reaction=excluded.reaction, updated_at=excluded.updated_at
                    """,
                    (item_id, reaction, updated_at, updated_at),
                )
            connection.commit()

    def get_reaction(self, item_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT reaction FROM reactions WHERE item_id=?", (item_id,)
            ).fetchone()
        return None if row is None else str(row["reaction"])

    def feedback_by_topic(self) -> dict[str, float]:
        effects: dict[str, float] = {}
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.topics_json, reactions.reaction
                FROM reactions JOIN items ON items.id=reactions.item_id
                """
            ).fetchall()
        for row in rows:
            weight = _REACTION_WEIGHTS[str(row["reaction"])]
            for topic in json.loads(row["topics_json"]):
                effects[str(topic)] = effects.get(str(topic), 0.0) + weight
        return effects

    def list_favorites(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.*, reactions.reaction
                FROM reactions JOIN items ON items.id=reactions.item_id
                WHERE reactions.reaction='STAR'
                ORDER BY reactions.updated_at DESC
                """
            ).fetchall()
        return [self._decoded_item(row) for row in rows]

    def seen_item_ids(self) -> set[str]:
        with self.connect() as connection:
            return {
                str(row[0])
                for row in connection.execute("SELECT DISTINCT item_id FROM run_items")
            }

    def seen_story_keys(self) -> set[str]:
        with self.connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT items.story_key
                    FROM run_items JOIN items ON items.id=run_items.item_id
                    """
                )
            }

    def recent_source_counts(self, limit: int = 20) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.source_id
                FROM run_items
                JOIN items ON items.id=run_items.item_id
                JOIN runs ON runs.id=run_items.run_id
                ORDER BY runs.completed_at DESC, run_items.position
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            source = str(row["source_id"])
            counts[source] = counts.get(source, 0) + 1
        return counts

    def acquire_discovery_lock(
        self, owner: str, acquired_at: str, *, stale_after_seconds: int = 180
    ) -> bool:
        moment = _utc(acquired_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, acquired_at FROM discovery_lock WHERE singleton=1"
            ).fetchone()
            if row is not None:
                if row["owner"] == owner:
                    connection.commit()
                    return True
                if moment - _utc(str(row["acquired_at"])) <= timedelta(seconds=stale_after_seconds):
                    connection.commit()
                    return False
                connection.execute("DELETE FROM discovery_lock WHERE singleton=1")
            connection.execute(
                "INSERT INTO discovery_lock(singleton, owner, acquired_at) VALUES (1, ?, ?)",
                (owner, acquired_at),
            )
            connection.commit()
            return True

    def release_discovery_lock(self, owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM discovery_lock WHERE singleton=1 AND owner=?", (owner,)
            )
            connection.commit()

    def save_run(
        self,
        run_id: str,
        *,
        started_at: str,
        completed_at: str,
        status: str,
        model_status: str,
        source_statuses: Mapping[str, Any],
        ranked_items: Sequence[RankedItem | Mapping[str, Any]],
    ) -> None:
        if status not in {"SUCCESS", "PARTIAL", "FAILED"} or len(ranked_items) > 3:
            raise ValueError("invalid run status or card count")
        _utc(started_at)
        _utc(completed_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs(id, started_at, completed_at, status, model_status, source_statuses_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, completed_at, status, model_status, _json(source_statuses)),
            )
            for position, raw in enumerate(ranked_items, start=1):
                if isinstance(raw, RankedItem):
                    item = raw.item
                    score = raw.score
                    reason = raw.reason
                    is_serendipity = raw.is_serendipity
                else:
                    item = raw["item"]
                    score = raw["score"]
                    reason = raw["reason"]
                    is_serendipity = raw.get("is_serendipity", False)
                connection.execute(
                    """
                    INSERT INTO run_items(run_id, item_id, position, score, reason, is_serendipity)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        item["id"],
                        position,
                        float(score),
                        str(reason),
                        int(bool(is_serendipity)),
                    ),
                )
            connection.commit()

    def list_history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            runs = connection.execute(
                "SELECT * FROM runs ORDER BY completed_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            result: list[dict[str, Any]] = []
            for run_row in runs:
                item_rows = connection.execute(
                    """
                    SELECT items.*, reactions.reaction, run_items.position, run_items.score,
                           run_items.reason, run_items.is_serendipity
                    FROM run_items JOIN items ON items.id=run_items.item_id
                    LEFT JOIN reactions ON reactions.item_id=items.id
                    WHERE run_items.run_id=? ORDER BY run_items.position
                    """,
                    (run_row["id"],),
                ).fetchall()
                run = dict(run_row)
                run["source_statuses"] = json.loads(run.pop("source_statuses_json"))
                run["items"] = [self._decoded_item(row) for row in item_rows]
                result.append(run)
        return result

    def update_source_cache(
        self,
        source_id: str,
        *,
        last_attempt_at: str,
        last_success_at: str | None,
        next_refresh_at: str,
        status: str,
        error: str | None,
        item_count: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_cache(
                    source_id, last_attempt_at, last_success_at, next_refresh_at,
                    status, error, item_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=COALESCE(excluded.last_success_at, source_cache.last_success_at),
                    next_refresh_at=excluded.next_refresh_at,
                    status=excluded.status,
                    error=excluded.error,
                    item_count=excluded.item_count
                """,
                (
                    source_id,
                    last_attempt_at,
                    last_success_at,
                    next_refresh_at,
                    status,
                    error,
                    item_count,
                ),
            )
            connection.commit()

    def source_cache(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM source_cache ORDER BY source_id").fetchall()
        return {str(row["source_id"]): dict(row) for row in rows}


__all__ = ["REACTIONS", "SCHEMA_VERSION", "Database"]
