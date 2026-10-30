"""Login, first-account registration, CSRF and rate limiting. Deny by default."""
import hmac
import secrets
import time

from flask import abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import execute, query

MAX_FAILURES = 5
WINDOW_SECONDS = 300
PUBLIC = {"login", "register_user", "static"}
# Checked for unknown usernames too, so response time does not reveal which exist.
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(32)
    return session["csrf"]


def register(app, cfg):
    app.jinja_env.globals["csrf_token"] = csrf_token

    def has_user():
        return bool(query(cfg.db, "SELECT 1 FROM users LIMIT 1"))

    def start_session(user_id):
        session.clear()  # new CSRF token, no fixation
        session["user_id"] = user_id

    def rate_limited(key):
        execute(cfg.db, "DELETE FROM login_attempts WHERE at < ?", (time.time() - WINDOW_SECONDS,))
        rows = query(cfg.db, "SELECT COUNT(*) AS n FROM login_attempts WHERE key = ?", (key,))
        return rows[0]["n"] >= MAX_FAILURES

    @app.before_request
    def guard():
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            if not session.get("csrf") or not hmac.compare_digest(sent, session["csrf"]):
                abort(403, "Invalid CSRF token")
        if request.endpoint in PUBLIC:
            return None
        user_id = session.get("user_id")
        if user_id and query(cfg.db, "SELECT 1 FROM users WHERE id = ?", (user_id,)):
            return None
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify(error="Login required"), 401
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not has_user():
            return redirect(url_for("register_user"))
        error, status = None, 200
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            key = f"{request.remote_addr}|{username.lower()}"
            rows = query(cfg.db, "SELECT id, password_hash FROM users WHERE username = ?", (username,))
            if rate_limited(key):
                error, status = "Too many attempts. Try again in a few minutes.", 429
            elif check_password_hash(rows[0]["password_hash"] if rows else DUMMY_HASH, password) and rows:
                execute(cfg.db, "DELETE FROM login_attempts WHERE key = ?", (key,))
                start_session(rows[0]["id"])
                return redirect(url_for("index"))
            else:
                execute(cfg.db, "INSERT INTO login_attempts (key, at) VALUES (?, ?)", (key, time.time()))
                error, status = "Invalid username or password.", 401
        return render_template("login.html", title="Login", error=error), status

    @app.route("/register", methods=["GET", "POST"])
    def register_user():
        if has_user():  # only the first account; there is no public signup
            return redirect(url_for("login"))
        error, status = None, 200
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not username or len(username) > 64 or len(password) < 8:
                error, status = "Username is required and password needs 8+ characters.", 400
            else:
                # Atomic: two racing registrations cannot both become the first account.
                _, created = execute(
                    cfg.db,
                    "INSERT INTO users (username, password_hash) SELECT ?, ? "
                    "WHERE NOT EXISTS (SELECT 1 FROM users)",
                    (username, generate_password_hash(password)),
                )
                if not created:
                    return redirect(url_for("login"))
                start_session(query(cfg.db, "SELECT id FROM users WHERE username = ?", (username,))[0]["id"])
                return redirect(url_for("index"))
        return render_template("login.html", title="Register", error=error), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))
