# Reusable scenarios and live analysis edits

A rule owns event selection, time windows, evidence options and its analysis
workflow. Edit these in the HA page or owner rule API; saving creates an immutable
local revision and applies to future activity. This requires no image release,
add-on restart or CMA profile replacement. Existing queued work and callbacks
keep the original revision and analysis plan.

The shared worker uses the fixed [system prompt](../examples/worker/system-prompt.md)
to execute the plan supplied with each signed candidate. The Monitor selects
bounded evidence; the worker performs semantic analysis and verified downstream
writes. Each event still gets an independent CMA Agent context. Multiple rules,
cameras and scenario event types can share the same channel and worker runtime.

| Change | Apply at |
| --- | --- |
| Analysis criteria, prompt, output formatting, destination IDs | Rule `analysis`; save a new local revision |
| Predicates, quiet/cooldown limits, existing evidence options | Rule configuration |
| Add another scenario or camera using existing adapters | Import a template, bind local parameters and save a new rule |
| Add a destination service | Provision its connection and explicit worker/key grants once, then bind its name in a rule |
| Worker protocol/model, credentials or channel routing | CMA / deployment settings |
| New source/evidence adapter, new algorithm or UI feature | Core and installed HA package release |

## Use a template

In the HA page, open **New rule**, choose Dining, Driveway or HA events, fill
parameters, and generate the rule. Review its analysis instructions and workflow
settings, then save. New rules start disabled and delivery off. Enable them after
checking source routing, historical retention and the worker's service grants.
Adding a rule does not configure an external camera, Relay source or connection.

The current Dining template preserves its ten-column Sheets workflow; its
`workflow` parameter needs the private service names, connection ID, destination
paths, column order and timezone described in the [dining guide](../examples/dining/README.md).
Driveway and HA-event templates return callback summaries without external
business writes by default. Customize the workflow in the rule when needed.

**Import template JSON** accepts third-party templates without an image change.
**Download reusable template** downloads the unbound template; form values and
rendered rule context are not included. Review third-party defaults/instructions
before sharing or enabling them. Installation bindings and credentials belong
outside source repositories; a template is data, never an executable plugin.

The equivalent owner APIs are:

- `GET /api/scenarios`: built-in portable templates.
- `POST /api/scenarios/render`: `{id, parameters}` for a built-in template, or
  `{template, parameters}` for any imported template. It validates and returns
  `{pipeline}` without writing rules, sending events or calling a model.
- Use the existing rule create/update APIs to save the returned pipeline with
  normal revision preconditions. Restore previous settings by saving them as a
  new revision; original history remains accessible.

Offline rendering also needs no running service:

```sh
uv run python scenarios.py --template scenarios/driveway.json \
  --parameters /private/site/driveway-parameters.json \
  --output /private/site/driveway-rule.json
```

The renderer writes a new private file with mode 0600, refuses to overwrite and
rejects output inside the source checkout.

## Template contract

A `.json` template has schema `video-event-monitor.scenario.v1`, a stable `id`,
independent `version`, display `title`, `parameters` and `pipeline`. Parameters
have `type`, `title` and optional `default`; supported types are string, integer,
number, boolean and object. Parameters without defaults are required. A whole
JSON value `{"$param":"camera"}` substitutes a typed parameter. No string
interpolation, expressions, commands, imports or recursive expansion of supplied
values occurs. Unknown parameters and unsupported schemas are rejected.

See [the three complete templates](../scenarios). Templates are limited to
48 KiB and 32 parameters. The rendered pipeline must pass the normal 64 KiB
pipeline validator. Every rendered analysis plan records template ID, version
and SHA-256 for provenance; these are independent of the installed core version.
Subsequent local edits live in the local rule revision, not in that original hash.

`pipeline.analysis` is optional for compatibility with existing workers/rules:

```json
{
  "version": 1,
  "instructions": "Summarize the supplied observations and return uncertainty.",
  "context": {"config": {"destination_service": "example-records"}}
}
```

Instructions are limited to 16384 characters; context is a JSON object up to
16 KiB. It can hold local service names, connection IDs, destination paths and
workflow schemas. Recognized credential keys are rejected as a guard against
common mistakes; this is not a secret scanner. Keep all credentials in service
connections. A plan cannot expand the shared worker's granted services. Different
trust boundaries need separate channels/keys even when they share the protocol.
The Monitor never executes analysis instructions or calls a model itself.

Candidates carry a snapshot of `analysis`, plus `monitoring_rule` with its exact
revision and versioned evidence/result paths. Retries retain the serialized
original event. Changes to analysis do not change the aggregation policy digest;
managed rule edits still use the normal isolated-revision cutover behavior.
Test events remain synthetic and never send business notifications.

## Install or migrate a shared worker once

Render a generic profile-managed channel with explicit service grants:

```sh
python examples/worker/render.py --settings /private/site/worker-settings.json \
  --output /private/site/channel-request.json
```

Settings contain `owner_id`, `model_service`, `model`, `services` (including the
Monitor's worker-facing service and only required destinations), plus optional
`name`, `thinking_effort` and `hmac_key_id`. The output deliberately contains
credential placeholders. Create a distinct scoped channel binding key, fill it
and the HMAC secret privately, then use the [CMA creation procedure](../examples/dining/README.md#4-create-a-profile-managed-per-event-channel).
Keep `per_event` partitioning and the fixed generic system prompt. Apply the
returned event URL/signing configuration to HA and restart once; configure
scoped idle cleanup using the resulting native workspace/profile identities.

Existing dining workers remain compatible with rules that omit `analysis`.
To migrate, back up state, save a rule containing the current dining instructions
and private configuration in `analysis.context.config`, then switch to the shared
worker while activity is quiet. Verify a synthetic test, independent destination
readback, callback and restart. An immutable provider profile may require a new
channel for this one-time protocol change. Later analysis edits stay in rules.
