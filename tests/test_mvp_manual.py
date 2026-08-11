import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scout_mvp.contracts import validate_document
from scout_mvp.ids import run_id
from scout_mvp.manual import main, run_manual
from scout_mvp.storage import JsonlStore


STARTED = "2026-08-11T00:00:00Z"
FINISHED = "2026-08-11T00:00:01Z"


class SequenceClock:
    def __init__(self, *values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class ManualRunTests(unittest.TestCase):
    def test_callable_zero_card_run_uses_clock_appends_one_valid_envelope_and_makes_no_network_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            clock = SequenceClock(STARTED, FINISHED)

            with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
                run = run_manual(
                    root,
                    profile_id="example-profile",
                    clock=clock,
                    invocation_id_factory=lambda: "manual-1",
                )

            self.assertEqual(run["id"], run_id("example-profile", STARTED, "manual-1"))
            self.assertEqual(run["profile_id"], "example-profile")
            self.assertEqual(run["invocation_id"], "manual-1")
            self.assertEqual(run["status"], "SUCCESS")
            self.assertEqual(run["started_at"], STARTED)
            self.assertEqual(run["finished_at"], FINISHED)
            self.assertEqual(run["card_ids"], [])
            self.assertEqual(run["counts"]["cards"], 0)
            self.assertEqual(run["network_calls"], 0)
            self.assertIsNone(validate_document("RunV1", run))

            stored = JsonlStore(root).read("runs.jsonl", kind="RunV1")
            self.assertEqual(stored, [run])

    def test_same_clock_values_use_distinct_injected_invocation_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            invocation_ids = iter(("manual-a", "manual-b"))
            clock = lambda: STARTED

            first = run_manual(
                root,
                clock=clock,
                invocation_id_factory=lambda: next(invocation_ids),
            )
            second = run_manual(
                root,
                clock=clock,
                invocation_id_factory=lambda: next(invocation_ids),
            )

            self.assertEqual(first["invocation_id"], "manual-a")
            self.assertEqual(second["invocation_id"], "manual-b")
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(first["id"], run_id("example-profile", STARTED, "manual-a"))
            self.assertEqual(second["id"], run_id("example-profile", STARTED, "manual-b"))
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [first, second])

    def test_cli_zero_card_run_prints_the_same_valid_envelope_and_appends_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            output = io.StringIO()

            with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
                exit_code = main(
                    ["--root", str(root), "--profile-id", "example-profile"],
                    clock=lambda: STARTED,
                    invocation_id_factory=lambda: "manual-cli",
                    stdout=output,
                )

            self.assertEqual(exit_code, 0)
            printed = json.loads(output.getvalue())
            self.assertEqual(printed["invocation_id"], "manual-cli")
            self.assertEqual(printed["id"], run_id("example-profile", STARTED, "manual-cli"))
            self.assertEqual(printed["status"], "SUCCESS")
            self.assertEqual(printed["card_ids"], [])
            self.assertEqual(printed["network_calls"], 0)
            self.assertIsNone(validate_document("RunV1", printed))
            self.assertEqual(JsonlStore(root).read("runs.jsonl", kind="RunV1"), [printed])


if __name__ == "__main__":
    unittest.main()
