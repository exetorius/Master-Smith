# Master Smith - worked by a coding agent in this folder

Master Smith builds game-ready hard-surface 3D assets (weapons, vehicles, aircraft, props) as ASSEMBLIES of parts:
every part is drawn alone, meshed alone, registered to its picture and fitted into the box the plan gives it. Since
2026-09-28 the owner works it from here: the coding agent (Claude Code, Codex, or another that reads this file) is
the director, planner and reviewer; the deterministic tools
in `python -m mastersmith.ms` do the drawing, meshing, registering and assembling. No server, no web page, no
container, no OpenRouter: the service, the chat app and the one-seed finish were deleted on 2026-09-28 (git
history before commit "Delete the service" has them).

Run everything from this folder with the venv: `.venv/Scripts/python.exe -m mastersmith.ms <command>`.
The step-by-step recipe is `.claude/skills/build/SKILL.md` (Claude Code invokes it as `/build`; any other agent
reads the file). Read it before a build. Agent-specific notes live in that agent's own file (`CLAUDE.md`, ...).

## Where things live

- `out/<Name>/` one job (folder name = asset name; `ms new` makes it):
  `brief.json` · `ref/` reference pictures · `plan/` side.png, front.png, `*_grid.png`, `dims.json`, `plan.json` ·
  `parts/<Part>/` side.png, quarter.png, seed.glb, registered.blend, registration.json, seed_render.png, fit.json ·
  `delivery/` SM_<Name>.glb + LODs, T_ maps, preview_*.png, `preview_views.png` (six sides), `preview.html`
  (written by `assemble`; `ms preview` opens it in the browser), report.json, zip.
- `mastersmith/skills/<category>.md` what a good asset of that category is (weapon, vehicle, aircraft, helicopter,
  prop, character, environment). Read the one for the job's category before planning.
- `mastersmith/blender/` the Blender scripts: `register_part.py` (turn the seed to match its picture),
  `assemble.py` (fit, tint, zones, glass, sharpen, bore alignment, bake, LODs, previews), `six_views.py`.
- `.env` holds FAL_KEY (git-ignored). NEVER print, echo, cat or grep the keys; never put them in a message.
- Local models: TRELLIS.2 (`trellis-cli.exe`, E:/local-models) meshes for free; FLUX.2 klein draws locally
  (`--model local`, weak). ComfyUI at E:/local-models/ComfyUI (Qwen-Image-Edit angles LoRA, unused so far).

## What costs money

- Nano Banana picture (`--model nano`, the default for `picture`, `view`, `part-pictures`): about $0.08 each,
  the three-quarter picture too. A 14-part gun is about 28 pictures = ~$2.30. `nano-pro` costs more; use it only
  for the hero reference picture.
- TRELLIS local mesh (`mesh --vendor local`): free, ~1 min per part on this PC. Tripo (`--vendor tripo`) ~$0.30;
  Hi3D v3 (`--vendor hitem3d3`) $2.10 per part - only when the owner asks for the paid mesher.
- Blender passes: free. Registration ~30 s, assembly 5-15 min, six views ~2 min.
Say the estimate before a step that spends, in one line; never spend on a step the owner did not ask for.

## The owner's rules (learned the hard way; do not relearn them)

1. LOOK at every picture and render with the Read tool before moving on. Nothing is "good" until you have seen
   `delivery/preview_views.png` from all six sides (top, bottom, front, back, left, right) and the previews.
2. Judge function by common sense, no reference needed: a bullet must be able to leave the gun (barrel, muzzle,
   sights and receiver on ONE axis, seen from the front and the top), wheels touch the ground, a canopy sits on the
   hull, nothing floats or pokes through. A muzzle is round, not oval.
3. Real materials: metal (barrel, muzzle device, bolt, rails) is dark reflective steel, grips and pads are rubber,
   optics have glass, furniture is polymer. One graphite-looking material everywhere is a fail. Colours come from
   the picture (`ms plan` samples them per part), not from guesses.
4. Edges are crisp: the assembler sharpens planar faces; a soft-plastic look means the part picture was soft or the
   seed was bad - redraw or re-mesh, do not accept it.
5. Every part is meshed from its OWN pictures (no Blender-coded parts since 2026-09-28: "try fully sculpting with
   TRELLIS"). Split the way a modeller would: barrel, muzzle device, each rail, each sight, magazine, grip, stock,
   handguard, each control, each wheel, each pod. Never lump (four tyres together came back as one black blob).
6. The part pictures are kept in `parts/<Part>/` so the same parts can be meshed again with a better model later.
   Never delete them.
7. Sizes are read off the gridded picture as percent boxes; touching parts overlap 1-2 %; boxes cover the whole
   silhouette. Thin free-standing parts (barrel, muzzle) get their height measured off the silhouette by `ms plan`.
8. The biggest part keeps the depth the mesher gave it (`keep_depth`); every other part fills its box on all three
   axes (`fill_box`). Parts on the bore axis (barrel, muzzle, suppressor, sights) are `centreline` parts and get
   moved onto the body's bore by the assembler.
9. Registration: a seed from a three-quarter picture is yaw-swept, pitch-swept, and, when long, sheared out of its
   perspective. Check `parts/<Part>/seed_render.png` against `side.png`; if it sits wrong, `ms register <job>
   <Part> --yaw <deg> --pitch <deg>` (yaw about the vertical, pitch in the side plane, degrees) and look again.
10. Commit only when `.venv/Scripts/python.exe -m pytest tests -q` passes. Commit messages end with
    the agent's own co-author line (see its file). Never merge PRs; never push to `dev`/`main`.

## The plan JSON (written by you, validated by `ms plan`)

Percent boxes are read off `plan/side_grid.png` (0 = left/top edge, 100 = right/bottom edge; forward end on the
RIGHT) and `plan/front_grid.png` when there is one (looking back at the forward end, the object's left on the right
of the picture). Without a front picture, `front_span` is your estimate of the part's width as percent of the
object's full width, centred parts symmetric about 50.

```json
{"parts": [
  {"name": "Barrel", "what": "one sentence: shape, features, colour and finish, as seen alone in the picture",
   "method": "vendor",
   "side_box": [x_left, x_right, z_top, z_bottom],
   "front_span": [y_left, y_right],
   "material": {"color": "#rrggbb", "finish": "metal|polymer|rubber|painted|glass|wood|fabric",
                "metal": true, "roughness": 0.35, "glass": false, "keep_texture": false},
   "zones": [{"name": "Pad", "side_box": [...], "front_span": [...], "material": {...}}]}
 ],
 "notes": "anything the assembly must respect"}
```
`zones` are areas of a part in a different material (rubber pad on a polymer stock, glass lens on a scope). `metal`
is true only for bare metal. A rifle is 10-16 parts, a pistol 6-10, a truck 12-20, an aircraft 8-14.

## Commands (`python -m mastersmith.ms ...`)

| command | does |
|---|---|
| `new <Name> --category weapon --size 0.68 --description "..."` | makes `out/<Name>/` with brief.json |
| `picture <job> --out ref/ref_0.png --prompt "..." [--ref file] [--model nano\|nano-pro\|local]` | draws a picture |
| `view <job> --which side\|front\|back\|top\|quarter --from ref/ref_0.png [--mirror] [--fixes "..."]` | one standard view of the same object |
| `grid <job> --side ref/ref_side.png [--front ref/ref_front.png] [--mirror]` | crops to the silhouette, draws the percent grids, writes dims.json |
| `plan <job> plan.json` | validates your plan, snaps thin parts, samples colours, writes plan/plan.json |
| `part-pictures <job> <Part> [--fixes "..."] [--no-quarter]` | side picture of that part alone + its three-quarter picture |
| `mesh <job> <Part> [--vendor local\|tripo\|hitem3d3] [--from quarter\|side]` | meshes the part and registers it |
| `register <job> <Part> [--yaw deg] [--pitch deg] [--from side]` | registers again, with your correction |
| `assemble <job> [--parts A,B] [--no-sharpen]` | fits, tints, bakes, LODs, previews, six views |
| `sheet <file.glb>` | six views of any GLB |
| `preview <job> [--no-open]` | serves `delivery/preview.html` (3D viewer, six views, every part's pictures beside its seed) and opens it |
| `package <job>` | README, manifest, zip in delivery/ |
| `status <job>` | what the job has so far |

`<job>` is `out/<Name>`. Every command prints where it wrote; Read those files.

## Testing and code changes

`.venv/Scripts/python.exe -m pytest tests -q` (about 10 s; the tests pin the LLM/no-spend env). Blender scripts
cannot be unit-tested; run them on a real part and look. When a Blender pass fails, its log is
`out/<Name>/<tag>.log`. Keep the code style: one-line docstrings that say why, dated notes for lessons learned.
