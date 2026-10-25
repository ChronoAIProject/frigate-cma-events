# Vehicle activity with historical evidence

This uses the same Frigate source adapter, generic aggregation engine and
evidence selector as dining, with different configuration. It needs no person,
seat, table or food detection. Ten seconds of car/motorcycle review intervals
in a configured gate zone qualify a candidate. The output type is
`security.vehicle_visit_candidate`; budgets sample at most 18 source frames
and select up to six frames in two pages.

Copy [pipeline.example.json](pipeline.example.json) outside the checkout and
set your source URI, camera subject and crop zone. Deploy with
`PIPELINE_CONFIG_FILE`, the owner's `FRIGATE_PROXY_URL`, mounted
`FRIGATE_API_KEY`, `WORKER_SERVICE` and distinct mounted `WORKER_TOKEN`. Set
your own CMA channel URL and HMAC credentials. Source connectivity plus a
successful Frigate REST repair are required before finalization.

Configure a separate worker to describe only supported vehicle activity using
frame IDs/timestamps. A candidate must not establish visitor identity, arrival,
departure or authorization, and must never implicitly authorize gate/device
control. Choose and grant a destination separately; Sheets is optional.

Use separate app/state and a separate channel from dining. The root deployment
guide describes secrets and single-instance ownership boundaries.
