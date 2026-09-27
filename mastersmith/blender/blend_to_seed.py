"""Inside Blender: open a delivered SM_<Name>.blend and export its LOD0 (textures packed) as a GLB that
can seed a re-finish, plus the brief that was stored in the file. -- <blend> <out_glb> <out_json>"""
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
blend, out_glb, out_json = [os.path.abspath(a) for a in argv]
bpy.ops.wm.open_mainfile(filepath=blend)
lod0 = next((o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith("SM_") and "LOD" not in o.name and not o.name.startswith("UCX")), None)
if lod0 is None:
    lod0 = next(o for o in bpy.data.objects if o.type == "MESH" and not o.name.startswith("UCX"))
bpy.ops.object.select_all(action="DESELECT")
lod0.select_set(True)
bpy.context.view_layer.objects.active = lod0
# Only the body seeds the re-finish. The glass slot and the built panes are the finish pass's own additions (older
# deliveries also carry fitted interiors and parts) and are rebuilt by the finish; left in, their materials fought over
# the body's texture files (Havoc, 2026-09-18).
me = lod0.data
own = [i for i, sl in enumerate(lod0.material_slots) if i > 0 and sl.material and sl.material.name.startswith("MI_")]
if own:
    import numpy as np
    idx = np.empty(len(me.polygons), np.int32)
    me.polygons.foreach_get("material_index", idx)
    drop = np.isin(idx, np.array(own, np.int32))
    if drop.any() and (~drop).sum() > 100:
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.nonzero(drop)[0]], context="FACES")
        bm.to_mesh(me)
        bm.free()
        names = [lod0.material_slots[i].material.name for i in own]
        for i in sorted(own, reverse=True):
            lod0.active_material_index = i
            bpy.ops.object.material_slot_remove()
        print("[blend_to_seed] dropped %d faces of the finish pass's own parts (%s); the finish rebuilds them" % (int(drop.sum()), ", ".join(names)))
for img in bpy.data.images:
    if img.size[0] and not img.packed_file:
        try:
            img.pack()
        except RuntimeError:
            pass
bpy.ops.export_scene.gltf(filepath=out_glb, use_selection=True, export_format="GLB", export_yup=True)
spec = None
txt = bpy.data.texts.get("ms_spec.json") or bpy.data.texts.get("anvil_spec.json")
if txt:
    try:
        spec = json.loads(txt.as_string())
    except ValueError:
        spec = None
ref_path = None
ref = bpy.data.images.get("ms_reference") or bpy.data.images.get("anvil_reference")
if ref is not None and ref.size[0]:
    ref_path = os.path.join(os.path.dirname(out_glb), "previous_reference.png")
    ref.filepath_raw = ref_path
    ref.file_format = "PNG"
    ref.save()
json.dump({"spec": spec, "reference": ref_path, "triangles": sum(len(p.vertices) - 2 for p in lod0.data.polygons)}, open(out_json, "w"))
print("[blend_to_seed] exported", out_glb)
