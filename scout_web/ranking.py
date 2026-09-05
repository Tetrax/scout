from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

MAX_CARDS = 3
MAX_DISCOVERY_AGE = timedelta(days=180)
_SOURCE_BASE = {
    "cisa_kev_fortinet": 5.0,
    "fortinet_psirt": 4.5,
    "github_hermes_releases": 4.0,
    "github_openai_codex_releases": 3.8,
}


@dataclass(frozen=True, slots=True)
class RankedItem:
    item: dict[str, Any]
    score: float
    reason: str
    is_serendipity: bool = False


def _published(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _freshness_score(published: datetime | None, now: datetime) -> tuple[float, str, bool]:
    if published is None:
        return -1.0, "date non fournie par la source", True
    age = now - published
    if age < timedelta(days=-1) or age > MAX_DISCOVERY_AGE:
        return -1000.0, "hors fenêtre de découverte", False
    if age <= timedelta(days=7):
        return 4.0, "publié cette semaine", True
    if age <= timedelta(days=30):
        return 2.5, "publié ce mois-ci", True
    if age <= timedelta(days=90):
        return 0.5, "publication datée, encore dans la fenêtre", True
    return -2.0, "publication ancienne clairement datée", True


def _interest_topics(interests: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    enabled = [interest for interest in interests if interest.get("enabled", True)]
    all_topics: set[str] = set()
    for interest in enabled:
        all_topics.update(str(topic) for topic in interest.get("topics", []))
    if not enabled:
        return all_topics, set()
    top_weight = max(float(interest.get("weight", 0.0)) for interest in enabled)
    dominant: set[str] = set()
    for interest in enabled:
        if float(interest.get("weight", 0.0)) == top_weight:
            dominant.update(str(topic) for topic in interest.get("topics", []))
    return all_topics, dominant


def _score_one(
    item: Mapping[str, Any],
    interests: Sequence[Mapping[str, Any]],
    feedback_by_topic: Mapping[str, float],
    source_counts: Mapping[str, int],
    now: datetime,
) -> RankedItem | None:
    title = item.get("title")
    url = item.get("url")
    topics = {str(topic) for topic in item.get("topics", [])}
    source_id = str(item.get("source_id", ""))
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.startswith("https://"):
        return None
    freshness, freshness_reason, eligible = _freshness_score(_published(item.get("published_at")), now)
    if not eligible:
        return None

    matched_names: list[str] = []
    matched_weights: list[float] = []
    for interest in interests:
        if not interest.get("enabled", True):
            continue
        configured = {str(topic) for topic in interest.get("topics", [])}
        if configured.intersection(topics):
            weight = max(0.0, min(5.0, float(interest.get("weight", 0.0))))
            matched_weights.append(weight)
            matched_names.append(str(interest.get("name", "intérêt")))
    # One item can carry several related taxonomy tags.  Use the strongest
    # configured interest rather than multiplying its score through tag overlap.
    interest_score = (max(matched_weights) * 1.15) if matched_weights else 0.0

    feedback_score = sum(float(feedback_by_topic.get(topic, 0.0)) for topic in topics)
    feedback_score = max(-10.0, min(10.0, feedback_score))
    repetition_penalty = min(6.0, max(0, int(source_counts.get(source_id, 0))) * 0.75)
    score = _SOURCE_BASE.get(source_id, 1.0) + freshness + interest_score + feedback_score - repetition_penalty

    details: list[str] = []
    if matched_names:
        details.append("intérêt : " + ", ".join(matched_names[:2]))
    else:
        details.append("signal adjacent aux intérêts configurés")
    if feedback_score > 0:
        details.append("retours précédents favorables sur des thèmes proches")
    elif feedback_score < 0:
        details.append("retours précédents défavorables pris en compte")
    if repetition_penalty:
        details.append("répétition de source pénalisée")
    details.append(freshness_reason)
    reason = "Appréciation personnalisée (déduction) : " + " ; ".join(details) + "."
    return RankedItem(item=dict(item), score=round(score, 4), reason=reason)


def rank_candidates(
    candidates: Iterable[Mapping[str, Any]],
    interests: Sequence[Mapping[str, Any]],
    feedback_by_topic: Mapping[str, float],
    seen_item_ids: set[str],
    recent_source_counts: Mapping[str, int],
    now: datetime,
    *,
    seen_story_keys: set[str] | None = None,
) -> list[RankedItem]:
    """Rank fact-locked candidates and return zero to three diverse cards.

    Previously shown items receive the strongest possible already-seen penalty: they
    are kept in history but excluded from a new discovery. Silence contributes no
    feedback entry and is therefore neutral.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("ranking time must be timezone-aware")
    scored: list[RankedItem] = []
    previously_seen_stories = seen_story_keys or set()
    for candidate in candidates:
        identifier = candidate.get("id")
        story_key = candidate.get("story_key")
        if (
            not isinstance(identifier, str)
            or identifier in seen_item_ids
            or (
                isinstance(story_key, str)
                and story_key in previously_seen_stories
            )
        ):
            continue
        ranked = _score_one(
            candidate,
            interests,
            feedback_by_topic,
            recent_source_counts,
            now.astimezone(timezone.utc),
        )
        if ranked is not None:
            scored.append(ranked)
    scored.sort(
        key=lambda entry: (
            -entry.score,
            -(_published(entry.item.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            str(entry.item.get("id")),
        )
    )

    deduplicated: list[RankedItem] = []
    story_keys: set[str] = set()
    urls: set[str] = set()
    for entry in scored:
        story_key = str(entry.item.get("story_key", ""))
        url = str(entry.item.get("url", ""))
        if story_key in story_keys or url in urls:
            continue
        story_keys.add(story_key)
        urls.add(url)
        deduplicated.append(entry)

    selected: list[RankedItem] = []
    selected_ids: set[str] = set()
    selected_sources: set[str] = set()
    for entry in deduplicated:
        source_id = str(entry.item.get("source_id"))
        if source_id in selected_sources:
            continue
        selected.append(entry)
        selected_ids.add(str(entry.item["id"]))
        selected_sources.add(source_id)
        if len(selected) == MAX_CARDS:
            break
    if len(selected) < MAX_CARDS:
        for entry in deduplicated:
            identifier = str(entry.item["id"])
            if identifier in selected_ids:
                continue
            selected.append(entry)
            selected_ids.add(identifier)
            if len(selected) == MAX_CARDS:
                break

    _, dominant_topics = _interest_topics(interests)
    outsider_indexes = [
        index
        for index, entry in enumerate(selected)
        if not dominant_topics.intersection(str(topic) for topic in entry.item.get("topics", []))
    ]
    if len(selected) == MAX_CARDS and not outsider_indexes:
        outsider = next(
            (
                entry
                for entry in deduplicated
                if str(entry.item["id"]) not in selected_ids
                and not dominant_topics.intersection(
                    str(topic) for topic in entry.item.get("topics", [])
                )
                and entry.score >= 2.0
            ),
            None,
        )
        if outsider is not None:
            selected[-1] = outsider
            outsider_indexes = [len(selected) - 1]
    if len(selected) == MAX_CARDS and outsider_indexes:
        index = outsider_indexes[0]
        entry = selected[index]
        selected[index] = replace(
            entry,
            is_serendipity=True,
            reason=entry.reason
            + " Place de sérendipité : candidat de qualité hors intérêt dominant.",
        )
    return selected


__all__ = ["MAX_CARDS", "MAX_DISCOVERY_AGE", "RankedItem", "rank_candidates"]
