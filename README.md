<p align="center"><img src="docs/logo.png" width="180" alt="Master Smith"></p>

# Master Smith

Game-ready hard-surface 3D assets (weapons, vehicles, aircraft, props) built as **assemblies of parts**: every part
is drawn alone, meshed alone, registered to its picture and fitted into the box a plan gives it, then the parts are
assembled, sharpened, given real materials, baked, LOD'd and packaged for Unreal (or Unity / Godot).

There is no service and no chat app. A coding agent - Claude Code, Codex, anything that reads `AGENTS.md` - runs
in this folder and is the director, planner and reviewer; a set of small deterministic tools
(`python -m mastersmith.ms`) does the drawing, meshing, registering and assembling. Pictures come from fal.ai's
Nano Banana (about $0.08 each); meshes come from TRELLIS.2 on your own GPU for free (Tripo and Hi3D are wired in
when you want to pay for a crisper part); everything else is headless Blender. A 14-part rifle costs about $2.30
in pictures.

One person's tool: your keys, your machine, your models. Contributions are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

Requirements: Python 3.11+, [Blender 5.2](https://www.blender.org/download/) (used headless), a fal.ai key, an
NVIDIA GPU with ~10 GB free for TRELLIS.2 (see `mastersmith/local.py` for the local models folder), and a coding
agent CLI (Claude Code or Codex).

```bash
git clone https://github.com/kevinpbuckley/Master-Smith.git
cd Master-Smith
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # put FAL_KEY in it
claude                                                # or codex; then: /forge a modern bullpup carbine, 0.68 m
```

The agent reads [AGENTS.md](AGENTS.md) (the rules, the costs, the plan format) and follows the recipe in
[.claude/skills/forge/SKILL.md](.claude/skills/forge/SKILL.md): it draws the reference pictures and shows them to
you, writes the parts plan from the gridded views, draws and meshes each part, looks at every seed, assembles, and
reads the six-view sheet before it calls anything good. You can interrupt at any step; nothing runs unattended.

## How a build goes

```
ms new <Name> --category weapon --size 0.68 --description "..."   out/<Name>/brief.json
ms picture / ms view                                             ref/: the hero picture, side and front views  (approve them)
ms grid                                                          plan/: silhouette-cropped views with a percent grid
   (the agent writes plan/plan_draft.json: one box per part, materials, zones)
ms plan                                                          validates it, measures thin parts, samples colours
ms part-pictures <Part>                                          parts/<Part>/side.png + quarter.png (~$0.16)
ms mesh <Part>                                                   seed.glb from TRELLIS (free), registered to side.png
ms register <Part> --yaw/--pitch                                 a correction after looking at seed_render.png
ms assemble                                                      delivery/: SM_<Name>.glb + LODs + maps, previews, preview_views.png, preview.html
ms preview                                                       serves and opens the page: 3D viewer, six views, every part beside its seed
ms package                                                       README.txt, manifest, zip
```

Every part's pictures are kept in `out/<Name>/parts/<Part>/`, so the same asset can be meshed again later with a
better model (`ms mesh <Part> --vendor hitem3d3`, $2.10 a part) without drawing anything.

The assembler (`mastersmith/blender/assemble.py`) fits each seed into its box, moves the barrel-axis parts onto the
body's bore, splits material zones (rubber pads, bare steel, glass), tints to the colours sampled from the picture,
sharpens planar faces, bakes the full-detail parts into one atlas, builds LOD0/1/2 and a convex hull, and renders
the previews and the six orthographic views the review is judged on.

## Layout

```
AGENTS.md                  the rules any coding agent follows here; CLAUDE.md imports it
.claude/skills/forge/      the build recipe (/forge in Claude Code)
mastersmith/ms.py          the tools: new, picture, view, grid, plan, part-pictures, mesh, register, fit, brush, sdf, assemble, sheet, preview, package, status
mastersmith/sculpt.py      headless sculpting in numpy: brushes (inflate, move, smooth, flatten, crease) and silhouette fitting
mastersmith/sdfkit.py      exact parts as signed distance functions (primitives, CSG, smooth blends, repeat) meshed by marching cubes
mastersmith/config.py      keys, paths, model routing
mastersmith/pricing.py     price table (an unpriced endpoint is refused)
mastersmith/fal.py         fal queue client; local/ ids route to this PC
mastersmith/local.py       TRELLIS.2 and FLUX.2 klein on this PC
mastersmith/images.py      picture generation and edits (fal, local)
mastersmith/llm.py, llm_cli.py   the few model calls some helpers make: claude -p / codex exec
mastersmith/spec.py        the brief
mastersmith/skills/*.md    per-category guidance (weapon, vehicle, aircraft, helicopter, prop, ...)
mastersmith/stages/        plan (grids, validation, colour sampling), assembly (part pictures, registration),
                           review (six views), package, finish (runs Blender)
mastersmith/blender/       assemble.py, register_part.py, six_views.py, blib.py; hskit/build_part/codecheck for
                           code-built parts (MASTERSMITH_ALL_VENDOR=0)
docs/ASSEMBLY.md           the assembly design and its history
tests/                     pure tests; Blender tests behind MASTERSMITH_BLENDER_TESTS=1
```

## Money

`mastersmith/pricing.py` prices every fal endpoint that may be called; an unpriced endpoint is refused. With
`MASTERSMITH_NO_SPEND=1` (the default in `.env.example`) every paid call is refused before it is made, except the
picture models when `MASTERSMITH_PAID_PICTURES=1`. The tools print what a step cost; the agent says the estimate
before a step that spends.

## Community

Master Smith is discussed in the **#master-smith** channel of the VibeUE Discord: join at
[discord.gg/hZs73ST59a](https://discord.gg/hZs73ST59a), then open
[#master-smith](https://discord.com/channels/1420185370943029271/1552465698733957150). Builds, results, questions and
ideas go there; bugs go to [GitHub issues](https://github.com/kevinpbuckley/Master-Smith/issues).

## License

MIT. See [LICENSE](LICENSE).
