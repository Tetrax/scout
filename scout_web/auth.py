from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .database import Database

ANONYMOUS_TTL = timedelta(minutes=20)
AUTHENTICATED_TTL = timedelta(hours=12)
LOGIN_WINDOW = timedelta(minutes=10)
MAX_LOGIN_FAILURES = 5


@dataclass(frozen=True, slots=True)
class ServerSession:
    token_hash: str
    username: str | None
    csrf_token: str
    authenticated: bool
    created_at: str
    expires_at: str
    last_seen_at: str


def _text(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("session time must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored session time lacks timezone")
    return parsed.astimezone(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionManager:
    def __init__(self, database: Database, secret_key: str) -> None:
        self.database = database
        self.secret_key = secret_key.encode("utf-8")

    def create(
        self, *, now: datetime, authenticated: bool, username: str | None = None
    ) -> tuple[str, ServerSession]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        created_at = _text(now)
        expires_at = _text(now + (AUTHENTICATED_TTL if authenticated else ANONYMOUS_TTL))
        token_hash = _token_hash(token)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    token_hash, username, csrf_token, authenticated,
                    created_at, expires_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    username,
                    csrf_token,
                    int(authenticated),
                    created_at,
                    expires_at,
                    created_at,
                ),
            )
            connection.commit()
        return token, ServerSession(
            token_hash=token_hash,
            username=username,
            csrf_token=csrf_token,
            authenticated=authenticated,
            created_at=created_at,
            expires_at=expires_at,
            last_seen_at=created_at,
        )

    def load(self, token: str | None, *, now: datetime) -> ServerSession | None:
        if not token or len(token) > 256:
            return None
        now_text = _text(now)
        token_hash = _token_hash(token)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_text,))
            row = connection.execute(
                "SELECT * FROM sessions WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            if _parse(str(row["expires_at"])) <= now.astimezone(timezone.utc):
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                connection.commit()
                return None
            connection.execute(
                "UPDATE sessions SET last_seen_at=? WHERE token_hash=?",
                (now_text, token_hash),
            )
            connection.commit()
        return ServerSession(
            token_hash=token_hash,
            username=row["username"],
            csrf_token=str(row["csrf_token"]),
            authenticated=bool(row["authenticated"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            last_seen_at=now_text,
        )

    def revoke(self, token: str | None) -> None:
        if not token or len(token) > 256:
            return
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),)
            )
            connection.commit()

    def client_key(self, client_address: str | None) -> str:
        value = client_address or "unknown"
        return hmac.new(self.secret_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def is_login_limited(self, client_key: str, *, now: datetime) -> bool:
        cutoff = _text(now - LOGIN_WINDOW)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE client_key=? AND attempted_at>=?",
                    (client_key, cutoff),
                ).fetchone()[0]
            )
            connection.commit()
        return count >= MAX_LOGIN_FAILURES

    def record_login_failure(self, client_key: str, *, now: datetime) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO login_attempts(client_key, attempted_at) VALUES (?, ?)",
                (client_key, _text(now)),
            )
            connection.commit()

    def clear_login_failures(self, client_key: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE client_key=?", (client_key,))
            connection.commit()

    def counts(self) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT authenticated, COUNT(*) AS count FROM sessions GROUP BY authenticated"
            ).fetchall()
        counts = {"anonymous": 0, "authenticated": 0}
        for row in rows:
            key = "authenticated" if row["authenticated"] else "anonymous"
            counts[key] = int(row["count"])
        return counts


__all__ = [
    "AUTHENTICATED_TTL",
    "MAX_LOGIN_FAILURES",
    "ServerSession",
    "SessionManager",
]
