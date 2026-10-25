# Frigate CMA Events

A deployable reference application: turn Home Assistant / Frigate activity into
bounded dining observations, review them with a CMA worker, and record the
results in Google Sheets. An existing NyxBot can query and summarize the records.

**Start with the [complete dining example](examples/dining/README.md)** for local
prerequisites, private configuration generation, worker/channel setup, deployment
and tests. [examples/dining](examples/dining) contains synthetic templates; your
camera, spreadsheet, account and credentials are supplied during installation.

The service persists event receipts, merges activity and emits **candidate
metadata**. It performs no model inference, executes no commands and sends no
chat messages. Its optional evidence adapter reads bounded historical frames;
the separate CMA worker performs the image analysis and verified Sheet write.

```text
Frigate → HA Event Relay → durable aggregation → bounded candidate
  → CMA custom event channel → visual worker → Google Sheets → NyxBot query
```

The reference application uses relay HTTP ingestion and CMA's registered
`cma_codex` provider with an existing Agent. Channel partitioning routes events;
aggregation happens in this service before the model is called. `dining-evidence`
is a NyxID service entry for this same deployed app, not a separate process.

The service also implements CMA's L1 binding/conversation/Responses interfaces
for integrations that need them. Using that optional provider interface through
the gateway requires operator registration; the reference application uses
`/ingest` and does not require that registration.

## Behavior

- Merge overlapping review intervals and gaps up to `MERGE_SECONDS`.
- Wait for explicit review ends, a quiet window and delivery settlement.
- Require sufficient person/seat alert intervals and tableware evidence.
- Count overlapping intervals once; preserve ended reviews against late updates.
- Persist deduplication, admitted turns, their original contexts and decisions.
- Refuse finalization without a fresh healthy, resynchronized bridge heartbeat.
- Enforce a cooldown and daily candidate cap. Candidates have no images; their
  downstream budget declares one model turn and one comparison image with at most
  three source frames. Configured candidate delivery uses a signed CMA outbox.

Review intervals and cumulative zones are activity hints, not continuous seat
occupancy or proof that a meal happened. Dishes being removed does not prove their
food was eaten. The service labels its output accordingly.

## Deployment configuration

All installation-specific values are supplied at deployment. Published templates
and tests use synthetic values. Generated installation files stay outside this
checkout; private deployment settings, real event samples and credentials do
not belong in this repository.

Required environment variables:

| Variable | Purpose |
| --- | --- |
| `CAMERA` | Exact Frigate camera name |
| `SEATS_ZONE`, `TABLE_ZONE` | Exact configured Frigate zone names |
| `EVENT_SOURCE` | Allowed CloudEvent source URI |
| `PROVIDER_TOKEN` | This app's optional L1 provider API bearer, at least 32 characters; separate from a NyxID channel key |
| `API_TOKEN` | Different operator API bearer, at least 32 characters |

Tokens also support `_FILE` variants for mounted secrets. Never put token values
in the app spec, Git, logs or command-line arguments.

Optional settings:

| Variable | Default |
| --- | --- |
| `DATA_DIR` | `/data` (mount a persistent volume) |
| `PROVIDER_SLUG` | `frigate_rules` |
| `WORKER_NAME` | `Frigate rule consumer`; public agent-card display name only |
| `TIMEZONE` | `UTC` |
| `MERGE_SECONDS` | `300` |
| `DELIVERY_GRACE_SECONDS` | `60` |
| `MINIMUM_SECONDS`, `MAXIMUM_SECONDS` | `480`, `14400` |
| `COOLDOWN_SECONDS`, `MAX_PER_DAY` | `3600`, `3` |
| `HEARTBEAT_SECONDS`, `TICK_SECONDS` | `90`, `5` |
| `CMA_EVENT_URL` | CMA custom event URL; empty disables downstream delivery |
| `CMA_HMAC_KEY_ID` | HMAC key id configured on the CMA channel |
| `CMA_TIMEOUT_SECONDS` | `8`; bounded request timeout |
| `FRIGATE_PROXY_URL` | Exact HTTPS NyxID proxy URL for the owner's Frigate service; empty disables evidence |
| `EVIDENCE_SERVICE` | Owner's NyxID service slug for this app; supplied to the worker in candidate metadata |

The optional evidence adapter requires two separate deployment secrets:
`FRIGATE_API_KEY`, scoped only to Frigate and its exact node, and `EVIDENCE_TOKEN`,
used by a separate cloud HTTP service credential. CMA's managed worker key needs
only that HTTP service and Sheets, with no direct HA node grant. Rotate the
Frigate key before its NyxID expiry. No images or credentials belong in Git.

`GET /evidence/{meal_id}` requires `EVIDENCE_TOKEN`. It resolves only stored
finalized candidates (or explicit integration validations), uses their fixed
camera/timestamps, and returns a versioned JSON evidence pack with `images[]`
(base64 JPEG pages), frame IDs and actual timestamps. Clients should save/decode
locally and view each page once; never print base64 into a model transcript.
Missing recordings never fall back to current images. A small in-memory
cache avoids repeated downloads; source media is not persisted by this service.
The `tableware-diversity-v3` selector samples up to 24 historical frames, mixing
broad early/middle/late activity coverage with short bursts around Frigate
tableware-track anchors. It selects up to 12 complementary views using image
quality, visual novelty and tracked-object density, then packs at most four
frames per page (at most three pages). Near duplicates and poor-quality views
are filtered within each phase; the ceilings are not mandatory frame counts.
The scores are heuristics, not food, dish-count or occlusion detection. The
worker matches rotating dishes by appearance and frame IDs, not fixed positions;
neither unique dish identity nor a full turntable revolution is guaranteed.
Native recording snapshots are cropped to the configured `TABLE_ZONE` bounds
before resizing, so dishes retain more pixels than a downscaled room view.
Quality and novelty are scored inside the table polygon when available.
Metadata/crop failures are explicit warnings with an interval/full-scene fallback;
no phase is a claim that dining has ended or food was consumed.
Extraction has a time budget and retains only bounded cropped JPEGs in memory.
It releases the live-ingestion lock while sampling, permits only one sampling
job at a time, and returns `202 pending`/`Retry-After: 5` for overlapping requests.
Retry that same endpoint at most six times after five seconds, including an
interrupted download; a completed cached retrieval does not resample.

Evidence settings are deployment parameters: `EVIDENCE_CROP_PADDING` (normalized
full-frame padding, default 0.04), `EVIDENCE_MAX_WIDTH` / `EVIDENCE_MAX_HEIGHT`
(panel bounds, defaults 1280 / 720; never upscale), and `EVIDENCE_JPEG_QUALITY`
(default 92). Table geometry comes from Frigate configuration, not source code.
`EVIDENCE_SAMPLE_FRAMES` (default 24, range 6..48),
`EVIDENCE_SELECTED_FRAMES` (default 12, range 3..12, no more than samples),
`EVIDENCE_BURST_SECONDS` (default 12, range 2..60) and
`EVIDENCE_SAMPLING_SECONDS` (default 35, range 5..40) bound coverage and cost.
Event budgets advertise upstream source samples separately from selected frames
and image pages. Validation remains one source frame and one image.
These settings do not change the aggregation rules or require a new state volume.
With the adapter enabled, REST review repair is required before finalization,
independently of any relay `resynced` assertion. A failed or truncated repair
pauses decisions.

`POST /results/{meal_id}` with the evidence token records the worker's structured
`status`, `sheet_range` and `uncertainty` after write/readback. This is the
worker's report, not an independent Sheet check. Operators can read it via
`GET /api/results/{meal_id}`. `POST /api/validation` with `API_TOKEN` queues an
explicit synthetic `integration_test` candidate in a separate table; it does
not insert fake reviews or consume the real dining daily cap. Exclude test rows
from meal summaries. Admission delivery alone does not prove model completion
or a Sheet write.

When `CMA_EVENT_URL` is set, mount `CMA_HMAC_SECRET` (or set
`CMA_HMAC_SECRET_FILE`) as a deployment secret. The service signs each
`dining.meal_end_candidate` CloudEvent and keeps a durable outbox. Successful
2xx responses are marked delivered; 429 and 5xx responses retry with backoff,
while permanent 4xx responses remain retained for operator inspection. The exact
event body is stored before the first send.

Use one process, one replica and one persistent volume. The deployment renderer
uses those bounds. A changed ruleset requires a new volume; startup refuses to
mix existing decisions with a different camera or rule configuration. Use a
separate test app/volume for synthetic traffic. Storage is not automatically
pruned in v0.1; monitor the persistent volume and archive it when retiring a test.

For platforms whose PVC mounts are not writable by the non-root container, set
the deployment renderer's `PERSISTENCE_BACKEND=mongodb`. This adds the platform's
MongoDB add-on and stores serialized SQLite snapshots in it while keeping the
working files ephemeral. The add-on connection is supplied through `MONGODB_URI`
and `MONGODB_DATABASE`; no database credential belongs in source or plain env.

```sh
uv sync --frozen
uv run python -m unittest discover -s tests -v
# Set the required variables / mounted secrets before starting:
uv run uvicorn service:app --host 0.0.0.0 --port 8080 --no-access-log
```

The Docker image runs as an unprivileged user. GitHub Actions tests every push
and publishes `ghcr.io/chronoaiproject/frigate-cma-events:sha-<full-commit>`.

See [deployment instructions](deploy/README.md) for cloud spec/secrets and the
optional provider interface. The [dining walkthrough](examples/dining/README.md)
covers the complete application and generated worker/channel/relay templates.

## API and authentication

`GET /healthz` and `GET /.well-known/agent-card.json` are public and expose no
installation parameters. Other routes require bearer authentication:

| Interface | Token | Routes |
| --- | --- | --- |
| CMA binding lifecycle | `PROVIDER_TOKEN` | PUT/DELETE `/api/v1/cmaeg/bindings/{binding}` |
| CMA conversation lifecycle | `PROVIDER_TOKEN` | PUT/DELETE `.../{binding}/conversations/{conversation}` |
| Durable admission | `PROVIDER_TOKEN` | POST `/api/v2/responses`, `Idempotency-Key` required |
| Original event context | `PROVIDER_TOKEN` | GET `.../{binding}/conversations/{conversation}/events/{event}` |
| Aggregate counts | `API_TOKEN` | GET `/api/status` |
| Bounded decision list | `API_TOKEN` | GET `/api/bindings/{binding}/decisions` |
| Isolated synthetic/offline replay | `API_TOKEN` | POST `/api/replay` |

Binding ownership and profile digest are immutable. The binding bearer must
match its `agent_key`; no caller can self-register with a new arbitrary secret.
Deleting a binding or conversation revokes future access, including old contexts.
The provider token is a single-installation trust boundary; use separate
deployments/tokens for mutually untrusted owners. It does not grant any NyxID,
Home Assistant, model or deployment permission.

The Responses endpoint accepts only custom CloudEvents from `EVENT_SOURCE` for
`CAMERA`, with matching event identity. It returns `response.created` followed by
`response.completed` with **empty output**, so raw events cannot become chat
replies. A retry returns the same response ID; changed content under the same
identity is rejected. Context and aggregation admission commit together.

`frigate.review.new`, `.update` and `.end` carry the MQTT review envelope in
`data`. `wrap_review` creates stable CloudEvents from that envelope.
`frigate.bridge.heartbeat` reports connection health. HA Event Relay reports
`resynced:false` because MQTT connectivity cannot establish source history. With
the evidence adapter enabled, this service repairs reviews through the Frigate
REST API before finalization. A custom adapter using the standalone L1 provider
may assert `resynced:true` only after actually repairing history. Silence alone
is never proof of a meal ending.

## Deployed smoke tests

The smoke script creates only synthetic activity and never calls a vision model.
Its `CAMERA`, zone, source and provider settings must match the **test deployment**.
Credentials are read from environment variables or files.

```sh
uv run python scripts/smoke.py --base-url https://YOUR-TEST-APP.example \
  --state /private/path/smoke-state.json
# After restarting the deployed container, verify persistent receipt and decision:
uv run python scripts/smoke.py --base-url https://YOUR-TEST-APP.example \
  --state /private/path/smoke-state.json --resume
```

This exercises the deployed L1 provider directly and should use an isolated test
app with evidence/CMA delivery disabled. The dining example separately documents
an explicit integration validation through CMA, image review, Sheet write/readback
and NyxBot query, plus a real-meal test for camera detection and trigger quality.

Licensed under Apache-2.0.
