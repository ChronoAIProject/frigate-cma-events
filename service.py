"""Deterministic aggregation and bounded evidence; no model inference."""

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from aggregation import Aggregator, Rules
from config import Config
from evidence import EvidenceUnavailable, FrigateEvidence
from pipeline import Pipeline


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def cma_event_id(source, candidate_id, event_type="activity.window_candidate"):
    """Return a stable CloudEvent identity for one configured output."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, source + "\n" + event_type + "\n" + candidate_id))


def cma_signature(secret, webhook_id, webhook_timestamp, raw_body):
    signed = webhook_id + "." + webhook_timestamp + "." + raw_body
    value = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).digest()
    return "v1," + base64.b64encode(value).decode("ascii")


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
        self.pipeline = config.pipeline
        self.lock = threading.RLock()
        self.engines = {}
        media = self.pipeline.spec["evidence"]
        self.evidence = (FrigateEvidence(config.frigate_proxy_url, config.frigate_api_key, self.pipeline.subject,
                                        media["crop_zone"], config.evidence_options, labels=media["labels"],
                                        version=media["version"], cautions=media["cautions"],
                                        aliases=self.pipeline.spec["output"]["aliases"], allow_http=config.frigate_direct)
                         if config.frigate_proxy_url and media["adapter"] == "frigate" else None)
        self.evidence_cache = {}
        self.evidence_busy = False
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
        engine = Aggregator(path, self.pipeline)
        path.chmod(0o600)
        engine.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS turns (
                conversation TEXT NOT NULL, key TEXT NOT NULL, request_digest TEXT NOT NULL,
                response TEXT NOT NULL, event_id TEXT NOT NULL, context TEXT NOT NULL,
                PRIMARY KEY (conversation,key), UNIQUE (conversation,event_id)
            );
            CREATE TABLE IF NOT EXISTS cma_deliveries (
                event_id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
                delivered_at REAL, last_status INTEGER, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS validations (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS analysis_results (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        old_policy = self.get_meta(engine, "policy_digest")
        old_rules = self.get_meta(engine, "rules_digest")
        current = digest(self.pipeline.state_document())
        legacy = self.config.legacy_rules_document
        if ((old_policy is not None and old_policy != current)
                or (old_policy is None and old_rules is not None and (legacy is None or old_rules != digest(legacy)))):
            engine.close()
            raise ValueError("Persisted policy differs; upgrade legacy env first or use separate state for a different policy")
        with engine.db:
            self.put_meta(engine, "policy_digest", current)
        return engine

    def status(self):
        with self.lock:
            counts = {"pending": 0, "delivered": 0, "failed": 0}
            for engine in self.engines.values():
                counts["pending"] += engine.db.execute("SELECT count(*) FROM cma_deliveries WHERE delivered_at IS NULL").fetchone()[0]
                counts["delivered"] += engine.db.execute("SELECT count(*) FROM cma_deliveries WHERE delivered_at IS NOT NULL").fetchone()[0]
                counts["failed"] += engine.db.execute("SELECT count(*) FROM cma_deliveries WHERE delivered_at IS NULL AND last_status BETWEEN 400 AND 499 AND last_status != 429").fetchone()[0]
            return {"mode": "observe", "model_calls": 0, "bindings": len(self.engines),
                    "decisions": sum(e.db.execute("SELECT count(*) FROM decisions").fetchone()[0] for e in self.engines.values()),
                    "turns": sum(e.db.execute("SELECT count(*) FROM turns").fetchone()[0] for e in self.engines.values()),
                    "cma_deliveries": counts}

    def stamp_rules(self, engine):
        if self.config.legacy_rules_document is not None:
            self.put_meta(engine, "rules_digest", digest(self.config.legacy_rules_document))
        self.put_meta(engine, "policy_digest", digest(self.pipeline.state_document()))

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
            self.stamp_rules(engine)
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
                self.stamp_rules(engine)
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
        source = self.pipeline.spec["source"]
        if event.get("type") == source["health"]["event_type"]:
            if source["subject"] != "*" and event.get("subject") != source["subject"]:
                raise Problem(400, "subject_mismatch")
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
            raise Problem(400, "invalid_source_event") from None

    def ingest_relay(self, event):
        if not self.config.processing_enabled:
            raise Problem(503, "processing_paused")
        engine = self._relay_engine()
        with engine.db:
            result = self._ingest(engine, event, time.time())
        self.persist(engine)
        return result

    def candidate(self, candidate_id):
        check_id(candidate_id)
        engine = self._relay_engine()
        row = engine.db.execute("SELECT payload FROM decisions WHERE id=? AND reason='candidate'", (candidate_id,)).fetchone()
        if row is None:
            row = engine.db.execute("SELECT payload FROM validations WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise Problem(404, "candidate_not_found")
        payload = json.loads(row[0])
        return self.pipeline.aliases({**payload, "candidate_id": candidate_id,
                                      "subject": payload.get("subject", self.pipeline.subject)})

    def reconcile(self, engine, now):
        """REST repair precedes finalization; broker connectivity is insufficient."""
        cfg = self.pipeline
        try:
            after = now - cfg.maximum_seconds - cfg.merge_seconds - cfg.delivery_grace_seconds
            rows = self.evidence.reviews(after)
            from aggregation import wrap_review
            with engine.db:
                for row in rows:
                    old = engine.db.execute("SELECT start,end,data FROM reviews WHERE id=?", (row["id"],)).fetchone()
                    normalized = {k: row.get(k) for k in ("id", "camera", "severity", "data")}
                    if old and old["start"] == row["start_time"] and old["end"] == row.get("end_time") and json.loads(old["data"]) == normalized:
                        continue
                    phase = "end" if row.get("end_time") is not None else "update"
                    engine.ingest(wrap_review({"type": phase, "before": row, "after": row}, self.config.event_source), now)
            self.persist(engine)
            return True
        except (EvidenceUnavailable, ValueError, KeyError, TypeError):
            return False

    def admit(self, body, key):
        if not self.config.processing_enabled:
            raise Problem(503, "processing_paused")
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

    def _queue_cma_candidate(self, engine, payload, now):
        """Durably enqueue a candidate before attempting external delivery."""
        if not self.config.cma_event_url or payload.get("decision") != "candidate":
            return
        candidate_id = payload.get("candidate_id")
        subject = payload.get("subject")
        event_type = self.pipeline.spec["output"]["event_type"]
        if not isinstance(candidate_id, str) or not isinstance(subject, str):
            return
        event = {
            "specversion": "1.0",
            "id": cma_event_id(self.config.event_source, candidate_id, event_type),
            "source": self.config.event_source,
            "type": event_type,
            "subject": subject,
            "datacontenttype": "application/json",
            "data": payload,
        }
        if self.config.evidence_service and self.pipeline.spec["evidence"]["adapter"] != "none":
            event["data"] = {**payload, "evidence_budget": (payload["evidence_budget"] if payload.get("test")
                            else self.config.evidence_options.budget()), "evidence": {
                "service": self.config.evidence_service, "path": "evidence/" + candidate_id,
                "result_path": "results/" + candidate_id,
                "instructions": "Fetch this bounded evidence JSON through NyxID into a private file; decode images[].image_base64 locally and view each page once within evidence_budget. Never print base64. A 202 pending response or interrupted download may be retried at most 6 times after 5 seconds; cached retrieval does not resample. Frames have IDs and actual timestamps. Read all cautions; quality/novelty/track counts are heuristics, not semantic conclusions. Never call raw source media or live images. Follow the configured downstream workflow, deduplicate by candidate_id, independently verify side effects and then POST the configured result fields to result_path. integration_test is synthetic; unavailable evidence means pending."}}
        if self.config.evidence_service and self.config.evidence_token:
            event["data"] = {**event["data"], "result_callback": {"service": self.config.evidence_service,
                "path": "results/" + candidate_id, "statuses": self.pipeline.spec["output"]["result_statuses"],
                "fields": self.pipeline.spec["output"]["result_fields"]}}
        with engine.db:
            engine.db.execute(
                "INSERT OR IGNORE INTO cma_deliveries(event_id,body,created_at,next_attempt_at) VALUES(?,?,?,?)",
                (event["id"], canonical(event), now, now),
            )

    @staticmethod
    def _delivery_error(exc):
        if isinstance(exc, HTTPError):
            return exc.code, "http_error"
        if isinstance(exc, URLError):
            return None, "network_error"
        return None, "delivery_error"

    def _deliver_pending(self, engines):
        """Send due candidates; retries are durable and bounded by response class."""
        if not self.config.cma_event_url:
            return
        due = []
        now = time.time()
        with self.lock:
            for binding, engine in engines:
                if binding != "__relay_ingest__":
                    continue
                rows = engine.db.execute(
                    "SELECT event_id,body,attempts FROM cma_deliveries "
                    "WHERE delivered_at IS NULL AND next_attempt_at<=? ORDER BY created_at LIMIT 8", (now,)
                ).fetchall()
                due.extend((engine, row) for row in rows)
        for engine, row in due:
            webhook_id = row["event_id"]
            timestamp = str(int(time.time()))
            raw = row["body"].encode("utf-8")
            request = UrlRequest(
                self.config.cma_event_url,
                data=raw,
                method="POST",
                headers={
                    "Content-Type": "application/cloudevents+json",
                    "User-Agent": "frigate-cma-events/0.2",
                    "webhook-id": webhook_id,
                    "webhook-timestamp": timestamp,
                    "webhook-signature": cma_signature(self.config.cma_hmac_secret, webhook_id, timestamp, row["body"]),
                },
            )
            status, error = None, None
            try:
                with urlopen(request, timeout=self.config.cma_timeout_seconds) as response:
                    status = int(response.status)
                    response.read(1024)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                status, error = self._delivery_error(exc)
            delivered = status is not None and 200 <= status < 300
            retry = status is None or status == 429 or status >= 500
            with self.lock:
                attempts = int(row["attempts"]) + 1
                if delivered:
                    engine.db.execute(
                        "UPDATE cma_deliveries SET attempts=?,delivered_at=?,last_status=?,last_error=NULL WHERE event_id=?",
                        (attempts, time.time(), status, webhook_id),
                    )
                elif retry:
                    delay = min(300, max(2, 2 ** min(attempts - 1, 8)))
                    engine.db.execute(
                        "UPDATE cma_deliveries SET attempts=?,next_attempt_at=?,last_status=?,last_error=? WHERE event_id=?",
                        (attempts, time.time() + delay, status, error or "retryable_http_error", webhook_id),
                    )
                else:
                    # A permanent 4xx is retained for inspection but never hot-looped.
                    engine.db.execute(
                        "UPDATE cma_deliveries SET attempts=?,next_attempt_at=?,last_status=?,last_error=? WHERE event_id=?",
                        (attempts, time.time() + 10**9, status, error or "permanent_http_error", webhook_id),
                    )
                self.persist(engine)

    def flush(self):
        if not self.config.processing_enabled:
            return 0
        now = time.time()
        delivery_engines = []
        with self.lock:
            count = 0
            for engine in self.engines.values():
                if self.get_meta(engine, "revoked", False):
                    continue
                binding = self.get_meta(engine, "binding_id")
                if binding == "__relay_ingest__":
                    delivery_engines.append((binding, engine))
                heartbeat = self.get_meta(engine, "heartbeat", {})
                health_policy = self.pipeline.spec["source"]["health"]
                healthy = (health_policy["mode"] == "observed_events" or (heartbeat.get("healthy")
                           and (heartbeat.get("resynced") or not health_policy["require_resynced"])
                           and 0 <= now - heartbeat.get("at", 0) <= self.config.heartbeat_seconds))
                if binding == "__relay_ingest__" and self.evidence:
                    connected = heartbeat.get("healthy") and 0 <= now - heartbeat.get("at", 0) <= self.config.heartbeat_seconds
                    # A successful REST check replaces the relay's unverified resynced assertion.
                    healthy = bool(connected and self.reconcile(engine, now))
                decisions = engine.flush(now, source_healthy=bool(healthy))
                count += len(decisions)
                if decisions:
                    if binding == "__relay_ingest__":
                        for decision in decisions:
                            self._queue_cma_candidate(engine, decision, now)
                    self.persist(engine)
        self._deliver_pending(delivery_engines)
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
            next_report = 0.0
            while True:
                await asyncio.sleep(cfg.tick_seconds)
                await asyncio.to_thread(app.state.worker.flush)
                if time.monotonic() >= next_report:
                    print("pipeline health " + json.dumps(app.state.worker.status(), sort_keys=True), flush=True)
                    next_report = time.monotonic() + 60

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

    app = FastAPI(title="CMA Event Pipeline", version="0.2.2", lifespan=lifespan,
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
        return {"status": "ok", "version": "0.2.2", "worker_name": app.state.worker.config.worker_name,
                "model_calls": 0}

    @app.post("/ingest")
    async def ingest(request: Request):
        state = authorize(request, relay=True)
        value = await body(request)
        with state.lock:
            result = state.ingest_relay(value)
        return {"accepted": result, "event_id": value.get("id")}

    def evidence_authorize(request):
        state = request.app.state.worker
        if not state.config.evidence_token:
            raise Problem(503, "evidence_disabled")
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                   ("Bearer " + state.config.evidence_token).encode()):
            raise Problem(401, "not_authenticated")
        return state

    @app.get("/evidence/{candidate_id}")
    def evidence(candidate_id: str, request: Request):
        state = evidence_authorize(request)
        if state.evidence is None:
            raise Problem(503, "evidence_disabled")
        with state.lock:
            candidate = state.candidate(candidate_id)
            # A fixed bounded evidence pack per candidate; no source media on disk.
            if candidate_id in state.evidence_cache:
                return state.evidence_cache[candidate_id]
            if state.evidence_busy:
                return JSONResponse(state.pipeline.aliases({"status": "pending", "candidate_id": candidate_id}), status_code=202,
                                    headers={"Retry-After": "5"})
            state.evidence_busy = True
        # Historical extraction must not hold the lock used by live ingestion.
        try:
            try:
                result = state.evidence.composite(candidate)
            except EvidenceUnavailable as exc:
                raise Problem(424, str(exc)) from None
            with state.lock:
                while len(state.evidence_cache) >= 4:
                    state.evidence_cache.pop(next(iter(state.evidence_cache)))
                state.evidence_cache[candidate_id] = result
            return result
        finally:
            with state.lock:
                state.evidence_busy = False

    @app.post("/results/{candidate_id}")
    async def analysis_result(candidate_id: str, request: Request):
        state = evidence_authorize(request)
        if not state.config.processing_enabled:
            raise Problem(503, "processing_paused")
        value = await body(request)
        output = state.pipeline.spec["output"]
        if set(value) - {"status", *output["result_fields"]} or value.get("status") not in output["result_statuses"]:
            raise Problem(400, "invalid_result")
        if any(not isinstance(v, str) or len(v) > 2000 for v in value.values()):
            raise Problem(400, "invalid_result")
        with state.lock:
            candidate = state.candidate(candidate_id)
            if candidate.get("test") and value["status"] not in {"pending", "integration_test"}:
                raise Problem(400, "test_is_not_a_real_outcome")
            engine = state._relay_engine()
            with engine.db:
                engine.db.execute("INSERT OR REPLACE INTO analysis_results VALUES(?,?)", (candidate_id, canonical(value)))
            state.persist(engine)
        return state.pipeline.aliases({"candidate_id": candidate_id, "accepted": True})

    @app.post("/api/validation")
    def validation(request: Request):
        state = authorize(request, operator=True)
        if not state.config.processing_enabled:
            raise Problem(503, "processing_paused")
        if not state.evidence:
            raise Problem(503, "evidence_disabled")
        with state.lock:
            engine = state._relay_engine()
            # Separate table, explicit synthetic ID; never enters real review aggregation.
            try:
                rows = [r for r in state.evidence.reviews(time.time() - 86400) if r.get("end_time") is not None]
                review = max(rows, key=lambda r: r["end_time"])
            except (EvidenceUnavailable, ValueError, KeyError, TypeError):
                raise Problem(424, "validation_history_unavailable") from None
            candidate_id, now = "integration_test_" + uuid.uuid4().hex, review["end_time"]
            candidate = state.pipeline.aliases({"candidate_id": candidate_id, "subject": state.pipeline.subject, "decision": "candidate",
                         "started_at": review["start_time"], "ended_at": now, "test": True,
                         "validation_mode": "integration_test", "event_ids": [review["id"]],
                         "evidence_budget": {"max_model_turns": 1, "max_images": 1, "max_source_frames": 1}})
            with engine.db:
                engine.db.execute("INSERT INTO validations VALUES(?,?)", (candidate_id, canonical(candidate)))
                state._queue_cma_candidate(engine, candidate, time.time())
            state.persist(engine)
        return state.pipeline.aliases({"candidate_id": candidate_id, "test": True})

    @app.get("/.well-known/agent-card.json")
    def card():
        return {"name": app.state.worker.config.worker_name, "capabilities": {"streaming": True},
                "description": "CMA L1 deterministic metadata aggregation and optional bounded evidence; no model inference or shell"}

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
        return authorize(request, operator=True).status()

    @app.get("/api/results/{candidate_id}")
    def result_read(candidate_id: str, request: Request):
        state = authorize(request, operator=True)
        with state.lock:
            state.candidate(candidate_id)
            row = state._relay_engine().db.execute("SELECT payload FROM analysis_results WHERE id=?", (candidate_id,)).fetchone()
            return state.pipeline.aliases({"candidate_id": candidate_id, "result": json.loads(row[0]) if row else None})

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
            if "events" in value:
                from aggregation import replay_events
                state = request.app.state.worker
                cfg = Pipeline.parse(value["pipeline"]) if "pipeline" in value else state.pipeline
                return {"decisions": replay_events(value["events"], cfg, value.get("now")),
                        "model_calls": 0, "persistent_writes": 0}
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
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise Problem(400, "invalid_replay") from None
        return {"decisions": output, "model_calls": 0, "persistent_writes": 0}

    @app.get("/api/pipeline")
    def pipeline_config(request: Request):
        state = authorize(request, operator=True)
        return {"pipeline": state.pipeline.spec, "state_digest": digest(state.pipeline.state_document()),
                "evidence_options": {name: getattr(state.config.evidence_options, name)
                                     for name in state.config.evidence_options.__dataclass_fields__}}

    @app.get("/api/state/export")
    def export_state(request: Request):
        state = authorize(request, operator=True)
        if state.config.processing_enabled:
            raise Problem(409, "pause_processing_before_export")
        with state.lock:
            databases = []
            for binding, engine in sorted(state.engines.items()):
                raw = engine.db.serialize()
                if len(raw) > 12 * 1024 * 1024:
                    raise Problem(413, "state_too_large")
                databases.append({"binding": binding, "sha256": hashlib.sha256(raw).hexdigest(),
                                  "sqlite_base64": base64.b64encode(raw).decode("ascii")})
            if sum(len(row["sqlite_base64"]) for row in databases) > 20 * 1024 * 1024:
                raise Problem(413, "state_too_large")
            return {"schema": "event-pipeline-state.v1", "source": state.config.event_source,
                    "policy_digest": digest(state.pipeline.state_document()), "databases": databases}

    return app


app = create_app()
