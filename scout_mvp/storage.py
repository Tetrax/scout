from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Iterator, TypeAlias

from .contracts import validate_document

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock implementation.
    fcntl = None  # type: ignore[assignment]


PathLike: TypeAlias = str | os.PathLike[str]


class StorageRollbackError(OSError):
    """The original append failed and restoring the prior file state also failed."""

    def __init__(self, errors: list[BaseException]) -> None:
        super().__init__("JSONL rollback failed; storage integrity is unknown")
        self.errors = tuple(errors)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
_UNSUPPORTED_FLOCK_ERRNOS = {
    value
    for value in (
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
}


@contextlib.contextmanager
def _file_lock(fd: int, *, exclusive: bool) -> Iterator[None]:
    """Hold a best-effort advisory lock for the complete read/check/write span."""
    if fcntl is None:
        yield
        return

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(fd, operation)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_FLOCK_ERRNOS:
            raise
        yield
        return

    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _close_fd(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)


def _reject_unsafe_open(exc: OSError, description: str) -> ValueError:
    return ValueError(f"unsafe JSONL {description}")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate JSON object key: {key}")
        record[key] = value
    return record


class JsonlStore:
    """An append-only JSONL store confined to an explicit absolute root.

    All directories are traversed from descriptors with no-follow flags where the
    platform provides them.  The final file is opened relative to its verified
    parent descriptor, then checked as a regular file before any bytes are read
    or written.
    """

    def __init__(self, root: PathLike):
        if root is None:
            raise ValueError("store root is required")
        root_path = Path(root).expanduser()
        if not root_path.is_absolute():
            raise ValueError("store root must be absolute")
        if any(part in {".", ".."} for part in root_path.parts):
            raise ValueError("store root cannot contain traversal components")
        self.root = root_path

    @staticmethod
    def _relative_parts(relative_path: PathLike) -> list[str]:
        if relative_path is None:
            raise ValueError("relative path is required")
        raw = os.fspath(relative_path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("invalid relative path")

        posix_path = Path(raw)
        windows_path = PureWindowsPath(raw)
        if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ValueError("absolute paths are not allowed")

        parts = raw.replace("\\", "/").split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("path traversal or empty components are not allowed")
        return parts

    def _safe_path(self, relative_path: PathLike) -> Path:
        """Validate a safe relative path and return its lexical representation.

        The returned path is for diagnostics only.  Operations use descriptor
        traversal instead of reopening this path by name.
        """
        return self.root.joinpath(*self._relative_parts(relative_path))

    @staticmethod
    def _open_directory_at(parent_fd: int, name: str, *, create: bool) -> int:
        if not _NOFOLLOW:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISLNK(existing.st_mode):
                raise ValueError("symlink JSONL directory component is not allowed")

        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise _reject_unsafe_open(exc, "directory component") from exc
        except OSError as exc:
            raise _reject_unsafe_open(exc, "directory component") from exc

        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ValueError("JSONL path component is not a directory")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_root(self, *, create: bool) -> int | None:
        """Open the absolute root by traversing from the real filesystem root."""
        try:
            current_fd = os.open(os.sep, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise _reject_unsafe_open(exc, "filesystem root") from exc

        try:
            # Absolute POSIX Path.parts starts with '/'.
            for component in self.root.parts[1:]:
                try:
                    next_fd = self._open_directory_at(current_fd, component, create=create)
                except FileNotFoundError:
                    if create:
                        raise
                    os.close(current_fd)
                    return None
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @contextlib.contextmanager
    def _root_descriptor(self, *, create: bool) -> Iterator[int | None]:
        fd = self._open_root(create=create)
        try:
            yield fd
        finally:
            _close_fd(fd)

    @classmethod
    @contextlib.contextmanager
    def _parent_descriptor(
        cls,
        root_fd: int,
        parent_parts: list[str],
        *,
        create: bool,
    ) -> Iterator[int | None]:
        current_fd: int | None = os.dup(root_fd)
        try:
            for component in parent_parts:
                try:
                    next_fd = cls._open_directory_at(current_fd, component, create=create)
                except FileNotFoundError:
                    if create:
                        raise
                    yield None
                    return
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd
        finally:
            _close_fd(current_fd)

    @staticmethod
    def _open_target(parent_fd: int, name: str, *, create: bool) -> int | None:
        if not _NOFOLLOW:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISLNK(existing.st_mode):
                raise ValueError("symlink JSONL target is not allowed")

        flags = (os.O_RDWR if create else os.O_RDONLY) | _NONBLOCK | _NOFOLLOW
        if create:
            flags |= os.O_APPEND | os.O_CREAT
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                return None
            raise ValueError("JSONL target disappeared during open")
        except OSError as exc:
            raise _reject_unsafe_open(exc, "target") from exc

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("JSONL target must be a regular file")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _read_fd(fd: int) -> bytes:
        size = os.fstat(fd).st_size
        if size == 0:
            return b""
        chunks: list[bytes] = []
        offset = 0
        while offset < size:
            chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
            if not chunk:
                raise ValueError("JSONL file changed while being read")
            chunks.append(chunk)
            offset += len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse_jsonl(data: bytes, *, path: Path, kind: str | None) -> list[dict[str, Any]]:
        if not data:
            return []
        if not data.endswith(b"\n"):
            raise ValueError(f"JSONL at {path} lacks a terminal newline")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"JSONL at {path} is not valid UTF-8") from exc

        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.split("\n")[:-1], start=1):
            if not line.strip():
                raise ValueError(f"blank JSONL line at {path}:{line_number}")
            try:
                record = json.loads(
                    line,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_reject_duplicate_json_keys,
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSONL record at {path}:{line_number} is not an object")
            if kind is not None:
                validate_document(kind, record)
            records.append(record)
        JsonlStore._unique_ids(records, context="JSONL")
        return records

    @staticmethod
    def _id_key(record: dict[str, Any]) -> str | None:
        if "id" not in record:
            return None
        try:
            return json.dumps(
                record["id"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("record id must be JSON serializable") from exc

    @classmethod
    def _unique_ids(cls, records: Iterable[dict[str, Any]], *, context: str) -> set[str]:
        identifiers: set[str] = set()
        for record in records:
            key = cls._id_key(record)
            if key is None:
                continue
            if key in identifiers:
                raise ValueError(f"duplicate id in {context}")
            identifiers.add(key)
        return identifiers

    @staticmethod
    def _prepare_batch(
        records: Iterable[dict[str, Any]], *, kind: str | None
    ) -> list[bytes]:
        try:
            batch = list(records)
        except TypeError as exc:
            raise ValueError("records must be iterable") from exc

        for record in batch:
            if not isinstance(record, dict):
                raise ValueError("JSONL records must be objects")
        JsonlStore._unique_ids(batch, context="append batch")
        serialized: list[bytes] = []
        for record in batch:
            if kind is not None:
                validate_document(kind, record)
            try:
                line = json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("record is not JSON serializable") from exc
            serialized.append((line + "\n").encode("utf-8"))
        return serialized

    @staticmethod
    def _rollback(fd: int, original_size: int) -> None:
        errors: list[BaseException] = []
        try:
            os.ftruncate(fd, original_size)
        except BaseException as exc:
            errors.append(exc)
        try:
            os.fsync(fd)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise StorageRollbackError(errors)

    def append(
        self,
        relative_path: PathLike,
        records: Iterable[dict[str, Any]],
        *,
        kind: str | None = None,
    ) -> int:
        """Validate and append a complete batch without truncating an existing log."""
        path = self._safe_path(relative_path)
        try:
            batch = list(records)
        except TypeError as exc:
            raise ValueError("records must be iterable") from exc
        serialized = self._prepare_batch(batch, kind=kind)
        if not serialized:
            return 0
        payload = b"".join(serialized)
        parts = self._relative_parts(relative_path)

        with self._root_descriptor(create=True) as root_fd:
            assert root_fd is not None
            with self._parent_descriptor(root_fd, parts[:-1], create=True) as parent_fd:
                assert parent_fd is not None
                fd = self._open_target(parent_fd, parts[-1], create=True)
                assert fd is not None
                try:
                    with _file_lock(fd, exclusive=True):
                        original_size = os.fstat(fd).st_size
                        existing = self._parse_jsonl(
                            self._read_fd(fd), path=path, kind=kind
                        )
                        existing_ids = self._unique_ids(existing, context="existing JSONL")
                        batch_ids = self._unique_ids(batch, context="append batch")
                        if existing_ids.intersection(batch_ids):
                            raise ValueError("duplicate id against existing JSONL")

                        os.fchmod(fd, 0o600)
                        try:
                            offset = 0
                            while offset < len(payload):
                                written = os.write(fd, payload[offset:])
                                remaining = len(payload) - offset
                                if written <= 0 or written > remaining:
                                    raise OSError(errno.EIO, "invalid JSONL write result")
                                offset += written
                            os.fsync(fd)
                        except BaseException as append_error:
                            try:
                                self._rollback(fd, original_size)
                            except StorageRollbackError as rollback_error:
                                if isinstance(
                                    append_error,
                                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                                ):
                                    raise append_error from rollback_error
                                raise rollback_error from append_error
                            raise
                finally:
                    os.close(fd)
        return len(serialized)

    def read(
        self,
        relative_path: PathLike,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read records in append order; a missing root or log is empty."""
        path = self._safe_path(relative_path)
        parts = self._relative_parts(relative_path)
        with self._root_descriptor(create=False) as root_fd:
            if root_fd is None:
                return []
            with self._parent_descriptor(root_fd, parts[:-1], create=False) as parent_fd:
                if parent_fd is None:
                    return []
                fd = self._open_target(parent_fd, parts[-1], create=False)
                if fd is None:
                    return []
                try:
                    with _file_lock(fd, exclusive=False):
                        return self._parse_jsonl(self._read_fd(fd), path=path, kind=kind)
                finally:
                    os.close(fd)


AppendOnlyJSONLStore = JsonlStore


def append_jsonl(
    root: PathLike,
    relative_path: PathLike,
    records: Iterable[dict[str, Any]],
    *,
    kind: str | None = None,
) -> int:
    return JsonlStore(root).append(relative_path, records, kind=kind)


def read_jsonl(
    root: PathLike,
    relative_path: PathLike,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    return JsonlStore(root).read(relative_path, kind=kind)


__all__ = [
    "AppendOnlyJSONLStore",
    "JsonlStore",
    "StorageRollbackError",
    "append_jsonl",
    "read_jsonl",
]
