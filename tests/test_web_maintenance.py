from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from scout_web.database import Database
from scout_web.maintenance import backup_database, restore_database, verify_database
from scout_web.sources import CollectedItem


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "scout.sqlite3"
        self.backup_path = self.root / "backups" / "scout-backup.sqlite3"
        self.db = Database(self.database_path)
        self.db.migrate()
        self.db.upsert_items(
            [
                CollectedItem(
                    source_id="fortinet_psirt",
                    external_id="FG-IR-26-163",
                    title="Original",
                    url="https://fortiguard.fortinet.com/psirt/FG-IR-26-163",
                    published_at="2026-09-04T00:00:00Z",
                    summary="Source facts.",
                    topics=("fortinet",),
                    story_key="title:original",
                    collected_at="2026-09-05T04:00:00Z",
                )
            ]
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_online_backup_is_standalone_private_and_integrity_checked(self) -> None:
        backup_database(self.database_path, self.backup_path)

        self.assertTrue(self.backup_path.is_file())
        self.assertEqual(os.stat(self.backup_path).st_mode & 0o777, 0o600)
        self.assertEqual(verify_database(self.backup_path), "ok")
        backup_db = Database(self.backup_path)
        self.assertEqual(backup_db.list_candidates()[0]["title"], "Original")

    def test_restore_is_atomic_and_recovers_the_verified_snapshot(self) -> None:
        backup_database(self.database_path, self.backup_path)
        with self.db.connect() as connection:
            connection.execute("UPDATE items SET title='Changed'")
            connection.commit()

        restore_database(self.backup_path, self.database_path, service_stopped=True)

        restored = Database(self.database_path)
        self.assertEqual(restored.list_candidates()[0]["title"], "Original")
        self.assertEqual(verify_database(self.database_path), "ok")

    def test_restore_refuses_corrupt_input_or_missing_stop_acknowledgement(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(ValueError):
            restore_database(corrupt, self.database_path, service_stopped=True)
        backup_database(self.database_path, self.backup_path)
        with self.assertRaises(ValueError):
            restore_database(self.backup_path, self.database_path, service_stopped=False)


if __name__ == "__main__":
    unittest.main()
