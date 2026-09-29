"""A part authored in the asset frame (an SDF mesh, issue #12) -> registered.blend like a registered seed.
    blender -b -Y --python import_part.py -- <args.json>
args: {"glb", "material": {...plan material...}, "name", "out_blend", "out_render", "out_json"}
No registration: the GLB is already in the asset frame (X forward, Z up) and centred. It gets the planned material
(the same procedural one code parts got) and sharp edges by angle, so the assembler treats it like any vendor part."""
import json
import math
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blib  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=os.path.abspath(args["glb"]))
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
for o in meshes:
    mw = o.matrix_world.copy()
    o.parent = None
    o.matrix_world = mw
for o in [o for o in bpy.data.objects if o.type != "MESH"]:
    bpy.data.objects.remove(o, do_unlink=True)
blib.select_only(meshes)
if len(meshes) > 1:
    bpy.ops.object.join()
ob = bpy.context.view_layer.objects.active
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
lo, hi = blib.dims(ob)
ob.data.transform(__import__("mathutils").Matrix.Translation(-(lo + hi) * 0.5))
ob.name = "Part"
ob.data.materials.clear()
ob.data.materials.append(blib.plan_material(args.get("name", "part"), args.get("material")))
# marching cubes tiles flat faces with tiny triangles: dissolve the planar ones (2 degrees keeps every real edge)
blib.select_only([ob])
mod = ob.modifiers.new("planar", "DECIMATE")
mod.decimate_type = "DISSOLVE"
mod.angle_limit = math.radians(2.0)
bpy.ops.object.modifier_apply(modifier="planar")
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.quads_convert_to_tris()
bpy.ops.object.mode_set(mode="OBJECT")
# crease what is really an edge
bpy.ops.object.shade_smooth_by_angle(angle=math.radians(30))
bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args["out_blend"]), compress=True)
if args.get("out_render"):
    blib.setup_render(512, 24, look="preview")
    st = blib.Stage(ob, look="preview")
    st.render("iso", os.path.abspath(args["out_render"]))
    st.close()
lo, hi = blib.dims(ob)
json.dump({"triangles": blib.tri_count(ob), "size_m": [round(float(v), 5) for v in (hi - lo)]}, open(args["out_json"], "w"), indent=1)
print("[import_part] %d tris, %s mm" % (blib.tri_count(ob), [round(float(v) * 1000, 1) for v in (hi - lo)]), flush=True)
