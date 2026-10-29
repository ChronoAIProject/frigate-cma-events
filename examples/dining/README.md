# Dining review: Home Assistant → CMA → Sheets

This is a deployable example of the generic event pipeline. Frigate
detects activity locally; a deterministic service selects candidates before a
CMA worker performs bounded visual analysis and writes a Google Sheet. An
existing NyxBot can query and summarize those records. All published values are
synthetic. Your camera, owner, services, spreadsheet and credentials belong in
private deployment files.

```text
Dining camera -> Frigate -> MQTT reviews -> HA Event Relay
                                                |
                                     authenticated local metadata
                                                v
                                    CMA Event Pipeline on HA
                              dedupe / merge / REST repair / limits
                                                |
                                     signed meal-end candidate
                                                v
                                      CMA Event Channel
                                                |
                                          remote dining worker -> Google Sheets
                                                |                     ^
                                     bounded evidence/result          |
                                                |               existing NyxBot
                               NyxID -> ordinary HA node              |
                                                |                   user
                                 local Pipeline /evidence and /results
                                                |
                                    local Frigate recordings
                                                |
                            <=24 samples -> <=12 frames -> <=3 pages
```

## Components and ownership

| Component | Source / responsibility |
| --- | --- |
| Frigate + MQTT broker | Existing local services: detections, recordings and review messages |
| HA node + Event Relay | [nyx-homeassistant-node](https://github.com/ChronoAIProject/nyx-homeassistant-node): local proxy and configurable event forwarding |
| Aggregation + evidence + outboxes | This repository owns the generic core; one process/state set |
| HA Pipeline packaging | `nyx-homeassistant-node/cma-event-pipeline`; pinned core image, HA options, local SQLite `/data/state` |
| Event channel + worker | [CMA](https://github.com/ChronoAIProject/cma): route candidates and execute visual analysis |
| Credentials + service routes + NyxBot | [NyxID](https://github.com/ChronoAIProject/NyxID): broker scoped access and provide the existing user-facing assistant |
| Personal installation | Private platform settings/files; never committed to any of these repositories |

The worker-facing evidence service name is private deployment configuration.
It is a NyxID HTTP routing/credential entry for this same Pipeline, not another
application. On HA, its scoped delegation can reach a node-bound worker route;
a separate operator route injects the operator credential. The worker and channel are CMA configuration, not another custom
worker codebase. This path uses CMA's existing `cma_codex` provider and the app's
`/ingest` endpoint; it does not require registering a new gateway provider.

## 1. Prepare the local services and destination

- Install/configure Frigate, an MQTT broker, NyxID Node and Home Assistant Event
  Relay, plus the CMA Event Pipeline add-on for local aggregation. Keep the
  relay disabled until the chosen Pipeline and channel are ready.
- Fix the camera angle so the table is visible. Configure the camera's seat and
  table-surface zones and track `person` plus supported tableware labels
  (`plate`, `bowl`, `cup`, `bottle`). This example does not change camera settings.
- Enable Frigate MQTT with the `frigate` topic prefix. Confirm `frigate/reviews`
  messages, actual zone names and retained historical recordings. For a custom
  prefix, change the topic in your private relay configuration.
- For local HA processing, configure the Pipeline's `frigate_url` with Frigate's
  trusted internal hostname/API port. For optional cloud aggregation, use a
  narrowly scoped NyxID Frigate service/key. Verify `api/review?limit=5` and
  historical `api/{camera}/recordings/{timestamp}/snapshot.jpg` availability
  through the selected source path. Do not expose the internal API publicly.
- Connect your Google Workspace service. Create a dedicated spreadsheet tab;
  [sheet-schema.json](sheet-schema.json) defines ten columns in A:J, with meal_id
  in B. Existing tabs may use translated column names supplied privately through
  `SHEET_COLUMNS_JSON`; retain the same semantic order.

Meal intervals are activity hints. Cumulative zones do not establish continuous
occupancy, and cleared dishes do not establish consumption. This application
records whole-table observations, not identities or individual preferences.

Evidence uses tableware-track phases and native-resolution crops of the Frigate
table zone. The v3 selector mixes broad coverage with short bursts to capture
different turntable positions. It filters near duplicates and lower-quality
views, selects up to 12 frames from 24 samples, and returns at most three pages
in `images[]`. The worker matches dishes across frames by appearance and frame
IDs; positions, track counts and quality scores cannot establish dish identity.
`EVIDENCE_CROP_PADDING`, `EVIDENCE_MAX_WIDTH`, `EVIDENCE_MAX_HEIGHT` and
`EVIDENCE_JPEG_QUALITY` tune the crop margin, panel bounds and encoding.
`EVIDENCE_SAMPLE_FRAMES`, `EVIDENCE_SELECTED_FRAMES`, `EVIDENCE_BURST_SECONDS`
and `EVIDENCE_SAMPLING_SECONDS` bound sampling, selected views, burst spacing
and processing time. Inspect the selected historical pages and their frame IDs,
phase/sampling/warning metadata when checking
recognition quality; a late activity frame is not necessarily a post-meal frame.

## 2. Prepare private configuration

### Local HA deployment

Follow the [HA deployment guide](../../docs/home-assistant.md) for installation,
option mapping and node delegation. No cloud origin, cloud database or raw
Frigate proxy key is needed for local processing. Keep site parameters and
credentials outside source control, in a private directory and HA options.

For a new installation, resolve [pipeline.example.json](pipeline.example.json)
with your camera/source/zones and rule values, or generate that policy from the
same defaults as the application renderer:

```sh
# Set DINING_SITE_DIR, CAMERA, SEATS_ZONE, TABLE_ZONE and EVENT_SOURCE privately.
# Optional TIMEZONE defaults to UTC. This writes only the policy, not credentials.
uv run python - <<'PY'
import json, os
from pathlib import Path
from profiles import Rules
site = Path(os.environ["DINING_SITE_DIR"])
site.mkdir(parents=True, exist_ok=True, mode=0o700)
site.chmod(0o700)
policy = Rules(camera=os.environ["CAMERA"], seats_zone=os.environ["SEATS_ZONE"],
               table_zone=os.environ["TABLE_ZONE"],
               timezone=os.environ.get("TIMEZONE", "UTC")).pipeline(os.environ["EVENT_SOURCE"]).spec
with (site / "pipeline.json").open("x") as output:
    os.chmod(output.name, 0o600)
    json.dump(policy, output, ensure_ascii=False, indent=2)
PY
```

Tune the resolved policy deliberately before first start. Serialize its JSON as
the add-on's `pipeline_config` string. Copy the
[worker prompt](worker-prompt.md), [channel template](channel.example.json) and
[Sheet schema](sheet-schema.json) to private files. Supply the actual
worker-facing service, model, owner and Sheet settings:

- Append a private `dining-config.json` to the worker prompt. It contains
  `spreadsheet_id`, `tab`, `sheet_service`, `sheet_connection`, `evidence_service`,
  `timezone`, and the ten `columns` in schema order.
- Compute `values_base` as `v4/spreadsheets/<spreadsheet-id>/values/`; append the
  URL-encoded quoted-tab range for `headers_path` (`A1:J1`), `ids_path` (`B:B`),
  and `read_path` (`A:J`). `append_path` is that same encoded A:J range followed
  by `:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`.
- Build the normal CMA `worker-request.json` with `name` and a `profile` containing
  `model`, `reasoning_effort`, `permission_mode`, `permissions.services` and
  `system_prompt`, as used by the cloud renderer below. The service list includes
  only the configured evidence, Google Workspace and model services.
- Resolve the channel template's name/owner/model/service/prompt/key-ID variables
  privately. Reserve one `record_id` UUID. Leave the
  credential placeholders for the lifecycle steps below.

An existing installation can reuse its private worker/channel/Sheet bundle and
policy while migrating runtime placement. Preserve its service ID, channel and
candidate IDs. The add-on's private state import handles persistence migration.

### Optional cloud bundle renderer

The current `examples/dining/render.py` generates a **cloud** app bundle; it does
not produce HA Pipeline options. Use it for the cloud alternative, or reuse
already-rendered worker/Sheet artifacts without deploying its cloud app.

Run from the repository root:

```sh
export DINING_SITE_DIR="$HOME/.config/dining-example"
mkdir -p "$DINING_SITE_DIR"
chmod 700 "$DINING_SITE_DIR"
cp examples/dining/parameters.env.example "$DINING_SITE_DIR/site.env"
# Edit site.env with your own non-secret settings and public API origins.
set -a
. "$DINING_SITE_DIR/site.env"
set +a
python3 examples/dining/render.py \
  --image ghcr.io/chronoaiproject/frigate-cma-events@sha256:YOUR_PUBLISHED_DIGEST \
  --output-dir "$DINING_SITE_DIR/bundle"
```

`APP_ORIGIN` is your cloud app's intended HTTPS origin. `FRIGATE_PROXY_URL` is
the exact NyxID proxy URL for your Frigate service. `CMA_OWNER_ID` is the human
who will own the CMA Agent/channel; `TARGET_ORG_ID`, when supplied, selects the
cloud app's organization and does not change CMA ownership. Model and service
names must be available to that owner.

The renderer reads only non-secret allowlisted parameters. It creates a 0700
directory and 0600 files, refuses output inside this checkout (including through
symlinks), and refuses overwriting an existing bundle. Secret fields and the
not-yet-created CMA Agent remain explicit `SET_...` placeholders.

| Generated file | Use |
| --- | --- |
| `pipeline.json` | Fully resolved declarative dining policy, also embedded in the app spec |
| `app-spec.json` | Cloud deployment: camera/rules, evidence service and persistence |
| `app-secrets.json` | Private secret placeholders; fill before uploading |
| `relay-options.json` | HA add-on options; fill broker password and relay token |
| `dining-config.json` | Worker destination, exact Google paths and schema |
| `worker-request.json` | Normal CMA Agent lifecycle creation request with private configuration in its prompt |
| `channel-request.json` | Existing-Agent channel request with one reserved record UUID |
| `sheet-schema.json`, `sheet-header.json` | Column semantics and optional new-tab header initialization |
| `assistant-memory.md` | Private reference text for your existing NyxBot to query these records |

Edit generated files only in the private directory. If regenerating, use a new
empty output directory and deliberately migrate any filled credentials/IDs.
The generic service contains no dining decision branches: conditions, aliases,
tableware labels, crop zone and output contract come from this policy. For an
existing deployment, follow the [two-step migration](../../docs/pipeline.md#existing-dining-installations)
before replacing legacy environment settings; retain its state and channel.

## 3. Supply credentials and register the evidence service

### HA options and routes

Set distinct `relay_token`, `api_token`, `provider_token` and `worker_token`
values in HA. Set `frigate_url` to the local API and leave `frigate_api_key`
empty if that trusted internal API needs no bearer. Keep the matching CMA HMAC
secret/key-ID in the Pipeline and channel; the Relay receives only its ingest
token. Credentials in HA password fields remain in private options/backups.

Create a node-bound worker service and a separate owner-only operator service.
For CMA managed workers, configure the [scoped NyxID delegation](../../docs/home-assistant.md#worker-and-operator-access)
while preserving the existing worker-facing service ID. The worker gets the
worker-facing evidence service and Google/model access, not raw Frigate or
operator routes. Keep managed-key scope/lifecycle under CMA control.

### Cloud alternative

Fill `app-secrets.json` privately:

- `API_TOKEN`, `PROVIDER_TOKEN`, `RELAY_TOKEN`, `EVIDENCE_TOKEN`: distinct random
  strings with at least 32 characters. `PROVIDER_TOKEN` protects this app's
  optional L1 provider API; it is not a NyxID key or the CMA channel key.
- `FRIGATE_API_KEY`: a NyxID key with `read proxy`, limited to the exact Frigate
  service and backing HA node. Store its one-time output outside the checkout.
  Rotate it before its NyxID expiry.
- `CMA_HMAC_SECRET`: a standard-base64 encoding of at least 32 random bytes.
  Use the same base64 **string** in the channel's `hmac_secrets`; do not decode
  it when signing. `CMA_HMAC_KEY_ID` must agree in the app and channel.

For the cloud alternative, register an owner-scoped NyxID custom HTTP service
for `APP_ORIGIN`, using bearer
auth with `EVIDENCE_TOKEN` and no node binding. For example, save that token in a
private file and use:

```sh
nyxid service add --custom --slug "$EVIDENCE_SERVICE" \
  --label 'Dining evidence' --endpoint-url "$APP_ORIGIN" \
  --auth-method bearer --auth-key-name Authorization \
  --credential-file "$DINING_SITE_DIR/evidence-token" --terminal --output json \
  > "$DINING_SITE_DIR/evidence-service.json"
```

Use the service's actual returned slug in all generated settings. The worker
gets this HTTP service and Google Workspace access; the Frigate/node key stays
with the aggregation app. CMA managed keys currently do not support granting
the backing node merely by approving its service; see [CMA #1111](https://github.com/ChronoAIProject/cma/issues/1111).

## 4. Create a profile-managed per-event channel

Use current owner NyxID authorization through the supported public API. Keep
bearer headers, request bodies, receipts and idempotency keys in the private site
directory. Complete CMA's normal NyxID sign-in once for renewable sandbox access.

1. Use the generated `channel-request.json` profile: exact worker prompt, selected
   services, model and dedicated directory workspace. Keep `provider` as
   `{"slug":"cma_codex"}` (profile target) and `partition.key` as `per_event`.
   Do not create and borrow one existing Agent for all meals. The provider
   provisions a separate native Agent/worktree for each stable event ID.
2. Create a distinct NyxID channel binding key authorized for selected services.
   Fill `credentials.agent_key` and the matching HMAC secret. Do not reuse app,
   source, sandbox-managed or other binding credentials.
3. POST to `$CMA_GATEWAY_BASE/channels` with the generated record UUID, exact
   request and stable idempotency key. Preserve the returned receipt. Recover an
   uncertain response through `GET /channel-records/{record_id}` or exact retry.
4. Verify ready state and provider field decisions. A ready channel does not
   prove that a visual task has executed successfully. Copy the returned
   `endpoints.custom_event_url` verbatim to `cma_event_url` in HA options.
5. After the first task, obtain the native Agent's dedicated workspace ID and
   the native immutable Agent profile ID. Configure optional scoped idle cleanup as described
   in the root guide and HA add-on guide. Keep the management key only on HA.
   The Sheet and pipeline callbacks retain business history after Agent cleanup.

The generated `worker-request.json` remains an optional standalone-agent example;
creating it is unnecessary for the profile-managed event channel. Service scope
is supplied by the channel profile. Supported owner credentials and operation
permissions still apply; no browser/machine capability is required locally.

## 5. Deploy, initialize the Sheet and enable the relay

For HA, start the configured Pipeline add-on and verify health/status through
its ordinary node-backed service. Point Relay's existing destination at the
actual internal `/ingest` hostname/port, set `allow_local_http: true` and use
`auth_token=relay_token`. Keep its destination ID during migration so queued
entries use the updated destination. The add-on stores SQLite in `/data/state`;
no cloud app or MongoDB is involved. See the [HA runbook](../../docs/home-assistant.md).

For the cloud alternative, deploy the completed `app-spec.json` through your
cloud platform and upload
`app-secrets.json` through its secret API. [Deployment details](../../deploy/README.md)
cover the chrono-cd paths. Use one replica and the rendered MongoDB add-on or a
durable volume. Wait for the platform to apply the spec, then verify the live
`/healthz` and authenticated `/api/status`; an applied spec alone is insufficient.

Create the configured tab in the configured spreadsheet. For a **new empty tab**,
initialize A1:J1 through the configured Google Workspace connection with
`sheet-header.json`, `PUT headers_path?valueInputOption=RAW`. For an existing
tab, read and verify its headers first. The worker's private configuration uses
`v4/spreadsheets/...`; the service base already supplies the Google API host.

Fill the private relay's broker password and the chosen Pipeline's matching
ingest token, then copy
the options into the HA Event Relay add-on and start it. The example forwards
only the configured camera's reviews and heartbeats. Its route has an empty
topic filter because heartbeat envelopes do not have an MQTT topic.

MQTT connection health does not imply history recovery: the relay reports
`resynced=false`. With evidence enabled, this app repairs review history through
the Frigate REST API before finalizing activity. Failed/truncated repair pauses
decisions rather than inferring that silence means everybody left.

Privately configure the existing NyxBot using `assistant-memory.md`, or let it
read the private destination configuration. Give it the Google service access
needed to answer record queries. It remains the user's interaction agent; the
separate CMA worker consumes the candidates and writes rows.

## 6. Test at three levels

| Test | What it verifies | Limits |
| --- | --- | --- |
| Offline / synthetic | Unit tests, grouping, deduplication, durable state | No camera/model or real-meal claim |
| Explicit integration validation | Candidate outbox → CMA → bounded historical image → model → Sheet write/readback → NyxBot query | Bypasses real MQTT review admission and meal-trigger thresholds |
| Next actual meal | Camera detections, relay, trigger rules and useful dish observations | Requires real activity and visible food; evaluate recognition quality separately |

Run `uv run python -m unittest discover -s tests -v` locally. The existing
[`scripts/smoke.py`](../../scripts/smoke.py) exercises synthetic provider traffic
only on an isolated test app with evidence/CMA delivery disabled and shortened
rule thresholds; its restart/resume check verifies persistence.

For local HA validation, use the actual owner-only operator service:

```sh
nyxid proxy request <operator-service-slug> "api/validation" --method POST --body '{}'
nyxid proxy request <operator-service-slug> "api/results/<returned-candidate-id>"
```

The node injects the API token. Do not send that token to NyxID as the owner
Authorization credential. Submit once and retain the candidate ID.

For the cloud alternative, save the header line
`Authorization: Bearer <API_TOKEN>` in a private 0600 `operator.headers` file, then:

```sh
curl --fail-with-body --request POST \
  --header "@$DINING_SITE_DIR/operator.headers" \
  "$APP_ORIGIN/api/validation" > "$DINING_SITE_DIR/validation.json"
```

This selects one frame near the latest completed review from the configured
camera within the last day. Historical recording availability is required. Save
the returned synthetic `meal_id`; a successful admission is not completion.
Poll `GET /api/results/{meal_id}` with the operator header until the worker
reports `integration_test` and its verified range. Independently read that range
and column B through Google Workspace: exactly one matching ID, ten matching
cells and status `integration_test`. Ask the existing NyxBot to query that same
ID and exclude it from real meal counts. Tests never send completion notifications.
If admission times out, inspect existing deliveries before
submitting another validation: `/api/validation` does not have an idempotency
key contract and a second POST can create a second test.

For the real-meal test, retain the fixed angle and enough light, eat normally,
then leave food/dishes visible after leaving the area. The default rule uses
eight minutes of qualifying person/seat review intervals and tableware evidence,
a five-minute quiet window and delivery grace. Allow extra time for Frigate to
close its review and for worker execution. Candidates are limited to three per
day with an hour cooldown. These are deployment parameters, not food semantics.

### Optional completion summaries

Set `NOTIFY_SERVICE` at bundle rendering time to enable a completion hook. Leave
it unset to keep notifications disabled and grant no notification service access.
Create an owner-private NyxID connection whose fixed endpoint is a webhook,
with its authentication secret stored in the connection, not the worker prompt.
Use `NOTIFY_PATH` for the hook's relative path when the connection points to its
parent URL. Verify the exact proxy URL; do not assume an upstream treats a
trailing slash on an empty path as equivalent to the hook route.
Grant that one connection to the channel bootstrap key as well as the profile.
The worker calls it only after a real `recorded` result and all ten Sheet cells
have been verified. It never calls Telegram or another chat API itself.

The receiver must durably deduplicate `event_id` before starting a summary run.
The contract is `kind=dining_record_completed`, `candidate_id`, `sheet_range`,
`status=recorded`, and `event_id=dining-summary-v1:<candidate_id>`. An uncertain
transport is retried with the same ID and body at most twice. Notification failure
does not repeat visual analysis or Sheet writes. HTTP acceptance proves queueing,
so verify the receiver's run and outbound delivery receipt during installation.

For example, a NyxID bearer-authenticated webhook trigger can target an existing
NyxBot, use `thread_policy=new` and `overlap=queue`, and bind `deliver_to` to an
owner-authorized chat. Give the summarizer fixed read-only instructions to locate
the unique recorded row and preserve meal/post-meal uncertainty. Its final answer
is automatically delivered; do not also call a posting tool. Keep bot/chat IDs,
trigger URLs and secrets in private deployment configuration. An owner-signed
`dining.notification_replay` event can verify an existing row without fetching
images or writing data; `manual_backfill=true` labels the initial delivery as a
historical notification test. Its stable notification ID also prevents a replay
from sending a second summary.

Keep the hook disabled until that full acceptance check succeeds. Upstream
[CMA #1231](https://github.com/ChronoAIProject/cma/issues/1231) tracks a failure
when expanding channel scope to a personal custom connection, and
[NyxID #2099](https://github.com/ChronoAIProject/NyxID/issues/2099) tracks model
authorization failures in new webhook automation threads. Queue acceptance and
local example tests do not establish that either deployed path works. Preserve
the original worker scope and keep maintenance summaries in a private pending
outbox while those conditions apply; do not broaden grants as a workaround.

## Troubleshooting and maintenance

- No candidate: inspect Frigate labels/zones and review ends, relay connectivity,
  REST repair, duration/quiet limits, cooldown and daily cap in that order.
- Candidate delivered but no row: inspect the actual worker turn and result.
  HTTP 2xx admission is not task success. Correct service grants, API paths or
  schemas while retaining the same meal_id and already-used visual evidence.
- No historical frames: record a pending result. Retention must cover the quiet
  window, processing delay and expected outages; a current image cannot replace
  a missing past frame.
- Restart: verify the configured persistence backend restores decisions,
  receipts and pending outbox entries. Archive retired test state separately.
- Rotation: for HA, renew the dedicated node-service delegation before expiry
  and update its private worker-facing credential. A cloud installation also
  needs its scoped Frigate reader key renewed. Keep HMAC/key-ID changes
  consistent; CMA owns its managed sandbox key lifecycle.
- Migration cleanup: verify the local chain and restart persistence, archive
  source specs/state privately, clear/remove the obsolete database and cloud
  app, then revoke only their exclusive keys. Keep current routing/delegation
  and operator services; they are credential roles for one running Pipeline.
- Failed worker attempts: stop/delete the unused Agent and retire its dedicated
  sandbox via CMA APIs. Check actual terminal state and revocation; an accepted
  request can remain pending if runtime dependencies fail. Retained history is
  not a running worker. Keep cleanup receipts private.
- Full retirement: disable the application Relay route before removing its
  Pipeline/worker/channel, then intentionally archive/remove its private state
  and grants. Other configured workflows must remain unaffected.

Independent meals require a profile-managed CMA target and `per_event` partition.
The channel template now uses these defaults. Do not attach all meals to a
single existing Agent: gateway partitioning cannot reset that Agent's context.
Use the optional scoped CMA cleanup described in the root guide to retire idle
per-event runtimes; keep durable meal history in the Sheet and local callbacks.
