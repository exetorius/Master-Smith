---
name: forge
description: Build a game-ready hard-surface asset (weapon, vehicle, aircraft, prop) as an assembly of parts from this Claude Code session with the ms tools - reference pictures, gridded plan, per-part pictures, TRELLIS meshes, registration, assembly, six-view review, package. Use when the owner asks to build, rebuild, fix or re-mesh a model.
---

# Build an asset with `ms`

`PY=.venv/Scripts/python.exe -m mastersmith.ms`. Everything below is run from the repo root. Read AGENTS.md's rules
first; read `mastersmith/skills/<category>.md` for the category. Say the cost of a step before spending.

## 0. Pictures that already exist are used, never redrawn without asking
Before ANY step that draws (2, 4, and `--fixes` redraws): `$PY status out/<Name>` (or `ls out/<Name>/ref
out/<Name>/parts/*/`) and look at what is there. `ref/ref_*.png`, `parts/<Part>/side.png`, `parts/<Part>/quarter.png`
and `parts/<Part>/build.py` are the asset's source, kept on purpose (a job cleared down to them is normal: meshes,
registrations and deliveries are rebuilt for free). When they exist, show the owner the list and ASK whether to use
them or draw again; use them unless told otherwise. The tools refuse to draw over an existing picture (`kept: ...
already exists`); `--redraw` is passed only after the owner said to draw again. `ms new` on an existing job keeps its
pictures and brief (`--rebrief` rewrites the brief).

## 1. Brief
`$PY new <Name> --category weapon|vehicle|aircraft|helicopter|prop --size <longest side, m> --description "..."`
Sizes: a rifle 0.65-1.0 m, a pistol 0.2 m, a truck 5-6 m, a gunship 12-18 m. `--tris 100000` for a hero asset.

## 2. Reference pictures (approve them with the owner before meshing anything)
- A hero picture: `$PY picture out/<Name> --out ref/ref_0.png --prompt "..."` (`--ref` a photo when the owner gave
  one; `--model nano-pro` for the hero). Describe the design fully: era, materials, colours, every feature.
- Standard views from it: `$PY view out/<Name> --which side --from ref/ref_0.png` (forward end must point RIGHT;
  `--mirror` if it came out the other way) and `--which front`. Vehicles/aircraft also `--which back`, `--which top`.
- Read each picture. Redraw with `--fixes "..."` when the design drifted. Show the owner the paths and WAIT for
  approval unless told to skip it.

## 3. Grid and plan
- `$PY grid out/<Name> --side ref/ref_side.png --front ref/ref_front.png` -> `plan/side_grid.png`,
  `plan/front_grid.png`, dims. Read both grids.
- Write `out/<Name>/plan/plan_draft.json` (shape in CLAUDE.md): split the object the way a modeller would, one
  part per shape, percent boxes read off the grids, touching boxes overlapping 1-2 %, boxes covering the whole
  silhouette, real materials per part, zones for pads/lenses/bare metal. Every part `"method": "vendor"`.
- `$PY plan out/<Name> plan/plan_draft.json` -> prints the parts with their mm sizes, snapped heights and sampled
  colours. Check the sizes make sense (a barrel 13-25 mm across, a magazine 25-35 mm wide, a wheel round).

## 4. Part pictures (about $0.16 per part; vendor parts only)
For each part: `$PY part-pictures out/<Name> <Part>` -> `parts/<Part>/side.png` and `quarter.png`. The biggest
part's side picture is the approved side view with the other parts ERASED (nothing drawn); every other part is drawn
alone from the approved view. Read both pictures per part: the part must be whole, alone, same design and colours,
on white. Redraw with `--fixes "..."` when not.

## 4b. Code parts (free, seconds each)
Machined parts are `"method": "code"` in the plan. Write `out/<Name>/parts/<Part>/build.py` with
`def build(kit, L, W, H)` (part-local frame: +X forward, +Y left, +Z up, metres, centred on the box; the kit's calls
are in `mastersmith/blender/hskit.py`: box, cylinder, tube, profile, revolve, cut, union, hole, slot, array, rotate,
fillet). `$PY build out/<Name> <Part>` -> `parts/<Part>/<Part>.blend` and `<Part>_side/front/iso.png`. Read the
renders next to `side.png`; edit and build again until the shape and the orientation are right. No pictures needed
for a code part, but `part-pictures` for it is still worth the $0.16 when the shape is not obvious from the plan.
When the part picture and common sense disagree (a barrel drawn thick at the muzzle, an AR T-handle for a bullpup's
side slot, eleven fine teeth on a 68 mm rail), common sense and the reference picture win; say so in the review.
A code part's box is its TIGHT silhouette read off the grid: the assembler stretches a build to fill its box on
every axis (up to 2x), so a 41 mm box around a 13 mm trigger blade turned it into a wedge (2026-09-28). A vendor
seed with a hollow the picture invented (the Tripo receiver's open bolt trough) is filled by the code part that sits
on it: deepen that part's box and give the builder a keel (the bullpup's TopRail).

## 5a. Part review gate: every part passes before anything is assembled
Nothing goes to `assemble` until every part has been judged AGAINST ITS PICTURE, not as a shape alone (2026-09-28:
a magazine built as a straight box went unnoticed until the owner asked for the comparison). For each part, Read
its `side.png` (and `quarter.png`) beside its render (`<Part>_side.png` and `<Part>_iso.png` for a code part,
`seed_render.png` for a meshed one) and give the owner one table: part, pass/fail, what differs. Fail means fix at
that part's step (edit build.py, re-register, redraw and re-mesh) and review again. Tell the owner the table, with
the failures and their fixes, before step 6.

## 5. Mesh and register (free with TRELLIS)
`$PY mesh out/<Name> <Part>` for each part -> `seed.glb`, then registered to its side picture -> `registered.blend`,
`seed_render.png`, IoU printed. Read `seed_render.png` beside `side.png` and `quarter.png`: same object, same
orientation, forward end right, upright, nothing missing. If not: `$PY register out/<Name> <Part> --yaw 180` (or `--pitch -30`, degrees; `--from side` to skip the
sweeps) and look again. IoU under 0.5 usually means the quarter picture was a different object: redraw it.
Meshing with `--vendor hitem3d3` ($2.10) only when the owner asks; the kept pictures make that a one-liner later.

## 6. Assemble and review
`$PY assemble out/<Name>` (5-15 min) -> `delivery/SM_<Name>.glb`, previews, `preview_views.png`, `preview.html`.
`$PY preview out/<Name>` serves the page and opens it for the owner: a 3D viewer of the GLB, the six views, the
previews, the reference pictures and every part's pictures beside its registered seed. Give the owner that URL.
Read `preview_views.png` and every `preview_*.png` yourself. Check, in this order, and fix before anything else:
1. Function (common sense): barrel, muzzle, sights and receiver on one axis from the FRONT and the TOP; the bore
   is at the muzzle's centre; wheels on the ground; nothing floating, nothing poking through, no gaps at joins.
2. Proportions against the side picture: each part in its box, the magazine not fat, the barrel not thin.
3. Materials: steel is dark and reflective, rubber matt, glass glassy, polymer satin; colours match the picture.
4. Edges crisp, muzzle round, no blobs.
A wrong outline is fixed on the mesh: `$PY fit out/<Name> <Part>` sculpts the seed onto its side picture; a
local fault you can name, `$PY brush out/<Name> <Part> --op ...`; a part that is pure geometry (barrel, muzzle
device, rail, sight), `$PY sdf out/<Name> <Part>` from a `sdf.py` you write (AGENTS.md, "Sculpting without a
mouse"). Otherwise a wrong part is fixed at its own step: re-register (5), redraw and re-mesh (4-5), or a better box (3, then `plan`
and `assemble` again); `--parts A,B` assembles a subset while checking. Score it /10 for the owner, with the
defects named by view. Do not call it good under 7 without saying why.

## 7. Package
`$PY package out/<Name>` -> `delivery/<Name>.zip` with README and manifest. Tell the owner the path.

## Re-meshing kept parts later
The pictures in `parts/<Part>/` are the asset's source. `$PY mesh out/<Name> <Part> --vendor hitem3d3` (or a new
local model once wired into `mastersmith/local.py`) then `$PY assemble out/<Name>` rebuilds with the new mesh.
