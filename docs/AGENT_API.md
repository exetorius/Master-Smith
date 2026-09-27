# Driving Master Smith from a script or an agent

Everything the chat does is plain HTTP on the Python service (default `http://localhost:8080`). The OpenAPI schema is
served at `/docs` and `/openapi.json`. This page is the short version with `curl` recipes.

## Authentication

Put a key of your own in `.env` (make one with `python -c "import secrets; print(secrets.token_urlsafe(24))"`; the old
example value `ms_dev_change_me` is refused at start-up):

```
MASTERSMITH_API_KEY=<your key>
```

and send it as `Authorization: Bearer <your key>` or `X-API-Key: <your key>`. Requests are then the user
`agent` (rename with `MASTERSMITH_API_USER`). With no key configured, every request is the local user and no header
is needed.

```bash
export MS=http://localhost:8080
export H="Authorization: Bearer <your key>"
curl -s $MS/healthz                       # {"ok":true,"blender":true,"local_mode":false,"worker":true}
curl -s -H "$H" $MS/v1/me                 # who you are, spend, provider balances, recent jobs
curl -s -H "$H" $MS/v1/config             # effective models, vendors, paths, flags; which keys are set (never values)
curl -s -H "$H" $MS/v1/providers          # what the fal and OpenRouter accounts have left
```

## Test a brief without spending

```bash
curl -s -H "$H" -H "Content-Type: application/json" "$MS/v1/jobs?dry_run=1" -d '{
  "spec": {"name": "AmmoCrate", "category": "prop", "size_m": 1.0, "engine": "unreal",
           "description": "weathered oak military ammunition crate with rope handles and black stencils"}
}'
```

Returns the resolved brief (defaults filled in), the worst-case estimate with each priced step, and the provider
balances. Nothing is queued. `GET /v1/estimate?name=..&category=..&description=..` does the same from query
parameters.

## See the reference picture before buying a mesh

```bash
REF=$(curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/reference -d '{"spec": {...}}')
echo "$REF" | jq '.pictures, .checks, .usd_cost'       # "/v1/jobs/<id>/files/ref_0.png" ... fetch and look at them
DIR=$(echo "$REF" | jq -r .dir)
```

This runs the picture stage only (tens of seconds, cents) and answers when the pictures are ready. Approve by
building with `"reference_job": "<dir>"` in the spec: the build seeds from those pictures and draws nothing new.
Change your mind by calling `/v1/reference` again with a changed description.

## Build

```bash
JOB=$(curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/jobs -d "{\"spec\": {..., \"reference_job\": \"$DIR\"}}" | jq -r .job_id)
```

Leave `reference_job` out to draw the picture and build in one go.

The answer is `{"job_id", "status": "queued", "kind", "estimate_credits"}`, or HTTP 402 with `error` when a provider
balance (or, on a shared instance, the user's credits) cannot cover the worst case.

Bring in a model you already have: upload it, then queue an import.

```bash
P=$(curl -s -H "$H" -F "file=@my_rifle.glb" $MS/v1/uploads | jq -r .path)
curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/jobs/import -d "{\"path\": \"$P\", \"spec\": {\"name\": \"Rifle\", \"category\": \"weapon\", \"size_m\": 1.2, \"glass\": true}}"
```

Re-finish an earlier job's mesh with a changed brief (no new mesh is bought):

```bash
curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/jobs/refinish -d "{\"source_job\": \"$JOB\", \"overrides\": {\"tri_budget\": 80000, \"rig\": true}}"
```

Delete a part the vendor grew, in Blender, and re-finish (cents: masks, a facing check, the review):

```bash
curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/jobs/refinish -d "{\"source_job\": \"$JOB\", \"overrides\": {\"remove_parts\": [\"the extra cylinder attached to the magazine\"]}}"
```

The first call answers a **preview**, not a job: `{"status": "preview_removal", "pictures": [{label, url, coverage}]}`
with the source job's renders and the faces that would go tinted red. Look at them. Repeat the call with
`?confirm_removal=1` to queue the re-finish, or reword the phrase and preview again. The `debug` answer of the
finished job lists `removed_parts` with the face counts; a phrase whose mask found nothing, or covered more than 30%
of the object, deletes nothing and says so in the log.

Model an attachment on its own and fit it onto the existing mesh (no new body; the part's picture + one seed, about $0.80):

```bash
curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/jobs/refinish -d "{\"source_job\": \"$JOB\", \"overrides\": {\"add_parts\": [{\"name\": \"CockpitInterior\", \"phrase\": \"the cockpit interior: pilot seat, instrument panel and side consoles\", \"anchor\": \"glass\", \"place\": \"inside\", \"size_m\": 2.2}]}}"
```

`anchor` is a phrase for the segmenter ("the top rail"), `glass` (the canopy faces) or `body`; `place` is one of
`inside`, `on_top`, `in_front`, `behind`, `below`; `size_m` is the part's longest dimension (0 = fit to the anchor's
box); `picture` may name an uploaded file to seed the part from. The finished job's `debug` answer lists
`added_parts` with the scale, the part's size and the anchor box it was fitted to.

## Watch and debug

```bash
curl -s -H "$H" $MS/v1/jobs/$JOB                  # status, log tail (40 lines), summary, file URLs
curl -s -H "$H" "$MS/v1/jobs/$JOB/log?tail=0"     # the whole log as text (tail=N for the last N lines)
curl -s -H "$H" $MS/v1/jobs/$JOB/debug            # job.json, work-dir listing, Blender log tails, bill by stage
curl -s -H "$H" $MS/v1/jobs/$JOB/work/probe_iso.png -o probe_iso.png      # any work file: probes, masks, args, logs
curl -s -H "$H" $MS/v1/jobs/$JOB/files/SM_AmmoCrate.glb -o SM_AmmoCrate.glb # any delivered file
```

Statuses: `queued` → `running` → `done` | `failed` | `refused`. A failed job's `error` names the stage; the
`debug` answer carries the Blender `prepare.log` / `finish.log` tails that explain it. Jobs run one at a time.

Delivered files follow the engine conventions: `SM_<Name>.fbx/.glb` (+ `_LOD1`, `_LOD2` FBX), `T_<Name>_BC/N/ORM.png`,
`SK_<Name>` and `A_<Name>_*` when rigged, `preview_*.png`, `README.txt`, `<Name>.zip`, `job.json` in the job dir.

## Chat, the way the web app does it

```bash
curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/chat -d '{"session_id": "t1", "message": "a matte black Glock 17, 0.2 m, Unreal"}'
curl -s -H "$H" -H "Content-Type: application/json" $MS/v1/chat -d '{"session_id": "t1", "message": "go"}'
curl -s -H "$H" $MS/v1/sessions/t1        # the director transcript, brief, last tool calls, chat cost
curl -s -X DELETE -H "$H" $MS/v1/sessions/t1
```

Each `/v1/chat` answer carries `reply`, `brief`, `last_job`, `tools` (the director's calls this turn, with results),
`providers` and `chat_cost_usd`. Attach an uploaded picture or model with
`"attachments": [{"path": "<from /v1/uploads>", "name": "x.png", "kind": "image"}]`.

## Running it for tests

- `MASTERSMITH_DATA=/tmp/ms-test python -m mastersmith serve --port 8081` keeps a test instance's builds, uploads and
  ledger apart from your real ones.
- `MASTERSMITH_NO_WORKER=1` on the service means jobs queue but never run: handy for testing the API surface with no
  Blender and no spend. Run `python -m mastersmith worker` when you want them to go.
- `tests/test_api.py` shows the FastAPI test client against a temporary data directory with a fixed key.
- A build reaches fal and OpenRouter and spends real money; `?dry_run=1` does not.
