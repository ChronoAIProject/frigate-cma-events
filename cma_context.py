"""Optional owner-authorized cleanup for disposable CMA per-event agents.

This uses public resource APIs. The management key stays on the local service;
workers never receive it. Deploy only with a profile target, per_event partition, and its dedicated workspace.
Cleanup requires matching CMA resource membership and binding authority.
"""
from datetime import datetime
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from evidence import NoRedirect


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class ContextCleanup:
    def __init__(self, config, token):
        allowed = {"cma_url", "binding_id", "workspace_id", "grace_seconds"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("Invalid CMA context cleanup configuration")
        for key in ("cma_url",):
            url = urlsplit(config.get(key, ""))
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("CMA cleanup requires HTTPS service bases")
        for key, pattern in (("binding_id", r"bnd_[a-zA-Z0-9]{8,128}"), ("workspace_id", r"wks_[a-zA-Z0-9]{8,128}")):
            if not re.fullmatch(pattern, config.get(key, "")):
                raise ValueError("CMA cleanup requires exact channel and dedicated workspace")
        self.grace = config.get("grace_seconds", 600)
        if type(self.grace) is not int or not 300 <= self.grace <= 86400:
            raise ValueError("Invalid CMA cleanup retention")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("CMA cleanup requires a separate scoped owner key")
        self.config, self.token = config, token
        self.opener = build_opener(NoRedirect)
        self.done = set()
        self.status = {"enabled": True, "retired": 0, "pending": 0, "last_success_at": None, "error": None}

    def call(self, base, path, method="GET"):
        headers = {"Authorization": "Bearer " + self.token, "User-Agent": "cma-context-cleanup/1"}
        if method == "DELETE":
            headers["Idempotency-Key"] = "context-cleanup-" + path.rsplit("/", 1)[-1]
        request = Request(base.rstrip("/") + "/" + path, method=method,
                          headers=headers)
        with self.opener.open(request, timeout=8) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("CMA cleanup response too large")
            return json.loads(raw) if raw else None

    def run(self, now=None):
        now = time.time() if now is None else now
        try:
            self._run(now)
            self.status.update(last_success_at=now, error=None)
        except Exception as exc:
            # Never include URLs, bearer values, upstream bodies or event data.
            self.status["error"] = "http_" + str(exc.code) if isinstance(exc, HTTPError) else type(exc).__name__
        return dict(self.status)

    def _run(self, now):
        cfg = self.config
        binding = cfg["binding_id"]
        page = self.call(cfg["cma_url"], "agents?" + urlencode({"workspace": cfg["workspace_id"], "limit": 50}))
        agents = page.get("items", [])
        if page.get("next_cursor") or len(agents) >= 50:
            raise ValueError("Dedicated cleanup workspace exceeds bounded scan")
        pending = 0
        for agent in agents[:50]:
            aid = agent.get("id", "")
            if not re.fullmatch(r"agt_[a-zA-Z0-9]{8,128}", aid) or aid in self.done:
                continue
            if agent.get("status") == "deleted":
                self.done.add(aid)
                self.status["retired"] += 1
                continue
            pending += 1
            if agent.get("status") not in {"ready", "stopped", "failed"}:
                continue
            if now - timestamp(agent["updated_at"]) < self.grace:
                continue
            usage = self.call(cfg["cma_url"], "agents/" + aid + "/channel-usage")
            bindings = usage.get("bindings", [])
            if (not bindings or any(b.get("binding_id") != binding for b in bindings)
                    or any(b.get("last_used_at") and now - timestamp(b["last_used_at"]) < self.grace for b in bindings)):
                continue
            # Recheck the resource immediately before mutation; never stop work
            # or a pending approval, and never act on a borrowed/shared agent.
            detail = self.call(cfg["cma_url"], "agents/" + aid)
            if detail["workspace"]["id"] != cfg["workspace_id"] or detail["status"] not in {"ready", "stopped", "failed"}:
                continue
            if now - timestamp(detail["updated_at"]) < self.grace:
                continue
            self.call(cfg["cma_url"], "agents/" + aid, "DELETE")
            final = self.call(cfg["cma_url"], "agents/" + aid)
            if final["status"] == "deleted":
                self.done.add(aid)
                self.status["retired"] += 1
                pending -= 1
        self.status["pending"] = pending
        if len(self.done) > 1000:
            self.done.clear()
