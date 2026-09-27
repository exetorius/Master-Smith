---
reference_view: three-quarter front-left view from slightly above, the whole vehicle visible, wheels or tracks on the ground
second_view: the direct left-side profile
mirror_as_third_view: false
forward_axis: long
origin: bottom
default_tris: 120000
default_size_m: 5.0
front_hint: the nose with the headlights, grille, windscreen or cockpit
glass_prompt: the windshield and the side windows of the vehicle
rig_parts_prompt: wheel
---
# Vehicles (cars, trucks, tanks, aircraft, helicopters, boats)

A three-quarter view tells the vendor about the front, the side and the roof at once; a pure profile
loses the front. Ask for one vehicle, plain white background, no people, no scenery, wheels or tracks
resting on the ground, rotors and wings fully visible. Name the livery and the finish: matte olive drab,
gloss red paint, bare riveted aluminium, rubber tyres, glass canopy.

Real machines have real proportions: name the type (M1 Abrams, AH-64, F-16, Ford F-150) so the picture
model draws it from memory instead of inventing one. For fantasy or sci-fi vehicles describe the
silhouette in one sentence before the details.

Finishing: long axis becomes +X (forward), origin at the bottom centre so it sits on the ground, real
length in metres (a car 4.5, a main battle tank 8 to 10 including the gun, an attack helicopter 15 to
18, a fighter jet 15). Wheels, tracks and rotors stay part of the single static mesh unless the customer
asks for animated parts. Collision is a convex hull; large vehicles get a coarser hull.

Realism check: windows should read as glass, paint should not look like ceramic (roughness above 0.35
on painted metal), tyres should be dark and matte, and the vehicle must be one vehicle, not two fused.
