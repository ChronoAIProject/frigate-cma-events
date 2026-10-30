# Video monitoring rules

Core 0.3 adds a local multi-rule registry and management page. The same runtime
supports camera activity, vehicle visits, dining and generic HA/CloudEvent
summaries. Rules contain declarative parameters, never executable scripts.
Visual interpretation remains the downstream worker's responsibility.

## Management page

In HA, open **Settings → Add-ons → Video Event Monitor → Open web UI**.
The existing add-on slug and image package are unchanged, so upgrading an
installed CMA Event Pipeline preserves its data and service routes.

The page lists rules and their current switches, versions and delivery counts.
Choose **New rule**, set a stable lowercase ID and display label, then select a
Frigate camera, activity labels/zone, minimum duration, merge/settle intervals,
cooldown, daily cap and optional evidence region. Generate and review the JSON,
then save. Advanced JSON exposes additional conditions, source filters, event
schemas and all [evidence options](pipeline.md#evidence).
Generating JSON replaces the editor's complete policy after confirmation; use
JSON directly when retaining a complex existing rule's requirements.

New rules start disabled, with CMA delivery off. Enable local processing to
inspect recent candidate/suppression decisions before enabling delivery.
Each rule's revision selector shows its historical decisions and reported
results. Copy a rule to create an independent rule; archive stops processing
without deleting history. Archiving is permanent; a copy can be enabled later.
The page never stores a bearer token in browser storage. Outside HA Ingress,
an operator may enter a token for the current page session only.

A deployment currently has one CMA destination/signing credential and one
worker/evidence credential. Each rule selects whether to use that destination
and sets its own `output.event_type` and result contract. Configure the CMA
channel's subscriptions and consumer for those event types. A dining consumer
does not automatically become a general security or vehicle-analysis worker.
Separate owners/credential boundaries require separate deployments.

## Persistence and revisions

On first upgrade, the effective startup policy is imported as `default` v1.
Its original SQLite files, candidate IDs, outbox, results and legacy callback
paths remain valid. Thereafter, the registry is authoritative: changing startup
`pipeline_config` does not replace stored rules. Edit rules in the page/API;
connection URLs, credentials, cleanup policy and timers remain deployment options
and require restarting when changed.

`DATA_DIR/monitoring.sqlite3` stores immutable rule documents and current heads.
New revision state lives in `DATA_DIR/rules/<id>/<revision>/`. In HA, `DATA_DIR`
is `/data/state`. HA add-on backups include the registry, revisions and original
history. Preserve the full `/data` volume, run one process/writer and take a
backup before upgrades. Images remain a bounded volatile cache; Frigate owns raw
recordings and their retention. State is not automatically pruned.

Every edit, enable/disable or archive creates a new revision. Its activity
starts at creation time; spans that started earlier are ignored, including REST
repair records. This prevents an old meal/visit from being reissued under a new
policy. Windows, cooldowns and daily counters restart with the revision. Editing
while an activity is in progress can therefore omit that activity. Edit during
quiet periods when continuity matters.

Retired revisions stop ingestion, aggregation and delivery; their unsent queues
remain paused for inspection. A delivery already in flight can finish, and
workers can still fetch stored candidate evidence and post results for the old
revision. No queue is automatically replayed when a replacement revision starts.
Namespaced candidate identities and callback paths prevent cross-rule/revision
collisions. The imported `default` v1 retains compatibility paths.

Limits: 32 rule IDs (including archived IDs) and 256 ordinary revisions.
Stopping or archiving remains available at the revision limit. The API rejects
other additional revisions rather than silently deleting history. Monitor
storage; archive/export the full data volume deliberately. The old portable
`/api/state/export` supports only an untouched single legacy revision and refuses
a partial export after managed revisions exist. MongoDB snapshot deployments
retain legacy operation; managed edits require local persistent storage, since
the optional legacy snapshot backend does not back up the rule registry.

## Relay input and health

Forward Frigate review events for all cameras to this service, then select
cameras/labels/zones in rules. Route `frigate.bridge.heartbeat` separately from
the MQTT topic filter, with the same source URI and `subject: "*"` for a shared
bridge. A global heartbeat is applied to each matching camera rule. Older
camera-specific heartbeats still work for that camera.

Unmatched source/type/subject events return `accepted: ignored`, with no CMA
work. When several rules match, `/ingest` returns their individual admission
outcomes. Admission is idempotent per rule; fanout is not a cross-database
transaction, so retry an uncertain request with the original event identity.

With a deployed Frigate URL, camera rules repair reviews before finalization,
even when image evidence is off. The shared bridge must also be connected and
its heartbeat fresh. This avoids interpreting broker connectivity as complete
history. Generic point-event summaries may explicitly use `observed_events`;
that mode cannot establish absence or a real-world outcome.

## Owner API

All management endpoints require `Authorization: Bearer <API_TOKEN>`, or a
trusted HA Ingress session. Relay/provider/worker credentials cannot manage rules.
JSON documents use the existing [pipeline schema](pipeline.md).

| Method / path | Behavior |
| --- | --- |
| `GET /api/management` | Non-secret deployment capabilities and default source URI |
| `GET /api/cameras` | Camera names, zone names, tracked labels and recording-enabled flag only |
| `GET /api/rules` | Current rule documents, including archived IDs |
| `POST /api/rules` | Create `{id, label?, pipeline, enabled?, delivery_enabled?}` |
| `GET /api/rules/{id}` | Current document with revision `ETag` |
| `PUT` or `PATCH /api/rules/{id}` | Change supplied fields; requires `If-Match: "<revision>"` or JSON `expected_revision` |
| `DELETE /api/rules/{id}` | Archive; requires `If-Match` or JSON `expected_revision` |
| `GET /api/rules/{id}/revisions` | Immutable configuration history |
| `GET /api/rules/{id}/revisions/{n}/status` | Revision status and current/processing flags |
| `GET /api/rules/{id}/revisions/{n}/decisions` | Last 100 decisions and reported results |
| `GET /api/rules/{id}/revisions/{n}/results/{candidate}` | Read a reported result |
| `POST /api/rules/{id}/revisions/{n}/validation` | One explicit synthetic historical-image validation, active revisions only |

Supply `If-Match` or an integer `expected_revision` in the update/archive JSON
body. The latter survives proxies that omit conditional HTTP headers. If both
are supplied, they must agree. Missing both returns 428; a stale revision returns
409. Neither form weakens authentication or the atomic revision check. Invalid documents
return 400 without changing the current head. New IDs match
`[a-z][a-z0-9_-]{0,63}`; `default` is reserved for migration. Credentials, storage
paths, arbitrary URLs and executable code cannot be set through rule fields.
The source URI identifies admitted events; it is not fetched as a URL.

Worker events supply exact versioned paths:
`rules/<id>/revisions/<n>/evidence/<candidate>` and
`rules/<id>/revisions/<n>/results/<candidate>`. Fetch/post these using the existing
worker credential, not owner APIs. `/evidence`, `/results`, `/api/pipeline` and
legacy provider endpoints remain pinned to imported `default` v1. The optional
legacy provider refuses new turns once that revision is no longer active.

HA Ingress trusts the direct Supervisor peer `172.30.32.2`, an authenticated
remote-user header and the ingress prefix. The HA launcher disables forwarded
IP parsing; caller-supplied forwarding/user headers on ordinary routes do not
establish an Ingress identity. HA's admin-only panel gates ingress sessions.
State-changing ingress requests additionally require
`X-Monitoring-Request: 1`; CORS is not enabled. No API token is embedded in the
page. Standalone deployments leave `HA_INGRESS=false`.
