import unittest

from scout_mvp.collection import validate_run
from scout_mvp.contracts import ContractValidationError, validate_document


SOURCE_URL = "https://example.com/advisories"
ITEM_URL = f"{SOURCE_URL}/ADV-2026-001"


def source_document():
    return {
        "id": "vendor_advisories",
        "name": "Example Vendor Advisories",
        "required": True,
        "enabled": True,
        "role": "PRIMARY",
        "max_items_per_run": 20,
        "url": SOURCE_URL,
        "access_mode": "READ_ONLY",
        "scope": "official Example Vendor advisories",
    }


def factual_gate_document():
    return {
        "event_id": "event-001",
        "provenance_status": "VALID",
        "evidence_access": "COLLECTED",
        "freshness": "CURRENT",
        "material_change": "YES",
        "critical_policy": "NORMAL",
        "contradiction_status": "RESOLVED",
        "gate_action": "ELIGIBLE",
        "locked_facts": [
            {
                "kind": "advisory_id",
                "value": "ADV-2026-001",
                "observation_ids": ["observation-001"],
            }
        ],
        "source_urls": [ITEM_URL],
    }


def decision_document():
    return {
        "id": "decision-001",
        "event_id": "event-001",
        "model": "gpt-5.6-sol",
        "decision": "SHOW",
        "thematic_fit": "DIRECT",
        "materiality": "HIGH",
        "attention": "NOW",
        "reason_code": "ACTIVE_SECURITY_RELEVANCE",
        "factual_draft": "Example Vendor advisory exists.",
        "rationale": "It is directly relevant to the security lane.",
        "source_urls": [ITEM_URL],
        "factual_gate_id": "gate-001",
        "gate_action": "ELIGIBLE",
    }


def card_document():
    return {
        "id": "card-001",
        "run_id": "run-001",
        "profile_id": "example-profile",
        "event_id": "event-001",
        "decision_id": "decision-001",
        "factual_gate_id": "gate-001",
        "rank": 1,
        "category": "FUTURE_CATEGORY",
        "title": "Example Vendor advisory",
        "what_changed": "A security advisory was published.",
        "why_for_me": "It is relevant to the active security lane.",
        "badges": ["SECURITY"],
        "published_at": None,
        "source_links": [
            {
                "source_id": "vendor_advisories",
                "name": "Example Vendor Advisories",
                "url": ITEM_URL,
                "access": "COLLECTED",
            }
        ],
        "delivered_to": "local",
        "delivered_at": None,
        "delivery_status": "PENDING",
    }


def run_document(*, card_ids=None, counts=None):
    card_ids = [] if card_ids is None else card_ids
    counts = {
        "sources": 0,
        "observations": 0,
        "events": 0,
        "cards": len(card_ids),
        "errors": 0,
    } if counts is None else counts
    return {
        "id": "run-001",
        "invocation_id": "manual-001",
        "profile_id": "example-profile",
        "trigger": "MANUAL",
        "status": "SUCCESS",
        "started_at": "2026-08-11T00:00:00Z",
        "finished_at": "2026-08-11T00:00:00Z",
        "source_ids": [],
        "observation_ids": [],
        "event_ids": [],
        "card_ids": card_ids,
        "counts": counts,
        "errors": [],
        "network_calls": 0,
    }


class ContractTestCase(unittest.TestCase):
    def assertInvalid(self, kind, document):
        with self.assertRaises(ContractValidationError):
            validate_document(kind, document)


class ProfileV1ContractTests(ContractTestCase):
    def test_minimal_user_first_profile_retains_priorities_and_critical_scope(self):
        profile = {
            "id": "example-profile",
            "version": "1",
            "priorities": ["Example Vendor", "Hermes"],
            "critical_scope": ["security advisories", "Hermes releases"],
        }

        self.assertIsNone(validate_document("ProfileV1", profile))

    def test_unrecognized_profile_fields_are_rejected(self):
        profile = {
            "id": "example-profile",
            "version": "1",
            "priorities": ["Example Vendor"],
            "critical_scope": ["security"],
            "unexpected": True,
        }

        self.assertInvalid("ProfileV1", profile)


class SourceV1ContractTests(ContractTestCase):
    def test_canonical_source_is_bounded_enabled_and_read_only(self):
        self.assertIsNone(validate_document("SourceV1", source_document()))

    def test_invalid_source_url_is_rejected(self):
        source = source_document()
        source["url"] = "http://example.com/advisories"

        self.assertInvalid("SourceV1", source)


class ObservationV1ContractTests(unittest.TestCase):
    def test_observation_requires_source_url_and_fetch_provenance(self):
        observation = {
            "id": "observation-001",
            "source_id": "vendor_advisories",
            "external_id": "ADV-2026-001",
            "kind": "SECURITY",
            "observed_at": "2026-08-11T00:00:00Z",
            "title": "Example Vendor advisory",
            "text": "Advisory evidence.",
            "canonical_url": ITEM_URL,
            "provenance": {
                "source_url": SOURCE_URL,
                "retrieved_at": "2026-08-11T00:00:00Z",
                "response_status": 200,
                "content_sha256": "0" * 64,
                "collector": "fixture",
                "read_only": True,
            },
        }

        self.assertIsNone(validate_document("ObservationV1", observation))


class EventV1ContractTests(unittest.TestCase):
    def test_event_keeps_observation_and_source_url_provenance(self):
        event = {
            "id": "event-001",
            "observation_ids": ["observation-001"],
            "title": "Example Vendor advisory",
            "summary": "Advisory evidence.",
            "canonical_url": ITEM_URL,
            "first_seen_at": "2026-08-11T00:00:00Z",
            "last_seen_at": "2026-08-11T00:00:00Z",
            "material_change": False,
            "provenance": {
                "source_urls": [ITEM_URL],
                "observation_ids": ["observation-001"],
                "resolution": "observation-id-v1",
            },
        }

        self.assertIsNone(validate_document("EventV1", event))


class FactualGateV1ContractTests(ContractTestCase):
    def test_factual_gate_uses_canonical_authoritative_enums_and_structured_facts(self):
        self.assertIsNone(validate_document("FactualGateV1", factual_gate_document()))

    def test_factual_gate_rejects_free_string_locked_facts(self):
        gate = factual_gate_document()
        gate["locked_facts"] = ["Example Vendor advisory exists"]

        self.assertInvalid("FactualGateV1", gate)

    def test_invalid_provenance_requires_block(self):
        gate = factual_gate_document()
        gate["provenance_status"] = "INVALID"
        gate["gate_action"] = "ELIGIBLE"

        self.assertInvalid("FactualGateV1", gate)

    def test_invalid_provenance_with_block_is_valid(self):
        gate = factual_gate_document()
        gate["provenance_status"] = "INVALID"
        gate["gate_action"] = "BLOCK"

        self.assertIsNone(validate_document("FactualGateV1", gate))

    def test_must_show_policy_requires_must_show_gate_action(self):
        gate = factual_gate_document()
        gate["critical_policy"] = "MUST_SHOW"
        gate["gate_action"] = "ELIGIBLE"

        self.assertInvalid("FactualGateV1", gate)

    def test_must_show_policy_with_must_show_gate_action_is_valid(self):
        gate = factual_gate_document()
        gate["critical_policy"] = "MUST_SHOW"
        gate["gate_action"] = "MUST_SHOW"

        self.assertIsNone(validate_document("FactualGateV1", gate))


class DecisionV1ContractTests(ContractTestCase):
    def test_decision_uses_fixed_model_and_v1_attention_enums(self):
        self.assertIsNone(validate_document("DecisionV1", decision_document()))

    def test_wrong_model_is_rejected(self):
        decision = decision_document()
        decision["model"] = "gpt-5.6-mini"

        self.assertInvalid("DecisionV1", decision)

    def test_decision_requires_factual_gate_reference_and_allowed_gate_action(self):
        for field in ("factual_gate_id", "gate_action"):
            decision = decision_document()
            del decision[field]
            with self.subTest(field=field):
                self.assertInvalid("DecisionV1", decision)

        for gate_action in ("HOLD", "REVIEW", "BLOCK"):
            decision = decision_document()
            decision["gate_action"] = gate_action
            with self.subTest(gate_action=gate_action):
                self.assertInvalid("DecisionV1", decision)

    def test_must_show_gate_cannot_be_downgraded_by_a_reject_decision(self):
        decision = decision_document()
        decision["gate_action"] = "MUST_SHOW"
        decision["decision"] = "REJECT"

        self.assertInvalid("DecisionV1", decision)

    def test_must_show_gate_with_show_decision_is_valid(self):
        decision = decision_document()
        decision["gate_action"] = "MUST_SHOW"
        decision["decision"] = "SHOW"

        self.assertIsNone(validate_document("DecisionV1", decision))


class CardV1ContractTests(ContractTestCase):
    def test_card_exposes_canonical_visible_fields_and_open_category(self):
        self.assertIsNone(validate_document("CardV1", card_document()))

    def test_card_requires_decision_id(self):
        card = card_document()
        del card["decision_id"]

        self.assertInvalid("CardV1", card)

    def test_card_requires_factual_gate_id(self):
        card = card_document()
        del card["factual_gate_id"]

        self.assertInvalid("CardV1", card)

    def test_card_source_link_access_is_limited_to_trust_preserving_values(self):
        for access in ("CITED", "UNKNOWN", "COLLECTED_AND_CITED"):
            card = card_document()
            card["source_links"][0]["access"] = access
            with self.subTest(access=access):
                self.assertInvalid("CardV1", card)

        for access in ("COLLECTED", "CITED_NOT_COLLECTED"):
            card = card_document()
            card["source_links"][0]["access"] = access
            with self.subTest(access=access):
                self.assertIsNone(validate_document("CardV1", card))

    def test_card_delivery_status_is_limited_to_mvp_values(self):
        for status in ("SENT", "DELIVERED_ONCE", "UNKNOWN"):
            card = card_document()
            card["delivery_status"] = status
            with self.subTest(status=status):
                self.assertInvalid("CardV1", card)

        for status in ("PENDING", "DELIVERED", "FAILED"):
            card = card_document()
            card["delivery_status"] = status
            with self.subTest(status=status):
                self.assertIsNone(validate_document("CardV1", card))


class FeedbackV1ContractTests(ContractTestCase):
    def test_feedback_accepts_only_the_three_mvp_reactions(self):
        for reaction in {"DISLIKE", "LOVE", "STAR"}:
            feedback = {
                "id": "feedback-001",
                "card_id": "card-001",
                "profile_id": "example-profile",
                "label": reaction,
                "created_at": "2026-08-11T00:00:00Z",
            }
            with self.subTest(reaction=reaction):
                self.assertIsNone(validate_document("FeedbackV1", feedback))

    def test_invalid_reaction_is_rejected(self):
        feedback = {
            "id": "feedback-001",
            "card_id": "card-001",
            "profile_id": "example-profile",
            "label": "LIKE",
            "created_at": "2026-08-11T00:00:00Z",
        }

        self.assertInvalid("FeedbackV1", feedback)


class RunV1ContractTests(ContractTestCase):
    def test_collection_validator_accepts_zero_card_success(self):
        self.assertIsNone(validate_run(run_document()))

    def test_manual_success_run_allows_zero_cards_and_no_network(self):
        self.assertIsNone(validate_document("RunV1", run_document()))

    def test_partial_status_and_future_nonzero_network_calls_are_valid(self):
        run = run_document()
        run["status"] = "PARTIAL"
        run["network_calls"] = 3

        self.assertIsNone(validate_document("RunV1", run))

    def test_four_card_run_is_rejected(self):
        card_ids = ["card-1", "card-2", "card-3", "card-4"]
        self.assertInvalid("RunV1", run_document(card_ids=card_ids))

    def test_count_mismatch_is_rejected(self):
        run = run_document(counts={"sources": 0, "observations": 0, "events": 0, "cards": 1, "errors": 0})
        self.assertInvalid("RunV1", run)

    def test_duplicate_ids_are_rejected(self):
        run = run_document(card_ids=["card-1", "card-1"])
        self.assertInvalid("RunV1", run)

    def test_invocation_id_must_be_a_safe_lowercase_token(self):
        for invocation_id in ("", "Manual-001", "manual 001", "manual/001", "manual.001"):
            run = run_document()
            run["invocation_id"] = invocation_id
            with self.subTest(invocation_id=invocation_id):
                self.assertInvalid("RunV1", run)


class WeeklyReviewV1ContractTests(ContractTestCase):
    def test_weekly_review_records_runs_attention_answer_useful_cards_and_found_alone_answers(self):
        review = {
            "id": "weekly-review-001",
            "profile_id": "example-profile",
            "week_start": "2026-08-10",
            "week_end": "2026-08-16",
            "created_at": "2026-08-16T18:00:00Z",
            "run_ids": ["run-001"],
            "estimated_attention_minutes": 10,
            "weekly_answer": "PARTIALLY",
            "useful_card_ids": ["card-001"],
            "found_alone_answers": [
                {"card_id": "card-001", "answer": "PROBABLY_NO"}
            ],
        }

        self.assertIsNone(validate_document("WeeklyReviewV1", review))


if __name__ == "__main__":
    unittest.main()
