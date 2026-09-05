from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.prefetch import main


class PrefetchCliTests(unittest.TestCase):
    def test_explicit_database_path_prefetches_without_creating_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "scout.sqlite3"
            statuses = {
                "source": {"status": "OK", "item_count": 2},
            }
            with patch(
                "scripts.prefetch.collect_and_store", return_value=statuses
            ) as collect, patch(
                "sys.argv", ["prefetch", "--database", str(database_path), "--force"]
            ):
                result = main()

            self.assertEqual(result, 0)
            database = collect.call_args.args[0]
            self.assertEqual(database.path, database_path)
            self.assertTrue(collect.call_args.kwargs["force"])
            self.assertEqual(database.list_history(), [])
            self.assertEqual(database.seen_item_ids(), set())


if __name__ == "__main__":
    unittest.main()
