import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scout_mvp.ids import observation_id, run_id, stable_id
from scout_mvp.step2_candidates import build_triage_candidate
from scout_mvp.step2_events import resolve_release_event
from scout_mvp.step2_gate import build_factual_gate
from scout_mvp.step2_sources import HERMES_RELEASES_SOURCE
from scout_mvp.storage import JsonlStore
from scout_mvp.step2_transaction import (
    IncompleteStagingError,
    commit_prepared_transaction,
    discard_incomplete_staging,
    prepare_step2_transaction,
    reconcile_committed_transaction,
    recover_step2_transaction,
    Step2TransactionCollisionError,
    UnsafeTransactionPathError,
    validate_prepared_transaction,
)
from scout_mvp.triage import MODEL, rank_and_build_cards


STARTED_AT = "2026-08-11T00:00:00Z"
FINISHED_AT = "2026-08-11T00:00:01Z"
RUN_ID = run_id("example-profile", STARTED_AT, "invocation-001")


def run_document(*, run_id=RUN_ID, status="SUCCESS", card_ids=None):
    card_ids = [] if card_ids is None else list(card_ids)
    return {
        "id": run_id,
        "invocation_id": "invocation-001",
        "profile_id": "example-profile",
        "trigger": "MANUAL",
        "status": status,
        "started_at": STARTED_AT,
        "finished_at": FINISHED_AT,
        "source_ids": [HERMES_RELEASES_SOURCE["id"]],
        "observation_ids": [],
        "event_ids": [],
        "card_ids": card_ids,
        "counts": {
            "sources": 1,
            "observations": 0,
            "events": 0,
            "cards": len(card_ids),
            "errors": 0,
        },
        "errors": [],
        "network_calls": 0,
        "model": "gpt-5.6-sol",
    }


def complete_fixture():
    observation_identifier = observation_id(HERMES_RELEASES_SOURCE["id"], "release-1")
    release_url = "https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.1"
    observation = {
        "id": observation_identifier,
        "source_id": HERMES_RELEASES_SOURCE["id"],
        "external_id": "release-1",
        "kind": "RELEASE",
        "observed_at": STARTED_AT,
        "retrieved_at": STARTED_AT,
        "published_at": STARTED_AT,
        "title": "Hermes 2026.8.1",
        "text": "A fixture release.",
        "canonical_url": release_url,
        "source_url": HERMES_RELEASES_SOURCE["url"],
        "provenance": {
            "source_url": HERMES_RELEASES_SOURCE["url"],
            "retrieved_at": STARTED_AT,
            "response_status": 200,
            "content_sha256": "0" * 64,
            "collector": "fixture",
            "read_only": True,
        },
        "metadata": {
            "release_tag": "v2026.8.1",
            "release_name": "Hermes 2026.8.1",
            "prerelease": False,
        },
    }
    event = resolve_release_event(observation)
    gate = build_factual_gate(event, observation)
    candidate = build_triage_candidate(event, gate)
    assert candidate is not None
    decision = {
        "id": stable_id("decision", event["id"], gate["id"], MODEL),
        "event_id": event["id"],
        "model": MODEL,
        "decision": "SHOW",
        "thematic_fit": "DIRECT",
        "materiality": "HIGH",
        "attention": "NOW",
        "reason_code": "FIXTURE",
        "factual_draft": candidate["summary"],
        "rationale": "It is a fixture.",
        "source_urls": list(candidate["source_urls"]),
        "factual_gate_id": gate["id"],
        "gate_action": gate["gate_action"],
    }
    card = rank_and_build_cards(RUN_ID, [candidate], [decision], FINISHED_AT)[0]
    run = run_document(card_ids=[card["id"]])
    run.update(
        {
            "observation_ids": [observation["id"]],
            "event_ids": [event["id"]],
            "counts": {"sources": 1, "observations": 1, "events": 1, "cards": 1, "errors": 0},
            "network_calls": 1,
        }
    )
    return run, observation, event, gate, decision, card


class Step2TransactionPrepareTests(unittest.TestCase):
    def test_transaction_rejects_nonterminal_run_statuses_before_state_creation(self):
        for status in ("RUNNING", "PARTIAL"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "state"
                with self.assertRaisesRegex(ValueError, "terminal run status"):
                    prepare_step2_transaction(
                        root,
                        run_document(status=status),
                        HERMES_RELEASES_SOURCE,
                        [],
                        [],
                        [],
                        [],
                        [],
                    )
                self.assertFalse(root.exists())

    def test_transaction_rejects_nondeterministic_run_id(self):
        run = run_document(run_id="run-forged")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "run identity"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [],
                    [],
                    [],
                    [],
                    [],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_schema_valid_nonofficial_source_before_state_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            source = dict(HERMES_RELEASES_SOURCE)
            source.update(
                {
                    "id": "other-source",
                    "name": "Other source",
                    "url": "https://evil.example/feed",
                    "scope": "other",
                }
            )
            run = run_document()
            run["source_ids"] = ["other-source"]

            with self.assertRaisesRegex(ValueError, "official Hermes source"):
                prepare_step2_transaction(root, run, source, [], [], [], [], [])
            self.assertFalse(root.exists())

    def test_transaction_rejects_out_of_scope_urls_even_when_cross_links_are_consistent(self):
        run, observation, event, gate, decision, card = complete_fixture()
        evil = "https://evil.example/releases/tag/v2026.8.1"
        observation["canonical_url"] = evil
        event["canonical_url"] = evil
        event["provenance"]["source_urls"] = [evil]
        gate["source_urls"] = [evil]
        decision["source_urls"] = [evil]
        card["source_links"] = [
            {
                "source_id": HERMES_RELEASES_SOURCE["id"],
                "name": "Fake release",
                "url": evil,
                "access": "CITED_NOT_COLLECTED",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "official release"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_observation_without_required_release_tag(self):
        run, observation, event, gate, decision, card = complete_fixture()
        del observation["metadata"]["release_tag"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "release metadata"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_nondeterministic_observation_id_even_in_failed_audit_bundle(self):
        _run, observation, *_ = complete_fixture()
        observation["id"] = "observation-forged"
        failed = run_document(status="FAILED")
        failed["observation_ids"] = [observation["id"]]
        failed["counts"]["observations"] = 1
        failed["counts"]["errors"] = 1
        failed["errors"] = ["fixture_failed"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "observation identity"):
                prepare_step2_transaction(
                    root,
                    failed,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [],
                    [],
                    [],
                    [],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_event_that_differs_from_deterministic_resolution(self):
        run, observation, event, gate, decision, card = complete_fixture()
        event["event_type"] = "OTHER"
        event["provenance"]["resolution"] = "forged"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "deterministic event"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_gate_that_differs_from_authoritative_recomputation(self):
        run, observation, event, gate, decision, card = complete_fixture()
        gate["locked_facts"][0]["value"] = "v-forged"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "authoritative gate"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_event_without_exactly_one_gate(self):
        run, observation, event, _gate, _decision, _card = complete_fixture()
        run["card_ids"] = []
        run["counts"]["cards"] = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "every event.*gate"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [],
                    [],
                    [],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_eligible_gate_without_exactly_one_decision(self):
        run, observation, event, gate, _decision, _card = complete_fixture()
        run["card_ids"] = []
        run["counts"]["cards"] = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "eligible gate.*decision"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [],
                    [],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_model_rewrite_of_factual_draft(self):
        run, observation, event, gate, decision, card = complete_fixture()
        decision["factual_draft"] = "Forged factual content"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "factual draft"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_nondeterministic_decision_id(self):
        run, observation, event, gate, decision, card = complete_fixture()
        decision["id"] = "decision-forged"
        card["decision_id"] = decision["id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "decision identity"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_decision_gate_action_mismatch(self):
        run, observation, event, gate, decision, card = complete_fixture()
        decision["gate_action"] = "MUST_SHOW"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "gate action"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_contract_rejects_nonshow_decision_claiming_must_show(self):
        run, observation, event, gate, decision, _card = complete_fixture()
        decision["gate_action"] = "MUST_SHOW"
        decision["decision"] = "KEEP_INTERNAL"
        run["card_ids"] = []
        run["counts"]["cards"] = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaises(ValueError):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_card_that_differs_from_deterministic_ranking_output(self):
        run, observation, event, gate, decision, card = complete_fixture()
        card["title"] = "Forged card title"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "deterministic card"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_nondeterministic_card_id(self):
        run, observation, event, gate, decision, card = complete_fixture()
        card["id"] = "card-forged"
        run["card_ids"] = [card["id"]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "card identity"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_transaction_rejects_card_for_a_nonshow_decision(self):
        run, observation, event, gate, decision, card = complete_fixture()
        decision["decision"] = "KEEP_INTERNAL"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "card.*SHOW"):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_prepare_publishes_only_a_private_complete_staging_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            run = run_document()

            transaction = prepare_step2_transaction(
                root,
                run,
                HERMES_RELEASES_SOURCE,
                [],
                [],
                [],
                [],
                [],
            )

            staging_run = root / ".staging" / RUN_ID
            self.assertEqual(transaction.run_id, RUN_ID)
            self.assertEqual(transaction.state, "PREPARED")
            self.assertTrue(staging_run.is_dir())
            self.assertFalse((root / RUN_ID).exists())
            self.assertFalse((root / "runs.jsonl").exists())
            self.assertEqual(
                sorted(path.name for path in staging_run.iterdir()),
                ["run.jsonl", "sources.jsonl", "transaction.jsonl"],
            )
            self.assertEqual(stat.S_IMODE((root / ".staging").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(staging_run.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((staging_run / "run.jsonl").stat().st_mode), 0o600)

            manifest = JsonlStore(root).read(
                f".staging/{RUN_ID}/transaction.jsonl"
            )
            self.assertEqual(len(manifest), 1)
            self.assertEqual(
                manifest[0],
                {
                    "version": 1,
                    "run_id": RUN_ID,
                    "state": "PREPARED",
                    "prepared_at": FINISHED_AT,
                    "artifacts": [
                        {
                            "filename": "sources.jsonl",
                            "kind": "SourceV1",
                            "count": 1,
                            "sha256": hashlib.sha256(
                                (root / ".staging" / RUN_ID / "sources.jsonl").read_bytes()
                            ).hexdigest(),
                        },
                        {
                            "filename": "run.jsonl",
                            "kind": "RunV1",
                            "count": 1,
                            "sha256": hashlib.sha256(
                                (root / ".staging" / RUN_ID / "run.jsonl").read_bytes()
                            ).hexdigest(),
                        },
                    ],
                },
            )
            self.assertNotIn(b"session_id", (staging_run / "transaction.jsonl").read_bytes())

    def test_prepare_is_idempotent_for_exact_staging_and_rejects_a_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            run = run_document()
            first = prepare_step2_transaction(
                root, run, HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            before = {
                path.name: path.read_bytes()
                for path in first.directory.iterdir()
                if path.is_file()
            }

            second = prepare_step2_transaction(
                root, run, HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            after = {
                path.name: path.read_bytes()
                for path in second.directory.iterdir()
                if path.is_file()
            }
            self.assertEqual(second.run, run)
            self.assertEqual(after, before)

            mismatch = dict(run, finished_at="2026-08-11T00:00:02Z")
            with self.assertRaises(IncompleteStagingError):
                prepare_step2_transaction(
                    root, mismatch, HERMES_RELEASES_SOURCE, [], [], [], [], []
                )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in first.directory.iterdir()
                    if path.is_file()
                },
                before,
            )

    def test_incomplete_staging_is_explicit_and_not_silently_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            stage = root / ".staging" / RUN_ID
            stage.mkdir(parents=True, mode=0o700)
            (stage / "sources.jsonl").write_bytes(b"incomplete\n")
            os.chmod(stage / "sources.jsonl", 0o600)
            with self.assertRaises(IncompleteStagingError):
                recover_step2_transaction(root, RUN_ID)
            self.assertTrue(stage.exists())

    def test_commit_renames_the_complete_unit_and_reconciles_the_index_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            run = run_document()
            prepare_step2_transaction(
                root, run, HERMES_RELEASES_SOURCE, [], [], [], [], []
            )

            committed = commit_prepared_transaction(root, RUN_ID)

            self.assertEqual(committed.location, "committed")
            self.assertTrue((root / RUN_ID / "run.jsonl").is_file())
            self.assertFalse((root / ".staging" / RUN_ID).exists())
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [run])
            with self.assertRaises(Exception):
                validate_prepared_transaction(root, RUN_ID)

            retry = commit_prepared_transaction(root, RUN_ID)
            self.assertEqual(retry.run, run)
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [run])

    def test_prepare_and_validate_round_trip_all_optional_artifacts_and_cross_links(self):
        run, observation, event, gate, decision, card = complete_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            prepared = prepare_step2_transaction(
                root,
                run,
                HERMES_RELEASES_SOURCE,
                [observation],
                [event],
                [gate],
                [decision],
                [card],
            )
            loaded = validate_prepared_transaction(root, RUN_ID)

            self.assertEqual(prepared.location, "staging")
            self.assertEqual(loaded.source, HERMES_RELEASES_SOURCE)
            self.assertEqual(loaded.observations, [observation])
            self.assertEqual(loaded.events, [event])
            self.assertEqual(loaded.gates, [gate])
            self.assertEqual(loaded.decisions, [decision])
            self.assertEqual(loaded.cards, [card])
            self.assertEqual(
                sorted(path.name for path in loaded.directory.iterdir()),
                [
                    "cards.jsonl",
                    "decisions.jsonl",
                    "events.jsonl",
                    "factual-gates.jsonl",
                    "observations.jsonl",
                    "run.jsonl",
                    "sources.jsonl",
                    "transaction.jsonl",
                ],
            )

    def test_invalid_cross_link_fails_before_creating_any_state(self):
        run, observation, event, gate, decision, card = complete_fixture()
        card["decision_id"] = "unknown-decision"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaises(ValueError):
                prepare_step2_transaction(
                    root,
                    run,
                    HERMES_RELEASES_SOURCE,
                    [observation],
                    [event],
                    [gate],
                    [decision],
                    [card],
                )
            self.assertFalse(root.exists())

    def test_crash_hooks_leave_recoverable_states_without_duplicate_index_entries(self):
        class SimulatedCrash(BaseException):
            pass

        crash_points = (
            "AFTER_VALIDATE_BEFORE_RENAME",
            "AFTER_RENAME_BEFORE_INDEX",
            "AFTER_INDEX",
        )
        for point in crash_points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "state"
                run = run_document()
                prepare_step2_transaction(
                    root, run, HERMES_RELEASES_SOURCE, [], [], [], [], []
                )

                def crash_hook(observed_point):
                    if observed_point == point:
                        raise SimulatedCrash(point)

                with self.assertRaises(SimulatedCrash):
                    commit_prepared_transaction(root, RUN_ID, crash_hook=crash_hook)

                final = root / RUN_ID
                index = root / "runs.jsonl"
                if point == "AFTER_VALIDATE_BEFORE_RENAME":
                    self.assertTrue((root / ".staging" / RUN_ID).is_dir())
                    self.assertFalse(final.exists())
                    self.assertFalse(index.exists())
                elif point == "AFTER_RENAME_BEFORE_INDEX":
                    self.assertTrue(final.is_dir())
                    self.assertFalse((root / ".staging" / RUN_ID).exists())
                    self.assertFalse(index.exists())
                else:
                    self.assertTrue(final.is_dir())
                    self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [run])

                self.assertEqual(recover_step2_transaction(root, RUN_ID), "COMMITTED")
                self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [run])
                self.assertEqual(recover_step2_transaction(root, RUN_ID), "COMMITTED")
                self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [run])

    def test_validation_rejects_missing_extra_hash_and_manifest_mismatch(self):
        mutations = {
            "missing": lambda stage: (stage / "sources.jsonl").unlink(),
            "extra": lambda stage: (stage / "unexpected.jsonl").write_bytes(b"x\n"),
            "hash": lambda stage: (stage / "sources.jsonl").write_bytes(b"broken\n"),
            "manifest": lambda stage: (
                stage / "transaction.jsonl"
            ).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "run_id": RUN_ID,
                        "state": "WRONG",
                        "prepared_at": FINISHED_AT,
                        "artifacts": [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "state"
                prepare_step2_transaction(
                    root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
                )
                mutate(root / ".staging" / RUN_ID)
                if label in {"extra", "hash", "manifest"}:
                    target = (
                        root / ".staging" / RUN_ID / "unexpected.jsonl"
                        if label == "extra"
                        else root / ".staging" / RUN_ID / (
                            "sources.jsonl" if label == "hash" else "transaction.jsonl"
                        )
                    )
                    os.chmod(target, 0o600)
                with self.assertRaises(ValueError):
                    validate_prepared_transaction(root, RUN_ID)

    def test_discard_removes_only_an_explicitly_incomplete_fixed_shape_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            prepare_step2_transaction(
                root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            stage = root / ".staging" / RUN_ID
            (stage / "transaction.jsonl").unlink()

            self.assertTrue(discard_incomplete_staging(root, RUN_ID))
            self.assertFalse(stage.exists())
            self.assertFalse((root / RUN_ID).exists())
            self.assertFalse((root / "runs.jsonl").exists())

    def test_discard_refuses_unknown_entries_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            prepare_step2_transaction(
                root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            stage = root / ".staging" / RUN_ID
            (stage / "transaction.jsonl").unlink()
            unknown = stage / "unknown.jsonl"
            unknown.write_bytes(b"outside\n")
            os.chmod(unknown, 0o600)

            with self.assertRaises(UnsafeTransactionPathError):
                discard_incomplete_staging(root, RUN_ID)
            self.assertTrue(unknown.exists())
            self.assertTrue(stage.exists())

    def test_symlinked_staging_final_and_artifact_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            outside_file = outside / "sentinel"
            outside_file.write_bytes(b"untouched")
            os.chmod(outside_file, 0o600)

            root = base / "state-parent-symlink"
            root.mkdir(mode=0o700)
            (root / ".staging").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                prepare_step2_transaction(
                    root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
                )
            self.assertEqual(outside_file.read_bytes(), b"untouched")

            root = base / "state-file-symlink"
            prepare_step2_transaction(
                root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            stage = root / ".staging" / RUN_ID
            source = stage / "sources.jsonl"
            source.unlink()
            source.symlink_to(outside_file)
            with self.assertRaises(ValueError):
                validate_prepared_transaction(root, RUN_ID)
            self.assertEqual(outside_file.read_bytes(), b"untouched")

            root = base / "state-final-symlink"
            prepare_step2_transaction(
                root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            (root / RUN_ID).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                commit_prepared_transaction(root, RUN_ID)
            self.assertEqual(outside_file.read_bytes(), b"untouched")

    def test_atomic_publish_never_replaces_a_destination_created_in_the_rename_race(self):
        from scout_mvp import step2_transaction

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            prepare_step2_transaction(
                root, run_document(), HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            real_rename = step2_transaction._rename_noreplace

            def destination_race(src, dst, *, src_dir_fd, dst_dir_fd):
                os.mkdir(dst, mode=0o700, dir_fd=dst_dir_fd)
                return real_rename(src_dir_fd, src, dst_dir_fd, dst)

            with patch(
                "scout_mvp.step2_transaction._rename_noreplace",
                new=lambda src_dir_fd, src, dst_dir_fd, dst: destination_race(
                    src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
                ),
            ):
                with self.assertRaises(Step2TransactionCollisionError):
                    commit_prepared_transaction(root, RUN_ID)

            self.assertTrue((root / ".staging" / RUN_ID).is_dir())
            self.assertTrue((root / RUN_ID).is_dir())
            self.assertEqual(list((root / RUN_ID).iterdir()), [])
            self.assertFalse((root / "runs.jsonl").exists())

    def test_reconcile_treats_a_post_append_exception_as_committed_when_readback_is_exact(self):
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            run = run_document()
            prepare_step2_transaction(
                root, run, HERMES_RELEASES_SOURCE, [], [], [], [], []
            )
            with self.assertRaises(SimulatedCrash):
                commit_prepared_transaction(
                    root,
                    RUN_ID,
                    crash_hook=lambda point: (
                        (_ for _ in ()).throw(SimulatedCrash())
                        if point == "AFTER_RENAME_BEFORE_INDEX"
                        else None
                    ),
                )

            original_append = JsonlStore.append
            marker = OSError("post-index-close")

            def append_then_raise(store, relative_path, records, *, kind=None):
                result = original_append(store, relative_path, records, kind=kind)
                if relative_path == "runs.jsonl":
                    raise marker
                return result

            with patch("scout_mvp.step2_transaction.JsonlStore.append", new=append_then_raise):
                reconciled = __import__(
                    "scout_mvp.step2_transaction", fromlist=["reconcile_committed_transaction"]
                ).reconcile_committed_transaction(root, RUN_ID)
            self.assertEqual(reconciled.run, run)
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [run])


if __name__ == "__main__":
    unittest.main()
