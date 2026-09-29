# Assembly builds: plan the parts, build each one right, assemble

A single vendor mesh of a whole weapon comes out soft: rails, ports, sight blades and pins melt together, and the
finish then spends a stack of repair passes on it. An assembly build never makes the whole object in one go:

```
approved pictures ──► PLAN (builder LLM, vision)          one box per part, in metres, with a method and a material
                      │
                      ├─► CODE parts   builder LLM writes Blender geometry with the hard-surface kit
                      │                (boxes, tubes, extruded profiles, cuts, arrays), bevelled, checked
                      │                against its own render before it is accepted
                      │
                      └─► VENDOR parts the part is drawn alone from the reference, seeded on its own
                                       (Tripo, or Hi3D when the brief picks it), oriented, fitted to its box
                      ▼
                  ASSEMBLE (Blender)  parts placed in their boxes, vendor parts decimated to their share,
                                      one UV atlas baked to BC / N / ORM, LODs, collision, sockets, FBX / GLB
                      ▼
                  CHECK (builder LLM) the side view against the reference; box corrections re-assemble for free
```

## Why parts

- A barrel, a rail, a muzzle brake or a trigger guard is a few primitives. Built in code they have exact edges,
  real bevels and clean normals, at a few hundred triangles each.
- What code cannot model well goes to the vendor: the sculpted MAIN BODY (the moulded receiver and stock, a pistol
  frame with its grip, a vehicle's body shell) as one part, drawn WITHOUT the mechanical parts code builds. The
  vendor gets the body's shape and surface right; code gets the thin, sharp and repeated parts right. The picture
  prompt names the code parts inside the body's box to leave out, so nothing is modelled twice.
- Every part is checked before it joins the rest. Nothing is repaired afterwards, on this path or any other: the
  post-op repairs (cylinder repair, tone pull, de-light, recolour under masks, part removal, added parts) were
  removed on 2026-09-26. A wrong part is rebuilt, not patched.

## The frame

Asset frame: +X forward (the muzzle), +Y left, +Z up, metres, origin at the centre of the whole object's box.
The plan reads part positions off the side and front pictures, drawn with a labelled percentage grid, and converts
them with the object's own silhouette: the brief's size gives the length, the side picture's aspect the height, the
front picture's aspect the width.

A code part is built in its own frame, centred on its box: `x` in `[-L/2, L/2]`, `y` in `[-W/2, W/2]`,
`z` in `[-H/2, H/2]`.

## The hard-surface kit

The builder LLM writes one function, `build(kit, L, W, H)`, and returns the pieces. It may use only `kit`, `math`
and a few builtins; the code is checked by an allowlist before Blender runs it.

| Call | Makes |
|---|---|
| `kit.box(center, size, bevel=None)` | a bevelled box |
| `kit.cylinder(center, radius, length, axis="X", sides=32, radius2=None, bevel=None)` | a cylinder or cone |
| `kit.tube(center, r_outer, r_inner, length, axis="X", sides=32)` | a hollow tube |
| `kit.profile(points, width, offset=0.0, plane="XZ", bevel=None)` | a 2D outline extruded: `XZ` side outline extruded across Y, `XY` top outline up Z, `YZ` front outline along X |
| `kit.cut(target, *cutters)` | boolean difference; the cutters are consumed |
| `kit.hole(target, center, radius, depth, axis="Y")` / `kit.slot(target, center, size)` | common cuts |
| `kit.array(obj, count, offset)` | repeats a piece (rail teeth, ports, grooves) |
| `kit.mirror(obj, axis="Y")` | a mirrored copy joined in |
| `kit.move(obj, offset)` / `kit.rotate(obj, degrees, axis)` | placement |
| `kit.revolve(points, center, axis="X", sides=48)` | a lathe: (a, r) outline spun round the axis (stepped barrels, muzzle devices, knobs, rims, domes) |
| `kit.loft(sections, axis="X", samples=48)` | a solid skinned through differing cross-sections (a receiver that changes section, a nose, a fuselage) |
| `kit.sweep(points, radius, sides=16)` | a round rod along a 3D path (handles, loops, bent pipes) |
| `kit.fillet(obj, radius, segments=4, region=None)` | real rounded edges, optionally only inside a region box |
| `kit.smooth(obj, levels=2, crease_angle=None)` | subdivision of a blocky cage into moulded curves; sharper edges stay creased |
| `kit.bend(obj, degrees, along, toward, fixed=None)` | curves a piece (a banana magazine) |
| `kit.taper(obj, scale, along, keep="min")` | narrows the cross-section along an axis |
| `kit.shell(obj, thickness)` | hollows a piece inwards |

`bevel=None` picks a width from the piece's smallest side (6%, 0.2 to 1.5 mm), 0 turns it off. The builder is told to
pick the call by the shape (round: revolve; changing section: loft; moulded: smoothed cage or fillet) rather than
stacking boxes, and to list every feature it sees, with its position in mm, before it writes code.

## Choosing

`build_mode` on the brief: `"assembly"` builds a hard surface as parts; unset or `"single"` builds one seed. Assembly
is opt-in until it beats one seed on the same object (`MASTERSMITH_ASSEMBLY_DEFAULT=1` makes it the hard-surface
default again). Big parts (a housing, a stock) are written by `MASTERSMITH_BUILDER_MODEL` (default Claude Opus 5.5 on
a coding-agent CLI), small ones (pins, levers, sights) by the cheaper `MASTERSMITH_BUILDER_MODEL_SMALL` (Claude Sonnet 5):
the builder calls were 97% of an assembly's cost. The vendor for parts follows the brief's `seed_vendor`: Hi3D v3
when it names Hi3D, else Tripo.

## What makes it hold together (learnt on the first test objects, 2026-09-27)

Each of these came from a live build that went wrong; the first shippable assembly (a military utility truck, 5/10
"ship with notes") came after them.

- **Which end is the front is asked, not assumed.** A vision call says whether the side picture has the front on the
  left; the picture is mirrored when it does (a pistol came back muzzle-left).
- **The side picture alone is enough.** When the front view is refused (long guns often fail it), the builder plans
  from the side and estimates the widths.
- **One moulding stays one part.** A pistol frame keeps its grip and guard, a stock keeps its pistol grip: a cut
  through one moulding becomes a gap.
- **Every part knows its joints.** The service works out from the boxes which neighbour meets which face of a part,
  the builder is told that face must reach the box edge, and touching boxes overlap by 1-2%.
- **The builder checks itself against the same framing.** Each code part is rendered orthographically in clay,
  framed on its own box, beside the reference cropped to that box with a millimetre scale; up to two corrections.
  The check lists every feature the reference shows and marks each FOUND or MISSING; only a list with nothing
  missing ends in `VERDICT: OK`.
- **Code parts carry the reference's fine detail.** A high-pass of the plan's side and front pictures (inside the
  silhouette only) is projected onto code parts by position and normal before the bake: it shades the base colour and
  drives a bump, so the colour and normal maps both carry the panel lines, screws and ribs the picture shows.
- **Vendor parts are registered, not guessed.** The part is drawn alone in the reference's side view, seeded, and
  turned (four upright turns, all 24 as a fallback) until its side silhouette best matches the picture; it then
  fills its box on every side.
- **Code parts are handed over as .blend** (glTF drops the procedural material) and a cut that erases most of a part
  is refused (exact booleans with self-intersection on).
- **The bake is exact on any material.** Base colour and metalness are baked through an emission (the diffuse pass
  is zero on metal); every part keeps its own unwrap in an atlas tile sized by its surface area.
- **The triangle budget holds.** Code parts over their allowance are dissolved on flat areas, then collapsed to their
  share by surface area.

Test changes to the assembler for free: run `mastersmith/blender/assemble.py` on a finished job's
`work/assemble_final_args.json` with another `out_dir`; it rebuilds the delivery from the saved parts.
