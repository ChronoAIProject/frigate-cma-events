# Pipeline configuration

A pipeline is a version-1 JSON document, at most 64 KiB. Unknown keys and
unsupported operators/adapters are rejected. Configuration is read at startup
from `PIPELINE_CONFIG_FILE` or `PIPELINE_CONFIG_JSON`, never from an ingestion
request. It contains parameters and safe field paths; no eval, shell, SQL,
remote script or arbitrary URL execution is available. Credentials use mounted
secret variables separately.

Each deployment serves one pipeline. Multiple subjects are supported within
that pipeline; independent policies use separate deployments/state/channels.

## Structure

| Field | Meaning |
| --- | --- |
| `version`, `name` | Schema version 1 and stable pipeline identity |
| `source` | Adapter, exact source URI, subject selector, input types/filter and health policy |
| `window` | Merge gap, receipt settlement grace, maximum span |
| `activity` | Primary predicate plus minimum union duration / distinct-record count |
| `requirements` | Additional named predicates and thresholds |
| `limits` | Per-subject cooldown, daily candidate cap and IANA timezone |
| `output` | CloudEvent type/schema, field aliases, metadata limits, cautions and result contract |
| `evidence` | `none`, or bounded Frigate history with configured labels/region/selection |

All active conditions must pass. A condition has `name`, `match`,
`minimum_seconds` (default 0), `minimum_events` (default 1), and a suppression
`reason`. Conditions evaluate normalized attributes. Duration is the union of
matching record intervals, not the sum of messages. Point records have zero
duration; use counts for point-event applications.

`window` defaults: `merge_seconds: 60`, `settle_seconds: 10`,
`maximum_seconds: 14400`. `limits` defaults: `cooldown_seconds: 60`,
`max_per_day: 100`, `timezone: UTC`. Limits are scoped by normalized subject.
Open spans cannot finalize from silence. Quiet time groups received activity;
it does not establish completion, departure or absence.

## Predicates

A predicate is `{}` (match all), a logical object with one `all`, `any` or `not`
operator, or `{ "field": "safe.dot.path", "op": "...", "value": ... }`.
Logical nesting is bounded to six levels and at most 16 operands per node.

| Operator | Value / behavior |
| --- | --- |
| `eq` | Typed scalar equality; missing fields do not match |
| `in` | Actual string must belong to the configured string list |
| `contains_any` | Actual list must contain at least one configured string |
| `gte`, `lte` | Finite numeric comparison; no string or boolean coercion |
| `exists` | Boolean test for field presence |

```json
{"all":[
  {"field":"labels","op":"contains_any","value":["car","motorcycle"]},
  {"field":"zones","op":"contains_any","value":["example_gate"]}
]}
```

## Sources

`source.source` is an exact trusted CloudEvent source URI. `subject` is an exact
normalized subject or `*`. `types` is an explicit event-type allowlist.
`source.filter` applies predicates to the incoming envelope before normalization.

`frigate_reviews` accepts Frigate review lifecycle envelopes (`new`, `update`,
`end`). It validates camera/subject agreement, lifetimes and normalized string
lists. Normalized attributes are `labels`, `zones`, `severity`, `detections`.
It preserves review IDs and the original persisted representation for reversible
legacy upgrades. A REST repair uses the same adapter and deduplication path.

`cloudevents` supports:

- `mode: point`: one closed instantaneous record per CloudEvent. Timestamp comes
  from `fields.time` (default `time`), subject from `fields.subject` (default
  `subject`). Timestamps accept finite epoch seconds or timezone-aware ISO8601.
- `mode: span`: `fields.id`, `phase`, `start`, `end` are required safe paths.
  Phase values are `new`, `update`, `end`; only `end` carries a non-null end.
  IDs identify the same record across lifecycle messages. Envelope IDs identify
  individual messages and must remain unique across subjects/phases.
- `fields.attributes` maps normalized attribute names to selected envelope paths.
  Only these fields are persisted/exported as normalized attributes, capped at
  8 KiB per record. Record IDs are namespaced by source and subject.

HA Event Relay already emits native HA events as CloudEvents. The HA example
selects `homeassistant.state_changed`, filters on `data.new_state.state`, and
projects selected attributes. It requires no Frigate configuration.

## Source health

`source.health.mode: heartbeat` is the default. `event_type` names the health
CloudEvent (Frigate default `frigate.bridge.heartbeat`; otherwise
`bridge.heartbeat`). It must contain actual boolean `healthy` and `resynced`.
Receipt time controls freshness. `require_resynced` defaults true. Producers
must assert resynchronized history only after repairing it. Connectivity alone
is not a history guarantee. With the Frigate evidence adapter enabled, the
service independently repairs Frigate reviews before finalization.

`observed_events` is explicitly available only for generic point events. Native
HA bus forwarding does not prove complete history, so this mode summarizes only
received transitions and adds that limitation to every candidate. It cannot be
used to silently close an open span or claim absence/occupancy.

## Output and workers

`output.event_type` defaults `activity.window_candidate` and `schema` defaults
`activity-window-candidate.v1`. CMA destination URL/key-id and mounted HMAC
secret are supplied to deployment. Exact event bodies are stored in the durable
outbox. Output IDs incorporate source/type/candidate identity; retries reuse them.

Canonical candidate fields are `candidate_id`, `subject`, `event_ids`,
`started_at`, `ended_at`, `activity_seconds`, `event_count`, `conditions`,
`records`, `metadata_truncated`, `images`, `evidence_budget`, and `cautions`.
`event_ids` identify normalized records (Frigate review IDs or namespaced generic
record IDs). Generic metadata records additionally include bounded original
`source_event_ids`, actual times and explicitly projected attributes.

`max_event_ids` defaults 100 (maximum 100); `metadata_records` defaults 8
(range 0..16). Representative metadata records cover the window, capped at a
combined 64 KiB. Counts and candidate identity still cover all grouped records;
`metadata_truncated` reports that exported records are a subset. Truncation must
not be mistaken for complete input history.

`aliases` adds application-compatible fields from safe paths in the canonical
candidate. It cannot overwrite canonical or transport fields. The dining
configuration maps `meal_id` to `candidate_id`, `camera` to `subject`, and other
legacy fields explicitly. Domain cautions and worker instructions live in the
example configuration/templates.

`result_statuses` defaults to `processed`, `pending`, `integration_test`.
`result_fields` defaults to `reference`, `uncertainty`; every callback value is
a bounded string. Configure destination-specific fields in the example (dining
uses `recorded` and `sheet_range`). With `WORKER_SERVICE` / `WORKER_TOKEN`,
candidates include a result callback even without images. Worker credentials
cannot access operator endpoints. Callback acceptance is not proof of an
external write; verify the destination independently.

## Evidence

`evidence.adapter` defaults `none`. `frigate` requires a fixed Frigate subject
and either explicit local `FRIGATE_URL` (HTTP/HTTPS, optional bearer) or HTTPS
`FRIGATE_PROXY_URL` with credentials scoped to the exact service/node. Do not
set both URL variables. The [HA guide](home-assistant.md) maps these to add-on
options. `crop_zone`, `labels`, `cautions` and `version` are deployment parameters. No configured labels means interval/burst
sampling with explicit fallback metadata, not an implicit food/object class.

Options and defaults:

| Option | Default / bounds |
| --- | --- |
| `sample_frames` | 24 / 6..48 source samples |
| `selected_frames` | 12 / 3..12, no more than samples |
| `burst_seconds` | 12 / 2..60 |
| `sampling_seconds` | 35 / 5..40 |
| `crop_padding` | 0.04 / 0..0.15 normalized full-frame margin |
| `max_width`, `max_height` | 1280 / 720; never upscale |
| `jpeg_quality` | 92 / 60..95 |
| `density_weight`, `quality_weight`, `novelty_weight` | 0.25 / 0.35 / 0.4; each 0..1, positive combined weight |
| `duplicate_threshold` | 0.018 / 0..1 within-phase visual distance |
| `minimum_quality_ratio` | 0.6 / 0..1 relative to phase's best quality |
| `novelty_scale` | 0.12 / greater than 0, at most 1 |

Each option also supports an `EVIDENCE_<UPPERCASE_NAME>` env override. Native
frames are cropped before resizing; scoring uses the configured region polygon.
Page limit is derived from selected-frame limit (four frames per page, maximum
three pages). Sampling, image/byte sizes and memory retention are bounded.
These are generic quality/diversity heuristics, not trained semantic detection.
Interpreting rotating dishes, vehicles or other subjects belongs to the worker.

Evidence-only settings do not change the aggregation state digest. A different
input, predicate, window, limit or output policy requires separate state.

## Replay

```sh
uv run python aggregation.py examples/ha-state/events.example.json \
  --pipeline examples/ha-state/pipeline.example.json
```

Authenticated `POST /api/replay` accepts `{ "events": [CloudEvent, ...] }` using
the deployment's policy, or includes an explicit `pipeline` document and optional
`now` clock. It uses an in-memory database with at most 100 events, assumes source
health only for this isolated replay, and neither queues CMA delivery nor writes
persistent state. The old Frigate `rules`/`reviews` replay shape remains supported.

## Existing dining installations

1. Upgrade the image with the existing legacy env first. The compatibility loader
   reads [the dining configuration template](../examples/dining/pipeline.example.json)
   and constructs the same generic policy. It checks the original rules digest,
   stamps a policy digest, and retains the existing SQLite schema, record IDs,
   candidate IDs, receipts, decisions, outbox bodies and results.
2. Verify health, counts, historic candidate/result access and pending deliveries.
   Persist a checkpoint by stopping the single writer cleanly before changing
   configuration on snapshot-backed deployments.
3. Supply the equivalent fully rendered `pipeline.json` via JSON/file config.
   The matching policy digest permits this change without a state reset. Remove
   the old camera/seat/table/rule env inputs from the deployment.
4. Different aggregation policy is rejected against existing state. Use a new
   app/state directory for that case. Changing only evidence options is allowed.

Keep a private deployment/spec backup and use one writer during migration.
CMA output type, legacy aliases and ID prefix remain explicit in the dining
configuration, preserving existing workers and rows. Real event media and
installation parameters must stay outside Git.

## Runtime placement and retirement

The policy schema is identical on HA and in a cloud deployment. HA's add-on
wrapper supplies `pipeline_config` and private options, persists SQLite in
`/data/state`, and reads local Frigate history. CMA worker inference and
destination writes remain separate. See [HA deployment](home-assistant.md) for
networking, credential roles, managed-worker delegation and verification.

After migrating state and verifying the destination, retire the source
deployment/database and its exclusive credentials. Preserve private portable
backups and existing candidate IDs instead of keeping a live rollback service.
Worker-facing, delegated node and operator entries remain part of the active
auth path. Verify actual deletion/credential revocation; a stopped app or an
accepted asynchronous retirement is not completed resource removal.
