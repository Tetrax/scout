from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from scout_web.sources import (
    ENABLED_SOURCES,
    X_BOOKMARKS_DIAGNOSTIC,
    SourceError,
    parse_cisa_kev,
    parse_fortinet_rss,
    parse_github_releases,
    story_key_for,
)

NOW = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)


class SourceContractTests(unittest.TestCase):
    def test_exactly_four_bounded_public_sources_replace_disabled_x(self) -> None:
        self.assertEqual(
            list(ENABLED_SOURCES),
            [
                "fortinet_psirt",
                "cisa_kev_fortinet",
                "github_hermes_releases",
                "github_openai_codex_releases",
            ],
        )
        self.assertTrue(all(source.max_items <= 8 for source in ENABLED_SOURCES.values()))
        self.assertIn("désactivée", X_BOOKMARKS_DIAGNOSTIC.casefold())
        self.assertIn("unauthorized_client", X_BOOKMARKS_DIAGNOSTIC)

    def test_fortinet_rss_keeps_source_facts_and_absent_dates_absent(self) -> None:
        payload = b"""<?xml version='1.0'?>
        <rss version='2.0'><channel>
          <item><guid>FG-IR-26-163</guid><title>HTTP/2 Bomb</title>
            <link>https://fortiguard.fortinet.com/psirt/FG-IR-26-163</link>
            <description><![CDATA[<b>CVSSv3 Score: 5.8</b><p>Source description.</p>]]></description>
            <pubDate>Wed, 12 Aug 2026 00:00:00 -0700</pubDate></item>
          <item><guid>FG-IR-26-164</guid><title>No date</title>
            <link>https://fortiguard.fortinet.com/psirt/FG-IR-26-164</link>
            <description>Still factual.</description></item>
        </channel></rss>"""

        items = parse_fortinet_rss(payload, NOW)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "HTTP/2 Bomb")
        self.assertEqual(items[0].published_at, "2026-08-12T07:00:00Z")
        self.assertEqual(items[0].url, "https://fortiguard.fortinet.com/psirt/FG-IR-26-163")
        self.assertIn("Source description.", items[0].summary)
        self.assertIsNone(items[1].published_at)

    def test_cisa_filters_fortinet_and_constructs_only_fixed_catalog_links(self) -> None:
        payload = json.dumps(
            {
                "vulnerabilities": [
                    {
                        "cveID": "CVE-2026-12345",
                        "vendorProject": "Fortinet",
                        "product": "FortiOS",
                        "vulnerabilityName": "Fortinet FortiOS auth bypass",
                        "dateAdded": "2026-09-04",
                        "shortDescription": "Actively exploited auth bypass.",
                        "requiredAction": "Apply vendor mitigations.",
                    },
                    {
                        "cveID": "CVE-2026-99999",
                        "vendorProject": "Other",
                        "product": "Other",
                        "vulnerabilityName": "Out of scope",
                        "dateAdded": "2026-09-05",
                        "shortDescription": "Ignore me.",
                    },
                ]
            }
        ).encode()

        items = parse_cisa_kev(payload, NOW)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "CVE-2026-12345")
        self.assertEqual(items[0].published_at, "2026-09-04")
        self.assertEqual(
            items[0].url,
            "https://www.cisa.gov/known-exploited-vulnerabilities-catalog?search_api_fulltext=CVE-2026-12345",
        )
        self.assertIn("Apply vendor mitigations.", items[0].summary)

    def test_github_release_parser_rejects_wrong_repository_links(self) -> None:
        payload = json.dumps(
            [
                {
                    "id": 1,
                    "name": "Release",
                    "tag_name": "v1.2.3",
                    "html_url": "https://github.com/attacker/repo/releases/tag/v1.2.3",
                    "published_at": "2026-09-04T10:00:00Z",
                    "body": "Facts.",
                    "draft": False,
                }
            ]
        ).encode()

        with self.assertRaises(SourceError):
            parse_github_releases(payload, "github_hermes_releases", NOW)

    def test_github_release_parser_binds_canonical_link_to_tag_name(self) -> None:
        payload = json.dumps(
            [
                {
                    "id": 1,
                    "name": "Release",
                    "tag_name": "v1.2.3",
                    "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v9.9.9",
                    "published_at": "2026-09-04T10:00:00Z",
                    "body": "Facts.",
                    "draft": False,
                }
            ]
        ).encode()

        with self.assertRaises(SourceError):
            parse_github_releases(payload, "github_hermes_releases", NOW)

    def test_github_release_parser_bounds_and_preserves_canonical_link(self) -> None:
        payload = json.dumps(
            [
                {
                    "id": index,
                    "name": f"Hermes {index}",
                    "tag_name": f"v{index}",
                    "html_url": f"https://github.com/NousResearch/hermes-agent/releases/tag/v{index}",
                    "published_at": "2026-09-04T10:00:00Z",
                    "body": "First factual paragraph.\n\nSecond paragraph.",
                    "draft": False,
                }
                for index in range(1, 12)
            ]
        ).encode()

        items = parse_github_releases(payload, "github_hermes_releases", NOW)

        self.assertEqual(len(items), ENABLED_SOURCES["github_hermes_releases"].max_items)
        self.assertEqual(items[0].title, "Hermes 1")
        self.assertEqual(
            items[0].url,
            "https://github.com/NousResearch/hermes-agent/releases/tag/v1",
        )
        self.assertEqual(items[0].summary, "First factual paragraph. Second paragraph.")

    def test_malformed_or_entity_expanding_xml_fails_closed(self) -> None:
        with self.assertRaises(SourceError):
            parse_fortinet_rss(b"<!DOCTYPE rss [<!ENTITY x 'boom'>]><rss>&x;</rss>", NOW)
        with self.assertRaises(SourceError):
            parse_cisa_kev(b"{not-json", NOW)

    def test_same_cve_has_same_story_key_across_sources(self) -> None:
        first = story_key_for("Fortinet issue CVE-2026-12345", "Details")
        second = story_key_for("Different title", "CISA confirms CVE-2026-12345")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
