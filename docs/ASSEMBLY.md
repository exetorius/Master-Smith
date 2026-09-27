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
- What code cannot model well (a moulded grip, a sculpted stock) goes to the vendor ALONE. A part on its own is a
  simple shape the vendor gets right; the whole gun at once is where it melts.
- Every part is checked before it joins the rest. Nothing is repaired afterwards: no cylinder repair, no tone
  pull, no de-light, no recolour under masks, no part removal. A wrong part is rebuilt, not patched.

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

`bevel=None` picks a width from the piece's smallest side (6%, 0.2 to 1.5 mm), 0 turns it off.

## Choosing

`build_mode` on the brief: `None` builds hard-surface categories (weapon, vehicle, aircraft, helicopter) as an
assembly when the approved pictures include a side and a front view, and everything else as one seed. `"assembly"`
or `"single"` forces a path. The builder LLM is `MASTERSMITH_BUILDER_MODEL` (default Claude Opus 5.5 on
OpenRouter). The vendor for parts follows the brief's `seed_vendor`: Hi3D v3 when it names Hi3D, else Tripo.
