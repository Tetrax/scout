import hashlib
import unittest

from scout_mvp.ids import card_id, event_id, feedback_id, observation_id, run_id, stable_id


class StableIdTests(unittest.TestCase):
    def test_stable_id_is_sha256_over_explicit_nul_delimited_identity_parts(self):
        expected_digest = hashlib.sha256(b"observation\x00vendor_advisories\x00ADV-2026-001").hexdigest()

        self.assertEqual(
            stable_id("observation", "vendor_advisories", "ADV-2026-001"),
            f"observation-{expected_digest}",
        )

    def test_typed_ids_are_deterministic_and_namespace_scoped(self):
        self.assertEqual(
            observation_id("vendor_advisories", "ADV-2026-001"),
            observation_id("vendor_advisories", "ADV-2026-001"),
        )
        self.assertNotEqual(
            observation_id("vendor_advisories", "ADV-2026-001"),
            observation_id("national_catalog_vendor", "ADV-2026-001"),
        )
        self.assertTrue(event_id("canonical-event").startswith("event-"))
        self.assertTrue(card_id("run-1", "event-1").startswith("card-"))
        self.assertTrue(feedback_id("card-1", "LOVE").startswith("feedback-"))
        self.assertTrue(
            run_id("example-profile", "2026-08-11T00:00:00Z", "manual-1").startswith("run-")
        )

    def test_nul_empty_and_non_string_identity_parts_are_rejected(self):
        ambiguous_pairs = [
            ("observation", "a\x00b", "c"),
            ("observation", "a", "b\x00c"),
        ]
        for arguments in ambiguous_pairs:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    stable_id(*arguments)

        invalid_arguments = [
            ("", "part"),
            ("observation", ""),
            ("observation", None),
            (None, "part"),
            ("observation", 42),
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    stable_id(*arguments)


if __name__ == "__main__":
    unittest.main()
