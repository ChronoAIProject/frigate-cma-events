# CMA Event Pipeline

A configurable service for turning Frigate reviews, Home Assistant events and
custom CloudEvents into bounded activity candidates for a CMA worker. The
runtime handles normalization, filtering, per-subject windows, deduplication,
limits, optional historical evidence, and durable signed delivery. Workers
perform the application-specific analysis and verified destination writes.

```text
HA / MQTT / Frigate -> HA Event Relay -> /ingest
  -> configured source adapter + predicates
  -> per-subject windows + conditions + limits
  -> durable outbox -> owner's CMA event channel -> configured worker/destination
                         |
              optional bounded historical evidence
```

The service performs no model inference, runs no user scripts and sends no
chat messages. The published image is
`ghcr.io/chronoaiproject/frigate-cma-events`; the repository/image address remains
usable by existing installations.

## Examples

| Application | Input / rules | Evidence | Downstream |
| --- | --- | --- | --- |
| [Dining](examples/dining/README.md) | Frigate person intervals plus a configured object/zone requirement | Multi-view table crops | Dish observations, verified Sheets row |
| [HA activity](examples/ha-state/README.md) | Selected state transitions, grouped and counted per entity | None | Metadata summary, chosen destination |
| [Vehicle visits](examples/driveway/README.md) | Car/motorcycle intervals in a configured region | Vehicle views | Configured activity worker |

Each example uses the same runtime. Names, sources, entity/camera selectors,
regions, conditions, output event types, evidence labels and selection weights
are deployment configuration. Public examples contain synthetic values only.
The dining renderer produces a complete private installation bundle including
`pipeline.json`, worker instructions, channel and Sheet settings.

## Deployment choices

For an HA/Frigate installation, run aggregation and historical selection locally
with the [Home Assistant add-on](docs/home-assistant.md). It uses local SQLite
and the internal add-on network. CMA workers and NyxBot remain remote; selected
evidence is sent to the worker. The core repository and HA packaging repository
have separate responsibilities, with one implementation of the runtime.

[Cloud deployment](deploy/README.md) remains an alternative for other sites.
Its app-spec renderer and MongoDB snapshot support are optional; they are not
prerequisites for running on HA. Use one deployment/state set per independent
policy/owner and one writer per state set.

## Configuration

Supply a validated JSON document through `PIPELINE_CONFIG_FILE` or
`PIPELINE_CONFIG_JSON`. The [pipeline guide](docs/pipeline.md) describes the
schema, predicates, adapters and migration. For example:

```sh
# Copy an example to a private location and set installation-specific values.
export PIPELINE_CONFIG_FILE=/private/path/pipeline.json
uv run python deploy/render.py --image ghcr.io/OWNER/PACKAGE@sha256:DIGEST \
  --service events-api --output /private/path/app-spec.json
```

The renderer validates configuration and embeds it in the private app spec.
Secrets are declared by name, never copied into plain env or configuration.
`PROVIDER_TOKEN` and `API_TOKEN` are distinct mounted secrets of at least 32
characters; `RELAY_TOKEN` authenticates `/ingest`. Tokens support `_FILE`.
Configure `CMA_EVENT_URL`, `CMA_HMAC_KEY_ID` and mounted `CMA_HMAC_SECRET` to send
candidates to the owner's existing CMA channel/Agent. No new provider registry
entry or administrator access is required for normal relay ingestion.

For worker callbacks, optionally configure `WORKER_SERVICE` (the owner's NyxID
service entry for this app) and distinct mounted `WORKER_TOKEN`. The historical
Frigate adapter needs either an explicitly configured local `FRIGATE_URL`
(with optional bearer), or HTTPS `FRIGATE_PROXY_URL` and mounted
`FRIGATE_API_KEY` scoped to the exact source service/node. The two URL settings
are mutually exclusive. Existing
`EVIDENCE_SERVICE` / `EVIDENCE_TOKEN` names are supported. Model keys, Sheets and
NyxBot settings belong to the chosen worker/example, not the aggregation engine.

Other deployment controls are `DATA_DIR`, `HEARTBEAT_SECONDS` (90),
`TICK_SECONDS` (5), `CMA_TIMEOUT_SECONDS` (8), `PROVIDER_SLUG`, and the public
agent-card display name `WORKER_NAME`. See [deployment](deploy/README.md).

## Runtime guarantees

- Receipts and original admitted contexts persist. Reuse of an accepted event
  identity with different content is rejected; retries do not add activity.
- Normalize source lifecycles and merge overlapping/nearby records per subject.
  Count overlapping qualifying intervals once. Ended spans cannot reopen.
- Require explicit span ends, the configured quiet window and receipt grace.
- Apply declarative activity/requirement predicates, duration/count thresholds,
  cooldowns and daily limits per subject.
- Heartbeat mode requires fresh affirmative health. Frigate REST repair is
  required before finalization when its historical adapter is enabled.
- Explicit `observed_events` mode is limited to point-event summaries. It never
  establishes source continuity, absence, occupancy or completion.
- Candidate output contains bounded projected metadata, condition metrics,
  stable IDs, evidence budgets and configured cautions. It does not assert the
  application's real-world conclusion.
- Store the exact signed CMA outbox body before sending. Retry transient errors
  with backoff; retain permanent failures for inspection. CMA admission is not
  proof of worker completion or an external write.

One deployment runs one pipeline, one process and one replica. A wildcard
subject handles many entities with separate windows/limits. Independent
policies or owners use separate app/state/channel deployments. Configuration
loads at startup; HTTP callers cannot install code or modify policy.

## Historical evidence

The `frigate` adapter resolves stored candidate IDs only. It reads configured
object tracks and region geometry, combines broad time coverage with short
bursts, then ranks native-resolution crops by quality, configured track density
and visual novelty. Defaults: at most 24 source samples, 12 selected frames and
three JPEG pages. Two samples are reserved for the candidate tail over the final
180 seconds; both values are configurable as
`evidence.options.tail_coverage_frames` and
`evidence.options.tail_coverage_seconds`. The response reports available and
selected timestamps, unobserved intervals, and whether tail coverage is available.
Selection quotas follow the whole candidate interval, independently of object-track
phases, and keep usable observations from the start, middle and tail even when
they are visually identical. Optional `evidence.options.post_window_seconds`
(0–300, default 0) adds one historical observation after the candidate within the
same source/image budget. It does not prove that an activity actually finished.
Private operator status reports pending, delivered and permanently failed CMA
deliveries. A credential-free health summary is logged every minute.
Static scenes can yield fewer frames. Quality and novelty are heuristics, not
object identity or semantic recognition.

The worker saves `images[]` locally and views each page once within the supplied
budget. Every selected frame has an ID and actual historical timestamp. Missing
recordings never fall back to live images. Extraction has bounded time, image
sizes and volatile cache; it releases the ingestion lock while sampling. An
overlapping request returns `202 pending` with `Retry-After: 5`; retry the same
resource at most six times after five seconds, including interrupted downloads.
Cached retrieval does not resample.

Labels, crop zone, cautions and `EvidenceOptions` weights/thresholds are set in
`evidence` configuration. `EVIDENCE_*` deployment overrides remain supported;
see the [guide](docs/pipeline.md). Evidence settings can change without resetting
aggregation. Integration validation uses one source frame and one image.

## API

Public: `GET /healthz` and `GET /.well-known/agent-card.json`. Other routes use
bearer authentication; installation details are never in public health output.

| Route | Credential | Purpose |
| --- | --- | --- |
| `POST /ingest` | Relay | Admit a configured source CloudEvent |
| `GET /api/status` | Operator | Counts and delivery backlog |
| `GET /api/pipeline` | Operator | Effective policy, state digest, evidence options |
| `GET /api/bindings/{binding}/decisions` | Operator | Last 100 decisions |
| `POST /api/replay` | Operator | Isolated offline replay; no delivery/model/persistent writes |
| `GET /evidence/{candidate_id}` | Worker | Configured historical evidence pack |
| `POST /results/{candidate_id}` | Worker | Configured string status/result fields |
| `GET /api/results/{candidate_id}` | Operator | Reported analysis result |
| `POST /api/validation` | Operator | Explicit one-frame Frigate integration validation |
| `GET /api/state/export` | Operator, processing paused | Portable state snapshot for offline migration |

Callbacks are worker reports, not independent proof of external writes. Their
allowed statuses/fields come from `output`. Application workers must verify
side effects themselves. `integration_test` never asserts a real outcome.

Optional CMA L1 binding/conversation/Responses interfaces remain available with
`PROVIDER_TOKEN`. They return empty output, preserve exact original event context,
and enforce immutable ownership/profile digests and durable admission. Using
that optional provider interface via a gateway requires operator registration;
normal `/ingest` -> signed custom channel delivery does not.

## Development and state

```sh
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python aggregation.py examples/ha-state/events.example.json \
  --pipeline examples/ha-state/pipeline.example.json
# With private config and mounted secrets supplied:
uv run uvicorn service:app --host 0.0.0.0 --port 8080 --no-access-log
```

The Docker image runs as an unprivileged user. Mount writable persistent state
or use the platform MongoDB add-on (`PERSISTENCE_BACKEND=mongodb` in the renderer)
for bounded SQLite snapshots. State is not automatically pruned; monitor and
archive it. A changed aggregation policy requires separate state; startup refuses
to mix policy histories. The [migration procedure](docs/pipeline.md#existing-dining-installations)
allows an equivalent legacy dining installation to adopt explicit configuration
while preserving IDs, decisions, receipts, outbox and results.

Licensed under Apache-2.0.

## Home Assistant packaging and migration

See the [complete HA guide](docs/home-assistant.md) for option mapping, local
Relay routing, managed-worker delegation, testing and legacy-resource retirement.

The [CMA Event Pipeline add-on](https://github.com/ChronoAIProject/nyx-homeassistant-node/tree/main/cma-event-pipeline)
runs this same image/core beside HA Event Relay and Frigate. It stores SQLite in
the add-on's persistent `/data`, without MongoDB or Supervisor administration
permission. A NyxID node exposes only the configured worker service; no host port
is published. Only selected historical evidence leaves the local network.

For a direct local source, set `FRIGATE_URL` to the trusted Frigate base URL
(HTTP or HTTPS); optional `FRIGATE_API_KEY` is sent only when configured. Existing
cloud deployments retain HTTPS-only `FRIGATE_PROXY_URL`. Do not set both.

For migration, deploy with `PROCESSING_ENABLED=false` on the source. Ingestion,
provider turns, model-result callbacks and explicit validation return 503;
aggregation and delivery stop, while operator reads/export remain available.
The relay's durable outbox retries. Export `/api/state/export`, then restore via
`state_transfer.restore_state` before starting the destination. The add-on can
fetch that export once from a configured HTTPS URL using the operator token.
Restore validates schema, source, policy digest, checksums and SQLite integrity,
and refuses an existing database. Keep the source paused until the destination
is verified; run only one writer. Preserve the original worker-service identity
when moving its endpoint, and retain a private export for rollback.

After verifying a local migration, retain private exports/specs rather than a
live zero-replica rollback app. Remove the retired deployment and database,
revoke its exclusive keys, and update private operational records. Keep current
worker-facing/delegation/operator routes: they are distinct credentials for one
local application, not duplicate deployments.

### Independent CMA contexts

For independent candidates, configure a **profile** provider target and
`partition.key: per_event`. An existing-agent target shares its native context
even with that partition. Keep stable event IDs for retries and business history
in the destination. Optional `CMA_CONTEXT_CONFIG` JSON configures local idle-agent
cleanup with `cma_url` (HTTPS public API base), exact profile `binding_id`, its
exclusive `workspace_id` and `grace_seconds` (300–86400; default 600). Set
`CMA_CONTEXT_TOKEN` or its `_FILE` secret separately to an owner key restricted
to the CMA service. Obtain IDs from actual owned resources; never configure a
shared workspace or borrowed target. Cleanup requires workspace and current
binding membership, checks readiness and age again before deletion, preserves
working/approval states, and confirms the final resource status. Audit records
remain under CMA retention. Operator health reports cleanup errors and counts.
This is disabled by default and does not authorize the worker to manage Agents.
