# Frigate CMA Events

A small deterministic consumer for Frigate review events. It persists event
receipts, merges adjacent activity, and emits bounded **candidate metadata**.
It performs no model inference, downloads no images, executes no commands and
does not send chat messages. A separate CMA channel receives only finalized
candidates; NyxBot owns the bounded image review and sheet write.

```text
Frigate MQTT reviews → JSON relay → durable aggregation → bounded candidate
  → CMA custom event channel → NyxBot evidence review → structured sheet row
```

The service implements CMA's L1 binding, conversation, Responses SSE and event
context interfaces. **Deploying it does not register a provider in CMA.** A gateway
operator must add the deployed origin to the provider registry. The stock
`cma_codex` and `nyxbot` providers invoke agents on every event; channel partitioning
alone does not aggregate events. Candidate forwarding is deliberately separate
from the deterministic provider and carries no images.

## Behavior

- Merge overlapping review intervals and gaps up to `MERGE_SECONDS`.
- Wait for explicit review ends, a quiet window and delivery settlement.
- Require sufficient person/seat alert intervals and tableware evidence.
- Count overlapping intervals once; preserve ended reviews against late updates.
- Persist deduplication, admitted turns, their original contexts and decisions.
- Refuse finalization without a fresh healthy, resynchronized bridge heartbeat.
- Enforce a cooldown and daily candidate cap. Candidates have no images; their
  downstream budget declares one model turn and one comparison image with at most
  three source frames. **No downstream call is made by this service.**

Review intervals and cumulative zones are activity hints, not continuous seat
occupancy or proof that a meal happened. Dishes being removed does not prove their
food was eaten. The service labels its output accordingly.

## Deployment configuration

All installation-specific values are supplied at deployment. There are no camera
identifiers, network addresses, owner IDs, real event samples or credentials in
the source or test fixtures. All tests construct synthetic data.

Required environment variables:

| Variable | Purpose |
| --- | --- |
| `CAMERA` | Exact Frigate camera name |
| `SEATS_ZONE`, `TABLE_ZONE` | Exact configured Frigate zone names |
| `EVENT_SOURCE` | Allowed CloudEvent source URI |
| `PROVIDER_TOKEN` | Dedicated CMA channel agent bearer, at least 32 characters |
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

See [deployment instructions](deploy/README.md) to render a chrono-cd spec and
register the provider. Rendered installation files must stay outside this repo.

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
`frigate.bridge.heartbeat` carries `{"healthy":true,"resynced":true}`; the relay
must assert this only after checking broker/Frigate health and repairing any gap
after a reconnect. Silence alone is never proof of a meal ending.

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

This exercises the deployed provider directly. A real CMA-gateway round trip
requires provider registration, channel creation and a configured CMA event URL;
direct HTTP tests must not be presented as that round trip.

Licensed under Apache-2.0.
