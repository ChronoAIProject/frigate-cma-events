# Dining review: Home Assistant → CMA → Sheets

This is a deployable example of the generic event pipeline. Frigate
detects activity locally; a deterministic service selects candidates before a
CMA worker performs bounded visual analysis and writes a Google Sheet. An
existing NyxBot can query and summarize those records. All published values are
synthetic. Your camera, owner, services, spreadsheet and credentials belong in
private deployment files.

```text
Dining camera --RTSP--> Frigate --MQTT reviews--> HA Event Relay
                                                    |
                                           HTTPS metadata + durable queue
                                                    v
                                            frigate-cma-events
                                      dedupe / merge / REST repair / limits
                                                    |
                                         signed meal-end candidate
                                                    v
                                            CMA Event Channel
                                                    |
                                                    v
                                              Dining worker
                                                    |
                bounded evidence request -----------+------> Google Sheets
                    |                                          ^
             this app's /evidence                               |
                    |                                   existing NyxBot
             NyxID -> HA node -> Frigate recordings           -> user
                    |
              <=24 samples -> <=12 selected frames -> <=3 pages -> worker
```

## Components and ownership

| Component | Source / responsibility |
| --- | --- |
| Frigate + MQTT broker | Existing local services: detections, recordings and review messages |
| HA node + Event Relay | [nyx-homeassistant-node](https://github.com/ChronoAIProject/nyx-homeassistant-node): local proxy and configurable event forwarding |
| Aggregation + evidence + outboxes | This repository; one app, one replica, durable state |
| Event channel + worker | [CMA](https://github.com/ChronoAIProject/cma): route candidates and execute visual analysis |
| Credentials + service routes + NyxBot | [NyxID](https://github.com/ChronoAIProject/NyxID): broker scoped access and provide the existing user-facing assistant |
| Personal installation | Private platform settings/files; never committed to any of these repositories |

The `dining-evidence` name in a deployment is a NyxID HTTP service pointing to
this same app. The worker and channel are CMA configuration, not another custom
worker codebase. This path uses CMA's existing `cma_codex` provider and the app's
`/ingest` endpoint; it does not require registering a new gateway provider.

## 1. Prepare the local services and destination

- Install/configure Frigate, an MQTT broker, NyxID Node and Home Assistant Event
  Relay. Keep the relay disabled until the cloud app and channel are ready.
- Fix the camera angle so the table is visible. Configure the camera's seat and
  table-surface zones and track `person` plus supported tableware labels
  (`plate`, `bowl`, `cup`, `bottle`). This example does not change camera settings.
- Enable Frigate MQTT with the `frigate` topic prefix. Confirm `frigate/reviews`
  messages, actual zone names and retained historical recordings. For a custom
  prefix, change the topic in your private relay configuration.
- Make the Frigate HTTP API reachable through a NyxID service on the local node.
  Verify `api/review?limit=5` and a historical recording snapshot through that
  service. The snapshot API is `api/{camera}/recordings/{timestamp}/snapshot.jpg`.
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

## 2. Render a private configuration bundle

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

Register an owner-scoped NyxID custom HTTP service for `APP_ORIGIN`, using bearer
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

## 4. Create the worker, then attach its channel

Use your current owner NyxID authorization through the supported CMA public API.
Keep the Authorization header in a private 0600 file (`owner.headers`) rather
than putting a token on the command line. Its line must be
`Authorization: Bearer <OWNER_ACCESS_TOKEN>`. Keep request bodies, receipts and
idempotency keys in `DINING_SITE_DIR`.

1. Complete CMA's normal NyxID browser sign-in once to establish renewable
   sandbox authorization. [CMA's REST guide](https://github.com/ChronoAIProject/cma/blob/main/docs/API_REST.md)
   describes this prerequisite. A normal NyxID CLI login alone can leave
   lifecycle creation waiting for that renewable connection.
2. POST `worker-request.json` to `$CMA_API_BASE/agents` with a stable
   `Idempotency-Key`. Save the exact request and response. Poll
   `$CMA_API_BASE/agent-lifecycle/{operation_id}` until `status=succeeded`, then
   verify the returned Agent is `ready`. If waiting for connection, finish sign-in
   and resume using the original request/idempotency key.

   ```sh
   curl --fail-with-body --header "@$DINING_SITE_DIR/owner.headers" \
     --header 'Content-Type: application/json' \
     --header 'Idempotency-Key: YOUR_STORED_WORKER_OPERATION_ID' \
     --data-binary "@$DINING_SITE_DIR/bundle/worker-request.json" \
     "$CMA_API_BASE/agents" > "$DINING_SITE_DIR/worker-response.json"
   ```

3. Put the actual ready Agent ID in `channel-request.json` at
   `provider.target.agent_id`. The generated profile/prompt is already supplied
   to the Agent at creation: an existing-Agent channel retains that Agent's
   configuration instead of applying the channel profile over it.
4. Create a **distinct NyxID key for this channel binding**, authorized for the
   selected services, and put it in `credentials.agent_key`. Do not reuse the
   app's provider token, the Frigate key, a CMA-managed sandbox key, or another
   binding's key. Fill the channel HMAC secret from the app's matching secret.
5. POST the completed request to `$CMA_GATEWAY_BASE/channels`, preserving its
   generated `record_id`, exact body and a stable idempotency key. For example:

   ```sh
   curl --fail-with-body --header "@$DINING_SITE_DIR/owner.headers" \
     --header 'Content-Type: application/json' \
     --header "Idempotency-Key: YOUR_STORED_CHANNEL_OPERATION_ID" \
     --data-binary "@$DINING_SITE_DIR/bundle/channel-request.json" \
     "$CMA_GATEWAY_BASE/channels" > "$DINING_SITE_DIR/channel-response.json"
   ```

6. Verify `definition.creation.ready=true` and inspect provider field decisions.
   Refusals saying the existing Agent's configuration/credentials are retained
   are expected; a ready channel alone does not prove a successful model task.
   After an uncertain create, recover with `GET /channel-records/{record_id}`
   or repeat the exact original request. Do not invent another identity merely
   because a response was lost.
7. Copy `endpoints.custom_event_url` verbatim into the service env's
   `CMA_EVENT_URL` in private `app-spec.json`. Preserve opaque channel references;
   do not retype or shorten them.

Service access is chosen during normal Agent creation. Changes to an existing
Agent may currently require web approval even for an already-authorized owner
API client; see [CMA #1110](https://github.com/ChronoAIProject/cma/issues/1110).

## 5. Deploy, initialize the Sheet and enable the relay

Deploy the completed `app-spec.json` through your cloud platform and upload
`app-secrets.json` through its secret API. [Deployment details](../../deploy/README.md)
cover the chrono-cd paths. Use one replica and the rendered MongoDB add-on or a
durable volume. Wait for the platform to apply the spec, then verify the live
`/healthz` and authenticated `/api/status`; an applied spec alone is insufficient.

Create the configured tab in the configured spreadsheet. For a **new empty tab**,
initialize A1:J1 through the configured Google Workspace connection with
`sheet-header.json`, `PUT headers_path?valueInputOption=RAW`. For an existing
tab, read and verify its headers first. The worker's private configuration uses
`v4/spreadsheets/...`; the service base already supplies the Google API host.

Fill the private relay's broker password and `auth_token=RELAY_TOKEN`, then copy
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

For explicit integration validation, save the header line
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
ID and exclude it from real meal counts. No outbound chat notification is part
of this example. If admission times out, inspect existing deliveries before
submitting another validation: `/api/validation` does not have an idempotency
key contract and a second POST can create a second test.

For the real-meal test, retain the fixed angle and enough light, eat normally,
then leave food/dishes visible after leaving the area. The default rule uses
eight minutes of qualifying person/seat review intervals and tableware evidence,
a five-minute quiet window and delivery grace. Allow extra time for Frigate to
close its review and for worker execution. Candidates are limited to three per
day with an hour cooldown. These are deployment parameters, not food semantics.

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
- Rotation: renew the Frigate key before expiry, update app secrets, and keep
  channel HMAC/key-id changes consistent. CMA owns its managed key lifecycle.
- Retirement: stop the relay route, pause the dedicated worker/sandbox, and
  retire the cloud app/test app when no longer needed. Keep only deliberately
  retained private records; template source contains no installation data.
