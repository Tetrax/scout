from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from scout_web.app import create_app
from scout_web.sources import SourceDefinition

NOW = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse battery staple"


def _csrf(body: bytes) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
    if match is None:
        raise AssertionError("CSRF field not found")
    return match.group(1).decode()


def _github(owner: str, repo: str, identifier: int) -> bytes:
    return json.dumps(
        [
            {
                "id": identifier,
                "name": f"{repo} factual release",
                "tag_name": "v1.0.0",
                "html_url": f"https://github.com/{owner}/{repo}/releases/tag/v1.0.0",
                "published_at": "2026-09-04T10:00:00Z",
                "body": "Factual release notes from the official source.",
                "draft": False,
            }
        ]
    ).encode()


class AppFetcher:
    def __call__(self, source: SourceDefinition) -> bytes:
        if source.id == "fortinet_psirt":
            return b"""<rss><channel><item><guid>FG-IR-26-163</guid>
                <title>Fortinet current advisory</title>
                <link>https://fortiguard.fortinet.com/psirt/FG-IR-26-163</link>
                <description>FortiOS factual advisory.</description>
                <pubDate>Fri, 04 Sep 2026 10:00:00 +0000</pubDate>
                </item></channel></rss>"""
        if source.id == "cisa_kev_fortinet":
            return json.dumps(
                {
                    "vulnerabilities": [
                        {
                            "cveID": "CVE-2026-12345",
                            "vendorProject": "Fortinet",
                            "product": "FortiOS",
                            "vulnerabilityName": "Fortinet KEV",
                            "dateAdded": "2026-09-04",
                            "shortDescription": "Confirmed active exploitation.",
                            "requiredAction": "Apply mitigations.",
                        }
                    ]
                }
            ).encode()
        if source.id == "github_hermes_releases":
            return _github("NousResearch", "hermes-agent", 101)
        return _github("openai", "codex", 201)


class WebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(self.temp.name) / "scout.sqlite3"),
                "USERNAME": "valentin",
                "PASSWORD_HASH": generate_password_hash(PASSWORD, method="scrypt"),
                "SECRET_KEY": "test-secret-key-with-at-least-thirty-two-bytes",
                "COOKIE_SECURE": False,
                "TRUSTED_HOSTS": ["localhost"],
                "FETCHER": AppFetcher(),
                "NOW_FACTORY": lambda: NOW,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def login(self, password: str = PASSWORD):
        page = self.client.get("/login")
        return self.client.post(
            "/login",
            data={"csrf_token": _csrf(page.data), "username": "valentin", "password": password},
            follow_redirects=False,
        )

    def test_missing_runtime_secret_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            create_app(
                {
                    "TESTING": True,
                    "DATABASE": str(Path(self.temp.name) / "other.sqlite3"),
                    "USERNAME": "valentin",
                    "PASSWORD_HASH": "",
                    "SECRET_KEY": "short",
                }
            )

    def test_unauthenticated_pages_redirect_and_personal_api_returns_401(self) -> None:
        response = self.client.get("/")
        api = self.client.get("/api/status")
        robots = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json["error"], "authentication_required")
        self.assertEqual(robots.status_code, 200)
        self.assertIn(b"Disallow: /", robots.data)
        self.assertIn("noindex", robots.headers["X-Robots-Tag"])

    def test_login_rotates_to_server_session_and_logout_revokes_it(self) -> None:
        failed = self.login("wrong password")
        self.assertEqual(failed.status_code, 401)
        self.assertNotIn(PASSWORD.encode(), failed.data)

        response = self.login()
        self.assertEqual(response.status_code, 302)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(PASSWORD, cookie)
        self.assertEqual(self.client.get("/").status_code, 200)

        home = self.client.get("/")
        logout = self.client.post("/logout", data={"csrf_token": _csrf(home.data)})
        self.assertEqual(logout.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_login_rate_limit_is_enforced(self) -> None:
        page = self.client.get("/login")
        token = _csrf(page.data)
        for _ in range(5):
            response = self.client.post(
                "/login",
                data={"csrf_token": token, "username": "valentin", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 401)

        limited = self.client.post(
            "/login",
            data={"csrf_token": token, "username": "valentin", "password": "wrong"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_cookie_less_login_flood_keeps_anonymous_sessions_bounded(self) -> None:
        with patch("scout_web.auth.MAX_ANONYMOUS_SESSIONS", 3):
            client = self.app.test_client(use_cookies=False)
            for _ in range(8):
                self.assertEqual(client.get("/login").status_code, 200)

        sessions = self.app.extensions["scout_sessions"]
        self.assertEqual(sessions.counts()["anonymous"], 3)

    def test_csrf_is_required_on_every_authenticated_mutation(self) -> None:
        self.login()
        response = self.client.post("/discover", data={})
        self.assertEqual(response.status_code, 400)

    def test_end_to_end_discovery_feedback_correction_favorites_history_and_preferences(self) -> None:
        self.login()
        home = self.client.get("/")
        discovery = self.client.post(
            "/discover", data={"csrf_token": _csrf(home.data)}, follow_redirects=True
        )

        self.assertEqual(discovery.status_code, 200)
        self.assertIn(b"Fortinet", discovery.data)
        self.assertIn(b"Appr\xc3\xa9ciation personnalis\xc3\xa9e", discovery.data)
        self.assertIn(b"Mode d\xc3\xa9terministe", discovery.data)
        self.assertLessEqual(discovery.data.count(b'class="discovery-card"'), 3)
        item_match = re.search(rb'action="/items/(item_[0-9a-f]+)/reaction"', discovery.data)
        self.assertIsNotNone(item_match)
        item_id = item_match.group(1).decode()
        token = _csrf(discovery.data)

        starred = self.client.post(
            f"/items/{item_id}/reaction",
            data={"csrf_token": token, "reaction": "STAR", "return_to": "home"},
            follow_redirects=True,
        )
        self.assertIn(b"R\xc3\xa9action enregistr\xc3\xa9e", starred.data)
        favorites = self.client.get("/favorites")
        self.assertIn(item_id.encode(), favorites.data)

        corrected = self.client.post(
            f"/items/{item_id}/reaction",
            data={"csrf_token": _csrf(favorites.data), "reaction": "LOVE", "return_to": "favorites"},
            follow_redirects=True,
        )
        self.assertIn(b"Aucun favori", corrected.data)
        self.assertIn(b"Fortinet", self.client.get("/history").data)

        preferences = self.client.get("/preferences")
        first_id = re.search(rb'name="name_(\d+)"', preferences.data).group(1).decode()
        current = self.app.extensions["scout_db"].list_interests()
        payload = {"csrf_token": _csrf(preferences.data)}
        for interest in current:
            identifier = str(interest["id"])
            payload[f"name_{identifier}"] = (
                "Sécurité personnalisée" if identifier == first_id else interest["name"]
            )
            payload[f"weight_{identifier}"] = str(interest["weight"])
            payload[f"topics_{identifier}"] = ", ".join(interest["topics"])
            payload[f"enabled_{identifier}"] = "1"
        saved = self.client.post("/preferences", data=payload, follow_redirects=True)
        self.assertIn("Sécurité personnalisée".encode(), saved.data)

        api = self.client.get("/api/status")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json["model_status"], "DETERMINISTIC_DEGRADED")

    def test_security_headers_output_escaping_and_host_validation(self) -> None:
        login = self.client.get("/login")
        self.assertIn("default-src 'self'", login.headers["Content-Security-Policy"])
        self.assertIn("noindex", login.headers["X-Robots-Tag"])
        self.assertEqual(login.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.client.get("/login", base_url="http://evil.invalid").status_code, 400)

    def test_production_cookie_is_secure(self) -> None:
        self.app.config["COOKIE_SECURE"] = True
        response = self.login()
        self.assertIn("Secure", response.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
