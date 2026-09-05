from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from scout_web.database import Database
from scout_web.service import collect_and_store


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh Scout's bounded cache without creating a viewed run"
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("SCOUT_DATABASE", "/var/lib/scout/scout.sqlite3"),
    )
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    database = Database(arguments.database)
    database.migrate()
    statuses = collect_and_store(
        database,
        now=datetime.now(timezone.utc),
        force=arguments.force,
    )
    for source_id, status in statuses.items():
        print(f"{source_id}: {status['status']} ({status.get('item_count', 0)} items)")
    return 1 if all(status["status"] == "ERROR" for status in statuses.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
