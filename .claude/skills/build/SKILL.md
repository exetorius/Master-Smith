---
name: build
description: Build a game-ready hard-surface asset (weapon, vehicle, aircraft, prop) as an assembly of parts from this Claude Code session with the ms tools - reference pictures, gridded plan, per-part pictures, TRELLIS meshes, registration, assembly, six-view review, package. Use when the owner asks to build, rebuild, fix or re-mesh a model.
---

# Build an asset with `ms`

`PY=.venv/Scripts/python.exe -m mastersmith.ms`. Everything below is run from the repo root. Read CLAUDE.md's rules
first; read `mastersmith/skills/<category>.md` for the category. Say the cost of a step before spending.

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

## 4. Part pictures (about $0.16 per part)
For each part: `$PY part-pictures out/<Name> <Part>` -> `parts/<Part>/side.png` and `quarter.png`. The biggest
part's side picture is the approved side view with the other parts ERASED (nothing drawn); every other part is drawn
alone from the approved view. Read both pictures per part: the part must be whole, alone, same design and colours,
on white. Redraw with `--fixes "..."` when not.

## 5. Mesh and register (free with TRELLIS)
`$PY mesh out/<Name> <Part>` for each part -> `seed.glb`, then registered to its side picture -> `registered.blend`,
`seed_render.png`, IoU printed. Read `seed_render.png` beside `side.png`: same orientation, forward end right,
upright. If not: `$PY register out/<Name> <Part> --yaw 180` (or `--pitch -30`, degrees; `--from side` to skip the
sweeps) and look again. IoU under 0.5 usually means the quarter picture was a different object: redraw it.
Meshing with `--vendor hitem3d3` ($2.10) only when the owner asks; the kept pictures make that a one-liner later.

## 6. Assemble and review
`$PY assemble out/<Name>` (5-15 min) -> `delivery/SM_<Name>.glb`, previews, `preview_views.png`.
Read `preview_views.png` and every `preview_*.png`. Check, in this order, and fix before anything else:
1. Function (common sense): barrel, muzzle, sights and receiver on one axis from the FRONT and the TOP; the bore
   is at the muzzle's centre; wheels on the ground; nothing floating, nothing poking through, no gaps at joins.
2. Proportions against the side picture: each part in its box, the magazine not fat, the barrel not thin.
3. Materials: steel is dark and reflective, rubber matt, glass glassy, polymer satin; colours match the picture.
4. Edges crisp, muzzle round, no blobs.
A wrong part is fixed at its own step: re-register (5), redraw and re-mesh (4-5), or a better box (3, then `plan`
and `assemble` again); `--parts A,B` assembles a subset while checking. Score it /10 for the owner, with the
defects named by view. Do not call it good under 7 without saying why.

## 7. Package
`$PY package out/<Name>` -> `delivery/<Name>.zip` with README and manifest. Tell the owner the path.

## Re-meshing kept parts later
The pictures in `parts/<Part>/` are the asset's source. `$PY mesh out/<Name> <Part> --vendor hitem3d3` (or a new
local model once wired into `mastersmith/local.py`) then `$PY assemble out/<Name>` rebuilds with the new mesh.
