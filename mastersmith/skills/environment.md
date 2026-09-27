---
reference_view: three-quarter view from slightly above, the whole structure visible, on flat ground
second_view: the direct front elevation
mirror_as_third_view: false
forward_axis: long
origin: bottom
default_tris: 80000
default_size_m: 4.0
front_hint: the main entrance or facade
glass_prompt: window glass
---
# Environment pieces (buildings, walls, bunkers, ruins, rocks, kit pieces)

For traversable organic interiors, keep authored scale, pivots and passage collision
when the task is surface repair. Read `docs/ORGANIC_SURFACES.md` for texture density,
normal splits and the Unreal LOD-section material mapping pitfall. The existing
generic prop finish recentres/scales and generates convex collision; use a scoped
existing-mesh workflow when those operations would break level placement or passages.
`texture_fixes: ["smooth_organic_normals"]` is an opt-in shading repair for a
continuous organic mesh, not a default for buildings or mixed assemblies.

One structure, plain white background, three-quarter view so the roof and two facades show. Name the
construction materials (sandbags, concrete, corrugated steel, stone, timber) and the footprint in
metres. A single building or ruin seeds well; a whole street does not - split it into pieces and build
each as its own asset.

Finishing: origin at the bottom centre on the ground plane, longest horizontal axis along +X, real
footprint in metres. Collision is a convex hull, which is coarse for hollow buildings - the customer
should use complex-as-simple collision in the engine if they need to walk inside. Realism check: stone
and concrete must be matte, windows should read as openings or glass, and the structure must sit flat.
