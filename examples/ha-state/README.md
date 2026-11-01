# Home Assistant event windows

This is one Video Event Monitor rule example. See
[architecture and updates](../../docs/deployment.md)
([中文](../../docs/deployment.zh-CN.md)) for component locations and configuration ownership.

This second application uses the same runtime as the dining example, with no
camera, table, dish labels, images or Sheets dependency. It groups repeated
events whose new state is `on` by entity, waits for a quiet window, then emits
`home.activity_candidate` to the deployment's CMA custom event channel.

The example requires three distinct events; duplicate delivery does not
increase the count. Events whose new state is `off` are filtered. Attribute-only
updates with state `on` still count; add an old-state filter when actual state
transitions are required. Different entity subjects
have separate windows, cooldowns and daily limits. This is a received-event
summary, not an occupancy/absence detector: `observed_events` health deliberately
does not claim continuous source history. Use heartbeat and explicit span events
for workflows requiring verified source continuity and explicit ends.

Run the entirely synthetic replay:

```sh
uv run python aggregation.py examples/ha-state/events.example.json \
  --pipeline examples/ha-state/pipeline.example.json
```

The output is one candidate, `event_count: 3`, no images, and the configured
event-schema fields. Replay does not deliver to CMA or call any model.

To deploy, copy the configuration outside the checkout, set its source URI,
subject selector and rules to match your installation, and provide
`pipeline_config` for a new [HA installation](../../docs/home-assistant.md), or
`PIPELINE_CONFIG_FILE` when rendering an optional cloud app. Configure HA Event Relay's event bus
source to forward selected `state_changed` events to this app's authenticated
`/ingest`. Its envelope type is `homeassistant.state_changed`, source is
`urn:homeassistant:event_bus:<relay-source-id>`, and subject is the entity ID.
Use the exact relay ID in the private pipeline configuration. Keep the relay's
entity allowlist narrow. The app applies its own independent field filters.

Supply your own channel URL, HMAC key ID and secret through the matching
HA options, or `CMA_EVENT_URL`, `CMA_HMAC_KEY_ID` and mounted
`CMA_HMAC_SECRET` on a cloud deployment. Configure a CMA profile-managed channel
with `per_event` partition, [worker-prompt.md](worker-prompt.md) and only its
required destination grants.
The optional callback uses a distinct `WORKER_TOKEN` and owner-provided
`WORKER_SERVICE`; no worker service is needed for forwarding alone.

On an existing core 0.3 installation, create this as a new rule in the HA page/API;
changing startup `pipeline_config` does not add a rule. Multiple rules can share
one Monitor when its configured channel consumer understands their event types
and permissions. The dining-only prompt does not understand this event type.
Use a separate Monitor/channel when a separate consumer or credential boundary
is needed. Preserve existing rule history instead of replacing its SQLite files.
