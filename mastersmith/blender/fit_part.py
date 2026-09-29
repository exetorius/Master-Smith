"""Fit a registered part's outline to its pictures (issue #11).
    blender -b -Y --python fit_part.py -- <args.json>
args: {"blend", "views": [{"name", "npz", "yaw", "pitch", "weight"}], "iters", "step", "out_blend", "out_render", "out_json"}
Each view's .npz holds the picture mask's signed distance field (sdf, gx, gy; pixels, + outside), computed in the
venv with scipy. The mesh is deformed with mastersmith.sculpt.fit_silhouette and saved; the silhouette overlap with
every view before and after is reported (measured on a decimated copy, like register_part.py)."""
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


def arrays(o):
    me = o.data
    me.calc_loop_triangles()
    v = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", v)
    f = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get("vertices", f)
    return v.reshape(-1, 3).astype(np.float64), f.reshape(-1, 3).astype(np.int64)


def probe_arrays(o, faces_max=4000):
    """A decimated copy's arrays, for the silhouette overlap measure."""
    probe = o.copy()
    probe.data = o.data.copy()
    bpy.context.collection.objects.link(probe)
    tris = blib.tri_count(probe)
    if tris > faces_max:
        m = probe.modifiers.new("dec", "DECIMATE")
        m.ratio = faces_max / float(tris)
        blib.select_only([probe])
        bpy.ops.object.modifier_apply(modifier="dec")
    out = arrays(probe)
    bpy.data.objects.remove(probe, do_unlink=True)
    return out


verts, faces = arrays(ob)
views = []
for v in args["views"]:
    z = np.load(v["npz"])
    view = sculpt.make_view(verts, v["yaw"], v["pitch"], None, weight=v.get("weight", 1.0), sdf=(z["sdf"], z["gx"], z["gy"]))
    view["name"] = v["name"]
    views.append(view)
pv, pf = probe_arrays(ob)
before = {v["name"]: sculpt.silhouette_iou(pv, pf, v) for v in views}
fitted = sculpt.fit_silhouette(verts, faces, views, iters=int(args.get("iters", 8)), step=float(args.get("step", 0.6)))
ext = verts.max(axis=0) - verts.min(axis=0)
k = float((ext / np.maximum(np.asarray(args.get("box_size") or ext, dtype=np.float64), 1e-9)).mean())   # mesh units per metre
moved = np.linalg.norm(fitted - verts, axis=1) / k
ob.data.vertices.foreach_set("co", fitted.astype(np.float32).ravel())
ob.data.update()
pv, pf = probe_arrays(ob)
after = {v["name"]: sculpt.silhouette_iou(pv, pf, v) for v in views}
bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args["out_blend"]), compress=True)
if args.get("out_render"):
    blib.setup_render(512, 24, look="preview")
    st = blib.Stage(ob, look="preview")
    st.render("iso", os.path.abspath(args["out_render"]))
    st.close()
res = {"iou_before": {k: round(x, 3) for k, x in before.items()}, "iou_after": {k: round(x, 3) for k, x in after.items()},
       "vertices": len(verts), "max_move_mm": round(float(moved.max()) * 1000, 2), "mean_move_mm": round(float(moved.mean()) * 1000, 3)}
json.dump(res, open(args["out_json"], "w"), indent=1)
print("[fit] " + ", ".join("%s %.3f -> %.3f" % (k, before[k], after[k]) for k in before) + "; moved up to %.1f mm" % (moved.max() * 1000), flush=True)
