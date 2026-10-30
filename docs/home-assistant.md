# Home Assistant deployment

Run the generic core locally with the independent
[CMA Event Pipeline add-on](https://github.com/ChronoAIProject/nyx-homeassistant-node/tree/main/cma-event-pipeline).
The add-on repository packages a pinned core image and maps HA options; this
repository owns aggregation, evidence, authentication and HTTP behavior.
No cloud aggregation app or MongoDB is needed for this topology.

```text
Local camera -> Frigate -> MQTT -> HA Event Relay -> Pipeline /ingest
                                                     |
                                         rules + windows + dedupe + limits
                                                     |
                                           signed CMA Event Channel
                                                     |
                                                remote worker -> destination
                                                     |
                   NyxID worker service -> ordinary HA node -> Pipeline
                                                         /evidence and /results
                                                              |
                                                     local Frigate recordings
```

The worker performs visual inference; Pipeline performs deterministic metadata
processing and heuristic historical selection. Raw recordings stay in Frigate's
storage. Selected evidence is transmitted to the remote worker. NyxBot can query
the configured destination separately; the worker is not a replacement NyxBot.

## Install and configure

1. Add `https://github.com/ChronoAIProject/nyx-homeassistant-node` to the HA add-on
   store. Install the ordinary NyxID Node, Home Assistant Event Relay and **Video
   Event Monitor** (existing CMA Event Pipeline add-on slug). The temporary Admin node is for management only.
2. Copy an [example policy](../README.md#examples) outside the source checkout.
   Resolve its private source URI, selectors, conditions/windows, limits, output
   schema and evidence options. Supply the resolved JSON object serialized as
   the add-on's `pipeline_config` string. For dining, use the
   [application walkthrough](../examples/dining/README.md).
3. Fill the following options directly in HA. Use distinct random credentials
   of at least 32 characters; do not commit options or paste them in public logs.

| HA option | Core setting / purpose |
| --- | --- |
| `pipeline_config` | `PIPELINE_CONFIG_JSON`: resolved policy; no placeholders |
| `relay_token` | `RELAY_TOKEN`: authenticates `/ingest` |
| `api_token` | `API_TOKEN`: operator status, policy, replay, validation, result readback/export |
| `provider_token` | `PROVIDER_TOKEN`: reserved for optional L1 provider interfaces |
| `worker_token` | `WORKER_TOKEN`: stored-candidate evidence and result callbacks only |
| `worker_service` | `WORKER_SERVICE`: actual worker-facing NyxID service slug |
| `worker_name` | `WORKER_NAME`: non-sensitive public display name |
| `cma_event_url` | Exact custom event URL returned by CMA; preserve the opaque reference |
| `cma_hmac_key_id`, `cma_hmac_secret` | Matching CMA channel signing credentials |
| `frigate_url` | Explicit local Frigate HTTP/HTTPS base; actual add-on hostname and API port |
| `frigate_api_key` | Optional bearer for that local API; empty when its trusted internal API needs none |
| `heartbeat_seconds`, `tick_seconds` | Source freshness and processing interval |
| `state_import_url` | Optional one-time HTTPS migration export into an empty state directory |

The wrapper allowlists options; it always uses local SQLite `/data/state`.
It does not accept arbitrary environment variables, scripts, shell commands,
MongoDB settings or host paths. Connection/secrets options load at startup and require a restart. The initial
`pipeline_config` is imported once; manage persistent rules without restarting
through the [HA web page or owner API](monitoring.md).

HA password fields mask values in the UI; values still live in HA's private
options and can be included in backups. The runtime drops to UID/GID 1000,
requests no HA/Supervisor API access and publishes no host port. Inspect
Supervisor add-on information for its actual hostname; do not copy another
installation's repository prefix or container IP.

## Local source and Relay

For Frigate, set `frigate_url` to its trusted internal API, such as
`http://0123abcd-frigate:5000` (synthetic hostname). This core reads reviews,
object tracks, region geometry and historical recording snapshots locally.
It sends a bearer only when `frigate_api_key` is configured. The unauthenticated
internal Frigate API must not be exposed publicly.

Point the Relay's destination at the Pipeline's actual internal hostname:

```yaml
destinations:
  - id: event_pipeline
    url: http://0123abcd-cma-event-pipeline:8080/ingest
    allow_local_http: true
    auth_type: bearer
    auth_token: "<same value as relay_token>"
```

Explicit local HTTP accepts only supported trusted addresses/HA add-on hostnames;
HTTPS remains the Relay default. Configure narrow MQTT/HA source routes and use
exactly the same source URI as the policy. Include the required heartbeat route.
A topic-only review filter can exclude heartbeats, which have no MQTT topic.
Connection heartbeats report `resynced=false`; the Frigate adapter repairs source
history itself before finalizing a candidate, even without image evidence.
For multiple camera rules, forward all review subjects and a global heartbeat
with `subject: "*"`. Unmatched events do not create CMA work.

During migration, preserve an existing destination ID while changing its URL:
queued messages reference that ID. Pipeline's CMA outbox and Relay's delivery
queue are independent; a zero CMA pending count does not prove Relay drained.

## Worker and operator access

Create a node-bound bearer service for the Pipeline's internal URL, with node
binding and auth set in the initial service-create request. In the ordinary
node's `services` list, install that actual slug/URL with
`Authorization: Bearer <worker_token>`. Create a separate owner-only operator
service injecting `api_token`; workers must not get that route or credential.
The HA node performs credential injection; the Pipeline is a separate container.

A caller needs both service and node grants for a direct node-bound route.
CMA's current managed-key contract does not retain node grants
([CMA #1111](https://github.com/ChronoAIProject/cma/issues/1111)). Use this supported
explicit delegation for that client:

```text
CMA managed worker key (grant: existing worker-facing service)
  -> worker-facing NyxID service
       injects dedicated proxy-read delegation key
  -> HTTPS NyxID proxy URL of the inner node-bound service
  -> ordinary HA node
       injects worker_token
  -> Pipeline /evidence/{candidate_id} or /results/{candidate_id}
```

The dedicated key must grant exactly the inner service and its backing node,
with broad access flags false. Keep the worker-facing service ID during migration
so existing grants/prompt/channel remain valid. Leave CMA-managed keys under
CMA lifecycle control. Store the delegate privately, record its expiry, rotate
it before expiry, then verify actual worker access. This routing adds no cloud
application; these entries represent different auth roles for one Pipeline.

The worker retains its normal model and destination grants. Do not give it raw
Frigate access for this application. Both proxy hops must pass response bodies
and result POSTs; validate them through the actual worker route.

## Verify and operate

- Read public `healthz` and operator `api/status` / `api/pipeline`. Test that the
  worker route is denied access to operator endpoints.
- Use operator `POST api/replay` for isolated event/rule/deduplication checks.
  Expect `model_calls: 0`, `persistent_writes: 0`; this bypasses Relay/CMA.
- Submit `POST api/validation` once to exercise historical-image retrieval,
  signed delivery, actual worker image viewing, destination write/readback and
  `/results` callback. Save its synthetic candidate ID and independently verify
  the destination. Tests must report `integration_test`, not a real outcome.
  An uncertain POST must be inspected before retrying; it is not idempotent.
- Verify actual source activity/trigger behavior separately. For dining, a real
  meal with visible dishes is needed to assess recognition and leftovers quality.
- Restart the add-on and verify decisions, results and pending deliveries persist.
  Keep the entire `/data` on upgrade, including the rule registry and revision
  directories, and use HA backups; images are volatile bounded caches.
- Monitor storage: state is not automatically pruned. Raw recording retention
  and NAS capacity belong to Frigate and have separate settings.

## Migration and retirement

The [add-on runbook](https://github.com/ChronoAIProject/nyx-homeassistant-node/tree/main/cma-event-pipeline#persistence-and-migration)
covers the exact source-pause/export/restore sequence. Keep one writer, the same
aggregation policy and worker-facing service identity. Restore only into empty
state; remove `state_import_url` after successful migration.

After local-chain, historical-state, destination and restart verification:

1. Save the source spec/export and any database backup privately.
2. Confirm current Relay, worker routes and bootstrap settings no longer depend
   on the cloud source. Stop it and verify no pending source delivery remains.
3. Clear the retired app's exact database and verify removal of its collections,
   then delete its deployment/routes/secrets/database account. Check actual
   platform purge semantics: account removal is not proof of data removal.
4. Retire failed CMA Agents and their dedicated sandboxes through supported
   stop/delete/retirement APIs. Check real terminal states and credential
   revocation; lifecycle acceptance is not completed resource removal.
5. Revoke only manual credentials proved exclusive to retired resources. Keep
   current worker-facing, delegated node and operator services and channel keys.
6. Update private deployment inventories and assistant memory. Clearly label
   retained backups/receipts historical; never publish site values or media.

The [cloud deployment guide](../deploy/README.md) remains available for
installations choosing remote aggregation. Both deployment options use this
same generic core.
