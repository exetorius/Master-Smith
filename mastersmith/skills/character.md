---
reference_view: full body, facing the camera, standing fully upright (never hunched or crouched) in a relaxed A-pose with arms slightly away from the body, feet apart
second_view: seen directly from behind
mirror_as_third_view: false
forward_axis: up
origin: bottom
default_tris: 80000
default_size_m: 1.8
front_hint: the face and chest
---
# Characters and creatures

One character, whole body in frame including the feet, plain white background, neutral A-pose, no
weapon held across the body, no cape hiding the silhouette, hands open. Always draw the figure standing
upright in that A-pose even when the brief says hunched, crouching, shambling or limping: the auto-rigger
only reads upright humanoids, and the pose belongs to the animation, not the mesh. Animals and other
non-humanoids cannot be auto-rigged; say so in the notes and ship them static. Describe the outfit layer by
layer and name the materials (leather, steel plate, denim, fur). Give the character's height in metres.

Seed from the front view; a back view is only worth paying for when the outfit is very different behind
(a backpack, wings, a cape). The vendor mesh comes back as one fused shell with no skeleton - this
pipeline delivers a static mesh ready to be rigged in the engine, with the origin between the feet.

Finishing: the height becomes +Z (up), the character faces -Y in Blender which exports as facing the
engine's forward, origin at the floor between the feet, real height in metres. Collision is a capsule-like
convex hull. Realism check: skin and cloth must not look glossy, eyes should be visible, hands should
have fingers, feet should be flat on the floor.
