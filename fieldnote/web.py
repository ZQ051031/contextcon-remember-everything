"""Flask API and browser workspace."""

import hashlib
import os
from functools import wraps
from pathlib import Path
from threading import RLock
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request

from .context import ContextError, KINDS, LocalContext, MekoClient, MekoContext, MemoryMissing, recall_trace
from .respond import answer
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
AGENTS = {"fieldnote_planner": "Planner", "fieldnote_writer": "Brief writer"}


def create_app():
    load_dotenv(ROOT / ".env")
    key = os.getenv("MEKO_API_KEY", "").strip()
    datapack = os.getenv("MEKO_DATAPACK_ID", "").strip()
    model = os.getenv("OLLAMA_MODEL", "").strip()
    if key and not datapack:
        raise RuntimeError("Set MEKO_DATAPACK_ID to a writable Meko datapack UUID, or clear MEKO_API_KEY.")

    data_dir = Path(os.getenv("FIELDNOTE_DATA_DIR", str(ROOT / "data"))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace = "local"
    if key:
        fingerprint = hashlib.sha256(f"{datapack}:{key}".encode()).hexdigest()[:16]
        workspace = f"meko-{fingerprint}"
    store = Store(data_dir / f"{workspace}.sqlite3")
    context = MekoContext(store, MekoClient(key, datapack)) if key else LocalContext(store)

    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024, TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
    # Single-process app. Serialize mixed remote/local mutations so two requests
    # cannot initialize the same Meko conversation or race a session deletion.
    lock = RLock()

    def locked(handler):
        @wraps(handler)
        def run(*args, **kwargs):
            with lock:
                return handler(*args, **kwargs)
        return run

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(ContextError)
    def context_error(exc):
        return {"error": str(exc)}, 502

    @app.errorhandler(MemoryMissing)
    def missing(exc):
        return {"error": str(exc)}, 404

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(415)
    def bad_request(exc):
        return {"error": exc.description}, exc.code

    def json_body():
        data = request.get_json()
        if not isinstance(data, dict):
            abort(400, "Send a JSON object.")
        return data

    def input_text(data, field, limit, default=None):
        value = data.get(field, default)
        if not isinstance(value, str) or not (1 <= len(value.strip()) <= limit):
            abort(400, f"Enter {field} between 1 and {limit} characters.")
        return value.strip()

    def get_session(session_id):
        session = store.session(session_id)
        if session is None:
            abort(404, "That session could not be found.")
        return session

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/api/workspace")
    def get_workspace():
        return {"provider": context.name, "model": model, "agents": AGENTS,
                "sessions": store.sessions()}

    @app.post("/api/sessions")
    @locked
    def create_session():
        data = json_body()
        title = input_text(data, "title", 80, "New session")
        agent = input_text(data, "agent", 40, "fieldnote_planner")
        if agent not in AGENTS:
            abort(400, "Choose a Fieldnote assistant.")
        return store.create_session(title, agent), 201

    @app.get("/api/sessions/<session_id>/turns")
    @locked
    def get_turns(session_id):
        get_session(session_id)
        return jsonify(store.turns(session_id))

    @app.post("/api/sessions/<session_id>/turns")
    @locked
    def add_turn(session_id):
        data = json_body()
        session = get_session(session_id)
        question = input_text(data, "question", 4000)
        recall = data.get("recall", True)
        if not isinstance(recall, bool):
            abort(400, "Recall must be true or false.")

        if recall:
            trace = context.recall(session, question)
        else:
            trace = recall_trace(context.name, [], tool="Recall off", elapsed_ms=0, buckets={})
        trace["agent"] = session["agent"]
        trace["responder"] = model if model and trace["evidence"] else "Evidence brief"
        output = answer(question, trace, model, AGENTS[session["agent"]])
        turn_id = uuid4().hex
        captured = recall
        if recall:
            try:
                context.capture(session, turn_id, question, output, trace)
            except ContextError as exc:
                captured = False
                trace.setdefault("warnings", []).append(f"Turn saved here only. {exc}")
        trace["capture"] = (
            ("conversation_add_message" if key else "SQLite transcript")
            if captured else ("Preview only" if not recall else "Local transcript only")
        )
        return store.save_turn(session, turn_id, question, output, trace, captured), 201

    @app.delete("/api/sessions/<session_id>")
    @locked
    def delete_session(session_id):
        json_body()
        session = get_session(session_id)
        context.forget_session(session)
        store.delete_session(session_id)
        return "", 204

    @app.get("/api/sessions/<session_id>/memories")
    @locked
    def list_memories(session_id):
        session = get_session(session_id)
        query = request.args.get("q", "").strip()
        page = request.args.get("page", 1 if "page" not in request.args else None, type=int)
        if page is None or page < 1 or len(query) > 300:
            abort(400, "Use a search of at most 300 characters and a valid page number.")
        rows, pages = context.list(session, query, page)
        return {"memories": rows, "pages": pages}

    @app.post("/api/sessions/<session_id>/memories")
    @locked
    def add_memory(session_id):
        data = json_body()
        text = input_text(data, "text", 1500)
        kind = input_text(data, "kind", 20, "fact")
        if kind not in KINDS:
            abort(400, "Choose fact, preference, or decision.")
        return context.add(get_session(session_id), text, kind), 201

    @app.delete("/api/sessions/<session_id>/memories/<memory_id>")
    @locked
    def delete_memory(session_id, memory_id):
        json_body()
        context.delete(get_session(session_id), memory_id)
        return "", 204

    return app
