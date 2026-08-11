import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scout_mvp.contracts import validate_document
from scout_mvp.storage import JsonlStore
from scout_mvp.step2_sources import HERMES_RELEASES_SOURCE


STARTED_AT = "2026-08-11T00:00:00Z"
FINISHED_AT = "2026-08-11T00:00:01Z"


def release_payload(*, count=2):
    return [
        {
            "id": 1000 + index,
            "tag_name": f"v2026.8.{index}",
            "name": f"Hermes 2026.8.{index}",
            "body": f"Release {index} adds a useful change.",
            "html_url": f"https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.{index}",
            "published_at": f"2026-08-{10 + index:02d}T00:00:00Z",
            "draft": False,
            "prerelease": False,
        }
        for index in range(1, count + 1)
    ]


class FixedClock:
    def __init__(self, *values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class OfflineStep2TestCase(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch(
            "socket.socket.connect",
            side_effect=AssertionError("socket network is forbidden in Step 2 tests"),
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)


class Step2ArtifactUniquenessTests(unittest.TestCase):
    def test_duplicate_artifact_and_linked_identities_are_rejected_before_persistence(self):
        from scout_mvp.step2_run import _validate_artifact_uniqueness

        base = {
            "source": {"id": "source-1"},
            "observations": [{"id": "observation-1", "source_id": "source-1", "external_id": "ext-1"}],
            "events": [{"id": "event-1", "observation_ids": ["observation-1"]}],
            "gates": [{"id": "gate-1", "event_id": "event-1", "locked_facts": [{"observation_ids": ["observation-1"]}]}],
            "candidates": [{"event_id": "event-1", "factual_gate_id": "gate-1"}],
            "decisions": [{"id": "decision-1", "event_id": "event-1", "factual_gate_id": "gate-1"}],
            "cards": [{"id": "card-1", "event_id": "event-1", "decision_id": "decision-1", "factual_gate_id": "gate-1"}],
        }
        _validate_artifact_uniqueness(**base)

        duplicate_cases = {
            "source": {**base, "observations": [{"id": "source-1", "source_id": "source-1", "external_id": "ext-1"}]},
            "observations": {**base, "observations": [*base["observations"], {"id": "observation-2", "source_id": "source-1", "external_id": "ext-1"}]},
            "events": {**base, "events": [*base["events"], {"id": "event-2", "observation_ids": ["observation-1"]}]},
            "gates": {**base, "gates": [*base["gates"], {"id": "gate-2", "event_id": "event-1", "locked_facts": []}]},
            "candidates": {**base, "candidates": [*base["candidates"], {"event_id": "event-1", "factual_gate_id": "gate-2"}]},
            "decisions": {**base, "decisions": [*base["decisions"], {"id": "decision-2", "event_id": "event-1", "factual_gate_id": "gate-2"}]},
            "cards": {**base, "cards": [*base["cards"], {"id": "card-2", "event_id": "event-1", "decision_id": "decision-2", "factual_gate_id": "gate-2"}]},
        }
        for label, case in duplicate_cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "duplicate"):
                _validate_artifact_uniqueness(**case)

    def test_manual_run_persists_the_complete_observation_to_card_contract_path(self):
        from scout_mvp.step2_run import run_step2

        payload = json.dumps(release_payload(), separators=(",", ":")).encode("utf-8")
        fetch_calls = []
        model_calls = []

        def fetcher(url):
            fetch_calls.append(url)
            return payload, 200

        def model_runner(command, **kwargs):
            model_calls.append((command, kwargs))
            prompt = json.loads(command[-1])
            results = [
                {
                    "event_id": item["event_id"],
                    "decision": "SHOW",
                    "thematic_fit": "DIRECT",
                    "materiality": "HIGH",
                    "attention": "NOW",
                    "reason_code": "ACTIVE_HERMES_RELEASE",
                    "rationale": "Cette release est pertinente pour mon usage actif de Hermes.",
                }
                for item in prompt["candidates"]
            ]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": results}),
                stderr="session_id: 20260811_000002_abcdef\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            result = run_step2(
                state_root,
                profile_id="example-profile",
                profile_context=["Example user evaluating local-first developer tooling"],
                clock=FixedClock(STARTED_AT, FINISHED_AT),
                invocation_id_factory=lambda: "manual-001",
                fetcher=fetcher,
                model_runner=model_runner,
            )

            self.assertEqual(fetch_calls, [HERMES_RELEASES_SOURCE["url"]])
            self.assertEqual(len(model_calls), 1)
            self.assertIn("gpt-5.6-sol", model_calls[0][0])
            model_prompt = json.loads(model_calls[0][0][-1])
            self.assertEqual(model_prompt["profile"], {
                "id": "example-profile",
                "context": ["Example user evaluating local-first developer tooling"],
            })
            self.assertEqual(result.run["profile_id"], "example-profile")
            self.assertEqual({card["profile_id"] for card in result.cards}, {"example-profile"})
            self.assertEqual({card["delivered_to"] for card in result.cards}, {"local"})
            self.assertEqual(result.model_session_id, "20260811_000002_abcdef")
            self.assertEqual(result.run["status"], "SUCCESS")
            self.assertEqual(result.run["network_calls"], 2)
            self.assertEqual(result.run["counts"], {
                "sources": 1,
                "observations": 2,
                "events": 2,
                "cards": 2,
                "errors": 0,
            })
            self.assertEqual(result.run["source_ids"], [result.source["id"]])
            self.assertEqual(result.run["observation_ids"], [item["id"] for item in result.observations])
            self.assertEqual(result.run["event_ids"], [item["id"] for item in result.events])
            self.assertEqual(result.run["card_ids"], [item["id"] for item in result.cards])
            self.assertEqual({card["run_id"] for card in result.cards}, {result.run["id"]})
            self.assertEqual(result.run["model"], "gpt-5.6-sol")
            expected_session_hash = hashlib.sha256(result.model_session_id.encode("utf-8")).hexdigest()
            self.assertEqual(result.run.get("notes"), f"model_session_sha256:{expected_session_hash}")
            self.assertNotIn(result.model_session_id, result.run["notes"])
            self.assertEqual(result.run["started_at"], STARTED_AT)
            self.assertEqual(result.run["finished_at"], FINISHED_AT)

            store = JsonlStore(state_root)
            run_dir = state_root / result.run["id"]
            artifacts = {
                "sources.jsonl": ("SourceV1", [result.source]),
                "observations.jsonl": ("ObservationV1", result.observations),
                "events.jsonl": ("EventV1", result.events),
                "factual-gates.jsonl": ("FactualGateV1", result.gates),
                "decisions.jsonl": ("DecisionV1", result.decisions),
                "cards.jsonl": ("CardV1", result.cards),
            }
            for filename, (kind, expected) in artifacts.items():
                path = run_dir / filename
                self.assertTrue(path.is_file(), filename)
                for document in expected:
                    validate_document(kind, document)
                self.assertEqual(store.read(f"{result.run['id']}/{filename}", kind=kind), expected)
                raw_lines = path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(raw_lines), len(expected))
                self.assertEqual(
                    raw_lines,
                    [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in expected],
                )

            self.assertEqual(store.read("runs.jsonl", kind="RunV1"), [result.run])
            self.assertEqual(
                store.read(f"{result.run['id']}/run.jsonl", kind="RunV1"),
                [result.run],
            )
            manifest = store.read(f"{result.run['id']}/transaction.jsonl")
            self.assertEqual(len(manifest), 1)
            self.assertEqual(manifest[0]["run_id"], result.run["id"])
            self.assertEqual(manifest[0]["state"], "PREPARED")
            self.assertFalse(any(b"session_id" in path.read_bytes() for path in run_dir.glob("*.jsonl") if path.name != "runs.jsonl"))
            self.assertNotIn("candidates", " ".join(path.name for path in run_dir.iterdir()))


class Step2RunFailureTests(OfflineStep2TestCase):
    def test_retry_same_run_returns_committed_transaction_without_external_calls_or_duplicates(self):
        from scout_mvp.step2_run import run_step2

        payload = json.dumps(release_payload(count=1), separators=(",", ":")).encode("utf-8")

        def model_runner(command, **kwargs):
            event_id = json.loads(command[-1])["candidates"][0]["event_id"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": [{
                    "event_id": event_id,
                    "decision": "SHOW",
                    "thematic_fit": "DIRECT",
                    "materiality": "HIGH",
                    "attention": "NOW",
                    "reason_code": "IDEMPOTENT_RETRY",
                    "rationale": "Décision de fixture pour tester un retry sans duplication.",
                }]}),
                stderr="session_id: 20260811_000001_abcdef\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            first = run_step2(
                root,
                clock=FixedClock(STARTED_AT, FINISHED_AT),
                invocation_id_factory=lambda: "same-invocation",
                fetcher=lambda url: (payload, 200),
                model_runner=model_runner,
            )
            retry = run_step2(
                root,
                clock=FixedClock(STARTED_AT),
                invocation_id_factory=lambda: "same-invocation",
                fetcher=lambda url: self.fail("retry must not collect"),
                model_runner=lambda *args, **kwargs: self.fail("retry must not call model"),
            )

            self.assertEqual(retry.run, first.run)
            self.assertEqual(retry.cards, first.cards)
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [first.run])
            self.assertEqual(
                JsonlStore(root).read(f"{first.run['id']}/cards.jsonl", kind="CardV1"),
                first.cards,
            )

    def test_retry_committed_failed_prefix_returns_step2_error_without_external_calls(self):
        from scout_mvp.step2_gate import build_factual_gate as real_build_gate
        from scout_mvp.step2_run import Step2RunError, run_step2

        payload = json.dumps(release_payload(count=2), separators=(",", ":")).encode("utf-8")
        calls = 0

        def fail_on_second_gate(event, observation):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("injected deterministic-stage failure")
            return real_build_gate(event, observation)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with patch(
                "scout_mvp.step2_run.build_factual_gate",
                side_effect=fail_on_second_gate,
            ), self.assertRaises(Step2RunError) as first:
                run_step2(
                    root,
                    clock=FixedClock(STARTED_AT, FINISHED_AT),
                    invocation_id_factory=lambda: "failed-prefix",
                    fetcher=lambda url: (payload, 200),
                    model_runner=lambda *args, **kwargs: self.fail("model must not run"),
                )

            failed_run = first.exception.run
            self.assertEqual(failed_run["status"], "FAILED")
            store = JsonlStore(root)
            self.assertEqual(len(store.read(f"{failed_run['id']}/events.jsonl", kind="EventV1")), 2)
            self.assertEqual(
                len(store.read(f"{failed_run['id']}/factual-gates.jsonl", kind="FactualGateV1")),
                1,
            )

            with self.assertRaises(Step2RunError) as retry:
                run_step2(
                    root,
                    clock=FixedClock(STARTED_AT),
                    invocation_id_factory=lambda: "failed-prefix",
                    fetcher=lambda url: self.fail("retry must not collect"),
                    model_runner=lambda *args, **kwargs: self.fail("retry must not run model"),
                )
            self.assertEqual(retry.exception.run, failed_run)
            self.assertIsNone(retry.exception.cause)
            self.assertEqual(store.read("runs.jsonl", kind="RunV1"), [failed_run])

    def test_orchestrator_crash_points_recover_one_coherent_success_without_duplicates(self):
        from scout_mvp.ids import run_id
        from scout_mvp.step2_run import run_step2
        from scout_mvp.step2_transaction import recover_step2_transaction

        class SimulatedCrash(BaseException):
            pass

        payload = json.dumps(release_payload(count=1), separators=(",", ":")).encode("utf-8")

        def model_runner(command, **kwargs):
            event_id = json.loads(command[-1])["candidates"][0]["event_id"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": [{
                    "event_id": event_id,
                    "decision": "SHOW",
                    "thematic_fit": "DIRECT",
                    "materiality": "HIGH",
                    "attention": "NOW",
                    "reason_code": "CRASH_RECOVERY",
                    "rationale": "Décision de fixture pour tester la récupération transactionnelle.",
                }]}),
                stderr="session_id: 20260811_000001_abcdef\n",
            )

        for point in (
            "AFTER_VALIDATE_BEFORE_RENAME",
            "AFTER_RENAME_BEFORE_INDEX",
            "AFTER_INDEX",
        ):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                state_root = Path(directory) / "state"
                invocation = f"integration-{point.lower()}"
                identifier = run_id("example-profile", STARTED_AT, invocation)

                def crash_hook(observed):
                    if observed == point:
                        raise SimulatedCrash(point)

                with self.assertRaises(SimulatedCrash):
                    run_step2(
                        state_root,
                        clock=FixedClock(STARTED_AT, FINISHED_AT),
                        invocation_id_factory=lambda: invocation,
                        fetcher=lambda url: (payload, 200),
                        model_runner=model_runner,
                        transaction_crash_hook=crash_hook,
                    )

                retry = run_step2(
                    state_root,
                    clock=FixedClock(STARTED_AT),
                    invocation_id_factory=lambda: invocation,
                    fetcher=lambda url: self.fail("crash retry must not collect"),
                    model_runner=lambda *args, **kwargs: self.fail(
                        "crash retry must not call model"
                    ),
                )
                self.assertEqual(retry.run["id"], identifier)
                store = JsonlStore(state_root)
                runs = store.read("runs.jsonl", kind="RunV1")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["id"], identifier)
                self.assertEqual(runs[0]["status"], "SUCCESS")
                self.assertEqual(
                    store.read(f"{identifier}/run.jsonl", kind="RunV1"),
                    runs,
                )
                self.assertEqual(len(store.read(f"{identifier}/cards.jsonl", kind="CardV1")), 1)
                self.assertEqual(recover_step2_transaction(state_root, identifier), "COMMITTED")
                self.assertEqual(store.read("runs.jsonl", kind="RunV1"), runs)

    def test_precommit_index_failure_reports_committed_success_until_recovery_indexes_it(self):
        from scout_mvp.step2_run import Step2RunError, run_step2
        from scout_mvp.step2_transaction import recover_step2_transaction

        payload = json.dumps(release_payload(count=1), separators=(",", ":")).encode("utf-8")
        original_append = JsonlStore.append
        marker = OSError("precommit-index-failure")

        def fail_index(store, relative_path, records, *, kind=None):
            if str(relative_path) == "runs.jsonl":
                raise marker
            return original_append(store, relative_path, records, kind=kind)

        def model_runner(command, **kwargs):
            event_id = json.loads(command[-1])["candidates"][0]["event_id"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": [{
                    "event_id": event_id,
                    "decision": "SHOW",
                    "thematic_fit": "DIRECT",
                    "materiality": "HIGH",
                    "attention": "NOW",
                    "reason_code": "INDEX_RECOVERY",
                    "rationale": "Décision de fixture pour réconcilier l'index dérivé.",
                }]}),
                stderr="session_id: 20260811_000001_abcdef\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with patch("scout_mvp.step2_transaction.JsonlStore.append", new=fail_index):
                with self.assertRaises(Step2RunError) as raised:
                    run_step2(
                        root,
                        clock=FixedClock(STARTED_AT, FINISHED_AT),
                        invocation_id_factory=lambda: "index-precommit",
                        fetcher=lambda url: (payload, 200),
                        model_runner=model_runner,
                    )

            error = raised.exception
            identifier = error.run["id"]
            self.assertEqual(error.run["status"], "SUCCESS")
            self.assertIs(error.cause, marker)
            self.assertIs(error.audit_persistence_error, marker)
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [])
            self.assertEqual(
                JsonlStore(root).read(f"{identifier}/run.jsonl", kind="RunV1"),
                [error.run],
            )
            self.assertEqual(recover_step2_transaction(root, identifier), "COMMITTED")
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [error.run])

    def test_postcommit_index_append_exception_is_reconciled_as_success(self):
        from scout_mvp.step2_run import run_step2

        payload = json.dumps(release_payload(count=1), separators=(",", ":")).encode("utf-8")

        def model_runner(command, **kwargs):
            event_id = json.loads(command[-1])["candidates"][0]["event_id"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": [{
                    "event_id": event_id,
                    "decision": "SHOW",
                    "thematic_fit": "DIRECT",
                    "materiality": "HIGH",
                    "attention": "NOW",
                    "reason_code": "POST_COMMIT_PROBE",
                    "rationale": "Décision de fixture pour réconcilier une écriture durable.",
                }]}),
                stderr="session_id: 20260811_000001_abcdef\n",
            )

        original_append = JsonlStore.append
        state = {"raised": False}

        def append_then_raise(store, relative_path, records, *, kind=None):
            result = original_append(store, relative_path, records, kind=kind)
            if str(relative_path) == "runs.jsonl" and not state["raised"]:
                state["raised"] = True
                raise OSError("postcommit-index")
            return result

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with patch("scout_mvp.step2_transaction.JsonlStore.append", new=append_then_raise):
                result = run_step2(
                    state_root,
                    clock=FixedClock(STARTED_AT, FINISHED_AT),
                    invocation_id_factory=lambda: "postcommit-index",
                    fetcher=lambda url: (payload, 200),
                    model_runner=model_runner,
                )

            self.assertEqual(result.run["status"], "SUCCESS")
            self.assertEqual(JsonlStore(state_root).read("runs.jsonl", kind="RunV1"), [result.run])
            self.assertEqual(
                JsonlStore(state_root).read(
                    f"{result.run['id']}/cards.jsonl", kind="CardV1"
                ),
                result.cards,
            )

    def test_each_precommit_artifact_failure_leaves_only_identifiable_staging(self):
        from scout_mvp.step2_run import Step2RunError, run_step2

        payload = json.dumps(release_payload(count=2), separators=(",", ":")).encode("utf-8")

        def model_runner(command, **kwargs):
            prompt = json.loads(command[-1])
            results = [
                {
                    "event_id": item["event_id"],
                    "decision": "SHOW",
                    "thematic_fit": "DIRECT",
                    "materiality": "HIGH",
                    "attention": "NOW",
                    "reason_code": "PERSISTENCE_PROBE",
                    "rationale": "Décision de fixture pour tester la persistance locale.",
                }
                for item in prompt["candidates"]
            ]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": results}),
                stderr="session_id: 20260811_000001_abcdef\n",
            )

        boundaries = (
            "sources.jsonl",
            "observations.jsonl",
            "events.jsonl",
            "factual-gates.jsonl",
            "decisions.jsonl",
            "cards.jsonl",
            "run.jsonl",
            "transaction.jsonl",
        )
        original_append = JsonlStore.append
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                marker = OSError(f"injected-{boundary}")

                def fail_once(store, relative_path, records, *, kind=None):
                    if str(relative_path).endswith(boundary):
                        raise marker
                    return original_append(store, relative_path, records, kind=kind)

                state_root = Path(directory) / "state"
                with patch("scout_mvp.step2_transaction.JsonlStore.append", new=fail_once):
                    with self.assertRaises(Step2RunError) as raised:
                        run_step2(
                            state_root,
                            clock=FixedClock(STARTED_AT, FINISHED_AT),
                            invocation_id_factory=lambda: f"manual-{boundary.replace('.', '-')}",
                            fetcher=lambda url: (payload, 200),
                            model_runner=model_runner,
                        )

                error = raised.exception
                self.assertIs(error.cause, marker)
                self.assertIs(error.audit_persistence_error, marker)
                self.assertEqual(error.run["status"], "SUCCESS")
                self.assertEqual(JsonlStore(state_root).read("runs.jsonl", kind="RunV1"), [])
                self.assertFalse((state_root / error.run["id"]).exists())
                self.assertTrue((state_root / ".staging" / error.run["id"]).is_dir())

    def test_collector_failure_persists_only_the_frozen_source_and_failed_run(self):
        from scout_mvp.step2_run import Step2RunError, run_step2

        def fetcher(url):
            raise OSError("fixture secret must not be persisted")

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with self.assertRaises(Step2RunError) as raised:
                run_step2(
                    state_root,
                    clock=FixedClock(STARTED_AT, FINISHED_AT),
                    invocation_id_factory=lambda: "manual-collector-failure",
                    fetcher=fetcher,
                    model_runner=lambda *args, **kwargs: self.fail("model must not run"),
                )

            error = raised.exception
            self.assertEqual(error.state_root, state_root)
            self.assertEqual(error.run["status"], "FAILED")
            self.assertEqual(error.run["counts"], {
                "sources": 1,
                "observations": 0,
                "events": 0,
                "cards": 0,
                "errors": 1,
            })
            self.assertEqual(error.run["network_calls"], 1)
            self.assertEqual(error.run["errors"], ["collector_failed"])
            store = JsonlStore(state_root)
            run_id = error.run["id"]
            run_dir = state_root / run_id
            self.assertEqual(store.read("runs.jsonl", kind="RunV1"), [error.run])
            self.assertEqual(store.read(f"{run_id}/sources.jsonl", kind="SourceV1"), [HERMES_RELEASES_SOURCE])
            self.assertEqual(store.read(f"{run_id}/run.jsonl", kind="RunV1"), [error.run])
            self.assertEqual(
                store.read(f"{run_id}/transaction.jsonl")[0]["run_id"],
                run_id,
            )
            self.assertTrue((run_dir / "sources.jsonl").is_file())
            for filename in (
                "observations.jsonl",
                "events.jsonl",
                "factual-gates.jsonl",
                "decisions.jsonl",
                "cards.jsonl",
            ):
                self.assertFalse((run_dir / filename).exists(), filename)
            self.assertNotIn(b"fixture secret", (state_root / "runs.jsonl").read_bytes())

    def test_keyboard_interrupt_from_collector_propagates_and_is_not_persisted(self):
        from scout_mvp.step2_run import run_step2

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with self.assertRaises(KeyboardInterrupt):
                run_step2(
                    state_root,
                    clock=FixedClock(STARTED_AT, FINISHED_AT),
                    invocation_id_factory=lambda: "manual-keyboard-interrupt",
                    fetcher=lambda url: (_ for _ in ()).throw(KeyboardInterrupt()),
                    model_runner=lambda *args, **kwargs: self.fail("model must not run"),
                )
            self.assertEqual(JsonlStore(state_root).read("runs.jsonl", kind="RunV1"), [])

    def test_system_exit_from_model_propagates_and_is_not_persisted(self):
        from scout_mvp.step2_run import run_step2

        payload = json.dumps(release_payload(count=1), separators=(",", ":")).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with self.assertRaises(SystemExit):
                run_step2(
                    state_root,
                    clock=FixedClock(STARTED_AT, FINISHED_AT),
                    invocation_id_factory=lambda: "manual-system-exit",
                    fetcher=lambda url: (payload, 200),
                    model_runner=lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit(7)),
                )
            self.assertEqual(JsonlStore(state_root).read("runs.jsonl", kind="RunV1"), [])

    def test_model_failure_persists_deterministic_artifacts_without_decisions_or_cards(self):
        from scout_mvp.step2_run import Step2RunError, run_step2

        payload = json.dumps(release_payload(count=2), separators=(",", ":")).encode("utf-8")
        model_calls = []

        def model_runner(command, **kwargs):
            model_calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=2,
                stdout="model output must not be persisted",
                stderr="provider secret must not be persisted",
            )

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with self.assertRaises(Step2RunError) as raised:
                run_step2(
                    state_root,
                    clock=FixedClock(STARTED_AT, FINISHED_AT),
                    invocation_id_factory=lambda: "manual-model-failure",
                    fetcher=lambda url: (payload, 200),
                    model_runner=model_runner,
                )

            error = raised.exception
            self.assertEqual(len(model_calls), 1)
            self.assertEqual(error.run["status"], "FAILED")
            self.assertEqual(error.run["network_calls"], 2)
            self.assertEqual(error.run["counts"], {
                "sources": 1,
                "observations": 2,
                "events": 2,
                "cards": 0,
                "errors": 1,
            })
            store = JsonlStore(state_root)
            run_id = error.run["id"]
            run_dir = state_root / run_id
            self.assertEqual(store.read(f"{run_id}/sources.jsonl", kind="SourceV1"), [HERMES_RELEASES_SOURCE])
            self.assertEqual(len(store.read(f"{run_id}/observations.jsonl", kind="ObservationV1")), 2)
            self.assertEqual(len(store.read(f"{run_id}/events.jsonl", kind="EventV1")), 2)
            self.assertEqual(len(store.read(f"{run_id}/factual-gates.jsonl", kind="FactualGateV1")), 2)
            self.assertFalse((run_dir / "decisions.jsonl").exists())
            self.assertFalse((run_dir / "cards.jsonl").exists())
            state_bytes = b"".join(path.read_bytes() for path in [state_root / "runs.jsonl", *run_dir.glob("*.jsonl")])
            self.assertNotIn(b"model output must not be persisted", state_bytes)
            self.assertNotIn(b"provider secret must not be persisted", state_bytes)

    def test_stale_release_skips_sol_and_persists_a_successful_zero_card_run(self):
        from scout_mvp.step2_run import run_step2

        stale = release_payload(count=1)
        stale[0]["published_at"] = "2026-06-01T00:00:00Z"
        payload = json.dumps(stale, separators=(",", ":")).encode("utf-8")
        model_calls = []

        def model_runner(*args, **kwargs):
            model_calls.append((args, kwargs))
            raise AssertionError("stale release must not invoke Sol")

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            result = run_step2(
                state_root,
                clock=FixedClock(STARTED_AT, FINISHED_AT),
                invocation_id_factory=lambda: "manual-stale",
                fetcher=lambda url: (payload, 200),
                model_runner=model_runner,
            )

            self.assertEqual(model_calls, [])
            self.assertEqual(result.candidates, [])
            self.assertEqual(result.decisions, [])
            self.assertEqual(result.cards, [])
            self.assertEqual(result.gates[0]["gate_action"], "HOLD")
            self.assertEqual(result.run["status"], "SUCCESS")
            self.assertEqual(result.run["network_calls"], 1)
            self.assertEqual(result.run["counts"]["cards"], 0)
            run_dir = state_root / result.run["id"]
            self.assertFalse((run_dir / "decisions.jsonl").exists())
            self.assertFalse((run_dir / "cards.jsonl").exists())
            self.assertEqual(JsonlStore(state_root).read("runs.jsonl", kind="RunV1"), [result.run])

    def test_manual_run_caps_three_cards_and_keeps_a_deterministic_adjacent_serendipity_path(self):
        from scout_mvp.step2_run import run_step2

        payload_items = release_payload(count=5)
        for index, item in enumerate(payload_items):
            item["published_at"] = f"2026-08-{10 - index:02d}T00:00:00Z"
        payload = json.dumps(payload_items, separators=(",", ":")).encode("utf-8")

        def model_runner(command, **kwargs):
            prompt = json.loads(command[-1])
            results = []
            for index, item in enumerate(prompt["candidates"], start=1):
                if index <= 3:
                    values = {
                        "decision": "SHOW",
                        "thematic_fit": "DIRECT",
                        "materiality": "HIGH",
                        "attention": "NOW",
                        "reason_code": "DIRECT_RELEASE",
                    }
                elif index == 4:
                    values = {
                        "decision": "SHOW",
                        "thematic_fit": "ADJACENT",
                        "materiality": "MEDIUM",
                        "attention": "LATER",
                        "reason_code": "ADJACENT_LEARNING",
                    }
                else:
                    values = {
                        "decision": "SHOW",
                        "thematic_fit": "DIRECT",
                        "materiality": "LOW",
                        "attention": "LATER",
                        "reason_code": "LOW_PRIORITY_RELEASE",
                    }
                results.append({
                    "event_id": item["event_id"],
                    **values,
                    "rationale": "Cette release apporte une information exploitable ou un apprentissage adjacent.",
                })
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"results": results}),
                stderr="session_id: 20260811_000003_abcdef\n",
            )

        def run_in(root):
            return run_step2(
                root,
                clock=FixedClock(STARTED_AT, FINISHED_AT),
                invocation_id_factory=lambda: "manual-ranking",
                fetcher=lambda url: (payload, 200),
                model_runner=model_runner,
            )

        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = run_in(Path(first_dir) / "state")
            second = run_in(Path(second_dir) / "state")

            expected_event_ids = [first.events[0]["id"], first.events[1]["id"], first.events[3]["id"]]
            self.assertEqual(len(first.cards), 3)
            self.assertEqual([card["event_id"] for card in first.cards], expected_event_ids)
            self.assertEqual([card["rank"] for card in first.cards], [1, 2, 3])
            self.assertEqual(first.cards[2]["badges"], ["RELEASE", "ADJACENT", "LATER"])
            self.assertEqual([card["event_id"] for card in second.cards], expected_event_ids)
            for card in first.cards:
                self.assertEqual(card["run_id"], first.run["id"])
                validate_document("CardV1", card)


class Step2CliTests(OfflineStep2TestCase):
    def test_cli_defaults_to_the_bounded_internal_model_runner(self):
        from scripts.run_step2 import main
        import io

        captured = {}
        state_root = Path("/tmp/scout-step2-cli-bounded-test")
        config_path = Path("/tmp/scout-step2-cli-bounded-test-config.json")
        config_path.write_text(json.dumps({
            "state_root": str(state_root),
            "profile_id": "example-profile",
            "profile_context": ["Example profile context"],
        }), encoding="utf-8")
        self.addCleanup(config_path.unlink, missing_ok=True)
        fake_result = SimpleNamespace(
            state_root=state_root,
            run={
                "id": "run-cli-bounded",
                "status": "SUCCESS",
                "counts": {
                    "sources": 1,
                    "observations": 0,
                    "events": 0,
                    "cards": 0,
                    "errors": 0,
                },
            },
            cards=[],
            decisions=[],
            model_session_id=None,
        )

        def run_fn(*args, **kwargs):
            captured.update(kwargs)
            return fake_result

        self.assertEqual(
            main(
                ["--config", str(config_path)],
                run_fn=run_fn,
                stdout=io.StringIO(),
                clock="clock",
                invocation_id_factory="invocation",
                fetcher="fetcher",
            ),
            0,
        )
        self.assertIsNone(captured["model_runner"])

    def test_cli_requires_only_absolute_state_root_and_injected_entry_runs_once(self):
        from scripts.run_step2 import main

        calls = []
        state_root = Path("/tmp/scout-step2-cli-test")
        config_path = Path("/tmp/scout-step2-cli-test-config.json")
        config_path.write_text(json.dumps({
            "state_root": str(state_root),
            "profile_id": "example-profile",
            "profile_context": ["Example profile context"],
        }), encoding="utf-8")
        self.addCleanup(config_path.unlink, missing_ok=True)
        fake_result = SimpleNamespace(
            state_root=state_root,
            run={
                "id": "run-cli-1",
                "status": "SUCCESS",
                "counts": {
                    "sources": 1,
                    "observations": 1,
                    "events": 1,
                    "cards": 1,
                    "errors": 0,
                },
            },
            cards=[{"id": "card-1"}],
            decisions=[{"id": "decision-1"}],
            model_session_id="20260811_000004_abcdef",
        )

        def run_fn(*args, **kwargs):
            calls.append((args, kwargs))
            return fake_result

        import io

        output = io.StringIO()
        self.assertEqual(
            main(
                ["--config", str(config_path)],
                run_fn=run_fn,
                stdout=output,
                clock="clock",
                invocation_id_factory="invocation",
                fetcher="fetcher",
                model_runner="model",
            ),
            0,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], (str(state_root),))
        self.assertEqual(calls[0][1], {
            "profile_id": "example-profile",
            "profile_context": ["Example profile context"],
            "clock": "clock",
            "invocation_id_factory": "invocation",
            "fetcher": "fetcher",
            "model_runner": "model",
        })
        self.assertEqual(json.loads(output.getvalue()), {
            "run_id": "run-cli-1",
            "status": "SUCCESS",
            "counts": fake_result.run["counts"],
            "model_session_id": "20260811_000004_abcdef",
            "card_paths": [str(state_root / "run-cli-1" / "cards.jsonl")],
            "decision_paths": [str(state_root / "run-cli-1" / "decisions.jsonl")],
        })
        with self.assertRaises(SystemExit):
            main(["--state-root", str(state_root), "--source", "other"], run_fn=run_fn)
        with self.assertRaises(SystemExit):
            main(["--state-root", "relative-state"], run_fn=run_fn)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
