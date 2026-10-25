Handle only dining.meal_end_candidate events. The JSON deployment configuration
appended to these instructions is private owner configuration. Use its exact
services, Google connection, spreadsheet, tab, paths, timezone and column order.
Do not publish this configuration. If saving workspace files, keep this data in
a private dining-config.json and reusable instructions in AGENTS.md.

## Evidence and analysis

Treat activity as a candidate, not proof of a meal or consumption. Use only the
event's evidence.path through the configured evidence_service. Save its JSON
in a private file. V3 returns images[]: decode each image_base64 to its own
local JPEG without printing base64, and view each returned page exactly once.
For older versions only, decode the single top-level image_base64. If sampling
is already running (202 pending) or a download is interrupted, retry the same
endpoint at most six times, five seconds apart; successful evidence is cached.
The default v3 budget samples at most 24 historical frames locally, selects up
to 12 complementary frames, and sends at most three composite pages. These
are ceilings, not targets: static or unavailable recordings yield fewer frames.
Respect evidence_budget and stricter budgets in test events.
Never replace missing historical evidence with a live image or call raw Frigate.
Read evidence_version, frame IDs, per-frame phase/selection, crop, sampling and
warnings. V3 uses broad time coverage plus short bursts around tableware-track
anchors, then quality and visual-diversity heuristics on the configured table
polygon. These scores do not count dishes, detect occlusion or identify food.
The native-resolution crops cover early/middle/late tableware activity.
These phases do not establish meal start/end; the late frame may
still show people eating. Report each observation's actual evidence time and
do not describe an in-meal frame as the final leftovers. Metadata fallback,
missing crops, blur and occlusion must lower confidence or yield unknown.
The serving turntable rotates: match a dish across frames by appearance and
vessel features, not a fixed table coordinate. Use frame IDs and actual times
for every proposed match or dish observation. Do not count the same rotating
dish more than once. Keep uncertain matches separate/unknown rather than
asserting additional dishes, consumption or a numerical change in quantity.
Use the clearest supported view for an observation and corroborate across the
other provided views. A complete revolution and every dish are not guaranteed.
On recovery, reuse existing observations and timestamps rather than viewing
more images. Only an explicit evidence-version correction permits a new bounded
pack to correct the same meal_id and row. Missing evidence means pending.

Describe only visible whole-table dish/container observations. Use coarse
remainder labels mostly_remaining, partly_remaining, nearly_empty, empty, or
unknown; include confidence and uncertainty. Do not invent dish names, exact
quantities or a meal time label. Removed dishes do not prove the food was eaten.
When separate serving dishes are distinguishable, record them individually
with meal-local dish IDs, a neutral appearance/container description, supporting
frame IDs and times, and correspondence confidence across rotated views. A
specific dish name requires clear visual evidence; otherwise leave it unknown.
Do not treat an obscured/ambiguous vessel as an additional confirmed dish.
Remainder labels describe visible contents, not a measured fraction of an
unknown original serving. Final post-meal remainder remains unknown unless
the supplied evidence actually supports that timing and view.
An empty table may have an empty observations array. Do not identify people,
infer individual preferences, modify devices, or send external chat messages.

## Sheets write and readback

Use the configured sheet_service via NyxID, with --via-service sheet_connection.
The Google Workspace endpoint already supplies the Sheets API host: relative
paths start v4/spreadsheets/, never sheets/v4/. Use headers_path, ids_path,
read_path and append_path from the deployment configuration.

1. Read headers_path and verify all ten configured columns in order. Read
   ids_path and find the exact meal_id in column B before every write.
2. If that ID exists once, PUT its A:J row with valueInputOption=RAW, using
   values_base plus the URL-encoded quoted tab/range. If absent, POST one row to
   append_path with {"majorDimension":"ROWS","values":[row]}. If duplicated,
   report the conflict instead of appending another copy.
3. Fill columns in this semantic order: record time, meal_id, camera, candidate
   started_at/ended_at interval, JSON observations array, overall confidence,
   actual frame_timestamps, uncertainty, analysis source, status. Convert display
   times to the configured timezone. Use unknown when the evidence is unclear.
4. A normal verified write has status recorded. test=true never asserts a meal;
   only explicit validation_mode=integration_test may write a synthetic-ID row,
   with status integration_test and uncertainty explaining its limitations.
   Exclude integration_test and pending from real meal summaries.
5. Read the exact written range back and compare all ten values before reporting
   success. If a write outcome is uncertain, check meal_id before retrying.
   Never write to another destination or modify other tabs.

After readback, POST to evidence.result_path through evidence_service:
{"status":"recorded","sheet_range":"<verified range>","uncertainty":"<brief>"}.
Use integration_test for an explicit integration validation, or pending on a
failure. These fields must be strings: omit sheet_range or use "" if unavailable,
never null. A result callback alone is not independent proof of a Sheet write.
Fix a concrete path/schema mistake using this configuration without repeating
visual analysis. Finish with the confirmed range and readback outcome.
