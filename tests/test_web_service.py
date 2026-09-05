from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scout_web.database import Database
from scout_web.service import DiscoveryBusy, collect_and_store, run_discovery
from scout_web.sources import SourceDefinition, SourceError

NOW = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)


def github_payload(owner: str, repo: str, prefix: int) -> bytes:
    return json.dumps(
        [
            {
                "id": prefix + index,
                "name": f"{repo} release {index}",
                "tag_name": f"v{index}",
                "html_url": f"https://github.com/{owner}/{repo}/releases/tag/v{index}",
                "published_at": f"2026-09-0{index}T10:00:00Z",
                "body": f"Factual release notes {index}.",
                "draft": False,
            }
            for index in range(1, 5)
        ]
    ).encode()


FORTINET = b"""<rss><channel><item><guid>FG-IR-26-163</guid>
<title>Fortinet current advisory</title>
<link>https://fortiguard.fortinet.com/psirt/FG-IR-26-163</link>
<description>FortiOS factual advisory.</description>
<pubDate>Fri, 04 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
CISA = json.dumps(
    {
        "vulnerabilities": [
            {
                "cveID": "CVE-2026-12345",
                "vendorProject": "Fortinet",
                "product": "FortiOS",
                "vulnerabilityName": "Fortinet KEV",
                "dateAdded": "2026-09-04",
                "shortDescription": "Confirmed active exploitation.",
                "requiredAction": "Apply mitigations.",
            }
        ]
    }
).encode()


class FixtureFetcher:
    def __init__(self, *, fail: set[str] | None = None, empty: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.empty = empty or set()
        self.calls: list[str] = []

    def __call__(self, source: SourceDefinition) -> bytes:
        self.calls.append(source.id)
        if source.id in self.fail:
            raise SourceError("simulated upstream failure")
        if source.id in self.empty:
            return b"<rss><channel /></rss>" if source.kind == "fortinet_rss" else json.dumps({"vulnerabilities": []} if source.kind == "cisa_kev" else []).encode()
        return {
            "fortinet_psirt": FORTINET,
            "cisa_kev_fortinet": CISA,
            "github_hermes_releases": github_payload("NousResearch", "hermes-agent", 100),
            "github_openai_codex_releases": github_payload("openai", "codex", 200),
        }[source.id]


class DiscoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "scout.sqlite3")
        self.db.migrate()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prefetch_collects_real_shape_without_marking_seen_or_creating_history(self) -> None:
        statuses = collect_and_store(self.db, now=NOW, fetcher=FixtureFetcher(), force=True)

        self.assertEqual({entry["status"] for entry in statuses.values()}, {"OK"})
        self.assertGreater(len(self.db.list_candidates()), 0)
        self.assertEqual(self.db.seen_item_ids(), set())
        self.assertEqual(self.db.list_history(), [])

    def test_cache_prevents_repeated_network_calls_until_refresh(self) -> None:
        fetcher = FixtureFetcher()
        collect_and_store(self.db, now=NOW, fetcher=fetcher, force=False)
        collect_and_store(self.db, now=NOW, fetcher=fetcher, force=False)

        self.assertEqual(len(fetcher.calls), 4)
        self.assertEqual(
            {entry["status"] for entry in self.db.source_cache().values()},
            {"CACHED"},
        )

    def test_cached_source_failures_remain_explicit_until_retry_is_due(self) -> None:
        source_ids = {
            "fortinet_psirt",
            "cisa_kev_fortinet",
            "github_hermes_releases",
            "github_openai_codex_releases",
        }
        first = run_discovery(
            self.db,
            now=NOW,
            fetcher=FixtureFetcher(fail=source_ids),
        )
        recovery_fetcher = FixtureFetcher()

        cached = run_discovery(
            self.db,
            now=NOW + timedelta(minutes=1),
            fetcher=recovery_fetcher,
        )

        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(cached["status"], "FAILED")
        self.assertEqual(recovery_fetcher.calls, [])
        self.assertEqual(
            {entry["status"] for entry in cached["source_statuses"].values()},
            {"ERROR"},
        )
        self.assertEqual(
            {entry["status"] for entry in self.db.source_cache().values()},
            {"ERROR"},
        )

    def test_source_failure_and_empty_source_are_nonfatal_and_explicit(self) -> None:
        fetcher = FixtureFetcher(
            fail={"fortinet_psirt"}, empty={"cisa_kev_fortinet"}
        )

        statuses = collect_and_store(self.db, now=NOW, fetcher=fetcher, force=True)

        self.assertEqual(statuses["fortinet_psirt"]["status"], "ERROR")
        self.assertNotIn("simulated upstream failure", statuses["fortinet_psirt"].get("detail", ""))
        self.assertEqual(statuses["cisa_kev_fortinet"]["status"], "EMPTY")
        self.assertEqual(statuses["github_hermes_releases"]["status"], "OK")

    def test_discovery_is_bounded_persisted_and_second_run_does_not_repeat_seen_cards(self) -> None:
        first = run_discovery(self.db, now=NOW, fetcher=FixtureFetcher())
        second = run_discovery(
            self.db,
            now=datetime(2026, 9, 5, 4, 1, tzinfo=timezone.utc),
            fetcher=FixtureFetcher(),
        )

        self.assertLessEqual(len(first["items"]), 3)
        self.assertLessEqual(len(second["items"]), 3)
        self.assertTrue(
            {entry["id"] for entry in first["items"]}.isdisjoint(
                {entry["id"] for entry in second["items"]}
            )
        )
        self.assertEqual(first["model_status"], "DETERMINISTIC_DEGRADED")
        self.assertEqual(len(self.db.list_history()), 2)

    def test_existing_lock_rejects_concurrent_discovery_without_network(self) -> None:
        self.assertTrue(self.db.acquire_discovery_lock("other", "2026-09-05T04:00:00Z"))
        fetcher = FixtureFetcher()

        with self.assertRaises(DiscoveryBusy):
            run_discovery(self.db, now=NOW, fetcher=fetcher)

        self.assertEqual(fetcher.calls, [])


if __name__ == "__main__":
    unittest.main()
