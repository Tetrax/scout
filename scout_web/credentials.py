from __future__ import annotations

import argparse
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from werkzeug.security import generate_password_hash

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True, slots=True)
class CredentialPaths:
    env_path: Path
    access_path: Path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_temporary(directory: Path, prefix: str, content: str) -> tuple[Path, os.stat_result]:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = content.encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("credential write made no progress")
            written += count
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return path, metadata


def _same_inode(path: Path, expected: os.stat_result) -> bool:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    return observed.st_dev == expected.st_dev and observed.st_ino == expected.st_ino


def create_initial_credentials(
    directory: str | os.PathLike[str],
    *,
    url: str,
    username: str,
) -> CredentialPaths:
    config_dir = Path(directory)
    parsed_url = urlsplit(url)
    if not config_dir.is_absolute():
        raise ValueError("credential directory must be absolute")
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("Scout URL must be an HTTPS origin")
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("username must use 1..80 safe characters")

    if os.path.lexists(config_dir):
        if config_dir.is_symlink() or not config_dir.is_dir():
            raise ValueError("credential directory must be a real directory")
    else:
        config_dir.mkdir(parents=True, mode=0o700)
    os.chmod(config_dir, 0o700)

    env_path = config_dir / "scout.env"
    access_path = config_dir / "access.txt"
    if os.path.lexists(env_path) or os.path.lexists(access_path):
        raise FileExistsError("Scout credentials already exist; refusing replacement")

    password = secrets.token_urlsafe(24)
    secret_key = secrets.token_urlsafe(48)
    password_hash = generate_password_hash(password, method="scrypt")
    env_content = (
        f"SCOUT_USERNAME={username}\n"
        f"SCOUT_PASSWORD_HASH={password_hash}\n"
        f"SCOUT_SECRET_KEY={secret_key}\n"
    )
    access_content = (
        "Scout — accès initial\n"
        f"URL : {url.rstrip('/')}\n"
        f"Utilisateur : {username}\n"
        f"Mot de passe : {password}\n"
        "\nConserve ce fichier privé. Le mot de passe n'est pas stocké en clair dans le service.\n"
    )

    env_temporary, env_metadata = _private_temporary(config_dir, ".scout.env.", env_content)
    access_temporary, access_metadata = _private_temporary(
        config_dir, ".access.txt.", access_content
    )
    published: list[tuple[Path, os.stat_result]] = []
    try:
        os.link(env_temporary, env_path, follow_symlinks=False)
        published.append((env_path, env_metadata))
        os.link(access_temporary, access_path, follow_symlinks=False)
        published.append((access_path, access_metadata))
        _sync_directory(config_dir)
    except BaseException:
        for path, metadata in reversed(published):
            if _same_inode(path, metadata):
                path.unlink()
        _sync_directory(config_dir)
        raise
    finally:
        env_temporary.unlink(missing_ok=True)
        access_temporary.unlink(missing_ok=True)

    return CredentialPaths(env_path=env_path, access_path=access_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Scout credentials once, without logging them")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--url", default="https://scout.valdev.me")
    parser.add_argument("--username", default="valentin")
    arguments = parser.parse_args()
    result = create_initial_credentials(
        arguments.directory,
        url=arguments.url,
        username=arguments.username,
    )
    print("credentials_created=true")
    print(f"runtime_env={result.env_path}")
    print(f"recovery_file={result.access_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CredentialPaths", "create_initial_credentials"]
