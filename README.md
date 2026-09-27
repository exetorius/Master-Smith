<p align="center"><img src="docs/logo.png" width="180" alt="Master Smith"></p>

# Master Smith

Prompt in, game-ready 3D model out. A chat agent directs a fixed pipeline: a clean reference picture, a vendor
mesh seeded from it, and a headless Blender finish that makes the mesh engine-ready (oriented, scaled to real
metres, glass slot, LODs, collision hull, packed PBR maps, previews, FBX/GLB). You can also hand it a model you
already have and get the same finish.

It runs on your own **fal.ai** key (pictures, meshes, masks, rigs) and **OpenRouter** key (the
director and the vision checks; OpenRouter's image models are available too). Nothing else is required. A typical build costs about a dollar of provider
spend; the director's chat costs cents.

One person's tool: your keys, your machine, your models. Contributions are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start (local)

Requirements: Python 3.11+, Node 22+, [Blender 5.2](https://www.blender.org/download/) (used headless), a fal.ai key
and an OpenRouter key.

```bash
git clone https://github.com/kevinpbuckley/Master-Smith.git
cd Master-Smith
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # put FAL_KEY and OPENROUTER_API_KEY in it
python -m mastersmith serve                           # API + worker on http://localhost:8080
```

Blender is expected at `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe` on Windows or `blender` on your
PATH elsewhere; set `BLENDER_BIN` otherwise.

Then the chat, in a second terminal:

```bash
cd web
npm ci
cp .env.example .env.local
npm run dev                                           # http://localhost:3000 (this machine only)
```

Describe an asset:

```
you>   a realistic modern bolt-action sniper rifle with a scope and bipod, bronze chassis, carbon barrel, 1.2 m, Unreal
smith> Brief: SniperRifle, weapon, realistic, Unreal, 60k tris, 1.2 m. Plan: clean side-profile picture, muzzle view
       + mirrored profile, Tripo multiview seed, Blender finish, review. Worst case about $1.10. Say go.
you>   go
```

On "go" the director first draws the reference picture (cents) and shows it in the chat with a **Build from this**
button. The mesh is bought only after you approve the picture; say what to change and it draws again. The build
then seeds from the approved picture and draws nothing new. Say "skip the preview" to do it in one go.

The dashboard above the chat follows the build: the log, the probe renders, then the finished model in a viewer
with the files to download. Every picture a job drew (the reference angles) is listed there too, and each picture
shown in the chat has a save link. Output also lands in `out/<Name>_<job>/delivery/`.

The header's three selectors choose, per session, the mesh vendor, the picture model (fal.ai's Nano Banana 2 by
default; fal's other picture models and OpenRouter's are listed too) and the director model; every label carries
the price.

Aircraft and helicopters get a glass canopy slot by default; the seed's own interior shows through it.

### Changing a build

Nothing is repaired on a finished mesh: the model is built right from its pictures. "Make it 2 m", "80k triangles",
"rig it", "give it glass" or "for Godot" re-finishes the same mesh (cents). Anything you would see - a wrong, missing
or extra part, a shape, a colour, a material, a melted detail - edits the reference pictures, shows them for approval
and builds again; an assembly (below) plans its parts again from the new pictures. When the reviewer says rebuild,
the director says what the pictures or the brief should change and asks before spending.

### Bring your own model

Attach a `.glb`, `.gltf`, `.fbx`, `.obj` or a delivered `.blend` in the chat and say what it is. It is finished
without buying a new mesh: oriented to +X forward, scaled (or kept at its own size), glass detected, LODs, collision,
maps packed, previews, package. From then on the conversation works on that model: "make it 2 m" or "rig it"
re-finishes it; a change you would see is a new build from pictures.

Without the chat:

```bash
python -m mastersmith import my_rifle.glb --name Rifle --category weapon --size 1.2 --glass
```

### Docker

```bash
cp .env.example .env            # keys
docker compose up --build       # API on :8080 (with Blender inside), chat on :3000
```

Both ports are published on 127.0.0.1 only: the chat spends your provider credit. To reach it from another machine,
set `MASTERSMITH_WEB_PASSWORD` in `.env` (the chat then asks for it) and change the `web` port mapping in
`docker-compose.yml`; keep the API itself private.

Builds, uploads and the spend ledger persist in the `mastersmith-data` volume. Blender's Cycles renders and
decimation are CPU-bound: give the API container cores and 8 GB.

Start and stop:

```powershell
.\scripts\start.ps1            # starts Docker Desktop if needed, brings both containers up, opens the chat
.\scripts\start.ps1 -Build     # rebuild the images first (after pulling code changes)
.\scripts\stop.ps1             # tear the containers down when you are not using it (the data volume stays)
.\scripts\stop.ps1 -Wipe       # ...and delete the volume too
```

Linux/macOS: `./scripts/start.sh [--build]` and `./scripts/stop.sh [--wipe]`. Running containers come back on their
own when Docker restarts after a reboot; stopped ones stay stopped until you start them.

## How a build goes

1. **Reference picture.** A clean product shot generated from the brief, or your own photo edited into one, or a
   photograph of the real thing found on the web when you name it (an F-150, a Glock 17). A vision model checks
   it: one object, plain background, right view. Weapons and vehicles get extra views for multiview seeding.
   Hard surfaces (weapons, vehicles, aircraft, helicopters) with an approved side and front view are then built as
   an **assembly** of planned parts, each modelled right on its own and checked against the pictures
   ([docs/ASSEMBLY.md](docs/ASSEMBLY.md)); everything else, and `build_mode: "single"`, goes on as below.
2. **3D seed.** Tripo H3.1 with detailed geometry and HD textures by default; Meshy v7 and Hitem3D are wired in as
   alternatives (`MASTERSMITH_SEED_MODEL`). Hitem3D v3 comes in two flavours at the same price: one picture
   (`hitem3d3`) or every approved angle in its named front / left / back / right slots (`hitem3d3mv`). The vendor mesh is the asset; nothing sculpts it afterwards.
3. **Blender finish** (headless, free) in two passes around a decision step:
   - *prepare*: join, long axis to +X (or +Z up for characters), scale to real metres, origin, probe renders;
   - *decide*: the vision model says which probe shows the front; SAM 3 returns masks for glass and wheels from
     text prompts;
   - *finish*: masks are projected onto faces. Painted glass gets the `MI_<Name>_Glass` slot; an empty window
     frame gets a pane built into it. Then roughness sanity, a high-poly normal/AO bake,
     `T_<Name>_BC/N/ORM.png`, LOD0/1/2, `UCX_` convex hull, previews, `SM_<Name>.fbx` (+LOD FBXs), `.glb`, `.blend`.
4. **Rig** when asked (default for characters): Meshy auto-rig with walk and run clips for humanoids; wheel bones
   for vehicles via a Hunyuan part split; Muzzle/Grip/Sight sockets for weapons.
5. **Review.** A vision model compares the renders with the reference and scores it. A **gate** checks the
   triangle budget, maps, size, hull and score, and a **package** writes `README.txt` with the engine import
   steps and a zip of everything.

The director (a cheap OpenRouter model) only talks and fills in the brief; it never writes Blender code and never
sees images. Why this shape: a bake-off showed that the reference picture is where realism comes from, that
decimating the vendor mesh in Blender is visually lossless, and that retopology and retexture vendors made things
worse; later, that every repair on a finished mesh cost more than building it right (2026-09-26). So the pipeline spends on the picture and the seed and does the rest itself.

## Money

`mastersmith/pricing.py` prices every fal endpoint the pipeline may call; an unpriced endpoint is refused. A build
reserves its worst-case estimate, spends, then settles to the real cost (fal table price + OpenRouter's reported
usage). The real account balances are read from fal and OpenRouter (`mastersmith/providers.py`): the chat header
shows them, the director quotes against them, and a build whose worst case exceeds a known balance is refused before
it spends anything. An unreadable balance never blocks a build. The ledger keeps score of what your keys spent,
job by job (`python -m mastersmith spend`); nothing is charged, held or refused by Master Smith itself.

## API

Put any made-up string in `.env` as `MASTERSMITH_API_KEY` and send it as `Authorization: Bearer <key>` (or
`X-API-Key`). With no key configured, every request is the local user: this is one person's tool. [docs/AGENT_API.md](docs/AGENT_API.md) has `curl` recipes for driving and debugging it: dry-run
estimates, queueing, polling, full logs, the debug bundle with Blender log tails, work files, and chat sessions.

| Method | Path | What |
| --- | --- | --- |
| POST | `/v1/chat` | `{message, session_id, attachments:[{path,name,kind}]}` → reply, brief, last job, spend |
| POST | `/v1/uploads` | multipart `file` (picture or model) → `{path, name, kind}` for attachments |
| POST | `/v1/jobs` | `{spec}` → queued build |
| POST | `/v1/jobs/import` | `{path, spec}` → finish an uploaded model |
| POST | `/v1/jobs/refinish` | `{source_job, overrides}` → re-finish an earlier job's seed |
| GET | `/v1/jobs`, `/v1/jobs/{id}`, `/v1/jobs/{id}/files/{name}` | status, log, summary, previews, downloads |
| GET | `/v1/estimate?name=..&category=..` | worst-case cost of a brief |
| GET | `/v1/providers` | what the fal and OpenRouter accounts have left (cached a minute; `?refresh=1`) |
| GET | `/v1/wallet`, `/v1/me`, `/healthz` | spend, identity, liveness |

Jobs are queued and run one at a time by the worker thread (Blender is CPU-bound). Run more workers with
`python -m mastersmith worker` against the same data directory, or set `MASTERSMITH_NO_WORKER=1` on the API.

**Another brain.** Claude Code, Codex or any MCP client can be the director with the same prompt, skills and
tools: `python -m mastersmith mcp` exposes them, the repo's `.mcp.json` and `/director` skill wire Claude Code up,
and every chat driven that way is recorded and shows in the web. The director's thinking then runs on your
subscription instead of OpenRouter; pictures, meshes and checks still run in the API. See
[docs/AGENT_MODE.md](docs/AGENT_MODE.md).

The chat in `web/` is a Next.js app on the Vercel AI SDK. Its route handlers proxy to this API (so an API key, if
any, stays server-side) and turn each turn into a UI message stream with a `data-turn` part carrying the brief and
the queued job.

## Layout

```
mastersmith/config.py      keys, paths, model routing
mastersmith/pricing.py     price table + job estimate
mastersmith/fal.py         fal queue client (submit/poll/upload/download)
mastersmith/llm.py         OpenRouter chat + tools + vision, cost capture
mastersmith/images.py      OpenRouter image generation and edits
mastersmith/wallet.py      SQLite spend ledger: what your keys spent, job by job
mastersmith/spec.py        the brief
mastersmith/brief.py       words in the brief that decide the category
mastersmith/skills/*.md    per-category guidance with front matter the pipeline reads
mastersmith/stages/        reference, plan + assembly (parts), seed, probe (facing + masks), finish (runs Blender),
                           rig, review, gate, package
mastersmith/blender/       headless Blender scripts: prepare, finish, build_part + assemble (parts), rig, blend_to_seed
mastersmith/pipeline.py    build() from a brief; rework() on an existing mesh (import / refinish)
mastersmith/agent.py       the director (chat + tools)
mastersmith/service.py     FastAPI: chat, uploads, jobs, files, wallet
mastersmith/worker.py      the build queue
mastersmith/cli.py         chat / run / import / rerun / spend / serve / worker
web/                       Next.js chat (Vercel AI SDK)
tests/                     pure tests; a Blender test behind MASTERSMITH_BLENDER_TESTS=1
```

## Adding a skill or a vendor

A skill is a Markdown file in `mastersmith/skills/` with `reference_view`, `second_view`, `mirror_as_third_view`,
`forward_axis` (long|up), `origin` (bottom|center) in the front matter, plus optional `glass_prompt` and
`rig_parts_prompt`, the phrases the probe turns into SAM masks.
A new fal endpoint needs a row in `pricing.FAL_PRICES` and a call site in a stage; nothing else.

## License

MIT. See [LICENSE](LICENSE).
