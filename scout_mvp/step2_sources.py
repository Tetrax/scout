"""Step 2A official Hermes release source boundary and collector."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, TypeAlias
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import validate_document
from .ids import observation_id

OFFICIAL_RELEASE_API_URL = (
    "https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=5"
)
OFFICIAL_RELEASE_HTML_PREFIX = (
    "https://github.com/NousResearch/hermes-agent/releases/"
)
MAX_RELEASE_TAG_CHARS = 200
MAX_RELEASE_NAME_CHARS = 300
_SAFE_RELEASE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

HERMES_RELEASES_SOURCE = {
    "id": "hermes_releases",
    "name": "Official Hermes releases",
    "required": True,
    "enabled": True,
    "role": "PRIMARY",
    "max_items_per_run": 5,
    "url": OFFICIAL_RELEASE_API_URL,
    "access_mode": "READ_ONLY",
    "scope": "official NousResearch/hermes-agent releases only",
}

# Public spelling used by the Step 2A source catalog.
hermes_releases = HERMES_RELEASES_SOURCE

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30
HTTP_USER_AGENT = "Scout-MVP-Step2A/1.0"
HTTP_ACCEPT = "application/vnd.github+json"
BODY_TRUST_BOUNDARY = "UNTRUSTED_DATA_ONLY"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request: Request, *, timeout: int):
    """Open one request without allowing urllib to contact redirect targets."""
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


FetchResult: TypeAlias = tuple[bytes, int]
Fetcher: TypeAlias = Callable[[str], FetchResult]


class Step2CollectionError(ValueError):
    """Raised when the bounded Hermes release collector cannot accept a response."""


# Short public alias for callers that do not need the Step 2-specific name.
CollectionError = Step2CollectionError


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def validate_official_api_url(value: Any) -> str:
    """Require the one exact GitHub releases API endpoint."""
    if not isinstance(value, str) or value != OFFICIAL_RELEASE_API_URL:
        raise Step2CollectionError("URL is not the exact official Hermes release API endpoint")
    return value


def validate_official_release_html_url(value: Any, *, expected_tag: str | None = None) -> str:
    """Require one encoded-safe segment on the canonical GitHub release route."""
    if not isinstance(value, str):
        raise Step2CollectionError("official Hermes release URL must be text")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise Step2CollectionError("official Hermes release URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise Step2CollectionError("official Hermes release URL has an unsafe authority or suffix")

    raw_segments = parsed.path.split("/")
    if len(raw_segments) != 6 or raw_segments[:5] != [
        "",
        "NousResearch",
        "hermes-agent",
        "releases",
        "tag",
    ]:
        raise Step2CollectionError("official Hermes release URL must use the exact tag route")
    if any(unquote(segment) in {".", ".."} for segment in raw_segments):
        raise Step2CollectionError("official Hermes release URL contains dot traversal")

    tag_segment = raw_segments[5]
    if not tag_segment or "%" in tag_segment:
        raise Step2CollectionError("official Hermes release URL tag segment must be canonical text")
    decoded_tag = unquote(tag_segment)
    if (
        not decoded_tag
        or decoded_tag in {".", ".."}
        or "/" in decoded_tag
        or "\\" in decoded_tag
        or not _SAFE_RELEASE_TAG_RE.fullmatch(decoded_tag)
    ):
        raise Step2CollectionError("official Hermes release URL tag segment escapes its route")
    if expected_tag is not None and decoded_tag != expected_tag:
        raise Step2CollectionError("official Hermes release URL tag does not match tag_name")
    return value


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise Step2CollectionError(f"release {field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Step2CollectionError(f"release {field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Step2CollectionError(f"release {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _read_response_limited(response: Any) -> bytes:
    """Read at most the bounded response size and reject larger bodies."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, MAX_RESPONSE_BYTES - total + 1))
        if not isinstance(chunk, bytes):
            raise Step2CollectionError("Hermes release response did not return bytes")
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise Step2CollectionError("Hermes release response exceeds 2 MiB cap")
        chunks.append(chunk)
    return b"".join(chunks)


def urllib_fetch(url: str) -> FetchResult:
    """Perform exactly one bounded HTTPS GET with explicit GitHub headers."""
    validate_official_api_url(url)

    request = Request(
        url,
        headers={"User-Agent": HTTP_USER_AGENT, "Accept": HTTP_ACCEPT},
        method="GET",
    )
    try:
        response = urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
    except HTTPError as exc:
        # Preserve the single response status so the collector can fail closed
        # without retrying or treating an error payload as release data.
        _validate_final_api_url(exc, url)
        try:
            body = _read_response_limited(exc)
        except Exception as read_exc:
            raise Step2CollectionError("could not read GitHub error response") from read_exc
        return body, int(exc.code)

    try:
        _validate_final_api_url(response, url)
        status = response.getcode()
        return _read_response_limited(response), int(status)
    except Step2CollectionError:
        raise
    except Exception as exc:
        raise Step2CollectionError("could not read GitHub release response") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _validate_final_api_url(response: Any, requested_url: str) -> None:
    final_url_getter = getattr(response, "geturl", None)
    final_url = requested_url if not callable(final_url_getter) else final_url_getter()
    validate_official_api_url(final_url)


def _normalized_text(
    value: Any,
    *,
    field: str,
    fallback: str | None = None,
    max_chars: int | None = None,
) -> str:
    """Validate a text field without rewriting collected untrusted data."""
    if value is None and fallback is not None:
        return fallback
    if not isinstance(value, str):
        raise Step2CollectionError(f"release {field} must be a string or null")
    if not value and fallback is not None:
        return fallback
    if max_chars is not None and len(value) > max_chars:
        raise Step2CollectionError(f"release {field} exceeds {max_chars} characters")
    return value


def _release_item(item: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        raise Step2CollectionError(f"release item {index} is not an object")

    draft = item.get("draft", False)
    if not isinstance(draft, bool):
        raise Step2CollectionError(f"release item {index} has malformed draft flag")
    if draft:
        return None

    release_id = item.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        raise Step2CollectionError(f"release item {index} has malformed numeric id")

    tag = _normalized_text(
        item.get("tag_name"), field="tag_name", max_chars=MAX_RELEASE_TAG_CHARS
    )
    if not tag:
        raise Step2CollectionError(f"release item {index} has an empty tag_name")
    if not _SAFE_RELEASE_TAG_RE.fullmatch(tag):
        raise Step2CollectionError(f"release item {index} has a non-canonical tag_name")

    html_url = item.get("html_url")
    try:
        validate_official_release_html_url(html_url, expected_tag=tag)
    except Step2CollectionError as exc:
        raise Step2CollectionError(
            f"release item {index} is outside the exact official Hermes release URL"
        ) from exc

    published_at = item.get("published_at")
    _parse_utc_timestamp(published_at, field="published_at")

    prerelease = item.get("prerelease", False)
    if not isinstance(prerelease, bool):
        raise Step2CollectionError(f"release item {index} has malformed prerelease flag")

    name = _normalized_text(
        item.get("name"),
        field="name",
        fallback=tag,
        max_chars=MAX_RELEASE_NAME_CHARS,
    )
    body = _normalized_text(item.get("body"), field="body", fallback="")
    return {
        "id": release_id,
        "tag": tag,
        "name": name or tag,
        "body": body,
        "html_url": html_url,
        "published_at": published_at,
        "prerelease": prerelease,
    }


def collect_hermes_releases(
    fetcher: Fetcher = urllib_fetch, observed_at: str | None = None
) -> list[dict[str, Any]]:
    """Collect up to five official, non-draft Hermes releases in API order."""
    _parse_utc_timestamp(observed_at, field="observed_at")
    validate_document("SourceV1", HERMES_RELEASES_SOURCE)

    validate_official_api_url(HERMES_RELEASES_SOURCE["url"])
    try:
        result = fetcher(HERMES_RELEASES_SOURCE["url"])
    except Step2CollectionError:
        raise
    except Exception as exc:
        raise Step2CollectionError("Hermes release fetch failed") from exc

    if not isinstance(result, tuple) or len(result) not in {2, 3}:
        raise Step2CollectionError("Hermes fetcher must return (bytes, status[, final_url])")
    raw_payload, response_status = result
    final_url = result[2] if len(result) == 3 else HERMES_RELEASES_SOURCE["url"]
    try:
        validate_official_api_url(final_url)
    except Step2CollectionError as exc:
        raise Step2CollectionError("Hermes release response redirected off the exact API endpoint") from exc
    if not isinstance(raw_payload, bytes):
        raise Step2CollectionError("Hermes release response must be bytes")
    if isinstance(response_status, bool) or not isinstance(response_status, int):
        raise Step2CollectionError("Hermes release response status must be an integer")
    if len(raw_payload) > MAX_RESPONSE_BYTES:
        raise Step2CollectionError("Hermes release response exceeds 2 MiB cap")
    if response_status != 200:
        raise Step2CollectionError(f"Hermes release endpoint returned HTTP {response_status}")

    try:
        parsed = json.loads(
            raw_payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Step2CollectionError("Hermes release response is not valid JSON") from exc
    if not isinstance(parsed, list):
        raise Step2CollectionError("Hermes release response must be a top-level JSON array")

    content_sha256 = hashlib.sha256(raw_payload).hexdigest()
    observations: list[dict[str, Any]] = []
    # Validate numeric identity uniqueness across the complete bounded HTTP
    # response before applying the per-run processing cap.
    all_release_ids: set[int] = set()
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        release_id = item.get("id")
        if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
            continue
        if release_id in all_release_ids:
            raise Step2CollectionError("duplicate numeric release id in Hermes response")
        all_release_ids.add(release_id)

    seen_release_ids: set[int] = set()
    for index, item in enumerate(parsed[: HERMES_RELEASES_SOURCE["max_items_per_run"]]):
        release = _release_item(item, index)
        if release is None:
            continue
        if release["id"] in seen_release_ids:
            raise Step2CollectionError("duplicate numeric release id in Hermes response")
        seen_release_ids.add(release["id"])
        external_id = str(release["id"])
        observation = {
            "id": observation_id(HERMES_RELEASES_SOURCE["id"], external_id),
            "source_id": HERMES_RELEASES_SOURCE["id"],
            "external_id": external_id,
            "kind": "RELEASE",
            "observed_at": observed_at,
            "retrieved_at": observed_at,
            "published_at": release["published_at"],
            "title": release["name"],
            "text": release["body"],
            "canonical_url": release["html_url"],
            "source_url": HERMES_RELEASES_SOURCE["url"],
            "provenance": {
                "source_url": HERMES_RELEASES_SOURCE["url"],
                "retrieved_at": observed_at,
                "response_status": response_status,
                "content_sha256": content_sha256,
                "collector": "hermes-releases-http-v1",
                "read_only": True,
            },
            "metadata": {
                "release_tag": release["tag"],
                "release_name": release["name"],
                "prerelease": release["prerelease"],
                "body_trust": BODY_TRUST_BOUNDARY,
            },
        }
        validate_document("ObservationV1", observation)
        observations.append(observation)

    return observations


__all__ = [
    "BODY_TRUST_BOUNDARY",
    "CollectionError",
    "Fetcher",
    "HERMES_RELEASES_SOURCE",
    "HTTP_ACCEPT",
    "HTTP_TIMEOUT_SECONDS",
    "HTTP_USER_AGENT",
    "MAX_RELEASE_NAME_CHARS",
    "MAX_RELEASE_TAG_CHARS",
    "MAX_RESPONSE_BYTES",
    "OFFICIAL_RELEASE_API_URL",
    "OFFICIAL_RELEASE_HTML_PREFIX",
    "Step2CollectionError",
    "collect_hermes_releases",
    "hermes_releases",
    "validate_official_api_url",
    "validate_official_release_html_url",
    "urllib_fetch",
]
