# Another brain: Claude Code, Codex or any MCP client as the director

The web chat's director is an OpenRouter model. For testing, or just to use a subscription you already pay for, the
director's *thinking* can run in Claude Code, Codex or any MCP client instead, with the same system prompt, skills,
tools and remedies. Pictures, meshes, masks, the Blender finish and the vision checks still run in the API on your
fal.ai and OpenRouter keys; only the director's own tokens move. OpenRouter stays available in the web at any time.

Every chat driven this way is recorded on the API (`record_turn`), so it appears in the web's sidebar with its jobs,
pictures and downloads like any other, and can be continued there or here.

## How it fits

```
Claude Code / Codex  --MCP (stdio)-->  python -m mastersmith mcp  --HTTP-->  the API (:8080)
                                       (one tool per director tool)         session state, jobs, chats
```

`python -m mastersmith mcp` exposes: `director_prompt`, `set_brief`, `make_reference`, `build`, `import_model`,
`job_status`, `read_skill`, `ask_customer`, `balance`, `upload_picture`, `record_turn`. Each call runs the same
director method the web chat uses, in a session named by `MASTERSMITH_SESSION` (default `director`).

Under the hood the API offers `GET /v1/sessions/{id}/prompt`, `POST /v1/sessions/{id}/tool {name, args}` and
`POST /v1/sessions/{id}/turn {user, reply}`; any client that can speak HTTP can be the director the same way.

## Claude Code

The repo ships `.mcp.json` (the server) and the `/director` skill. From the repo folder, with the API running:

```
claude                      # in E:\Master-Smith; approve the master-smith MCP server when asked
/director                   # then talk: "a weathered oak ammunition crate, 1 m, Unreal"
```

The skill tells Claude to read `director_prompt` first, act only through the tools, show reference pictures for
approval before building, and record every turn. Set `MASTERSMITH_API_KEY` in `.mcp.json`'s `env` if the API has one.

Headless, for scripts and tests:

```
claude -p "a wooden barrel, 1 m, Unreal; set the brief and draw the reference" --mcp-config .mcp.json \
       --allowedTools "mcp__master-smith__*" --append-system-prompt-file .claude/skills/director/SKILL.md
```

## Codex

```
codex mcp add master-smith -- python -m mastersmith mcp
```

with `MASTERSMITH_API_URL` (and `MASTERSMITH_API_KEY`, `MASTERSMITH_SESSION`) in the environment Codex starts from.
Then tell Codex: "Call director_prompt and act as the Master Smith director. I want a wooden barrel, 1 m, Unreal."

## Repair acceptance

The director distinguishes a completed job from an accepted asset. `job_status.summary.quality` is the live
quality assessment, including for older jobs whose stored gate incorrectly passed a `rebuild` verdict.
A requested cabin or added assembly needs per-part visual evidence in `review.assembly_checks`, not just a
successful join or a triangle count. Future finishes render `preview_assembly_iso.png` and
`preview_assembly_top.png` from the delivered LOD0, keeping the hull and glass in place. Obscured detail is
reported as unverified. Missing parts, missing close-ups and a rebuild verdict prevent visual acceptance;
files remain available for inspection. `gate.technical_ok` reports packaging checks separately.

Before another failed assembly rework, change a specific fit/geometry setting rather than repeating the same
brief. Reuse bought parts; avoid generating a separate control already included in a cockpit module. In addition
to `offset_m` and `size_m`, `add_parts[].yaw_degrees` can override the prepared part's facing with -180, -90, 0,
90 or 180 degrees, without another facing-model call. Omit it for automatic facing. A missing interior anchor
is reported, never silently replaced with the whole body's bounds. These safeguards do not repair existing
models retroactively or start paid jobs automatically.

### Targeted repair planning

`plan_repair` reads a completed job and proposes concrete brief edits without spending. For Havoc-style failures,
it prioritizes preserving a clean seed's maps, omitting redundant additions, and inspecting obscured controls;
it does not automatically launch a build. `add_parts[].provides` inventories the components in a module
(seat, panel, consoles, stick, pedals, floor, walls, bulkhead). The director checks for a loose control already
included in a compound cockpit before spending. Part-generation prompts honor the requested exclusions.

The finish preserves source normal/AO maps when multiple material UV domains make the shared-atlas baker unsafe.
For an existing bad result, `texture_fixes: ["preserve_seed_maps"]` re-finishes its original seed without derived
normal/AO baking or reference projection. Reviews receive the source seed preview and a separately labeled
canopy-hidden diagnostic view: the cutaway proves internal geometry, not the delivered glass's appearance.

For local debugging, `scripts/replay_finish.py SOURCE_JOB EMPTY_OUTPUT` replays only Blender with saved masks
and purchased parts, making no provider calls. Use read-only source data and a separate diagnostic output. Optional
`--omit-additions CabinShell Joystick` and `--preserve-seed-maps` allow isolated repair comparisons without
replacing the original delivery or paying for another generation.

## What still costs money

| Where | What | Paid by |
| --- | --- | --- |
| The client (Claude Code, Codex) | the director's reasoning and tool calls | your subscription |
| The API | reference pictures, extra angles, cockpit/part pictures | OpenRouter image models |
| The API | picture checks, facing, review | OpenRouter vision model |
| The API | meshes, masks, rigs, repaint | fal.ai |

Building from Claude Code is exactly as expensive on fal and OpenRouter *images* as building from the web; what you
save is the director's chat, which is cents a turn with Gemini Flash and dollars an hour with Opus.
