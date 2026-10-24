"""Exercise a deployed provider with synthetic events; no model or chat calls."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aggregation import wrap_review
from config import secret
from service import digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    provider = {"Authorization": "Bearer " + secret("PROVIDER_TOKEN")}
    operator = {"Authorization": "Bearer " + secret("API_TOKEN")}
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30, follow_redirects=False) as client:
        def checked(method, path, *, status=200, headers=provider, **kwargs):
            response = client.request(method, path, headers=headers, **kwargs)
            if response.status_code != status:
                raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
            return response

        assert client.get("/healthz").json()["model_calls"] == 0
        assert client.get("/api/status").status_code == 401
        if args.resume:
            saved = json.loads(args.state.read_text())
            response = checked("POST", "/api/v2/responses", json=saved["turn"], headers={**provider, "Idempotency-Key": saved["key"]})
            assert response.text == saved["response"]
            path = f"/api/v1/cmaeg/bindings/{saved['binding']}/conversations/{saved['conversation']}/events/{saved['event_id']}"
            assert checked("GET", path).json() == saved["turn"]["event_context"]
            decisions = checked("GET", f"/api/bindings/{saved['binding']}/decisions", headers=operator).json()["decisions"]
            assert len(decisions) == 1 and decisions[0]["decision"] == "candidate"
            print(json.dumps({"restart_persistence": "passed", "same_response_id": True, "decisions": 1, "model_calls": 0}))
            return
        camera, seats, table, source = [os.environ[name] for name in ("CAMERA", "SEATS_ZONE", "TABLE_ZONE", "EVENT_SOURCE")]
        identity = uuid.uuid4().hex[:16]
        binding_id, conversation_id = "smoke_" + identity, "conv_" + identity
        profile = {"schema_version": 1, "metadata": {"name": "Synthetic provider test", "provider": "frigate_rules",
                   "owner": {"kind": "human", "subject": "synthetic-test-owner"}},
                   "model": {"access": "managed", "service": "unused", "api_type": "responses", "model": "none", "thinking_effort": "low"},
                   "permission": {"action_mode": "guarded", "service_scope": []}, "context": {}}
        checked("PUT", f"/api/v1/cmaeg/bindings/{binding_id}", json={"schema_version": 1, "profile": profile,
                "profile_digest": digest(profile), "agent_key": secret("PROVIDER_TOKEN"), "tools": {}})
        checked("PUT", f"/api/v1/cmaeg/bindings/{binding_id}/conversations/{conversation_id}", json={})
        now = time.time()
        saved = None
        events = []
        for index, (start, end) in enumerate(((now - 120, now - 100), (now - 99, now - 40), (now - 39, now - 35))):
            row = {"id": f"synthetic-{identity}-{index}", "camera": camera, "start_time": start, "end_time": end,
                   "severity": "alert", "data": {"objects": ["person", "plate"], "zones": [seats, table], "detections": []}}
            events.append(wrap_review({"type": "end", "before": row, "after": row}, source))
        events.append({"specversion": "1.0", "source": source, "id": "heartbeat-" + identity,
                       "type": "frigate.bridge.heartbeat", "subject": camera, "data": {"healthy": True, "resynced": True}})
        for index, event in enumerate(events):
            key = "smoke-" + event["id"]
            value = {"conversation": conversation_id, "stream": True,
                     "input": [{"role": "user", "content": [{"type": "input_text", "text": "Synthetic metadata test"}]}],
                     "metadata": {}, "event_context": {"schema_version": 1, "channel_id": "synthetic-channel",
                         "conversation_id": conversation_id, "event_ref": "synthetic-reference", "activity": {
                             "schema_version": 1, "event_id": event["id"], "kind": {"type": "event", "ce": event},
                             "actor": {"id": source, "kind": "system"}, "conversation": {"id": event["id"], "kind": "custom"},
                             "occurred_at": datetime.now(timezone.utc).isoformat(), "source": {"type": "custom"}}}}
            result = checked("POST", "/api/v2/responses", json=value, headers={**provider, "Idempotency-Key": key})
            assert "response.created" in result.text and "response.completed" in result.text
            if index == 0:
                assert checked("POST", "/api/v2/responses", json=value, headers={**provider, "Idempotency-Key": key}).text == result.text
                saved = {"binding": binding_id, "conversation": conversation_id, "event_id": event["id"],
                         "key": key, "turn": value, "response": result.text}
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            decisions = checked("GET", f"/api/bindings/{binding_id}/decisions", headers=operator).json()["decisions"]
            if decisions:
                break
            time.sleep(1)
        assert len(decisions) == 1 and decisions[0]["decision"] == "candidate", "Use a test deployment with short quiet/settlement thresholds"
        assert decisions[0]["review_count"] == 3 and decisions[0]["images"] == []
        args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.state.write_text(json.dumps(saved))
        args.state.chmod(0o600)
        print(json.dumps({"provider_http": "passed", "input_reviews": 3, "decisions": 1, "duplicate_receipt": "stable", "images": 0, "model_calls": 0}))


if __name__ == "__main__":
    main()
