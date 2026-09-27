# Generated textures on large organic surfaces

Distinguish UV-atlas boundaries, generated-image border wrapping, split shading
normals, invalid material sections, and geometry gaps before selecting a repair.
A material being assigned to a component does not establish that every rendered
LOD section uses it, nor that its appearance is accepted.

## Shared-vertex normal repair

For an explicitly selected continuous organic mesh, add
`"smooth_organic_normals"` to `Spec.texture_fixes`. The finish smooths the source
and LOD0 before tangent-normal baking, then the lower LODs before export. It
records `normal_repairs` in the delivery report. The reusable Blender helper is
`mastersmith/blender/surface_normals.py`; it also supports an existing-mesh route
that preserves level transforms and collision outside the generic prop finish.

The diagnostic counts differing corner normals at shared vertices. Intentional
creases also produce differences: do not infer damage from that count alone.
The repair removes hard edges on the selected mesh, without changing positions,
UVs, topology or face winding. Do not opt in for mechanical parts, mixed assemblies,
or surfaces with intentional creases. Separate those surfaces first. It does not
fix gaps, coincident disconnected vertices, overlapping shells or inverted faces.
Existing baked tangent-normal maps may require rebaking against the changed basis.

## Texture density and projection

`templates/unreal/organic_surface.hlsl` is a reusable Custom-expression template
from the accepted organic-surface setup. It is supplied as source, not automatically
installed in a delivery or equivalent to a GLB material. The caller wires:

| Input/output | Meaning |
|---|---|
| `P`, `N`, `Side` | Mesh-local position in cm, mesh-local geometry normal, TwoSidedSign |
| `DetailColor`, `DetailNormal` | Texture objects: sRGB RGB color + linear alpha roughness; linear encoded normal |
| `DetailMean` | Linear RGB mean of the color image, computed before the alpha packing |
| `DetailSize`, `DetailStrength`, `NormalStrength` | Physical density and artist-controlled color/relief strength |
| Main output, `LocalNormal`, `Roughness` | Base color; additional float3 local normal; additional float roughness |

Transform the output normal back to world space and normalize it; disable tangent
space normals on the parent. Set two-sided shading when the surface requires it.
The template performs raw RG normal decoding in each projection plane. It does not
use a UV atlas, supply displacement, or preserve atlas-specific anatomical regions.
Choose strength and density for the asset rather than copying one organ's settings.

Native level size does not add pixels to an atlas. Use generated PBR detail at an
explicit physical scale for close traversal. Mesh-local coordinates follow rigid
motion and uniform breathing scale; world projection can swim. Nonuniform or
skeletal deformation requires a suitable coordinate/normal treatment of its own.

Keep albedo, roughness and normals in matching coordinates. A UV-atlas normal
must not drive projection weights for a supposedly UV-independent detail layer.
Avoid front/back color switches that create patches across mixed winding. Smooth
triplanar blends do not repair hard vertex normals.

Randomized sampling alone does not make a generated image periodic. Use truly
tileable images or inset overlapping patches whose entire rotated footprint stays
inside the image. For example, support `d in [-1,1]^2` with
`uv = 0.5 + offset + rotate(d) * 0.28`, `offset in [-0.04,0.04]^2`, stays within
approximately `[0.064,0.936]`. Derivatives must use the same rotation/scale and
patch weights must go continuously to zero at the support boundary. Account for
the reduced image footprint when choosing density. Blending can blur detail;
profile texture samples when performance testing is authorized.

Color uses sRGB; normal/roughness/metallic use linear data. An RGB albedo + alpha
roughness texture can use sRGB because the alpha remains linear. Preserve alpha.
For conventional UE tangent normals, OpenGL +Y maps generally need green inversion;
DirectX -Y maps generally do not. Custom projection decoding needs its own basis
convention, not a blanket green flip. Document height as supplied versus actually used.

## Unreal handoff

Snapshot material slots, section-to-slot mappings for every LOD, collision,
screen sizes and actor transforms before reimport. ImportLOD can append slots.
Restoring only `static_materials` can leave sections referencing removed indices,
producing a gray/default surface while component slot 0 still reports the right MI.
Use `StaticMeshEditorSubsystem.get_lod_material_slot` / `set_lod_material_slot`.
For a deliberately single-material mesh, explicitly map all sections to slot 0;
for multiple materials, resolve stable slot names and stop on ambiguous mappings.
Do not use section 0 as a synonym for material slot 0. These editor tools require
Play mode to be stopped. Preserve separate passage collision and its trace mode.

Keep the user's chosen level lighting. Inspect under neutral asset-preview
lighting when needed, rather than changing a red body interior to white as a
texture fix. Preserve approved assets while repairing another asset.

State which appearance the delivery contains. A GLB showing Meshy atlas PBR does
not reproduce an Unreal-only Patina projection shader. Retain provider/job
provenance and label existing-mesh exports accurately. Never label a saved export
as visually accepted: only claim the checks or user acceptance actually obtained.
Respect project limits on tests, screenshots, PIE and paid generation.
