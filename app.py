"""Flask routes: the web face of the native commands in files.py and trash.py."""
import json
import logging
import os
import posixpath
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.exceptions import ClientDisconnected

import auth
import config
import database
from cli import CLI, CommandError
from files import CHUNK, FileError, SORT_KEYS, Storage
from trash import Trash

log = logging.getLogger("barefile")
SAFE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}
SAFE_TEXT = {"application/json", "application/xml", "application/javascript", "application/x-sh",
             "inode/x-empty"}
CSP = ("default-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'self'")


def preview_kind(mime):
    if mime in SAFE_IMAGES:
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("text/") or mime in SAFE_TEXT:
        return "text"
    return None


def event(**data):
    return f"data: {json.dumps(data)}\n\n"


def create_app(cfg=None):
    cfg = cfg or config.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cli = CLI(cfg)
    store = Storage(cfg, cli)
    store.init()
    database.init(cfg.db)
    trash = Trash(cfg, cli, store)

    app = Flask(__name__)
    app.config.update(SECRET_KEY=cfg.secret_key, MAX_CONTENT_LENGTH=cfg.max_upload or None,
                      SESSION_COOKIE_NAME="barefile", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=cfg.cookie_secure)
    auth.register(app, cfg)
    app.jinja_env.globals["read_only"] = cfg.read_only

    @app.before_request
    def refuse_changes():
        # Every change goes through these two endpoints.
        if cfg.read_only and request.endpoint in ("operation", "upload"):
            raise FileError("Read-only mode: changes are disabled", 403)

    @app.after_request
    def headers(response):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def error_response(message, status):
        if request.path.startswith("/api/"):
            return jsonify(error=message), status
        return Response(message, status, mimetype="text/plain")

    @app.errorhandler(FileError)
    def file_error(error):
        return error_response(str(error), error.status)

    @app.errorhandler(CommandError)
    def command_error(error):
        return error_response(str(error), 500)

    # Pages -----------------------------------------------------------------

    @app.get("/")
    def index():
        path = request.args.get("path", "").strip("/")
        query_text = request.args.get("q", "").strip()
        sort = request.args.get("sort", "name")
        order = request.args.get("order", "asc")
        sort = sort if sort in SORT_KEYS else "name"
        order = order if order in ("asc", "desc") else "asc"
        listing = {"entries": [], "total": "", "skipped": 0}
        if query_text:
            listing["entries"] = store.search(query_text)
        else:
            listing = store.list(path, sort, order)
        return render_template("files.html", path=path, parent=posixpath.dirname(path), q=query_text,
                               sort=sort, order=order, **listing)

    @app.get("/trash")
    def trash_page():
        return render_template("trash.html", items=trash.items())

    @app.get("/preview")
    def preview():
        path = request.args.get("path", "")
        full = store.file(path)
        kind = preview_kind(store.mime(full))
        return render_template("preview.html", path=path.strip("/"), name=os.path.basename(full),
                               parent=posixpath.dirname(path.strip("/")), kind=kind,
                               content=store.head(full) if kind == "text" else None)

    # File bytes ------------------------------------------------------------

    def serve(rel, inline):
        path = store.file(rel)
        size, mime = store.size(path), store.mime(path)
        kind = preview_kind(mime)
        # Inline only for types a browser will not run as script in this origin.
        inline = inline and kind in ("image", "pdf", "video")
        start, status = 0, 200
        length = size
        headers = {"Accept-Ranges": "bytes"}
        if request.range and len(request.range.ranges) == 1:
            span = request.range.range_for_length(size)
            if span is None:
                return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            start, length, status = span[0], span[1] - span[0], 206
            headers["Content-Range"] = f"bytes {span[0]}-{span[1] - 1}/{size}"
        disposition = "inline" if inline else "attachment"
        headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(os.path.basename(path))}"
        headers["Content-Length"] = str(length)
        # Browser PDF viewers refuse a sandboxed document, so PDFs get only the framing rule.
        headers["Content-Security-Policy"] = "frame-ancestors 'self'" if kind == "pdf" else "sandbox"
        return Response(store.read(path, start, length), status, headers=headers,
                        mimetype=mime)

    @app.get("/raw")
    def raw():
        return serve(request.args.get("path", ""), inline=True)

    @app.get("/download")
    def download():
        return serve(request.args.get("path", ""), inline=False)

    # API -------------------------------------------------------------------

    @app.get("/api/du")
    def du():
        return jsonify(du=store.du(request.args.get("path", "")))

    @app.post("/api/upload")
    def upload():
        length = request.content_length
        if length is None:
            abort(411)
        try:
            store.upload(request.args.get("path", ""), request.args.get("name", ""), request.stream, length)
        except FileError:
            # Read the rest so the browser sees this error rather than a reset connection.
            try:
                while request.stream.read(CHUNK):
                    pass
            except ClientDisconnected:
                pass
            raise
        return jsonify(ok=True)

    @app.post("/api/op")
    def operation():
        data = request.get_json(silent=True) or {}
        action, path, dest, name = (data.get(k, "") for k in ("action", "path", "dest", "name"))
        items = data.get("items") or []
        if not isinstance(items, list) or not all(isinstance(item, (str, int)) for item in items):
            raise FileError("Invalid items")
        if action in ("purge", "empty") and data.get("confirm") is not True:
            raise FileError("Permanent deletion must be confirmed")
        if action == "empty":
            items = [row["id"] for row in trash.items()]
        actions = {
            "add_file": lambda item: store.add_file(path, item),
            "add_folder": lambda item: store.add_folder(path, item),
            "rename": lambda item: store.rename(item, name),
            "copy": lambda item: store.copy(item, dest),
            "move": lambda item: store.move(item, dest),
            "delete": trash.delete,
            "restore": trash.restore,
            "purge": trash.purge,
            "empty": trash.purge,
        }
        if action not in actions:
            raise FileError("Unknown action")

        def stream():
            done = failed = 0
            yield event(done=0, total=len(items))
            for item in items:
                try:
                    for line in actions[action](item):
                        yield event(line=line)
                    done += 1
                except (FileError, CommandError) as error:
                    failed += 1
                    yield event(line=f"error: {error}")
                except Exception:
                    log.exception("operation %s failed", action)
                    failed += 1
                    yield event(line="error: operation failed")
                yield event(done=done, failed=failed, total=len(items))
            yield event(end=True, done=done, failed=failed, total=len(items))

        return Response(stream(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app
