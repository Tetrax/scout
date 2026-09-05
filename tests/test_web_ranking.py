from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scout_web.ranking import rank_candidates

NOW = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)


def item(
    identifier: str,
    *,
    source: str,
    published: str | None,
    topics: tuple[str, ...],
    story_key: str | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "source_id": source,
        "title": f"Title {identifier}",
        "url": f"https://example.invalid/{identifier}",
        "published_at": published,
        "summary": f"Summary {identifier}",
        "topics": list(topics),
        "story_key": story_key or f"title:{identifier}",
    }


INTERESTS = [
    {"name": "Fortinet & sécurité réseau", "weight": 5.0, "enabled": True, "topics": ["fortinet", "cybersecurity"]},
    {"name": "Hermes & agents IA", "weight": 4.0, "enabled": True, "topics": ["hermes", "ai_agents"]},
    {"name": "DevOps & automatisation", "weight": 3.0, "enabled": True, "topics": ["devops", "automation"]},
]


class RankingTests(unittest.TestCase):
    def test_feedback_changes_targeted_topic_ranking_and_silence_is_neutral(self) -> None:
        candidates = [
            item(
                "security",
                source="fortinet_psirt",
                published="2026-09-04T10:00:00Z",
                topics=("fortinet", "cybersecurity"),
            ),
            item(
                "ai",
                source="github_openai_codex_releases",
                published="2026-09-04T10:00:00Z",
                topics=("ai_agents", "automation"),
            ),
        ]

        neutral = rank_candidates(candidates, INTERESTS, {}, set(), {}, NOW)
        learned = rank_candidates(
            candidates,
            INTERESTS,
            {"fortinet": -5.0, "cybersecurity": -5.0, "ai_agents": 6.0},
            set(),
            {},
            NOW,
        )

        self.assertEqual(neutral[0].item["id"], "security")
        self.assertEqual(learned[0].item["id"], "ai")
        self.assertIn("retours", learned[0].reason.casefold())

    def test_seen_and_repeated_sources_are_penalized_and_results_are_diverse(self) -> None:
        candidates = [
            item(
                "fortinet-seen",
                source="fortinet_psirt",
                published="2026-09-05T01:00:00Z",
                topics=("fortinet",),
            ),
            item(
                "fortinet-new",
                source="fortinet_psirt",
                published="2026-09-04T01:00:00Z",
                topics=("fortinet",),
            ),
            item(
                "cisa",
                source="cisa_kev_fortinet",
                published="2026-09-03",
                topics=("fortinet", "cybersecurity"),
            ),
            item(
                "codex",
                source="github_openai_codex_releases",
                published="2026-09-04T12:00:00Z",
                topics=("ai_agents", "automation"),
            ),
        ]

        ranked = rank_candidates(
            candidates,
            INTERESTS,
            {},
            {"fortinet-seen"},
            {"fortinet_psirt": 4},
            NOW,
        )

        ids = [entry.item["id"] for entry in ranked]
        self.assertNotIn("fortinet-seen", ids)
        self.assertLessEqual(len(ranked), 3)
        self.assertEqual(len({entry.item["source_id"] for entry in ranked}), len(ranked))
        self.assertIn("codex", ids)

    def test_same_story_is_not_selected_twice(self) -> None:
        candidates = [
            item(
                "fortinet",
                source="fortinet_psirt",
                published="2026-09-04T10:00:00Z",
                topics=("fortinet",),
                story_key="cve:CVE-2026-12345",
            ),
            item(
                "cisa",
                source="cisa_kev_fortinet",
                published="2026-09-04",
                topics=("fortinet",),
                story_key="cve:CVE-2026-12345",
            ),
            item(
                "hermes",
                source="github_hermes_releases",
                published="2026-09-04T10:00:00Z",
                topics=("hermes",),
            ),
        ]

        ranked = rank_candidates(candidates, INTERESTS, {}, set(), {}, NOW)

        self.assertEqual(len([entry for entry in ranked if entry.item["story_key"] == "cve:CVE-2026-12345"]), 1)

    def test_story_seen_from_one_source_is_excluded_from_later_sources(self) -> None:
        candidates = [
            item(
                "same-news-other-source",
                source="fortinet_psirt",
                published="2026-09-04T10:00:00Z",
                topics=("fortinet",),
                story_key="cve:CVE-2026-12345",
            ),
            item(
                "new-news",
                source="github_hermes_releases",
                published="2026-09-04T10:00:00Z",
                topics=("hermes",),
            ),
        ]

        ranked = rank_candidates(
            candidates,
            INTERESTS,
            {},
            set(),
            {},
            NOW,
            seen_story_keys={"cve:CVE-2026-12345"},
        )

        self.assertEqual([entry.item["id"] for entry in ranked], ["new-news"])

    def test_stale_content_is_not_presented_as_recent_and_missing_date_stays_eligible(self) -> None:
        candidates = [
            item(
                "stale",
                source="github_hermes_releases",
                published="2025-01-01T00:00:00Z",
                topics=("hermes",),
            ),
            item(
                "undated",
                source="fortinet_psirt",
                published=None,
                topics=("fortinet",),
            ),
        ]

        ranked = rank_candidates(candidates, INTERESTS, {}, set(), {}, NOW)

        self.assertEqual([entry.item["id"] for entry in ranked], ["undated"])
        self.assertIn("date non fournie", ranked[0].reason.casefold())

    def test_zero_candidates_is_a_valid_result(self) -> None:
        self.assertEqual(rank_candidates([], INTERESTS, {}, set(), {}, NOW), [])


if __name__ == "__main__":
    unittest.main()
