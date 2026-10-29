"""Optional owner-authorized cleanup for disposable CMA per-event agents.

This uses public resource APIs. The management key stays on the local service;
workers never receive it. Deploy only with a profile target, per_event partition, and its dedicated workspace.
Cleanup requires matching CMA resource membership and immutable profile identity.
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
        allowed = {"cma_url", "agent_profile_ids", "workspace_id", "grace_seconds"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("Invalid CMA context cleanup configuration")
        for key in ("cma_url",):
            url = urlsplit(config.get(key, ""))
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("CMA cleanup requires HTTPS service bases")
        for key, pattern in (("workspace_id", r"wks_[a-zA-Z0-9]{8,128}"),):
            if not re.fullmatch(pattern, config.get(key, "")):
                raise ValueError("CMA cleanup requires exact channel and dedicated workspace")
        profiles = config.get("agent_profile_ids")
        if not isinstance(profiles, list) or not 1 <= len(profiles) <= 8 or any(not isinstance(p, str) or not re.fullmatch(r"agp_[a-zA-Z0-9]{8,128}", p) for p in profiles):
            raise ValueError("CMA cleanup requires pinned native Agent profile IDs")
        self.grace = config.get("grace_seconds", 600)
        if type(self.grace) is not int or not 300 <= self.grace <= 86400:
            raise ValueError("Invalid CMA cleanup retention")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("CMA cleanup requires a separate scoped owner key")
        self.config, self.token = config, token
        self.opener = build_opener(NoRedirect)
        self.done = set()
        self.status = {"enabled": True, "retired": 0, "pending": 0, "scan_limited": False,
                       "last_success_at": None, "error": None}

    def call(self, base, path, method="GET"):
        headers = {"Authorization": "Bearer " + self.token, "User-Agent": "cma-context-cleanup/1"}
        if method in {"DELETE", "POST"}:
            headers["Idempotency-Key"] = "context-cleanup-" + method.lower() + "-" + path.replace("/", "-")
            headers["Content-Type"] = "application/json"
        request = Request(base.rstrip("/") + "/" + path, method=method, data=b"{}" if method == "POST" else None,
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
        # Deleted resources remain in CMA's inventory. Filter by actionable
        # lifecycle states so historical tombstones cannot fill the scan forever.
        # Each run drains at most one page per state; deletion removes entries
        # from these filtered queues, so a backlog can progress on later ticks.
        agents = {}
        limited = False
        for state in ("ready", "stopped", "failed"):
            page = self.call(cfg["cma_url"], "agents?" + urlencode({
                "workspace": cfg["workspace_id"], "status": state, "limit": 50}))
            limited |= bool(page.get("next_cursor"))
            for agent in page.get("items", [])[:50]:
                agents[agent.get("id", "")] = agent
        self.status["scan_limited"] = limited
        pending = 0
        for agent in agents.values():
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
            if agent["status"] != "stopped" and now - timestamp(agent["updated_at"]) < self.grace:
                continue
            if (agent.get("workspace_id") != cfg["workspace_id"]
                    or agent.get("profile", {}).get("id") not in cfg["agent_profile_ids"]):
                continue
            # Recheck the resource immediately before mutation; never stop work
            # or a pending approval, and only act on the pinned profile in this dedicated workspace.
            detail = self.call(cfg["cma_url"], "agents/" + aid)
            if detail["workspace"]["id"] != cfg["workspace_id"] or detail["status"] not in {"ready", "stopped", "failed"} or detail["profile"]["id"] not in cfg["agent_profile_ids"]:
                continue
            if detail["status"] != "stopped" and now - timestamp(detail["updated_at"]) < self.grace:
                continue
            if detail["status"] == "ready":
                self.call(cfg["cma_url"], "agents/" + aid + "/stop", "POST")
                stopped = self.call(cfg["cma_url"], "agents/" + aid)
                if stopped["status"] != "stopped":
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
