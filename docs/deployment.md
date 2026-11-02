# Architecture, configuration and updates

[中文](deployment.zh-CN.md) · [HA installation](home-assistant.md) · [Rule management](monitoring.md)

Video Event Monitor runs event aggregation and historical image selection on HA.
CMA runs the analysis worker remotely. An optional existing NyxBot reads verified
results and sends a summary. These have separate configuration and update steps.

## Names and deployment locations

| Name | Meaning | Runs where |
| --- | --- | --- |
| **Video Event Monitor** | Product name and HA add-on display name | HA |
| `video-event-monitor` | This repository and the core image package | Inside Video Event Monitor on HA |
| `video-event-monitor` | HA add-on directory/slug in `nyx-homeassistant-node` | The same Video Event Monitor installation |
| Home Assistant Event Relay | MQTT/HA event forwarding with its own durable queue | Separate HA add-on |
| NyxID Node | Scoped access to configured HA/LAN HTTP services | Separate HA add-on |
| CMA event channel and worker profile | Event routing, model, instructions and permissions | CMA |
| Existing NyxBot and optional notification automation | Read results, produce and deliver a summary | NyxID, with the selected chat platform |

One product name, **Video Event Monitor**, and one technical name,
`video-event-monitor`, identify the repository, core image and HA add-on. The
HA wrapper image uses the same stem with a `-ha` suffix. Existing installations
need a [complete-state identity migration](https://github.com/ChronoAIProject/nyx-homeassistant-node/blob/main/docs/monitor-identity-migration.md)
before changing their HA slug. A NyxID service entry represents
an authenticated route; several entries can expose one application with different
credentials. Creating an entry does not deploy another container.

The [HA repository](https://github.com/ChronoAIProject/nyx-homeassistant-node)
packages the core and owns the Relay/node implementations. This repository owns
monitoring code and reusable examples. Worker/channel definitions and notification
automations are platform configuration. Dining is an [example](../examples/dining/README.md)
of the generic monitor. The [cloud aggregation alternative](../deploy/README.md)
is a different placement choice; an HA installation does not also need that app.

## Event consumption

```text
HOME ASSISTANT
Camera -> Frigate -> MQTT reviews -> Event Relay
                                         |
                             authenticated metadata
                                         v
                              Video Event Monitor
                         rules / merge / dedupe / limits
                                         |
                              signed candidate event
                                         v
CMA                         Custom event channel
                              profile + per_event
                                         |
                                   Analysis worker
                                         |
     +-----------------------------------+-----------------------------+
     |                                   |                             |
     v                                   v                             v
Bounded evidence request          Destination write             Result callback
NyxID -> HA NyxID Node            and independent readback       -> HA Monitor
  -> Monitor -> Frigate                 |
  <- selected image pages               | verified success only
                                        v
NYXID                    Optional scoped notification service
                         -> webhook automation, new thread
                         -> existing NyxBot, independent result read
                         -> fixed owner-authorized chat
```

The Monitor selects evidence deterministically; the CMA worker performs visual
inference. Default Frigate budgets are at most 24 source frames, 12 selected
frames and three pages; rules/tests may impose smaller budgets. Raw recordings
stay in Frigate storage. The optional completion event contains a record
reference, without images. NyxBot reads the stored result to summarize it.

The dining notification hook runs in the worker after ten-cell Sheet readback
and a successful `recorded` callback. Its receiver durably deduplicates the stable
notification ID. Test/pending records remain silent. A failed notification does
not repeat analysis or the destination write. The webhook receiver is configured
in NyxID; do not assume an independent CMA results channel has been provisioned.
See the [completion contract](../examples/dining/README.md#optional-completion-summaries).

Each candidate uses a profile target and `per_event` partition. An existing-Agent
target can reuse native context despite that partition. One Monitor deployment
currently has one CMA destination/signing configuration and worker credential.
Multiple rules can share it when the consumer supports their event types and
permissions. Adding a rule does not automatically create or retune a worker.

## Configuration authority

| Configuration | Source of truth | How to apply |
| --- | --- | --- |
| Filtering, cameras, windows, evidence budgets, analysis plans, output/result contract | Monitor's persistent rule registry | HA page or owner rule API; each edit creates a revision |
| `pipeline_config` / `PIPELINE_CONFIG_*` | First-start bootstrap only | Imported once as `default` v1; later edits use the registry |
| Frigate URL, CMA URL/HMAC, tokens, cleanup pins, timers | Private HA add-on options | Save and restart the Monitor |
| MQTT/HA subscriptions, routes and destination credentials | Private Event Relay options | Save and restart the Relay |
| Local HTTP targets and injected credentials | NyxID service records plus node options/local credential store | Apply the matching server route and node configuration |
| Model, shared worker protocol, services, partition and channel source | CMA profile/channel definition | Use CMA management APIs; independently read back the result |
| Summary instructions, bot and fixed chat | NyxID notification automation | Configure/enable the receiver and verify native delivery |
| Raw recording retention and disk/NAS storage | Frigate and HA storage configuration | Apply through those components |

The HA rule page currently manages rules, not all of the remote configuration
above. There is no single deploy command that synchronizes HA, CMA and NyxID.
`examples/dining/render.py` produces a private cloud app/worker/channel bundle;
it does not install HA add-ons or publish its outputs to platform APIs.

With the shared scenario worker, analysis instructions and local settings live in each rule’s `analysis` plan and apply through rule revisions. Third-party templates can be imported without an image release. See [portable scenarios](scenarios.md).

For legacy installations, `examples/dining/worker-prompt.md` is a deployment template. Its text is copied
into the private CMA profile. Editing or merging that file does not change an
existing worker until the profile/channel configuration is applied.

## Which changes need an HA upgrade?

| Change | Required application | HA image upgrade? |
| --- | --- | --- |
| Rule/evidence parameters, scenario analysis instructions/settings | Save a versioned rule; verify decisions during a quiet period | No |
| Monitor connection, token, timer or cleanup setting | Save HA options and restart Monitor | No |
| Shared worker protocol/model/grants | Apply CMA profile/channel, inspect refusals and read back | No; a new channel URL/HMAC or cleanup identity also requires HA option changes/restart |
| Completion notification | Configure receiver and worker hook/grants; test one replay and deduplication | No core change required for the existing dining hook |
| Monitor aggregation, evidence, API or UI code | Publish core image; update HA pin; publish a new add-on version; upgrade installed HA add-on | Yes |
| HA startup wrapper or add-on schema | Publish a new HA add-on version and upgrade it | Yes |
| Relay or node executable code | Publish and upgrade that independent add-on | That component only |
| Documentation | Publish documentation | No installed runtime change |

Core release sequence:

```text
core code + version -> CI/tests -> immutable core image digest
  -> HA video-event-monitor/Dockerfile pin + config.yaml version + changelog
  -> HA image CI/startup smoke -> Supervisor store refresh and add-on upgrade
  -> read installed version/options, test health and persistence
```

A repository merge or successful image build does not prove HA was upgraded.
HA pins a core digest, so publishing core `main`/`latest` does not move an installed
add-on. CI can build/publish images for documentation changes under its current
triggers; that still does not update a running instance. The add-on version and
core version are different release numbers. See [HA packaging](https://github.com/ChronoAIProject/nyx-homeassistant-node/tree/main/video-event-monitor#packaging-and-updates).

## Applying a worker/channel change

1. Privately save the current channel definition, HA options, rule registry/data
   backup and destination schema. Record the intended prompt and permission change.
2. Apply the complete supported CMA profile/channel definition with optimistic
   version checks and narrow service grants. Inspect provider refusals and read back.
3. If the provider refuses a scope expansion with
   `binding_reconfiguration_unsupported`, prepare a new profile/channel rather
   than enabling all-service access. A new binding can allocate a new workspace.
4. Run a bounded acceptance task. For notifications, replay one existing verified
   record with an explicit historical/test label; do not fetch images or write rows.
   Repeat its identical notification ID and check there is no second run/message.
5. Before switching HA, check the old CMA outbox and active work. Keep old
   credentials while old deliveries still depend on them. Save the new exact event
   URL/HMAC and pin cleanup to its actual dedicated workspace and native profile.
   Restart Monitor for these option changes; retain a usable rollback record.
6. Verify rule/history persistence, actual worker access, destination readback,
   callbacks and notification receipts. Retire only resources proved exclusive
   to the replaced setup. Keep historical business results and stable event IDs.

Changing HA rules and changing the consumer contract are separate operations.
Coordinate `output.event_type`, exact evidence/result paths and allowed result
fields. Existing multi-rule events carry their own versioned callback paths;
the legacy unversioned endpoints remain pinned to imported `default` v1.

## Storage and verification

| Data | Storage / lifetime |
| --- | --- |
| Frigate raw recordings | Frigate's configured local/NAS storage and retention |
| Relay queue | Relay add-on `/data`, independent of Monitor's queue |
| Rules/revisions, decisions, candidates, callbacks, CMA outbox | Monitor `/data/state`; preserve the full HA backup |
| Selected image pages | Bounded volatile cache; selected evidence is sent to CMA |
| Analysis record | Configured destination, such as a Sheet |
| Worker conversation/runtime | CMA; optional cleanup pins exact workspace/profile and keeps business results |
| Notification dedup/run history and native receipt | Receiver/platform; do not equate acceptance with chat delivery |

Check Relay backlog, Monitor CMA backlog and context-cleanup counts separately.
A cleanup `pending` count within the configured grace period is not an unsent
candidate. A CMA admission receipt is not an analysis result; a `recorded`
callback is not independent proof of a destination write; a queued notification
is not a native chat send receipt. Verify each boundary and inspect the HA page
in both languages when changing UI behavior. Offline replay, integration
validation and real activity establish different things.

Keep one private installation inventory with installed HA/core versions and
image digests, rule revisions, channel record/version, prompt digest, actual
workspace/profile IDs, notification receiver, credential references/expiry,
verification receipts and rollback backup. These are inventory fields, not a
new accepted configuration schema. Keep secrets, camera/address identifiers,
destination IDs, evidence and site-specific receipts outside public repositories.

See [monitoring](monitoring.md), [HA operations](home-assistant.md),
[policy schema](pipeline.md) and the chosen [example](../README.md#examples) for
the component-specific procedures. Dated design documents and changelogs record
history; use current schemas, scripts and installed-resource readback for operation.
