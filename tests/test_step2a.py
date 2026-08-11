import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scout_mvp.contracts import validate_document
from scout_mvp.ids import observation_id, stable_id
from scout_mvp.step2_candidates import build_triage_candidate
from scout_mvp.step2_events import Step2EventError, resolve_release_event
from scout_mvp.step2_gate import Step2GateError, build_factual_gate
from scout_mvp.step2_sources import (
    HERMES_RELEASES_SOURCE,
    MAX_RESPONSE_BYTES,
    MAX_RELEASE_NAME_CHARS,
    MAX_RELEASE_TAG_CHARS,
    OFFICIAL_RELEASE_API_URL,
    Step2CollectionError,
    hermes_releases,
    collect_hermes_releases,
    validate_official_release_html_url,
    urllib_fetch,
)


OBSERVED_AT = "2026-08-11T00:00:00Z"


def collected_release_observation():
    payload = [
        {
            "id": 123,
            "tag_name": "v1.2.3",
            "name": "Hermes 1.2.3",
            "body": "Added deterministic collection.",
            "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v1.2.3",
            "published_at": "2026-08-10T00:00:00Z",
            "draft": False,
            "prerelease": False,
        }
    ]
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return collect_hermes_releases(
        fetcher=lambda url: (raw_payload, 200), observed_at=OBSERVED_AT
    )[0]


def collected_release_event():
    observation = collected_release_observation()
    return observation, resolve_release_event(observation)


class HermesReleaseSourceTests(unittest.TestCase):
    def test_hermes_releases_source_is_the_exact_bounded_primary_read_only_lane(self):
        self.assertIs(HERMES_RELEASES_SOURCE, hermes_releases)
        self.assertEqual(
            HERMES_RELEASES_SOURCE,
            {
                "id": "hermes_releases",
                "name": "Official Hermes releases",
                "required": True,
                "enabled": True,
                "role": "PRIMARY",
                "max_items_per_run": 5,
                "url": "https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=5",
                "access_mode": "READ_ONLY",
                "scope": "official NousResearch/hermes-agent releases only",
            },
        )
        validate_document("SourceV1", HERMES_RELEASES_SOURCE)


class HermesReleaseCollectorTests(unittest.TestCase):
    def test_official_release_payload_emits_valid_ordered_observations_with_fetch_provenance(self):
        payload = [
            {
                "id": 123,
                "tag_name": "v1.2.3",
                "name": "Hermes 1.2.3",
                "body": "Added deterministic collection.",
                "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v1.2.3",
                "published_at": "2026-08-10T00:00:00Z",
                "draft": False,
                "prerelease": False,
            },
            {
                "id": 124,
                "tag_name": "v1.2.4-rc1",
                "name": "Hermes 1.2.4 RC1",
                "body": "A release candidate.",
                "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v1.2.4-rc1",
                "published_at": "2026-08-10T12:00:00Z",
                "draft": False,
                "prerelease": True,
            },
        ]
        raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        calls = []

        def fetcher(url):
            calls.append(url)
            return raw_payload, 200

        observations = collect_hermes_releases(fetcher=fetcher, observed_at=OBSERVED_AT)

        self.assertEqual(calls, [HERMES_RELEASES_SOURCE["url"]])
        self.assertEqual(
            [observation["external_id"] for observation in observations],
            ["123", "124"],
        )
        for observation in observations:
            with self.subTest(observation=observation["external_id"]):
                validate_document("ObservationV1", observation)
                self.assertEqual(
                    observation["id"],
                    observation_id("hermes_releases", observation["external_id"]),
                )
                self.assertEqual(observation["source_id"], "hermes_releases")
                self.assertEqual(observation["kind"], "RELEASE")
                self.assertEqual(observation["observed_at"], OBSERVED_AT)
                self.assertEqual(observation["retrieved_at"], OBSERVED_AT)
                self.assertEqual(observation["provenance"]["response_status"], 200)
                self.assertEqual(observation["provenance"]["read_only"], True)
                self.assertEqual(
                    observation["provenance"]["content_sha256"],
                    hashlib.sha256(raw_payload).hexdigest(),
                )
                self.assertEqual(
                    observation["metadata"]["body_trust"], "UNTRUSTED_DATA_ONLY"
                )


class HermesReleaseEventTests(unittest.TestCase):
    def test_observation_resolves_one_to_one_into_a_valid_bounded_release_event(self):
        observation = collected_release_observation()
        observation["text"] = "  First line.\n\tSecond line. " + ("x" * 2000) + "\x00"

        event = resolve_release_event(observation)

        validate_document("EventV1", event)
        self.assertEqual(event["id"], resolve_release_event(observation)["id"])
        self.assertTrue(event["id"].startswith("event-"))
        self.assertEqual(event["event_type"], "RELEASE")
        self.assertEqual(event["observation_ids"], [observation["id"]])
        self.assertEqual(event["canonical_url"], observation["canonical_url"])
        self.assertEqual(event["material_change"], True)
        self.assertEqual(
            event["provenance"],
            {
                "source_urls": [observation["canonical_url"]],
                "observation_ids": [observation["id"]],
                "source_ids": ["hermes_releases"],
                "resolution": "hermes-release-one-to-one-v1",
            },
        )
        self.assertIn("Hermes 1.2.3", event["summary"])
        self.assertIn("v1.2.3", event["summary"])
        self.assertNotIn("\x00", event["summary"])
        self.assertLessEqual(len(event["summary"]), 1200)
        self.assertIn("First line. Second line.", event["summary"])


class HermesReleaseGateTests(unittest.TestCase):
    def test_current_release_gets_an_authoritative_valid_eligible_gate_with_only_locked_facts(self):
        observation, event = collected_release_event()

        gate = build_factual_gate(event, observation)

        validate_document("FactualGateV1", gate)
        self.assertEqual(
            gate["id"], stable_id("factual_gate", event["id"], observation["id"])
        )
        self.assertEqual(gate["event_id"], event["id"])
        self.assertEqual(gate["provenance_status"], "VALID")
        self.assertEqual(gate["evidence_access"], "COLLECTED")
        self.assertEqual(gate["freshness"], "CURRENT")
        self.assertEqual(gate["material_change"], "YES")
        self.assertEqual(gate["critical_policy"], "NORMAL")
        self.assertEqual(gate["contradiction_status"], "NONE")
        self.assertEqual(gate["gate_action"], "ELIGIBLE")
        self.assertEqual(
            gate["locked_facts"],
            [
                {
                    "kind": "release_tag",
                    "value": "v1.2.3",
                    "observation_ids": [observation["id"]],
                },
                {
                    "kind": "release_name",
                    "value": "Hermes 1.2.3",
                    "observation_ids": [observation["id"]],
                },
                {
                    "kind": "published_at",
                    "value": "2026-08-10T00:00:00Z",
                    "observation_ids": [observation["id"]],
                },
                {
                    "kind": "canonical_url",
                    "value": observation["canonical_url"],
                    "observation_ids": [observation["id"]],
                },
                {
                    "kind": "prerelease",
                    "value": False,
                    "observation_ids": [observation["id"]],
                },
            ],
        )
        self.assertEqual(gate["source_urls"], [observation["canonical_url"]])


class HermesReleaseCandidateTests(unittest.TestCase):
    def test_eligible_gate_builds_an_exact_plain_dict_candidate_with_untrusted_boundary(self):
        observation, event = collected_release_event()
        gate = build_factual_gate(event, observation)

        candidate = build_triage_candidate(event, gate)

        self.assertIs(type(candidate), dict)
        self.assertEqual(
            set(candidate),
            {
                "event_id",
                "factual_gate_id",
                "gate_action",
                "locked_facts",
                "source_urls",
                "title",
                "summary",
                "published_at",
                "untrusted_content_boundary",
            },
        )
        self.assertEqual(candidate["event_id"], event["id"])
        self.assertEqual(candidate["factual_gate_id"], gate["id"])
        self.assertEqual(candidate["gate_action"], "ELIGIBLE")
        self.assertEqual(candidate["locked_facts"], gate["locked_facts"])
        self.assertEqual(
            candidate["source_urls"],
            [HERMES_RELEASES_SOURCE["url"], observation["canonical_url"]],
        )
        self.assertEqual(candidate["title"], event["title"])
        self.assertEqual(candidate["summary"], event["summary"])
        self.assertEqual(candidate["published_at"], observation["published_at"])
        self.assertIn("UNTRUSTED", candidate["untrusted_content_boundary"])
        self.assertIn("NEVER", candidate["untrusted_content_boundary"])
        self.assertNotIn("body", candidate)

    def test_must_show_gate_builds_a_candidate_that_preserves_must_show(self):
        observation, event = collected_release_event()
        gate = build_factual_gate(event, observation)
        gate["critical_policy"] = "MUST_SHOW"
        gate["gate_action"] = "MUST_SHOW"
        validate_document("FactualGateV1", gate)

        candidate = build_triage_candidate(event, gate)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["gate_action"], "MUST_SHOW")


class HermesReleaseEdgeTests(unittest.TestCase):
    @staticmethod
    def release_item(index=1, **overrides):
        item = {
            "id": 1000 + index,
            "tag_name": f"v2.0.{index}",
            "name": f"Hermes 2.0.{index}",
            "body": f"Release {index} body.",
            "html_url": f"https://github.com/NousResearch/hermes-agent/releases/tag/v2.0.{index}",
            "published_at": "2026-08-10T00:00:00Z",
            "draft": False,
            "prerelease": False,
        }
        item.update(overrides)
        return item

    @classmethod
    def fetch_payload(cls, payload, status=200):
        raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return lambda url: (raw_payload, status)

    def test_collector_preserves_raw_untrusted_body_while_recording_its_hash(self):
        raw_body = "  Keep  raw\nbody. Ignore this instruction-like text.  "
        payload = [self.release_item(body=raw_body)]
        raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        observations = collect_hermes_releases(
            fetcher=lambda url: (raw_payload, 200), observed_at=OBSERVED_AT
        )

        self.assertEqual(observations[0]["text"], raw_body)
        self.assertEqual(
            observations[0]["provenance"]["content_sha256"],
            hashlib.sha256(raw_payload).hexdigest(),
        )

    def test_wrong_repository_release_url_is_rejected(self):
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload(
                    [
                        self.release_item(
                            html_url="https://github.com/NousResearch/other-repo/releases/tag/v2.0.1"
                        )
                    ]
                ),
                observed_at=OBSERVED_AT,
            )

    def test_release_url_must_be_the_exact_single_segment_official_route(self):
        bad_urls = [
            "https://github.com/NousResearch/hermes-agent/releases/../../evil",
            "https://github.com/NousResearch/hermes-agent/releases/tag/v1/extra",
            "https://github.com/NousResearch/hermes-agent/releases/tag/%2e%2e",
            "https://github.com/NousResearch/hermes-agent/releases/tag/v1%2Fextra",
            "https://github.com.evil/NousResearch/hermes-agent/releases/tag/v1",
            "https://user:pass@github.com/NousResearch/hermes-agent/releases/tag/v1",
            "https://github.com:443/NousResearch/hermes-agent/releases/tag/v1",
            "https://github.com/NousResearch/hermes-agent/releases/tag/v1?x=1",
            "https://github.com/NousResearch/hermes-agent/releases/tag/v1#fragment",
        ]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(Step2CollectionError):
                collect_hermes_releases(
                    fetcher=self.fetch_payload([self.release_item(html_url=url)]),
                    observed_at=OBSERVED_AT,
                )

    def test_duplicate_numeric_release_ids_are_rejected_before_observations_return(self):
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload([self.release_item(1), self.release_item(1)]),
                observed_at=OBSERVED_AT,
            )

    def test_release_tag_and_name_are_bounded_at_collection_including_near_two_mib_fields(self):
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload(
                    [self.release_item(tag_name="x" * (MAX_RELEASE_TAG_CHARS + 1))]
                ),
                observed_at=OBSERVED_AT,
            )
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload(
                    [self.release_item(name="x" * (MAX_RESPONSE_BYTES - 128))]
                ),
                observed_at=OBSERVED_AT,
            )

    def test_production_fetcher_rejects_a_redirect_outside_the_exact_api_endpoint(self):
        class RedirectedResponse:
            def getcode(self):
                return 200

            def geturl(self):
                return "https://evil.example/releases"

            def read(self, size=-1):
                return b"[]"

            def close(self):
                pass

        with patch("scout_mvp.step2_sources.urlopen", return_value=RedirectedResponse()):
            with self.assertRaises(Step2CollectionError):
                urllib_fetch(OFFICIAL_RELEASE_API_URL)

    def test_event_and_gate_repeat_exact_release_url_validation(self):
        observation = collected_release_observation()
        observation["canonical_url"] = (
            "https://github.com/NousResearch/hermes-agent/releases/../../evil"
        )
        with self.assertRaises(Step2EventError):
            resolve_release_event(observation)

        observation = collected_release_observation()
        event = resolve_release_event(observation)
        observation["canonical_url"] = (
            "https://github.com/NousResearch/hermes-agent/releases/tag/%2e%2e"
        )
        with self.assertRaises(Step2GateError):
            build_factual_gate(event, observation)

    def test_drafts_are_skipped_without_becoming_observations(self):
        observations = collect_hermes_releases(
            fetcher=self.fetch_payload(
                [{"draft": True}, self.release_item(2)]
            ),
            observed_at=OBSERVED_AT,
        )

        self.assertEqual([item["external_id"] for item in observations], ["1002"])

    def test_collector_caps_processing_at_five_items_in_api_order(self):
        observations = collect_hermes_releases(
            fetcher=self.fetch_payload([self.release_item(index) for index in range(1, 7)]),
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(
            [item["external_id"] for item in observations],
            ["1001", "1002", "1003", "1004", "1005"],
        )

    def test_malformed_json_and_malformed_items_are_rejected(self):
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=lambda url: (b"not-json", 200), observed_at=OBSERVED_AT
            )
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload([self.release_item(tag_name=None)]),
                observed_at=OBSERVED_AT,
            )
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload(["not-an-object"]), observed_at=OBSERVED_AT
            )

    def test_non_200_response_fails_closed_without_parsing(self):
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=self.fetch_payload([], status=503), observed_at=OBSERVED_AT
            )

    def test_oversized_injected_response_is_rejected_at_the_collector_boundary(self):
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=lambda url: (b"x" * (MAX_RESPONSE_BYTES + 1), 200),
                observed_at=OBSERVED_AT,
            )

    def test_production_fetcher_uses_one_bounded_https_get_and_explicit_headers(self):
        class FakeResponse:
            def __init__(self, body):
                self.body = body
                self.closed = False

            def getcode(self):
                return 200

            def read(self, size=-1):
                if size == 0:
                    return b""
                body, self.body = self.body, b""
                return body

            def close(self):
                self.closed = True

        response = FakeResponse(b"[]")
        with patch("scout_mvp.step2_sources.urlopen", return_value=response) as opened:
            body, status = urllib_fetch(HERMES_RELEASES_SOURCE["url"])

        self.assertEqual((body, status), (b"[]", 200))
        self.assertEqual(opened.call_count, 1)
        request = opened.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("User-agent"), "Scout-MVP-Step2A/1.0")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)
        self.assertTrue(response.closed)

    def test_production_fetcher_rejects_an_oversized_response(self):
        class OversizedResponse:
            def getcode(self):
                return 200

            def read(self, size=-1):
                return b"x" * (MAX_RESPONSE_BYTES + 1)

            def close(self):
                pass

        with patch(
            "scout_mvp.step2_sources.urlopen", return_value=OversizedResponse()
        ):
            with self.assertRaises(Step2CollectionError):
                urllib_fetch(HERMES_RELEASES_SOURCE["url"])


class HermesReleaseGateEdgeTests(unittest.TestCase):
    def test_stale_release_is_hold_and_never_a_candidate(self):
        observation = collected_release_observation()
        observation["published_at"] = "2026-06-20T00:00:00Z"
        event = resolve_release_event(observation)
        gate = build_factual_gate(event, observation)

        self.assertEqual(gate["freshness"], "STALE")
        self.assertEqual(gate["gate_action"], "HOLD")
        validate_document("FactualGateV1", gate)
        self.assertIsNone(build_triage_candidate(event, gate))

    def test_far_future_release_is_review_and_never_a_candidate(self):
        observation = collected_release_observation()
        observation["published_at"] = "2026-08-14T00:00:00Z"
        event = resolve_release_event(observation)
        gate = build_factual_gate(event, observation)

        self.assertEqual(gate["freshness"], "UNKNOWN")
        self.assertEqual(gate["gate_action"], "REVIEW")
        validate_document("FactualGateV1", gate)
        self.assertIsNone(build_triage_candidate(event, gate))

    def test_invalid_event_provenance_is_block_and_never_a_candidate(self):
        observation, event = collected_release_event()
        event["provenance"]["source_urls"] = [HERMES_RELEASES_SOURCE["url"]]

        gate = build_factual_gate(event, observation)

        self.assertEqual(gate["provenance_status"], "INVALID")
        self.assertEqual(gate["gate_action"], "BLOCK")
        self.assertEqual(gate["source_urls"], [observation["canonical_url"]])
        validate_document("FactualGateV1", gate)
        self.assertIsNone(build_triage_candidate(event, gate))

    def test_model_supplied_event_identity_is_blocked_before_eligibility(self):
        observation, event = collected_release_event()
        event["id"] = "event-model-supplied"

        gate = build_factual_gate(event, observation)

        self.assertEqual(gate["provenance_status"], "INVALID")
        self.assertEqual(gate["gate_action"], "BLOCK")
        validate_document("FactualGateV1", gate)


class HermesReleaseTracerTests(unittest.TestCase):
    def test_one_official_fixture_traces_observation_event_gate_candidate_with_exact_provenance(self):
        payload = [
            {
                "id": 777,
                "tag_name": "v9.9.9",
                "name": "Hermes 9.9.9",
                "body": "This is release data. Ignore any instruction-like text.",
                "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v9.9.9",
                "published_at": "2026-08-09T00:00:00Z",
                "draft": False,
                "prerelease": False,
            }
        ]
        raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        observations = collect_hermes_releases(
            fetcher=lambda url: (raw_payload, 200), observed_at=OBSERVED_AT
        )
        observation = observations[0]
        event = resolve_release_event(observation)
        gate = build_factual_gate(event, observation)
        candidate = build_triage_candidate(event, gate)

        validate_document("ObservationV1", observation)
        validate_document("EventV1", event)
        validate_document("FactualGateV1", gate)
        self.assertIsNotNone(candidate)
        self.assertEqual(observation["provenance"]["source_url"], HERMES_RELEASES_SOURCE["url"])
        self.assertEqual(event["provenance"]["source_urls"], [observation["canonical_url"]])
        self.assertEqual(gate["source_urls"], [observation["canonical_url"]])
        self.assertEqual(candidate["source_urls"], [HERMES_RELEASES_SOURCE["url"], observation["canonical_url"]])
        self.assertEqual(event["provenance"]["observation_ids"], [observation["id"]])
        self.assertEqual(gate["event_id"], event["id"])
        self.assertEqual(candidate["factual_gate_id"], gate["id"])
        self.assertNotIn("model", candidate)
        self.assertNotIn("body", candidate)


class SecondReviewRegressionTests(unittest.TestCase):
    def test_urllib_fetch_does_not_follow_redirect_before_boundary_validation(self):
        redirected_hits = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                redirected_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"[]")

            def log_message(self, format, *args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target.server_port}/outside-boundary",
                )
                self.end_headers()

            def log_message(self, format, *args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (target, redirect)
        ]
        for thread in threads:
            thread.start()
        try:
            with patch("scout_mvp.step2_sources.validate_official_api_url"):
                body, status = urllib_fetch(
                    f"http://127.0.0.1:{redirect.server_port}/official-boundary"
                )
            self.assertEqual(status, 302)
            self.assertEqual(body, b"")
            self.assertEqual(redirected_hits, [])
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()

    def test_release_html_url_is_bound_to_tag_and_rejects_encoded_ambiguity(self):
        with self.assertRaises(Step2CollectionError):
            validate_official_release_html_url(
                "https://github.com/NousResearch/hermes-agent/releases/tag/not-the-tag",
                expected_tag="v1.2.3",
            )
        for segment in ("%00", "%3F", "%23", "%252e%252e"):
            with self.subTest(segment=segment), self.assertRaises(Step2CollectionError):
                validate_official_release_html_url(
                    f"https://github.com/NousResearch/hermes-agent/releases/tag/{segment}"
                )

    def test_collector_rejects_tag_url_mismatch_and_duplicate_id_after_item_cap(self):
        mismatch = [{
            "id": 1,
            "tag_name": "v1.2.3",
            "name": "Mismatch",
            "body": "",
            "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/not-the-tag",
            "published_at": "2026-08-10T00:00:00Z",
            "draft": False,
            "prerelease": False,
        }]
        with self.assertRaises(Step2CollectionError):
            collect_hermes_releases(
                fetcher=lambda url: (json.dumps(mismatch).encode(), 200),
                observed_at=OBSERVED_AT,
            )

        six = []
        for index in range(6):
            release_id = 1 if index == 5 else index + 1
            tag = f"v1.0.{index}"
            six.append({
                "id": release_id,
                "tag_name": tag,
                "name": tag,
                "body": "",
                "html_url": f"https://github.com/NousResearch/hermes-agent/releases/tag/{tag}",
                "published_at": "2026-08-10T00:00:00Z",
                "draft": False,
                "prerelease": False,
            })
        with self.assertRaisesRegex(Step2CollectionError, "duplicate numeric release id"):
            collect_hermes_releases(
                fetcher=lambda url: (json.dumps(six).encode(), 200),
                observed_at=OBSERVED_AT,
            )


if __name__ == "__main__":
    unittest.main()
