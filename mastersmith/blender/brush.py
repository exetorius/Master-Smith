"""Headless brush strokes on a registered part (issue #13).
    blender -b -Y --python brush.py -- <args.json>
args: {"blend", "strokes": [{"op", "at", "radius", "strength", ...}], "out_blend", "out_render", "out_json"}
The strokes are mastersmith.sculpt.stroke() records in the part's own frame (metres, the part centred at the origin
as register_part.py left it). The mesh is edited in place and saved; the render shows the result."""
import json
import os
import sys

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import blib  # noqa: E402
from mastersmith import sculpt  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
bpy.ops.wm.open_mainfile(filepath=os.path.abspath(args["blend"]))
ob = next(o for o in bpy.data.objects if o.type == "MESH")
me = ob.data
me.calc_loop_triangles()
verts = np.empty(len(me.vertices) * 3, np.float32)
me.vertices.foreach_get("co", verts)
verts = verts.reshape(-1, 3).astype(np.float64)
faces = np.empty(len(me.loop_triangles) * 3, np.int32)
me.loop_triangles.foreach_get("vertices", faces)
faces = faces.reshape(-1, 3).astype(np.int64)
# the seed is in the mesher's units and only reaches its box's metres at assembly: strokes are given in metres of
# the finished part, so scale them into the mesh (per axis for positions, the mean for radii and strengths)
ext = verts.max(axis=0) - verts.min(axis=0)
box = np.asarray(args.get("box_size") or ext, dtype=np.float64)
sc = ext / np.maximum(box, 1e-9)
k = float(sc.mean())
centre = (verts.max(axis=0) + verts.min(axis=0)) / 2
before = verts.copy()
for op in args["strokes"]:
    op = dict(op)
    op["at"] = list(centre + np.asarray(op.get("at", (0, 0, 0))) * sc)
    if "to" in op:
        op["to"] = list(centre + np.asarray(op["to"]) * sc)
    if "delta" in op:
        op["delta"] = list(np.asarray(op["delta"]) * sc)
    op["radius"] = float(op.get("radius", 0.01)) * k
    if op["op"] == "inflate":
        op["strength"] = float(op.get("strength", 0.0)) * k
    verts = sculpt.stroke(verts, faces, op)
moved = np.linalg.norm(verts - before, axis=1) / k
me.vertices.foreach_set("co", verts.astype(np.float32).ravel())
me.update()
bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args["out_blend"]), compress=True)
if args.get("out_render"):
    blib.setup_render(512, 24, look="preview")
    st = blib.Stage(ob, look="preview")
    st.render("iso", os.path.abspath(args["out_render"]))
    st.close()
json.dump({"strokes": len(args["strokes"]), "vertices_moved": int((moved > 1e-6).sum()), "vertices": len(verts),
           "max_move_mm": round(float(moved.max()) * 1000, 3)}, open(args["out_json"], "w"), indent=1)
print("[brush] %d strokes moved %d of %d vertices, up to %.2f mm" % (len(args["strokes"]), (moved > 1e-6).sum(), len(verts), moved.max() * 1000), flush=True)
