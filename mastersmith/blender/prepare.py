"""Pass A (inside Blender): vendor GLB -> oriented, scaled, origin-set single mesh saved as work.blend,
plus probe renders from known cameras (probe.json) for the facing check and the segmentation masks.
    blender -b --python prepare.py -- <args.json>"""
import json
import math
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blib  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
WORK = args["work_dir"]
os.makedirs(WORK, exist_ok=True)
notes = []


def log(msg):
    print("[prepare] " + msg, flush=True)
    notes.append(msg)


bpy.ops.wm.read_factory_settings(use_empty=True)
src = os.path.abspath(args["glb"])
ext = os.path.splitext(src)[1].lower()
if ext == ".fbx":
    bpy.ops.import_scene.fbx(filepath=src)
elif ext == ".obj":
    bpy.ops.wm.obj_import(filepath=src)
elif ext == ".stl":
    bpy.ops.wm.stl_import(filepath=src)
else:
    bpy.ops.import_scene.gltf(filepath=src)
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
if not meshes:
    raise RuntimeError("the GLB contained no meshes")
# vendor atlases above 4K (Hi3D v3 ships 8192x8192) are 1 GB per float copy in the material pass; 4K is what ships
for img in bpy.data.images:
    if img.size[0] > 4096 or img.size[1] > 4096:
        w, h = img.size
        img.scale(min(w, 4096), min(h, 4096))
        img.pack()
        log("texture %s downscaled from %dx%d to %dx%d" % (img.name, w, h, img.size[0], img.size[1]))
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
# Some vendors export every face with its own vertices (Meshy v7: 867k vertices for 590k faces, 158k islands); the
# floaties pass then reads the whole mesh as fragments and deletes it. Merging coincident vertices (a hair's width)
# restores the connectivity without moving anything; UVs live on the loops and survive.
import bmesh as _bm
_lo, _hi = blib.dims(ob)
_bmw = _bm.new()
_bmw.from_mesh(ob.data)
_nv0 = len(_bmw.verts)
_bm.ops.remove_doubles(_bmw, verts=_bmw.verts, dist=max((_hi - _lo).length * 1e-6, 1e-7))
_nv1 = len(_bmw.verts)
if _nv1 < _nv0:
    _bmw.to_mesh(ob.data)
    ob.data.update()
    log("merged %d coincident vertices (%d -> %d)" % (_nv0 - _nv1, _nv0, _nv1))
_bmw.free()
ob.name = "SM_" + args["name"]
ob.data.name = ob.name
ob.rotation_mode = "XYZ"
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
raw_tris = blib.tri_count(ob)
log("imported %d triangles in %d part(s)" % (raw_tris, len(meshes)))


# --- orient: longest horizontal axis -> X (or keep Z up for characters), scale, origin
lo, hi = blib.dims(ob)
ext = hi - lo
if args["forward_axis"] == "long" and ext.y > ext.x * 1.15:
    blib.apply_yaw(ob, -90)
    log("rotated: long axis was Y, now X")
lo, hi = blib.dims(ob)
ext = hi - lo
if args["forward_axis"] == "long" and ext.z > max(ext.x, ext.y) * 1.15 and not args.get("keep_upright"):   # a stick stays a stick
    # a sword or rifle that arrived standing up (a Longsword, 2026-09-18) lies along X like every other weapon
    import math as _math
    ob.rotation_mode = "XYZ"
    ob.rotation_euler = (0.0, _math.radians(90.0), 0.0)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    log("rotated: long axis was Z (standing), now X")
lo, hi = blib.dims(ob)
ext = hi - lo
longest = max(ext.x, ext.y, ext.z) if args["forward_axis"] == "long" else ext.z
if longest > 1e-6 and float(args["size_m"]) > 0:       # size_m <= 0: keep the mesh's own size (a convert)
    s = float(args["size_m"]) / longest
    if abs(s - 1.0) > 0.01:
        ob.scale = (s, s, s)
        blib.select_only([ob])
        bpy.ops.object.transform_apply(scale=True)
        log("scaled x%.3f to %.2f m" % (s, args["size_m"]))
lo, hi = blib.dims(ob)
centre = (lo + hi) * 0.5
shift = centre.copy()
if args["origin"] == "bottom":
    shift.z = lo.z
ob.location -= shift
blib.select_only([ob])
bpy.ops.object.transform_apply(location=True)
bpy.context.scene.cursor.location = (0, 0, 0)
bpy.ops.object.origin_set(type="ORIGIN_CURSOR")

# --- probe renders: the four horizontal ends + an iso, cameras recorded for mask projection
blib.setup_render(int(args.get("probe_size", 640)), 16)
stage = blib.Stage(ob)
views = ["posx", "negx", "posy", "negy", "iso"]
records = [stage.render(v, os.path.join(WORK, "probe_%s.png" % v)) for v in views]
stage.close()
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(WORK, "work.blend"), compress=False)
lo, hi = blib.dims(ob)
json.dump({"views": records, "raw_triangles": raw_tris, "dimensions_m": [round(v, 4) for v in (hi - lo)],
           "notes": notes}, open(os.path.join(WORK, "probe.json"), "w"), indent=1)
log("done")
