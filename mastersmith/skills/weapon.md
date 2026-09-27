---
reference_view: perfect left-side profile, camera level with the weapon, muzzle pointing right
second_view: seen from the muzzle end looking straight down the barrel, camera far ahead of the muzzle and exactly level with it, strictly orthographic with no perspective and no foreshortening, the barrel a straight line pointing at the camera, the muzzle centred
mirror_as_third_view: true
forward_axis: long
origin: center
default_tris: 60000
default_size_m: 1.0
front_hint: the muzzle end of the barrel
glass_prompt: scope lens glass
---
# Weapons (rifles, pistols, launchers, melee)

The side profile carries almost all of a weapon's identity, so the reference picture is a clean product
shot: one weapon, full length from muzzle to stock, plain white background, no hands, no sling, no
background clutter, no perspective foreshortening. Name the real materials in the caption: anodised
aluminium, carbon fibre, blued steel, polymer, walnut. Say which parts are black and which are coloured.

Seed with detailed geometry and HD textures: standard geometry melts scopes, bipods and iron sights
into soft plastic. A second view down the barrel and a mirrored profile give the vendor the cross
section it cannot guess from one side. When the customer supplied a photo and the muzzle view was
approved, `hitem3d3mv` (Hi3D v3 multi-view) is the pick: the crispest hard-surface geometry, every
approved angle in its named slots, the same price as its single-picture sibling.

Describe colours from the photo, part by part: a magazine or grip that shows the same grey as the body
IS grey, never "black steel" by habit. The reviewer holds the model to the caption.

Finishing: the long axis becomes +X (forward), origin at the centre of the body, real length in metres
(a rifle 1.0 to 1.3 m, a pistol 0.2 m, a rocket launcher 1.0 m). Collision is a convex hull; a weapon
never needs quads or a skeleton unless the customer asks for a rigged reload.

Realism check: the scope glass should read as glass, metal parts should have a low-to-mid roughness
with variation, and the silhouette must match the picture - a wrong silhouette means a new picture,
not a Blender repair.
