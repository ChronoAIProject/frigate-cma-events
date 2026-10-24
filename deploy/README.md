# Deploy and connect

1. Publish the image from this repository and ensure the GHCR package is public.
2. Set all site parameters in the deployment environment. Generate distinct
   `PROVIDER_TOKEN` and `API_TOKEN` values and keep them in private secret files.
3. Render the app spec **outside the checkout**:

   ```sh
   uv run python deploy/render.py --image ghcr.io/OWNER/PACKAGE@sha256:DIGEST \
     --service SERVICE --output /private/path/app-spec.json
   ```

   `CAMERA`, `SEATS_ZONE`, `TABLE_ZONE` and `EVENT_SOURCE` are required. Other
   optional rule/timer variables are copied when present. For downstream NyxBot
   delivery also set `CMA_EVENT_URL`, `CMA_HMAC_KEY_ID`, and mount the matching
   `CMA_HMAC_SECRET`; the renderer copies only the URL/key-id parameters and the
   secret name. `TARGET_ORG_ID` adds organization ownership to the spec. No
   secret values are read by the renderer.
4. PUT the full spec to chrono-cd `api/v1/projects/PROJECT/apps/APP?org_id=ORG_ID`.
   Set the two tokens through that service's `/secrets` endpoint. The app waits
   for its declared secrets. Wait until live status reports `phase: ready` and
   the public edge serves the app.
5. Run the synthetic deployed smoke test. Restart through chrono-cd, then run
   `--resume` to verify persistence. Keep test settings/data separate from real
   camera settings/data.

If the platform cannot make a non-root PVC writable, set
`PERSISTENCE_BACKEND=mongodb` before rendering. The renderer then adds the
platform MongoDB add-on and wires its URI/database fields into the service;
the service keeps its SQLite working copy in ephemeral storage and snapshots
it to MongoDB after each admitted mutation.

The image runs as an unprivileged user. The persistent volume must therefore be
writable by that user (for example, through the platform's pod `fsGroup` or an
equivalent storage policy). A scratch `emptydir` can validate HTTP behavior, but
it cannot satisfy the restart persistence check and must not be used for live
event state.

## CMA provider registration

The gateway operator adds this **entry** to the existing provider registry; do
not replace other provider entries. Substitute the deployed HTTPS origin:

```json
{
  "slug": "frigate_rules",
  "base_url": "https://YOUR-APP.example",
  "kind": "responses_http",
  "max_inflight": 8,
  "paths": {
    "agent_card": "/.well-known/agent-card.json",
    "binding": "/api/v1/cmaeg/bindings/{binding_id}",
    "conversation": "/api/v1/cmaeg/bindings/{binding_id}/conversations/{conversation_id}",
    "responses": "/api/v2/responses"
  }
}
```

Then create a dedicated CMA channel for the downstream NyxBot worker, with
`provider.slug: nyxbot`, a custom source using HMAC, `partition.key: event_subject`,
and a bounded system prompt. Supply the channel HMAC key id and secret to the
deployment as `CMA_HMAC_KEY_ID` and `CMA_HMAC_SECRET`. Keep the deterministic
provider's `frigate_rules` channel separate; it remains the L1 event consumer
used for aggregation.

The MQTT relay runs on a host with broker access and forwards Frigate events to
`/ingest`. The cloud service signs candidate events for the CMA channel, persists
the exact event envelope before forwarding, and preserves event identity across
retries. The relay must resynchronize review state after reconnecting before
claiming a healthy heartbeat. Relay provisioning and enabling Frigate MQTT are
not performed by this service.

Raw events stop at the deterministic consumer. Candidate forwarding targets a
separate NyxBot channel and carries no images; its prompt limits Frigate
evidence to the candidate budget and directs one structured Google Sheets row.

## Status meanings

- Public app readiness means the container and edge are reachable.
- Successful provider smoke tests mean the HTTP contract and persistence work.
- Neither establishes provider registration, MQTT connectivity or real meal
  recognition. Verify those independently before activating real events.
