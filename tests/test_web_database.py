from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scout_web.database import Database
from scout_web.sources import CollectedItem

NOW = "2026-09-05T04:00:00Z"


def collected(identifier: str, *, source: str = "fortinet_psirt") -> CollectedItem:
    return CollectedItem(
        source_id=source,
        external_id=identifier,
        title=f"Title {identifier}",
        url=f"https://example.invalid/{identifier}",
        published_at="2026-09-04T00:00:00Z",
        summary=f"Summary {identifier}",
        topics=("fortinet", "cybersecurity"),
        story_key=f"title:{identifier}",
        collected_at=NOW,
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "scout.sqlite3"
        self.db = Database(self.path)
        self.db.migrate()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migration_seeds_editable_open_interests_and_uses_wal(self) -> None:
        interests = self.db.list_interests()

        self.assertGreaterEqual(len(interests), 5)
        self.assertEqual(interests[0]["name"], "Fortinet & sécurité réseau")
        self.assertEqual(interests[0]["weight"], 5.0)
        self.assertEqual(self.db.journal_mode(), "wal")
        self.assertEqual(self.db.schema_version(), 1)

    def test_items_persist_across_database_instances_and_url_is_deduplicated(self) -> None:
        self.assertEqual(self.db.upsert_items([collected("one")]), 1)
        duplicate_url = CollectedItem(
            source_id="cisa_kev_fortinet",
            external_id="other-id",
            title="Updated title",
            url="https://example.invalid/one",
            published_at="2026-09-05",
            summary="Updated source summary",
            topics=("fortinet",),
            story_key="title:updated",
            collected_at=NOW,
        )
        self.assertEqual(self.db.upsert_items([duplicate_url]), 0)

        reopened = Database(self.path)
        reopened.migrate()
        rows = reopened.list_candidates()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Title one")

    def test_reaction_can_be_corrected_and_star_defines_favorite(self) -> None:
        self.db.upsert_items([collected("one")])
        item_id = self.db.list_candidates()[0]["id"]

        self.db.set_reaction(item_id, "STAR", NOW)
        self.assertEqual([row["id"] for row in self.db.list_favorites()], [item_id])
        self.db.set_reaction(item_id, "LOVE", "2026-09-05T04:01:00Z")

        self.assertEqual(self.db.get_reaction(item_id), "LOVE")
        self.assertEqual(self.db.list_favorites(), [])
        self.db.set_reaction(item_id, None, "2026-09-05T04:02:00Z")
        self.assertIsNone(self.db.get_reaction(item_id))

    def test_reaction_topic_effects_are_derived_without_mutating_interests(self) -> None:
        self.db.upsert_items([collected("one")])
        item_id = self.db.list_candidates()[0]["id"]
        before = self.db.list_interests()

        self.db.set_reaction(item_id, "DISLIKE", NOW)
        effects = self.db.feedback_by_topic()

        self.assertLess(effects["fortinet"], 0)
        self.assertEqual(self.db.list_interests(), before)

    def test_discovery_lock_rejects_concurrent_owner_and_allows_release(self) -> None:
        self.assertTrue(self.db.acquire_discovery_lock("owner-a", NOW))
        self.assertFalse(self.db.acquire_discovery_lock("owner-b", NOW))
        self.db.release_discovery_lock("owner-a")
        self.assertTrue(
            self.db.acquire_discovery_lock(
                "owner-b", "2026-09-05T04:00:01Z"
            )
        )

    def test_run_history_keeps_zero_card_runs_and_selected_items(self) -> None:
        self.db.save_run(
            "run-empty",
            started_at=NOW,
            completed_at=NOW,
            status="SUCCESS",
            model_status="DETERMINISTIC_DEGRADED",
            source_statuses={"fortinet_psirt": {"status": "EMPTY"}},
            ranked_items=[],
        )
        self.db.upsert_items([collected("one")])
        candidate = self.db.list_candidates()[0]
        self.db.save_run(
            "run-one",
            started_at=NOW,
            completed_at="2026-09-05T04:01:00Z",
            status="PARTIAL",
            model_status="DETERMINISTIC_DEGRADED",
            source_statuses={"fortinet_psirt": {"status": "OK"}},
            ranked_items=[
                {
                    "item": candidate,
                    "score": 12.5,
                    "reason": "Appréciation personnalisée : intérêt direct.",
                    "is_serendipity": False,
                }
            ],
        )

        history = self.db.list_history()

        self.assertEqual([run["id"] for run in history], ["run-one", "run-empty"])
        self.assertEqual(len(history[0]["items"]), 1)
        self.assertEqual(history[1]["items"], [])
        self.assertIn(candidate["id"], self.db.seen_item_ids())
        self.assertEqual(self.db.seen_story_keys(), {"title:one"})

    def test_source_facts_are_immutable_after_first_display(self) -> None:
        original = collected("locked")
        self.db.upsert_items([original])
        candidate = self.db.list_candidates()[0]
        self.db.save_run(
            "run-locked",
            started_at=NOW,
            completed_at=NOW,
            status="SUCCESS",
            model_status="DETERMINISTIC_DEGRADED",
            source_statuses={"fortinet_psirt": {"status": "OK"}},
            ranked_items=[
                {
                    "item": candidate,
                    "score": 10.0,
                    "reason": "Appréciation initiale.",
                    "is_serendipity": False,
                }
            ],
        )
        changed = CollectedItem(
            source_id=original.source_id,
            external_id=original.external_id,
            title="Titre source modifié ultérieurement",
            url=original.url,
            published_at="2026-09-05T05:00:00Z",
            summary="Résumé source modifié ultérieurement",
            topics=("autre",),
            story_key="title:changed",
            collected_at="2026-09-05T06:00:00Z",
        )

        self.db.upsert_items([changed])

        history_item = self.db.list_history()[0]["items"][0]
        self.assertEqual(history_item["title"], original.title)
        self.assertEqual(history_item["summary"], original.summary)
        self.assertEqual(history_item["published_at"], original.published_at)
        self.assertEqual(tuple(history_item["topics"]), original.topics)
        self.assertEqual(history_item["story_key"], original.story_key)


if __name__ == "__main__":
    unittest.main()
