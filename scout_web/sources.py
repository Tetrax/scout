from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from defusedxml.ElementTree import ParseError, fromstring

HTTP_USER_AGENT = "Scout/1.0 (+https://scout.valdev.me)"
_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_FORTINET_ADVISORY_RE = re.compile(r"^/psirt/FG-IR-\d{2,4}-\d{2,6}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_RELEASE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")


class SourceError(ValueError):
    """A fixed upstream source returned data Scout cannot safely accept."""


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    label: str
    url: str
    documentation_url: str
    max_items: int
    max_response_bytes: int
    timeout_seconds: int
    kind: str


@dataclass(frozen=True, slots=True)
class CollectedItem:
    source_id: str
    external_id: str
    title: str
    url: str
    published_at: str | None
    summary: str
    topics: tuple[str, ...]
    story_key: str
    collected_at: str


ENABLED_SOURCES: dict[str, SourceDefinition] = {
    "fortinet_psirt": SourceDefinition(
        id="fortinet_psirt",
        label="Fortinet PSIRT",
        url="https://filestore.fortinet.com/fortiguard/rss/ir.xml",
        documentation_url="https://www.fortiguard.com/rss-feeds",
        max_items=8,
        max_response_bytes=1024 * 1024,
        timeout_seconds=12,
        kind="fortinet_rss",
    ),
    "cisa_kev_fortinet": SourceDefinition(
        id="cisa_kev_fortinet",
        label="CISA KEV · Fortinet",
        url="https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        documentation_url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        max_items=8,
        max_response_bytes=4 * 1024 * 1024,
        timeout_seconds=12,
        kind="cisa_kev",
    ),
    "github_hermes_releases": SourceDefinition(
        id="github_hermes_releases",
        label="Hermes Agent · versions officielles",
        url="https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=5",
        documentation_url="https://docs.github.com/en/rest/releases/releases#list-releases",
        max_items=5,
        max_response_bytes=2 * 1024 * 1024,
        timeout_seconds=12,
        kind="github_releases",
    ),
    "github_openai_codex_releases": SourceDefinition(
        id="github_openai_codex_releases",
        label="OpenAI Codex · versions officielles",
        url="https://api.github.com/repos/openai/codex/releases?per_page=5",
        documentation_url="https://docs.github.com/en/rest/releases/releases#list-releases",
        max_items=5,
        max_response_bytes=2 * 1024 * 1024,
        timeout_seconds=12,
        kind="github_releases",
    ),
}

X_BOOKMARKS_DIAGNOSTIC = (
    "Source X Bookmarks désactivée : OAuth unauthorized_client observé le 2026-08-29. "
    "Aucun accusé de lecture ni checkpoint X n'est modifié. Le flux public des versions "
    "OpenAI Codex est le remplacement borné et gratuit."
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: Any, *, limit: int, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SourceError(f"{field} must be text")
    if _CONTROL_RE.search(value):
        raise SourceError(f"{field} contains control characters")
    parser = _TextExtractor()
    try:
        parser.feed(value[: 64 * 1024])
        parser.close()
    except Exception as exc:
        raise SourceError(f"{field} contains malformed markup") from exc
    text = html.unescape(" ".join(parser.parts))
    text = re.sub(r"[`#*_>~]+", " ", text)
    text = " ".join(text.split())
    if not text and not allow_empty:
        raise SourceError(f"{field} is empty")
    return text[:limit].rstrip()


def _now_text(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SourceError("collection time must be timezone-aware")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_timestamp(value: Any, *, field: str, allow_date: bool = False) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SourceError(f"{field} must be text or absent")
    try:
        if allow_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
            return value
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"{field} is not a valid source date") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceError(f"{field} lacks a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rss_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise SourceError("RSS pubDate is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceError("RSS pubDate lacks a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_payload(payload: bytes, *, max_bytes: int) -> Any:
    if not isinstance(payload, bytes) or len(payload) > max_bytes:
        raise SourceError("source response exceeds its byte limit")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SourceError("source response is not strict UTF-8 JSON") from exc


def story_key_for(title: str, summary: str) -> str:
    cves = sorted({match.upper() for match in _CVE_RE.findall(f"{title} {summary}")})
    if cves:
        return "cve:" + ",".join(cves)
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))
    return "title:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _topics(source_id: str, title: str, summary: str) -> tuple[str, ...]:
    base = {
        "fortinet_psirt": {"fortinet", "cybersecurity", "networking"},
        "cisa_kev_fortinet": {
            "fortinet",
            "cybersecurity",
            "networking",
            "active_exploitation",
        },
        "github_hermes_releases": {"hermes", "ai_agents", "automation"},
        "github_openai_codex_releases": {
            "codex",
            "ai_agents",
            "developer_tools",
            "automation",
        },
    }[source_id]
    text = f"{title} {summary}".casefold()
    keywords = {
        "linux": ("linux", "kernel"),
        "containers": ("docker", "container", "kubernetes"),
        "devops": ("ci/cd", "workflow", "deployment", "devops"),
        "cloud": ("cloud", "aws", "azure", "gcp"),
        "gaming": ("gaming", "game", "steam"),
    }
    for topic, terms in keywords.items():
        if any(term in text for term in terms):
            base.add(topic)
    return tuple(sorted(base))


def _github_release_url(value: Any, source_id: str, expected_tag: str) -> str:
    if not isinstance(value, str):
        raise SourceError("GitHub release URL must be text")
    parsed = urlsplit(value)
    expected_prefix = {
        "github_hermes_releases": "/NousResearch/hermes-agent/releases/tag/",
        "github_openai_codex_releases": "/openai/codex/releases/tag/",
    }.get(source_id)
    if (
        expected_prefix is None
        or parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != f"{expected_prefix}{expected_tag}"
        or parsed.query
        or parsed.fragment
    ):
        raise SourceError("GitHub release URL is outside the fixed official repository")
    return value


def parse_github_releases(
    payload: bytes, source_id: str, now: datetime
) -> list[CollectedItem]:
    source = ENABLED_SOURCES.get(source_id)
    if source is None or source.kind != "github_releases":
        raise SourceError("unknown GitHub release source")
    parsed = _json_payload(payload, max_bytes=source.max_response_bytes)
    if not isinstance(parsed, list):
        raise SourceError("GitHub release response must be an array")
    collected_at = _now_text(now)
    items: list[CollectedItem] = []
    identities: set[str] = set()
    for index, raw in enumerate(parsed):
        if len(items) >= source.max_items:
            break
        if not isinstance(raw, dict):
            raise SourceError(f"GitHub release {index} is not an object")
        draft = raw.get("draft", False)
        if not isinstance(draft, bool):
            raise SourceError(f"GitHub release {index} has malformed draft state")
        if draft:
            continue
        release_id = raw.get("id")
        if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
            raise SourceError(f"GitHub release {index} has no numeric identity")
        external_id = str(release_id)
        if external_id in identities:
            raise SourceError("GitHub release response contains duplicate identities")
        identities.add(external_id)
        raw_tag = raw.get("tag_name")
        if not isinstance(raw_tag, str) or not _SAFE_RELEASE_TAG_RE.fullmatch(raw_tag):
            raise SourceError(f"GitHub release {index} has a non-canonical tag")
        tag = raw_tag
        title = _clean_text(raw.get("name") or tag, limit=300, field="release title")
        url = _github_release_url(raw.get("html_url"), source_id, tag)
        published_at = _iso_timestamp(raw.get("published_at"), field="published_at")
        body = _clean_text(raw.get("body") or "", limit=900, field="release body", allow_empty=True)
        summary = body or f"Publication officielle {tag}."
        items.append(
            CollectedItem(
                source_id=source_id,
                external_id=external_id,
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                topics=_topics(source_id, title, summary),
                story_key=story_key_for(title, summary),
                collected_at=collected_at,
            )
        )
    return items


def _child_text(element: Any, local_name: str) -> str | None:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return child.text
    return None


def parse_fortinet_rss(payload: bytes, now: datetime) -> list[CollectedItem]:
    source = ENABLED_SOURCES["fortinet_psirt"]
    if not isinstance(payload, bytes) or len(payload) > source.max_response_bytes:
        raise SourceError("Fortinet RSS exceeds its byte limit")
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SourceError("Fortinet RSS contains a forbidden XML declaration")
    try:
        root = fromstring(payload)
    except (ParseError, ValueError) as exc:
        raise SourceError("Fortinet RSS is malformed XML") from exc
    collected_at = _now_text(now)
    items: list[CollectedItem] = []
    identities: set[str] = set()
    for raw in root.iter():
        if raw.tag.rsplit("}", 1)[-1] != "item":
            continue
        if len(items) >= source.max_items:
            break
        url_value = (_child_text(raw, "link") or "").strip()
        parsed = urlsplit(url_value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "fortiguard.fortinet.com"
            or not _FORTINET_ADVISORY_RE.fullmatch(parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            raise SourceError("Fortinet advisory link is outside the official PSIRT route")
        external_id = (_child_text(raw, "guid") or parsed.path.rsplit("/", 1)[-1]).strip()
        if not external_id or external_id in identities:
            raise SourceError("Fortinet RSS contains a missing or duplicate identity")
        identities.add(external_id)
        title = _clean_text(_child_text(raw, "title") or "", limit=300, field="PSIRT title")
        summary = _clean_text(
            _child_text(raw, "description") or "Résumé non fourni par la source.",
            limit=900,
            field="PSIRT description",
        )
        published_at = _rss_timestamp(_child_text(raw, "pubDate"))
        items.append(
            CollectedItem(
                source_id=source.id,
                external_id=external_id,
                title=title,
                url=url_value,
                published_at=published_at,
                summary=summary,
                topics=_topics(source.id, title, summary),
                story_key=story_key_for(title, summary),
                collected_at=collected_at,
            )
        )
    return items


def parse_cisa_kev(payload: bytes, now: datetime) -> list[CollectedItem]:
    source = ENABLED_SOURCES["cisa_kev_fortinet"]
    parsed = _json_payload(payload, max_bytes=source.max_response_bytes)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("vulnerabilities"), list):
        raise SourceError("CISA KEV response lacks its vulnerabilities array")
    matches = []
    for raw in parsed["vulnerabilities"]:
        if not isinstance(raw, dict):
            continue
        scope = f"{raw.get('vendorProject', '')} {raw.get('product', '')}".casefold()
        if "fortinet" in scope:
            matches.append(raw)
    matches.sort(
        key=lambda raw: (str(raw.get("dateAdded", "")), str(raw.get("cveID", ""))),
        reverse=True,
    )
    collected_at = _now_text(now)
    items: list[CollectedItem] = []
    identities: set[str] = set()
    for index, raw in enumerate(matches[: source.max_items]):
        cve = raw.get("cveID")
        if not isinstance(cve, str) or not re.fullmatch(r"CVE-\d{4}-\d{4,7}", cve):
            raise SourceError(f"CISA Fortinet item {index} has a malformed CVE identity")
        if cve in identities:
            raise SourceError("CISA KEV response contains duplicate Fortinet CVEs")
        identities.add(cve)
        title = _clean_text(
            raw.get("vulnerabilityName") or cve,
            limit=300,
            field="CISA vulnerability title",
        )
        parts = [raw.get("shortDescription"), raw.get("requiredAction")]
        raw_summary = " ".join(part for part in parts if isinstance(part, str) and part.strip())
        summary = _clean_text(
            raw_summary or "Résumé non fourni par la source.",
            limit=900,
            field="CISA vulnerability summary",
        )
        published_at = _iso_timestamp(raw.get("dateAdded"), field="dateAdded", allow_date=True)
        url = (
            "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"
            f"?search_api_fulltext={cve}"
        )
        items.append(
            CollectedItem(
                source_id=source.id,
                external_id=cve,
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                topics=_topics(source.id, title, summary),
                story_key=story_key_for(title, summary),
                collected_at=collected_at,
            )
        )
    return items


def fetch_bounded(source: SourceDefinition) -> bytes:
    if ENABLED_SOURCES.get(source.id) != source:
        raise SourceError("fetch destination is not an enabled fixed source")
    accept = "application/json" if source.kind in {"github_releases", "cisa_kev"} else "application/rss+xml, application/xml;q=0.9"
    request = Request(
        source.url,
        headers={"Accept": accept, "User-Agent": HTTP_USER_AGENT},
        method="GET",
    )
    opener = build_opener(_NoRedirect())
    response = None
    try:
        response = opener.open(request, timeout=source.timeout_seconds)
        if response.geturl() != source.url or int(response.getcode()) != 200:
            raise SourceError("fixed source redirected or returned a non-200 response")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > source.max_response_bytes:
                    raise SourceError("source response exceeds its declared byte limit")
            except ValueError as exc:
                raise SourceError("source Content-Length is malformed") from exc
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, source.max_response_bytes - total + 1))
            if not isinstance(chunk, bytes):
                raise SourceError("source response did not return bytes")
            if not chunk:
                break
            total += len(chunk)
            if total > source.max_response_bytes:
                raise SourceError("source response exceeds its byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    except SourceError:
        raise
    except HTTPError as exc:
        raise SourceError(f"source returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError("source request failed or timed out") from exc
    finally:
        if response is not None:
            response.close()


Fetcher = Callable[[SourceDefinition], bytes]


def collect_source(
    source_id: str,
    *,
    now: datetime,
    fetcher: Fetcher = fetch_bounded,
) -> list[CollectedItem]:
    source = ENABLED_SOURCES.get(source_id)
    if source is None:
        raise SourceError("unknown or disabled source")
    payload = fetcher(source)
    if source.kind == "fortinet_rss":
        return parse_fortinet_rss(payload, now)
    if source.kind == "cisa_kev":
        return parse_cisa_kev(payload, now)
    if source.kind == "github_releases":
        return parse_github_releases(payload, source.id, now)
    raise SourceError("enabled source has no parser")


__all__ = [
    "ENABLED_SOURCES",
    "HTTP_USER_AGENT",
    "X_BOOKMARKS_DIAGNOSTIC",
    "CollectedItem",
    "SourceDefinition",
    "SourceError",
    "collect_source",
    "fetch_bounded",
    "parse_cisa_kev",
    "parse_fortinet_rss",
    "parse_github_releases",
    "story_key_for",
]
