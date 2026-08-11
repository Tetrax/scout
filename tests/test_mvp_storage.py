import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scout_mvp.storage import JsonlStore, StorageRollbackError


PROFILE = {
    "id": "example-profile",
    "version": "1",
    "priorities": ["Example Topic"],
    "critical_scope": ["security advisories"],
}


class JsonlStoreTests(unittest.TestCase):
    def test_exact_two_appends_preserve_order_compact_sorting_newlines_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            first = dict(PROFILE, id="profile-1", version="1", priorities=["A"])
            second = dict(PROFILE, id="profile-2", version="2", priorities=["B"])
            relative_path = "profiles.jsonl"

            with patch("scout_mvp.storage.os.fsync", wraps=os.fsync) as fsync:
                self.assertEqual(store.append(relative_path, [first]), 1)
                self.assertEqual(store.append(relative_path, [second]), 1)
                self.assertEqual(fsync.call_count, 2)

            path = root / relative_path
            expected = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for record in (first, second)
            ).encode("utf-8")
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(store.read(relative_path), [first, second])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_absolute_and_traversal_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            record = PROFILE

            with self.assertRaises(ValueError):
                store.append(Path(directory) / "outside.jsonl", [record], kind="ProfileV1")
            with self.assertRaises(ValueError):
                store.append("nested/../outside.jsonl", [record], kind="ProfileV1")
            with self.assertRaises(ValueError):
                store.append("../outside.jsonl", [record], kind="ProfileV1")

            self.assertFalse((Path(directory) / "outside.jsonl").exists())
            self.assertFalse((root / "outside.jsonl").exists())

    def test_parent_symlink_is_rejected_for_append_and_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "nested").symlink_to(outside, target_is_directory=True)
            store = JsonlStore(root)

            with self.assertRaises(ValueError):
                store.append("nested/profiles.jsonl", [PROFILE], kind="ProfileV1")
            with self.assertRaises(ValueError):
                store.read("nested/profiles.jsonl", kind="ProfileV1")

    def test_final_symlink_replacement_between_validation_and_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            outside = Path(directory) / "outside.jsonl"
            root.mkdir()
            outside.write_bytes(b"")
            target = root / "profiles.jsonl"
            store = JsonlStore(root)
            real_open = os.open
            swapped = False

            def replace_with_symlink(path, *args, **kwargs):
                nonlocal swapped
                if not swapped and Path(path).name == target.name:
                    target.symlink_to(outside)
                    swapped = True
                return real_open(path, *args, **kwargs)

            with patch("scout_mvp.storage.os.open", side_effect=replace_with_symlink):
                with self.assertRaises(ValueError):
                    store.append("profiles.jsonl", [PROFILE], kind="ProfileV1")

            self.assertTrue(swapped)
            self.assertEqual(outside.read_bytes(), b"")

    def test_fifo_target_is_rejected_for_append_and_read(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            target = root / "profiles.jsonl"
            os.mkfifo(target)
            store = JsonlStore(root)

            with self.assertRaises(ValueError):
                store.append("profiles.jsonl", [PROFILE], kind="ProfileV1")
            with self.assertRaises(ValueError):
                store.read("profiles.jsonl", kind="ProfileV1")

    def test_reader_rejects_blank_truncated_and_invalid_jsonl(self):
        invalid_payloads = (b"{\"id\":\"one\"}", b"{\"id\":\"one\"}\n\n", b"not-json\n")
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "state"
                    root.mkdir()
                    (root / "records.jsonl").write_bytes(payload)
                    with self.assertRaises(ValueError):
                        JsonlStore(root).read("records.jsonl")

    def test_append_rejects_malformed_existing_jsonl_before_mutation(self):
        malformed_payloads = (b"{\"id\":\"old\"}", b"{\"id\":\"old\"}\n\n", b"not-json\n")
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "state"
                    root.mkdir()
                    path = root / "records.jsonl"
                    path.write_bytes(payload)
                    before = path.read_bytes()
                    with self.assertRaises(ValueError):
                        JsonlStore(root).append("records.jsonl", [dict(PROFILE, id="new")])
                    self.assertEqual(path.read_bytes(), before)

    def test_duplicate_ids_within_batch_are_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            records = [dict(PROFILE, id="same"), dict(PROFILE, id="same")]

            with self.assertRaises(ValueError):
                JsonlStore(root).append("records.jsonl", records)

            self.assertFalse(root.exists())

    def test_non_object_batch_records_are_rejected_before_root_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"

            with self.assertRaises(ValueError):
                JsonlStore(root).append("records.jsonl", [None])

            self.assertFalse(root.exists())

    def test_duplicate_id_against_existing_record_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            record = dict(PROFILE, id="same")
            store.append("records.jsonl", [record])
            before = (root / "records.jsonl").read_bytes()

            with self.assertRaises(ValueError):
                store.append("records.jsonl", [record])

            self.assertEqual((root / "records.jsonl").read_bytes(), before)
            self.assertEqual(store.read("records.jsonl"), [record])

    def test_partial_os_writes_still_append_the_exact_serialized_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            record = dict(PROFILE, id="partial")
            store = JsonlStore(root)
            real_write = os.write

            def partial_write(fd, data):
                amount = min(3, len(data))
                return real_write(fd, data[:amount])

            with patch("scout_mvp.storage.os.write", side_effect=partial_write) as write:
                self.assertEqual(store.append("records.jsonl", [record]), 1)

            self.assertGreater(write.call_count, 1)
            expected = (
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            self.assertEqual((root / "records.jsonl").read_bytes(), expected)

    def test_reader_rejects_duplicate_json_keys_and_duplicate_record_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            path = root / "records.jsonl"
            store = JsonlStore(root)

            path.write_bytes(b'{"id":"first","id":"second"}\n')
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                store.read("records.jsonl")

            path.write_bytes(b'{"id":"same"}\n{"id":"same"}\n')
            with self.assertRaisesRegex(ValueError, "duplicate id in JSONL"):
                store.read("records.jsonl")

    def test_write_failure_rolls_back_a_partial_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            first = dict(PROFILE, id="first")
            second = dict(PROFILE, id="second")
            store.append("records.jsonl", [first])
            path = root / "records.jsonl"
            before = path.read_bytes()
            real_write = os.write
            state = {"failed": False}

            def fail_after_partial_write(fd, data):
                if not state["failed"]:
                    state["failed"] = True
                    real_write(fd, data[:2])
                    raise OSError("simulated write failure")
                raise AssertionError("write retried after simulated failure")

            with patch("scout_mvp.storage.os.write", side_effect=fail_after_partial_write):
                with self.assertRaises(OSError):
                    store.append("records.jsonl", [second])

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store.read("records.jsonl"), [first])

    def test_rollback_failure_is_surfaced_with_original_append_as_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            first = dict(PROFILE, id="first")
            second = dict(PROFILE, id="second")
            store.append("records.jsonl", [first])
            real_write = os.write

            def fail_after_partial_write(fd, data):
                real_write(fd, data[:2])
                raise OSError("original append failure")

            rollback_error = OSError("rollback truncate failure")
            with patch("scout_mvp.storage.os.write", side_effect=fail_after_partial_write), patch(
                "scout_mvp.storage.os.ftruncate", side_effect=rollback_error
            ):
                with self.assertRaises(StorageRollbackError) as raised:
                    store.append("records.jsonl", [second])

            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn(rollback_error, raised.exception.errors)
            with self.assertRaises(ValueError):
                store.read("records.jsonl")

    def test_process_control_append_error_propagates_even_if_rollback_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            store.append("records.jsonl", [dict(PROFILE, id="first")])
            real_write = os.write

            def interrupt_after_partial_write(fd, data):
                real_write(fd, data[:2])
                raise KeyboardInterrupt()

            with patch("scout_mvp.storage.os.write", side_effect=interrupt_after_partial_write), patch(
                "scout_mvp.storage.os.ftruncate", side_effect=OSError("rollback failed")
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    store.append("records.jsonl", [dict(PROFILE, id="second")])

            self.assertIsInstance(raised.exception.__cause__, StorageRollbackError)

    def test_fsync_failure_rolls_back_the_complete_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            first = dict(PROFILE, id="first")
            second = dict(PROFILE, id="second")
            store.append("records.jsonl", [first])
            path = root / "records.jsonl"
            before = path.read_bytes()
            real_fsync = os.fsync
            calls = {"count": 0}

            def fail_first_fsync(fd):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("simulated fsync failure")
                return real_fsync(fd)

            with patch("scout_mvp.storage.os.fsync", side_effect=fail_first_fsync):
                with self.assertRaises(OSError):
                    store.append("records.jsonl", [second])

            self.assertGreaterEqual(calls["count"], 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store.read("records.jsonl"), [first])

    def test_invalid_document_does_not_append_or_create_a_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            relative_path = "profiles.jsonl"
            store.append(relative_path, [PROFILE], kind="ProfileV1")
            before = (root / relative_path).read_bytes()
            invalid = dict(PROFILE, unexpected=True)

            with self.assertRaises(ValueError):
                store.append(relative_path, [invalid], kind="ProfileV1")

            self.assertEqual((root / relative_path).read_bytes(), before)
            self.assertEqual(store.read(relative_path, kind="ProfileV1"), [PROFILE])

    def test_invalid_document_is_validated_before_root_or_file_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = JsonlStore(root)
            invalid = dict(PROFILE, id="Invalid Profile")

            with self.assertRaises(ValueError):
                store.append("profiles.jsonl", [invalid], kind="ProfileV1")

            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
