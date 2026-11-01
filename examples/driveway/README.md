# Vehicle activity with historical evidence

This is a Video Event Monitor rule example. See
[architecture and updates](../../docs/deployment.md)
([中文](../../docs/deployment.zh-CN.md)) for component locations and configuration ownership.

This uses the same Frigate source adapter, generic aggregation engine and
evidence selector as dining, with different configuration. It needs no person,
seat, table or food detection. Ten seconds of car/motorcycle review intervals
in a configured gate zone qualify a candidate. The output type is
`security.vehicle_visit_candidate`; budgets sample at most 18 source frames
and select up to six frames in two pages.

Copy [pipeline.example.json](pipeline.example.json) outside the checkout and
set your source URI, camera subject and crop zone. For local HA processing,
supply the resolved JSON through `pipeline_config` on first installation, or
create a rule through the HA page/API on an existing installation. Set a local `frigate_url`
in the [HA add-on](../../docs/home-assistant.md); set worker-service/token and
CMA channel/HMAC options there. For optional cloud processing, use
`PIPELINE_CONFIG_FILE`, HTTPS `FRIGATE_PROXY_URL`, a scoped mounted
`FRIGATE_API_KEY`, `WORKER_SERVICE` and distinct mounted `WORKER_TOKEN`. Source connectivity plus a
successful Frigate REST repair are required before finalization.

Configure the CMA worker to describe only supported vehicle activity using
frame IDs/timestamps. A candidate must not establish visitor identity, arrival,
departure or authorization, and must never implicitly authorize gate/device
control. Choose and grant a destination separately; Sheets is optional.

Multiple rules can share one Monitor and channel if the consumer supports all
their event types and permissions. The dining-only consumer cannot analyze
vehicle events. Use a separate Monitor/channel when a separate consumer or
credential boundary is required; the rule page does not create consumers.
The deployment guide describes storage and single-writer ownership boundaries.
