"""An isolated, deterministic CMA L1 provider. No model or image processing."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import re
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from aggregation import Aggregator, Rules
from config import Config


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


class Problem(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class SnapshotStore:
    """Persist SQLite snapshots in the optional Chrono-cd Mongo add-on."""

    def __init__(self, uri, database):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000,
                                  connectTimeoutMS=5000, socketTimeoutMS=5000)
        self.collection = self.client[database]["sqlite_snapshots"]
        self.client.admin.command("ping")

    def list(self):
        return self.collection.find({}, {"_id": 1, "blob": 1})

    def save(self, key, blob):
        if len(blob) > 12 * 1024 * 1024:
            raise ValueError("serialized state exceeds snapshot limit")
        self.collection.replace_one({"_id": key}, {"_id": key, "blob": blob}, upsert=True)

    def close(self):
        self.client.close()


def check_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value):
        raise Problem(400, "invalid_identifier")
    return value


class State:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.engines = {}
        self.snapshots = None
        if config.mongo_uri:
            try:
                self.snapshots = SnapshotStore(config.mongo_uri, config.mongo_database)
                config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                for snapshot in self.snapshots.list():
                    path = config.data_dir / (snapshot["_id"] + ".sqlite3")
                    if not path.exists():
                        path.write_bytes(bytes(snapshot["blob"]))
            except (OSError, PyMongoError, ValueError):
                if self.snapshots is not None:
                    self.snapshots.close()
                raise
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A PVC mount may be writable through its fsGroup while refusing chmod
        # on the mount point itself. The platform owns that directory; SQLite
        # files below it are still tightened to mode 0600 in _open().
        try:
            config.data_dir.chmod(0o700)
        except PermissionError:
            pass
        for path in config.data_dir.glob("*.sqlite3"):
            engine = self._open(path)
            row = engine.db.execute("SELECT value FROM meta WHERE key='binding_id'").fetchone()
            if row:
                self.engines[json.loads(row[0])] = engine
            else:
                engine.close()

    def _open(self, path):
        engine = Aggregator(path, self.config.rules)
        path.chmod(0o600)
        engine.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS turns (
                conversation TEXT NOT NULL, key TEXT NOT NULL, request_digest TEXT NOT NULL,
                response TEXT NOT NULL, event_id TEXT NOT NULL, context TEXT NOT NULL,
                PRIMARY KEY (conversation,key), UNIQUE (conversation,event_id)
            );
        """)
        old = self.get_meta(engine, "rules_digest")
        if old is not None and old != digest(asdict(self.config.rules)):
            engine.close()
            raise ValueError("Persisted rules differ; use a new data volume for a different ruleset")
        return engine

    def _relay_engine(self):
        binding = "__relay_ingest__"
        engine = self.engines.get(binding)
        if engine is not None:
            return engine
        path = self.config.data_dir / "relay-ingest.sqlite3"
        engine = self._open(path)
        with engine.db:
            self.put_meta(engine, "binding_id", binding)
            self.put_meta(engine, "owner", {"kind": "relay", "subject": "ingest"})
            self.put_meta(engine, "profile_digest", "relay-ingest-v1")
            self.put_meta(engine, "rules_digest", digest(asdict(self.config.rules)))
        self.engines[binding] = engine
        self.persist(engine)
        return engine

    def persist(self, engine):
        if self.snapshots is None:
            return
        binding = self.get_meta(engine, "binding_id")
        if binding is not None:
            self.snapshots.save(hashlib.sha256(binding.encode()).hexdigest(), engine.db.serialize())

    @staticmethod
    def get_meta(engine, key, default=None):
        row = engine.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put_meta(engine, key, value):
        engine.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(value)))

    def engine(self, binding):
        check_id(binding)
        engine = self.engines.get(binding)
        if engine is None or self.get_meta(engine, "revoked", False):
            raise Problem(404, "binding_not_found")
        return engine

    def bind(self, binding, body):
        check_id(binding)
        if body.get("schema_version") != 1 or body.get("target", {"type": "profile"}) != {"type": "profile"}:
            raise Problem(400, "unsupported_binding_target")
        profile = body.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("metadata"), dict):
            raise Problem(400, "invalid_profile")
        if (profile["metadata"].get("provider") != self.config.provider_slug
                or body.get("profile_digest") != digest(profile)):
            raise Problem(400, "profile_digest_or_provider_mismatch")
        agent_key = body.get("agent_key")
        if not isinstance(agent_key, str) or not hmac.compare_digest(agent_key.encode(), self.config.provider_token.encode()):
            raise Problem(403, "binding_credential_mismatch")
        if not isinstance(profile["metadata"].get("owner"), dict):
            raise Problem(400, "profile_owner_required")
        owner = profile["metadata"]["owner"]
        if set(owner) != {"kind", "subject"} or not all(isinstance(x, str) and x for x in owner.values()):
            raise Problem(400, "invalid_profile_owner")
        existing = self.engines.get(binding)
        if existing is not None:
            if self.get_meta(existing, "revoked", False):
                raise Problem(409, "binding_revoked")
            if self.get_meta(existing, "owner") != owner:
                raise Problem(403, "binding_owner_mismatch")
            if self.get_meta(existing, "profile_digest") != body["profile_digest"]:
                raise Problem(409, "binding_profile_immutable")
        else:
            if len(self.engines) >= 32:
                raise Problem(429, "binding_limit")
            path = self.config.data_dir / (hashlib.sha256(binding.encode()).hexdigest() + ".sqlite3")
            engine = self._open(path)
            with engine.db:
                self.put_meta(engine, "binding_id", binding)
                self.put_meta(engine, "owner", owner)
                self.put_meta(engine, "profile_digest", body["profile_digest"])
                self.put_meta(engine, "rules_digest", digest(asdict(self.config.rules)))
            self.engines[binding] = engine
            self.persist(engine)
        # L1 is deliberately honest: the required model envelope is unused.
        return {"binding_id": binding, "level": "l1", "honoured": ["profile.metadata.owner"],
                "refused": [{"field": field, "reason": "Deterministic consumer; no model, tools, or arbitrary code execution"}
                            for field in ("profile.model", "profile.permission", "profile.context", "profile.environment")]}

    def conversation(self, binding, conversation, create=False, remove=False):
        check_id(conversation)
        engine = self.engine(binding)
        with engine.db:
            if remove:
                engine.db.execute("UPDATE conversations SET active=0 WHERE id=?", (conversation,))
            elif create:
                for name, other in self.engines.items():
                    if name != binding and other.db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation,)).fetchone():
                        raise Problem(409, "conversation_owned_elsewhere")
                row = engine.db.execute("SELECT active FROM conversations WHERE id=?", (conversation,)).fetchone()
                if row is not None and not row[0]:
                    raise Problem(409, "conversation_revoked")
                engine.db.execute("INSERT OR IGNORE INTO conversations VALUES(?,1)", (conversation,))
        self.persist(engine)
        return {"conversation_id": conversation}

    def _ingest(self, engine, event, now):
        cfg = self.config
        if not isinstance(event, dict) or event.get("source") != cfg.event_source:
            raise Problem(400, "event_source_mismatch")
        if event.get("subject") != cfg.rules.camera:
            raise Problem(400, "camera_mismatch")
        if event.get("type") == "frigate.bridge.heartbeat":
            if event.get("specversion") != "1.0" or not isinstance(event.get("id"), str) or not event["id"]:
                raise Problem(400, "invalid_heartbeat")
            data = event.get("data", {})
            if not isinstance(data, dict) or type(data.get("healthy")) is not bool or type(data.get("resynced")) is not bool:
                raise Problem(400, "invalid_heartbeat")
            # Health is receipt-time based, never the producer's editable clock.
            self.put_meta(engine, "heartbeat", {"at": now, "healthy": data["healthy"], "resynced": data["resynced"]})
            return "heartbeat_recorded"
        try:
            return engine.ingest(event, now)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Problem(400, "invalid_review_event") from None

    def ingest_relay(self, event):
        engine = self._relay_engine()
        return self._ingest(engine, event, time.time())

    def admit(self, body, key):
        if not key or len(key) > 256 or not key.isascii() or not key.isprintable():
            raise Problem(400, "idempotency_key_required")
        conversation = body.get("conversation")
        check_id(conversation)
        context = body.get("event_context")
        if not isinstance(context, dict) or context.get("schema_version") != 1 or context.get("conversation_id") != conversation:
            raise Problem(400, "event_context_required")
        activity = context.get("activity", {})
        if not isinstance(activity, dict):
            raise Problem(400, "invalid_activity")
        kind = activity.get("kind", {})
        if not isinstance(kind, dict) or kind.get("type") != "event" or kind.get("attachments"):
            raise Problem(400, "metadata_events_only")
        event = kind.get("ce")
        if not isinstance(event, dict) or activity.get("event_id") != event.get("id"):
            raise Problem(400, "event_identity_mismatch")
        event_id = check_id(event.get("id"))
        if body.get("stream") is not True:
            raise Problem(400, "stream_required")
        engine = next((e for e in self.engines.values() if not self.get_meta(e, "revoked", False)
                       and e.db.execute("SELECT 1 FROM conversations WHERE id=? AND active=1", (conversation,)).fetchone()), None)
        if engine is None:
            raise Problem(404, "conversation_not_found")
        request_digest = digest(body)
        with engine.db:
            engine.db.execute("BEGIN IMMEDIATE")
            old = engine.db.execute("SELECT * FROM turns WHERE conversation=? AND (key=? OR event_id=?)", (conversation, key, event_id)).fetchone()
            if old:
                if old["request_digest"] != request_digest or old["key"] != key:
                    raise Problem(409, "idempotency_conflict")
                return json.loads(old["response"])
            self._ingest(engine, event, time.time())
            response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "status": "completed", "output": []}
            engine.db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?)", (conversation, key, request_digest,
                              canonical(response), event_id, canonical(context)))
        self.persist(engine)
        return response

    def flush(self):
        now = time.time()
        with self.lock:
            count = 0
            for engine in self.engines.values():
                if self.get_meta(engine, "revoked", False):
                    continue
                heartbeat = self.get_meta(engine, "heartbeat", {})
                healthy = heartbeat.get("healthy") and heartbeat.get("resynced") and 0 <= now - heartbeat.get("at", 0) <= self.config.heartbeat_seconds
                decisions = engine.flush(now, source_healthy=bool(healthy))
                count += len(decisions)
                if decisions:
                    self.persist(engine)
            return count

    def close(self):
        for engine in self.engines.values():
            self.persist(engine)
            engine.close()
        if self.snapshots is not None:
            self.snapshots.close()


def create_app(config=None):
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or Config.from_env()
        app.state.worker = State(cfg)

        async def timer():
            while True:
                await asyncio.sleep(cfg.tick_seconds)
                app.state.worker.flush()

        task = asyncio.create_task(timer())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            app.state.worker.close()

    app = FastAPI(title="Frigate CMA Events", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)

    @app.exception_handler(Problem)
    async def problem_handler(_, error):
        return JSONResponse({"type": "about:blank", "title": error.code, "status": error.status, "code": error.code},
                            status_code=error.status, media_type="application/problem+json")

    @app.middleware("http")
    async def protect(request, call_next):
        # Read a bounded stream before any JSON parser allocates its tree.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 256 * 1024:
                return JSONResponse({"code": "payload_too_large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def authorize(request, operator=False, relay=False):
        state = request.app.state.worker
        token = (state.config.api_token if operator else (state.config.relay_token or state.config.api_token) if relay
                 else state.config.provider_token)
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            raise Problem(401, "not_authenticated")
        return state

    async def body(request):
        try:
            value = json.loads(await request.body(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise Problem(400, "invalid_json") from None

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": "0.1.0", "model_calls": 0}

    @app.post("/ingest")
    async def ingest(request: Request):
        state = authorize(request, relay=True)
        value = await body(request)
        with state.lock:
            result = state.ingest_relay(value)
        return {"accepted": result, "event_id": value.get("id")}

    @app.get("/.well-known/agent-card.json")
    def card():
        return {"name": "Frigate rule consumer", "capabilities": {"streaming": True},
                "description": "CMA L1 deterministic metadata aggregation; no model inference, media, shell, or tools"}

    @app.put("/api/v1/cmaeg/bindings/{binding}")
    async def bind(binding: str, request: Request):
        state = authorize(request)
        value = await body(request)
        with state.lock:
            return state.bind(binding, value)

    @app.delete("/api/v1/cmaeg/bindings/{binding}")
    def unbind(binding: str, request: Request):
        state = authorize(request)
        with state.lock:
            engine = state.engine(binding)
            with engine.db:
                state.put_meta(engine, "revoked", True)
                engine.db.execute("UPDATE conversations SET active=0")
            state.persist(engine)
        return Response(status_code=204)

    @app.put("/api/v1/cmaeg/bindings/{binding}/conversations/{conversation}")
    def conversation_put(binding: str, conversation: str, request: Request):
        state = authorize(request)
        with state.lock:
            return state.conversation(binding, conversation, create=True)

    @app.delete("/api/v1/cmaeg/bindings/{binding}/conversations/{conversation}")
    def conversation_delete(binding: str, conversation: str, request: Request):
        state = authorize(request)
        with state.lock:
            state.conversation(binding, conversation, remove=True)
        return Response(status_code=204)

    @app.post("/api/v2/responses")
    async def responses(request: Request):
        state = authorize(request)
        value = await body(request)
        with state.lock:
            result = state.admit(value, request.headers.get("idempotency-key"))
        created = {"type": "response.created", "response": {"id": result["id"], "status": "in_progress"}}
        completed = {"type": "response.completed", "response": result}
        text = "".join("event: " + item["type"] + "\ndata: " + canonical(item) + "\n\n" for item in (created, completed))
        return StreamingResponse(iter([text]), media_type="text/event-stream")

    @app.get("/api/v1/cmaeg/bindings/{binding}/conversations/{conversation}/events/{event_id}")
    def context(binding: str, conversation: str, event_id: str, request: Request):
        state = authorize(request)
        with state.lock:
            engine = state.engine(binding)
            active = engine.db.execute("SELECT 1 FROM conversations WHERE id=? AND active=1", (conversation,)).fetchone()
            row = engine.db.execute("SELECT context FROM turns WHERE conversation=? AND event_id=?", (conversation, event_id)).fetchone()
            if not active or row is None:
                raise Problem(404, "event_context_not_found")
            return json.loads(row[0])

    @app.get("/api/status")
    def status(request: Request):
        state = authorize(request, operator=True)
        with state.lock:
            return {"mode": "observe", "model_calls": 0, "bindings": len(state.engines),
                    "decisions": sum(e.db.execute("SELECT count(*) FROM decisions").fetchone()[0] for e in state.engines.values()),
                    "turns": sum(e.db.execute("SELECT count(*) FROM turns").fetchone()[0] for e in state.engines.values())}

    @app.get("/api/bindings/{binding}/decisions")
    def decisions(binding: str, request: Request):
        state = authorize(request, operator=True)
        with state.lock:
            engine = state.engine(binding)
            rows = engine.db.execute("SELECT payload FROM decisions ORDER BY at DESC LIMIT 100")
            return {"decisions": [json.loads(r[0]) for r in rows]}

    @app.post("/api/replay")
    async def replay_api(request: Request):
        authorize(request, operator=True)
        value = await body(request)
        try:
            cfg = Rules(**value["rules"])
            reviews = value["reviews"]
            if not isinstance(reviews, list) or len(reviews) > 100:
                raise ValueError()
            engine = Aggregator(":memory:", cfg)
            try:
                from aggregation import wrap_review
                for row in sorted(reviews, key=lambda r: r["end_time"]):
                    engine.ingest(wrap_review({"type": "end", "before": row, "after": row}, "urn:frigate:replay"), row["end_time"])
                now = max(r["end_time"] for r in reviews) + max(cfg.merge_seconds, cfg.delivery_grace_seconds) + 1
                output = engine.flush(now, source_healthy=True)
            finally:
                engine.close()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Problem(400, "invalid_replay") from None
        return {"decisions": output, "model_calls": 0, "persistent_writes": 0}

    return app


app = create_app()
