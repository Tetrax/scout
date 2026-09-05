from __future__ import annotations

import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from .auth import AUTHENTICATED_TTL, SessionManager
from .database import REACTIONS, Database
from .service import MODEL_STATUS, DiscoveryBusy, run_discovery
from .sources import ENABLED_SOURCES, X_BOOKMARKS_DIAGNOSTIC, fetch_bounded

COOKIE_NAME = "scout_session"
_ITEM_ID_RE = re.compile(r"^item_[0-9a-f]{32}$")
_PUBLIC_ENDPOINTS = {"login", "login_post", "healthz", "robots", "static"}
_RETURN_ENDPOINTS = {
    "home": "home",
    "history": "history",
    "favorites": "favorites",
    "preferences": "preferences",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.casefold() in {"1", "true", "yes", "on"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config_from_environment() -> dict[str, Any]:
    return {
        "DATABASE": os.environ.get("SCOUT_DATABASE", "/var/lib/scout/scout.sqlite3"),
        "USERNAME": os.environ.get("SCOUT_USERNAME", ""),
        "PASSWORD_HASH": os.environ.get("SCOUT_PASSWORD_HASH", ""),
        "SECRET_KEY": os.environ.get("SCOUT_SECRET_KEY", ""),
        "COOKIE_SECURE": _env_bool("SCOUT_COOKIE_SECURE", True),
        "TRUST_PROXY": _env_bool("SCOUT_TRUST_PROXY", True),
        "TRUSTED_HOSTS": [
            host.strip()
            for host in os.environ.get(
                "SCOUT_TRUSTED_HOSTS", "scout.valdev.me,localhost,127.0.0.1"
            ).split(",")
            if host.strip()
        ],
        "REVISION": os.environ.get("SCOUT_REVISION", "development"),
        "FETCHER": fetch_bounded,
        "NOW_FACTORY": _now,
    }


def _validate_config(config: dict[str, Any]) -> None:
    database = Path(str(config.get("DATABASE", "")))
    username = config.get("USERNAME")
    password_hash = config.get("PASSWORD_HASH")
    secret_key = config.get("SECRET_KEY")
    trusted_hosts = config.get("TRUSTED_HOSTS")
    if not database.is_absolute():
        raise RuntimeError("SCOUT_DATABASE must be an absolute path")
    if not isinstance(username, str) or not username or len(username) > 80:
        raise RuntimeError("SCOUT_USERNAME is required and must be bounded")
    if not isinstance(password_hash, str) or not password_hash.startswith(("scrypt:", "pbkdf2:")):
        raise RuntimeError("SCOUT_PASSWORD_HASH must be a supported password derivative")
    if not isinstance(secret_key, str) or len(secret_key.encode("utf-8")) < 32:
        raise RuntimeError("SCOUT_SECRET_KEY must contain at least 32 bytes")
    if not isinstance(trusted_hosts, list) or not trusted_hosts:
        raise RuntimeError("SCOUT_TRUSTED_HOSTS must not be empty")
    if not callable(config.get("NOW_FACTORY")) or not callable(config.get("FETCHER")):
        raise TypeError("Scout clock and source fetcher must be callable")


def create_app(overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(_config_from_environment())
    if overrides:
        app.config.update(overrides)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.config["TRUSTED_HOSTS"] = list(app.config["TRUSTED_HOSTS"])
    _validate_config(app.config)
    if app.config.get("TRUST_PROXY", False):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]

    database = Database(app.config["DATABASE"])
    database.migrate()
    sessions = SessionManager(database, app.config["SECRET_KEY"])
    app.extensions["scout_db"] = database
    app.extensions["scout_sessions"] = sessions

    def current_time() -> datetime:
        value = app.config["NOW_FACTORY"]()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("NOW_FACTORY must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def issue_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=int(AUTHENTICATED_TTL.total_seconds()),
            secure=bool(app.config["COOKIE_SECURE"]),
            httponly=True,
            samesite="Strict",
            path="/",
        )

    @app.before_request
    def load_and_authorize() -> Response | None:
        # Flask leaves the endpoint unresolved when the Host header violates
        # TRUSTED_HOSTS.  Refuse it before any redirect tries to build a URL.
        if request.endpoint is None:
            return Response("Bad Request\n", status=400, mimetype="text/plain")
        now = current_time()
        token = request.cookies.get(COOKIE_NAME)
        g.scout_token = token
        g.scout_session = sessions.load(token, now=now)
        g.scout_new_token = None
        g.scout_clear_cookie = False

        if request.endpoint == "login" and request.method == "GET" and g.scout_session is None:
            new_token, new_session = sessions.create(now=now, authenticated=False)
            g.scout_new_token = new_token
            g.scout_token = new_token
            g.scout_session = new_session

        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = "" if g.scout_session is None else g.scout_session.csrf_token
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                abort(400, description="invalid CSRF token")

        if request.endpoint not in _PUBLIC_ENDPOINTS and (
            g.scout_session is None or not g.scout_session.authenticated
        ):
            if request.path.startswith("/api/"):
                return jsonify(error="authentication_required"), 401
            return redirect(url_for("login"))
        return None

    @app.after_request
    def security_headers(response: Response) -> Response:
        if getattr(g, "scout_new_token", None):
            issue_cookie(response, g.scout_new_token)
        if getattr(g, "scout_clear_cookie", False):
            response.delete_cookie(
                COOKIE_NAME,
                path="/",
                secure=bool(app.config["COOKIE_SECURE"]),
                httponly=True,
                samesite="Strict",
            )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    def template_context(**extra: Any) -> dict[str, Any]:
        return {
            "csrf_token": g.scout_session.csrf_token,
            "username": g.scout_session.username,
            "source_definitions": ENABLED_SOURCES,
            "x_diagnostic": X_BOOKMARKS_DIAGNOSTIC,
            "model_status": MODEL_STATUS,
            "revision": app.config["REVISION"],
            **extra,
        }

    def home_context(*, notice: str | None = None, error: str | None = None) -> dict[str, Any]:
        history_rows = database.list_history(limit=1)
        latest = history_rows[0] if history_rows else None
        return template_context(
            latest_run=latest,
            source_cache=database.source_cache(),
            notice=notice,
            error=error,
            return_to="home",
        )

    @app.get("/login")
    def login() -> Response | str:
        if g.scout_session.authenticated:
            return redirect(url_for("home"))
        return render_template(
            "login.html",
            csrf_token=g.scout_session.csrf_token,
            error=None,
            revision=app.config["REVISION"],
        )

    @app.post("/login")
    def login_post() -> Response | tuple[str, int]:
        now = current_time()
        client_key = sessions.client_key(request.remote_addr)
        if sessions.is_login_limited(client_key, now=now):
            response = make_response(
                render_template(
                    "login.html",
                    csrf_token=g.scout_session.csrf_token,
                    error="Trop de tentatives. Réessaie dans quelques minutes.",
                    revision=app.config["REVISION"],
                ),
                429,
            )
            response.headers["Retry-After"] = "600"
            return response
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_shape = (
            0 < len(username) <= 80
            and 0 < len(password.encode("utf-8")) <= 1024
        )
        valid_user = valid_shape and hmac.compare_digest(
            username.encode("utf-8"), app.config["USERNAME"].encode("utf-8")
        )
        valid_password = valid_shape and check_password_hash(
            app.config["PASSWORD_HASH"], password
        )
        if not (valid_user and valid_password):
            sessions.record_login_failure(client_key, now=now)
            return (
                render_template(
                    "login.html",
                    csrf_token=g.scout_session.csrf_token,
                    error="Identifiants invalides.",
                    revision=app.config["REVISION"],
                ),
                401,
            )
        sessions.clear_login_failures(client_key)
        sessions.revoke(g.scout_token)
        token, authenticated = sessions.create(
            now=now, authenticated=True, username=app.config["USERNAME"]
        )
        g.scout_new_token = token
        g.scout_session = authenticated
        return redirect(url_for("home"))

    @app.post("/logout")
    def logout() -> Response:
        sessions.revoke(g.scout_token)
        g.scout_new_token = None
        g.scout_clear_cookie = True
        return redirect(url_for("login"))

    @app.get("/")
    def home() -> str:
        notices = {
            "reaction": "Réaction enregistrée. Le prochain classement en tiendra compte.",
            "discovery": "Découverte terminée.",
        }
        return render_template(
            "home.html",
            **home_context(notice=notices.get(request.args.get("notice", ""))),
        )

    @app.post("/discover")
    def discover() -> Response | tuple[str, int]:
        try:
            run_discovery(
                database,
                now=current_time(),
                fetcher=app.config["FETCHER"],
            )
        except DiscoveryBusy:
            return (
                render_template(
                    "home.html",
                    **home_context(error="Une découverte est déjà en cours."),
                ),
                409,
            )
        return redirect(url_for("home", notice="discovery"))

    @app.post("/items/<item_id>/reaction")
    def reaction(item_id: str) -> Response:
        if not _ITEM_ID_RE.fullmatch(item_id):
            abort(404)
        value = request.form.get("reaction", "")
        reaction_value = value if value in REACTIONS else None if value == "" else "INVALID"
        if reaction_value == "INVALID":
            abort(400, description="invalid reaction")
        try:
            database.set_reaction(item_id, reaction_value, current_time().isoformat().replace("+00:00", "Z"))
        except KeyError:
            abort(404)
        return_name = request.form.get("return_to", "home")
        endpoint = _RETURN_ENDPOINTS.get(return_name, "home")
        return redirect(url_for(endpoint, notice="reaction"))

    @app.get("/favorites")
    def favorites() -> str:
        notice = (
            "Réaction enregistrée. Le prochain classement en tiendra compte."
            if request.args.get("notice") == "reaction"
            else None
        )
        return render_template(
            "favorites.html",
            **template_context(
                items=database.list_favorites(), notice=notice, return_to="favorites"
            ),
        )

    @app.get("/history")
    def history() -> str:
        notice = (
            "Réaction enregistrée. Le prochain classement en tiendra compte."
            if request.args.get("notice") == "reaction"
            else None
        )
        return render_template(
            "history.html",
            **template_context(
                runs=database.list_history(), notice=notice, return_to="history"
            ),
        )

    @app.get("/preferences")
    def preferences() -> str:
        notice = (
            "Centres d’intérêt enregistrés."
            if request.args.get("notice") == "saved"
            else None
        )
        return render_template(
            "preferences.html",
            **template_context(
                interests=database.list_interests(), notice=notice, error=None
            ),
        )

    @app.post("/preferences")
    def preferences_post() -> Response | tuple[str, int]:
        current = database.list_interests()
        interests: list[dict[str, Any]] = []
        for interest in current:
            identifier = str(interest["id"])
            if request.form.get(f"delete_{identifier}") == "1":
                continue
            interests.append(
                {
                    "name": request.form.get(f"name_{identifier}", ""),
                    "weight": request.form.get(f"weight_{identifier}", ""),
                    "topics": request.form.get(f"topics_{identifier}", "").split(","),
                    "enabled": request.form.get(f"enabled_{identifier}") == "1",
                }
            )
        new_name = request.form.get("new_name", "").strip()
        if new_name:
            interests.append(
                {
                    "name": new_name,
                    "weight": request.form.get("new_weight", "3"),
                    "topics": request.form.get("new_topics", "").split(","),
                    "enabled": True,
                }
            )
        try:
            database.replace_interests(
                interests,
                updated_at=current_time().isoformat().replace("+00:00", "Z"),
            )
        except (TypeError, ValueError):
            return (
                render_template(
                    "preferences.html",
                    **template_context(
                        interests=current,
                        notice=None,
                        error="Vérifie les noms, poids (0 à 5) et thèmes séparés par des virgules.",
                    ),
                ),
                400,
            )
        return redirect(url_for("preferences", notice="saved"))

    @app.get("/api/status")
    def api_status() -> Response:
        history_rows = database.list_history(limit=1)
        return jsonify(
            authenticated=True,
            model_status=MODEL_STATUS,
            latest_run=None if not history_rows else history_rows[0]["id"],
            sources=database.source_cache(),
            revision=app.config["REVISION"],
        )

    @app.get("/healthz")
    def healthz() -> Response:
        try:
            with database.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except (OSError, sqlite3.Error):
            return jsonify(status="unhealthy"), 503
        return jsonify(status="ok", revision=app.config["REVISION"])

    @app.get("/robots.txt")
    def robots() -> Response:
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def client_error(error):
        status = int(getattr(error, "code", 400))
        if request.path.startswith("/api/"):
            return jsonify(error="invalid_request"), status
        if g.get("scout_session") is not None and g.scout_session.authenticated:
            return (
                render_template(
                    "error.html",
                    **template_context(status=status, message="Requête refusée."),
                ),
                status,
            )
        csrf = None if g.get("scout_session") is None else g.scout_session.csrf_token
        return (
            render_template(
                "login.html",
                csrf_token=csrf,
                error="Requête refusée.",
                revision=app.config["REVISION"],
            ),
            status,
        )

    return app


__all__ = ["COOKIE_NAME", "create_app"]
