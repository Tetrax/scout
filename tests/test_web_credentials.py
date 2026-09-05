from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash

from scout_web.credentials import create_initial_credentials


class CredentialBootstrapTests(unittest.TestCase):
    def test_creates_private_recoverable_credentials_without_plaintext_in_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "scout"

            result = create_initial_credentials(
                config_dir,
                url="https://scout.valdev.me",
                username="valentin",
            )

            self.assertEqual(result.env_path, config_dir / "scout.env")
            self.assertEqual(result.access_path, config_dir / "access.txt")
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(result.env_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(result.access_path.stat().st_mode), 0o600)
            env = result.env_path.read_text(encoding="utf-8")
            access = result.access_path.read_text(encoding="utf-8")
            password = next(
                line.removeprefix("Mot de passe : ")
                for line in access.splitlines()
                if line.startswith("Mot de passe : ")
            )
            derivative = next(
                line.removeprefix("SCOUT_PASSWORD_HASH=")
                for line in env.splitlines()
                if line.startswith("SCOUT_PASSWORD_HASH=")
            )
            secret = next(
                line.removeprefix("SCOUT_SECRET_KEY=")
                for line in env.splitlines()
                if line.startswith("SCOUT_SECRET_KEY=")
            )
            self.assertGreaterEqual(len(password), 30)
            self.assertTrue(check_password_hash(derivative, password))
            self.assertNotIn(password, env)
            self.assertGreaterEqual(len(secret.encode("utf-8")), 48)
            self.assertIn("URL : https://scout.valdev.me", access)
            self.assertIn("Utilisateur : valentin", access)

    def test_refuses_to_replace_any_existing_runtime_or_recovery_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "scout"
            config_dir.mkdir(mode=0o700)
            existing = config_dir / "access.txt"
            existing.write_text("sentinel\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                create_initial_credentials(
                    config_dir,
                    url="https://scout.valdev.me",
                    username="valentin",
                )

            self.assertEqual(existing.read_text(encoding="utf-8"), "sentinel\n")
            self.assertFalse((config_dir / "scout.env").exists())

    def test_rejects_relative_directory_non_https_url_and_unsafe_username(self) -> None:
        cases = (
            (Path("relative"), "https://scout.valdev.me", "valentin"),
            (Path("/tmp/scout-test"), "http://scout.valdev.me", "valentin"),
            (Path("/tmp/scout-test"), "https://scout.valdev.me", "bad name"),
        )
        for directory, url, username in cases:
            with self.subTest(
                directory=directory, url=url, username=username
            ), self.assertRaises(ValueError):
                create_initial_credentials(directory, url=url, username=username)


if __name__ == "__main__":
    unittest.main()
