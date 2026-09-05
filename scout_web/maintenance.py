from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote


def _regular_file(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing regular file")


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(os.fspath(path), safe='/')}?mode=ro"


def verify_database(path: str | os.PathLike[str]) -> str:
    database = Path(path)
    _regular_file(database, "database")
    try:
        connection = sqlite3.connect(_readonly_uri(database), uri=True, timeout=5.0)
        try:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError("database integrity verification failed") from exc
    if result.casefold() != "ok":
        raise ValueError("database integrity verification did not return ok")
    return "ok"


def _sync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_database(
    source_path: str | os.PathLike[str], destination_path: str | os.PathLike[str]
) -> Path:
    source = Path(source_path)
    destination = Path(destination_path)
    _regular_file(source, "source database")
    if not destination.is_absolute() or source == destination:
        raise ValueError("backup destination must be a different absolute path")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(_readonly_uri(source), uri=True, timeout=10.0)
        destination_connection = sqlite3.connect(temporary, timeout=10.0)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
            result = str(
                destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            if result.casefold() != "ok":
                raise ValueError("backup integrity verification did not return ok")
        finally:
            destination_connection.close()
            source_connection.close()
        os.chmod(temporary, 0o600)
        _sync_file(temporary)
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def restore_database(
    backup_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    *,
    service_stopped: bool,
) -> Path:
    if not service_stopped:
        raise ValueError("restore requires explicit confirmation that Scout is stopped")
    backup = Path(backup_path)
    target = Path(target_path)
    _regular_file(backup, "backup")
    verify_database(backup)
    if not target.is_absolute() or backup == target:
        raise ValueError("restore target must be a different absolute path")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".restore", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(backup, temporary, follow_symlinks=False)
        os.chmod(temporary, 0o600)
        verify_database(temporary)
        _sync_file(temporary)
        os.replace(temporary, target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.exists() and not sidecar.is_symlink() and sidecar.is_file():
                sidecar.unlink()
        _sync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Coherent Scout SQLite maintenance")
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--database", required=True)
    backup_parser.add_argument("--output", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--database", required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup", required=True)
    restore_parser.add_argument("--database", required=True)
    restore_parser.add_argument("--service-stopped", action="store_true")
    arguments = parser.parse_args()
    if arguments.action == "backup":
        result = backup_database(arguments.database, arguments.output)
        print(f"backup_ok={result}")
    elif arguments.action == "verify":
        print(f"integrity_check={verify_database(arguments.database)}")
    else:
        result = restore_database(
            arguments.backup,
            arguments.database,
            service_stopped=arguments.service_stopped,
        )
        print(f"restore_ok={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
