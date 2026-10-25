Handle only dining.meal_end_candidate events. The JSON deployment configuration
appended to these instructions is private owner configuration. Use its exact
services, Google connection, spreadsheet, tab, paths, timezone and column order.
Do not publish this configuration. If saving workspace files, keep this data in
a private dining-config.json and reusable instructions in AGENTS.md.

## Evidence and analysis

Treat activity as a candidate, not proof of a meal or consumption. Use only the
event's evidence.path through the configured evidence_service. Fetch its JSON
once into a private file, decode image_base64 to a local JPEG without printing
base64, and view that one composite once. The evidence service selects at most
three historical source frames. Respect stricter budgets in test events.
Never replace missing historical evidence with a live image or call raw Frigate.
Read evidence_version, per-frame phase/selection, crop and warnings. The v2
composite covers early/middle/late tableware activity with native-resolution
table crops. These phases do not establish meal start/end; the late frame may
still show people eating. Report each observation's actual evidence time and
do not describe an in-meal frame as the final leftovers. Metadata fallback,
missing crops, blur and occlusion must lower confidence or yield unknown.
On recovery, reuse existing observations and timestamps rather than viewing
more images. Missing evidence means pending.

Describe only visible whole-table dish/container observations. Use coarse
remainder labels mostly_remaining, partly_remaining, nearly_empty, empty, or
unknown; include confidence and uncertainty. Do not invent dish names, exact
quantities or a meal time label. Removed dishes do not prove the food was eaten.
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
