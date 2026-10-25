# Cloud deployment

Choose a [pipeline example](../README.md#examples) and supply its configuration
at deployment. For the complete **HA relay → aggregation → CMA worker → Google Sheets**
application, follow [the dining walkthrough](../examples/dining/README.md).
It generates the app, relay, worker/channel and Sheet configuration from one
private parameter set. The normal application uses CMA's existing `cma_codex`
provider; it does not need a new provider registry entry.

## App spec and secrets

1. Publish the image from this repository and ensure the GHCR package is public.
2. Copy an example policy outside the checkout and set `PIPELINE_CONFIG_FILE`
   to its absolute path (or supply `PIPELINE_CONFIG_JSON`). Configure its source,
   subjects, conditions, windows, output and optional evidence adapter. These
   variables are mutually exclusive. Generate distinct `PROVIDER_TOKEN`,
   `API_TOKEN` and `RELAY_TOKEN` values and keep them in private secret files.
3. Render the app spec **outside the checkout**:

   ```sh
   uv run python deploy/render.py --image ghcr.io/OWNER/PACKAGE@sha256:DIGEST \
     --service SERVICE --output /private/path/app-spec.json
   ```

   The renderer validates and embeds the resolved policy in the app spec;
   the file does not need mounting. Generic policies do not require camera or
   dining variables. Legacy deployments may still use `CAMERA`, `SEATS_ZONE`,
   `TABLE_ZONE` and `EVENT_SOURCE`; see [migration](../docs/pipeline.md#existing-dining-installations)
   before replacing those settings on existing state. For downstream CMA
   delivery also set `CMA_EVENT_URL`, `CMA_HMAC_KEY_ID`, and mount the matching
   `CMA_HMAC_SECRET`; the renderer copies only the URL/key-id parameters and the
   secret name. Set `WORKER_NAME` to a non-sensitive public agent-card display
   name at deployment time; the provider slug remains stable. `TARGET_ORG_ID`
   adds organization ownership to the spec. No
   secret values are read by the renderer.
4. PUT the full spec to chrono-cd `api/v1/projects/PROJECT/apps/APP?org_id=ORG_ID`.
   Upload every declared secret through
   `api/v1/projects/PROJECT/apps/APP/services/SERVICE/secrets?org_id=ORG_ID`.
   The app waits for its declared secrets. With the evidence adapter, these are
   `PROVIDER_TOKEN`, `API_TOKEN`, `RELAY_TOKEN`, `CMA_HMAC_SECRET`,
   `FRIGATE_API_KEY` and `EVIDENCE_TOKEN`. A metadata-only worker callback instead
   uses `WORKER_SERVICE` and its separate `WORKER_TOKEN`; neither images nor a
   Frigate API key are required. Verify platform application status and
   the live `/healthz` and authenticated `/api/status` endpoints.
5. On an isolated test deployment, run the synthetic provider smoke test with
   evidence/CMA delivery disabled. Restart through chrono-cd, then run
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

## Optional L1 provider registration

This section is for users who want to send raw metadata through a separate
`frigate_rules` CMA provider binding. Direct HA relay ingestion at `/ingest`
already performs aggregation and candidate delivery without this registration.

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

The reference application's candidate outbox belongs to the direct relay-ingest
engine. Other provider bindings store decisions for their own contexts; they do
not feed that outbox. Use the documented direct `/ingest` path for the complete
worker application, and keep custom provider experiments on isolated test state.

## Status meanings

- Public app readiness means the container and edge are reachable.
- Successful provider smoke tests mean the HTTP contract and persistence work.
- Neither establishes provider registration, MQTT connectivity or useful
  real-world recognition. Verify those independently before activating real events.
- CMA 2xx delivery proves admission, not model completion or a Sheet write.
  Read `/api/results/{candidate_id}` and independently verify the downstream result.
