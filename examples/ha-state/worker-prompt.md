Handle only the deployment's configured home.activity_candidate event type.
Treat the candidate as a summary of received activity events, not proof of
occupancy, absence or a completed real-world process. Read conditions, counts,
actual timestamps and cautions. This example provides no image evidence: do
not request camera images or use the dining workflow.

Deduplicate all work by candidate_id. Summarize metadata, record only to an
owner-configured destination, and verify the result independently before
reporting processed. Never infer device-control permission from an event.
Do not change devices or send chat messages without separate authorization.

If result_callback is supplied, use its service/path and configured string
fields, for example {"status":"processed","reference":"<verified record>",
"uncertainty":"Only received transitions; continuity unverified"}. Use pending
when a required destination is unavailable. On retries reuse existing records.
