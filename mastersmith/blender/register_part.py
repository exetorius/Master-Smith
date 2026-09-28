"""Turn a vendor seed so its side silhouette matches the part's side picture (inside Blender).
    blender -b -Y --python register_part.py -- <args.json>
args: {"glb", "mask", "out_blend", "out_json"}; mask is a PNG whose non-black pixels are the part as seen from the side
(forward to the right, up is up), cropped to the part.

Image-to-3D vendors put a part in any orientation, and a four-way facing question left the pistol's grip tilted and
bent against its frame (2026-09-27). The four upright turns are tried first (the vendor keeps the picture's up), all 24
axis-aligned orientations only when none matches: each one's silhouette seen from the side (-Y, forward = +X to the
right, +Z up) is rasterised, normalised to its own box, and compared with the picture's by overlap (IoU), with a
penalty for a different aspect. The best one is applied, centred at the origin."""
import itertools
import json
import os
import sys

import bpy
import numpy as np
from mathutils import Matrix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blib  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
RES = 96


def rotations():
    """The 24 rotations of the cube: signed axis permutations with determinant +1."""
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            m = np.zeros((3, 3))
            for r, (c, s) in enumerate(zip(perm, signs)):
                m[r, c] = s
            if round(np.linalg.det(m)) == 1:
                out.append(m)
    return out


def raster(tris2d):
    """Fill 2D triangles (n, 3, 2) already in [0, RES) into a RES x RES boolean mask (row 0 = top)."""
    mask = np.zeros((RES, RES), bool)
    ys, xs = np.mgrid[0:RES, 0:RES]
    px, py = xs + 0.5, ys + 0.5
    for t in tris2d:
        (x0, y0), (x1, y1), (x2, y2) = t
        minx, maxx = int(max(0, np.floor(min(x0, x1, x2)))), int(min(RES - 1, np.ceil(max(x0, x1, x2))))
        miny, maxy = int(max(0, np.floor(min(y0, y1, y2)))), int(min(RES - 1, np.ceil(max(y0, y1, y2))))
        if maxx < minx or maxy < miny:
            continue
        sx, sy = px[miny:maxy + 1, minx:maxx + 1], py[miny:maxy + 1, minx:maxx + 1]
        d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(d) < 1e-12:
            continue
        a = ((y1 - y2) * (sx - x2) + (x2 - x1) * (sy - y2)) / d
        b = ((y2 - y0) * (sx - x2) + (x0 - x2) * (sy - y2)) / d
        inside = (a >= -1e-6) & (b >= -1e-6) & (a + b <= 1 + 1e-6)
        mask[miny:maxy + 1, minx:maxx + 1] |= inside
    return mask


def side_mask(verts, faces):
    """Silhouette from the side: x across (forward to the right), z up; normalised to its own bounding box."""
    x, z = verts[:, 0], verts[:, 2]
    lo_x, hi_x, lo_z, hi_z = x.min(), x.max(), z.min(), z.max()
    w, h = max(hi_x - lo_x, 1e-9), max(hi_z - lo_z, 1e-9)
    u = (x - lo_x) / w * (RES - 1e-3)
    v = (hi_z - z) / h * (RES - 1e-3)
    tris = np.stack([np.stack([u[faces[:, k]], v[faces[:, k]]], axis=1) for k in range(3)], axis=1)
    return raster(tris), w / h


def picture_mask(path):
    img = bpy.data.images.load(os.path.abspath(path))
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    a = a.reshape(h, w, 4)[::-1]                      # Blender stores rows bottom-up
    m = a[:, :, :3].max(axis=2) > 0.5
    ys, xs = np.nonzero(m)
    m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    aspect = m.shape[1] / float(m.shape[0])
    yi = (np.arange(RES) * m.shape[0] / RES).astype(int)
    xi = (np.arange(RES) * m.shape[1] / RES).astype(int)
    return m[yi][:, xi], aspect


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
# a light copy for the silhouettes
probe = ob.copy()
probe.data = ob.data.copy()
bpy.context.collection.objects.link(probe)
tris = blib.tri_count(probe)
if tris > 4000:
    m = probe.modifiers.new("dec", "DECIMATE")
    m.ratio = 4000.0 / tris
    blib.select_only([probe])
    bpy.ops.object.modifier_apply(modifier="dec")
me = probe.data
me.calc_loop_triangles()
verts = np.empty(len(me.vertices) * 3, np.float32)
me.vertices.foreach_get("co", verts)
verts = verts.reshape(-1, 3)
faces = np.empty(len(me.loop_triangles) * 3, np.int32)
me.loop_triangles.foreach_get("vertices", faces)
faces = faces.reshape(-1, 3)
target, t_aspect = picture_mask(args["mask"])


def score_all(rots):
    out = []
    for rot in rots:
        sil, aspect = side_mask(verts @ rot.T, faces)
        iou = (sil & target).sum() / float(max((sil | target).sum(), 1))
        out.append((iou - 0.35 * abs(np.log(aspect / t_aspect)), iou, aspect, rot))
    return sorted(out, key=lambda s: -s[0])


# the vendor keeps the picture's up as the model's up, so first only the four turns about the vertical axis: a grip's
# side outline is nearly point-symmetric, and with all 24 orientations the pistol's came out upside down and mirrored
upright = [r for r in rotations() if r[2, 2] == 1]
scores = score_all(upright)
mode = "upright"
if scores[0][1] < 0.5:
    scores = score_all(rotations())
    mode = "any"
if args.get("yaw_sweep"):
    # a seed made from a three-quarter picture comes out turned by that view's angle, not by a multiple of 90 degrees:
    # sweep the turn about the vertical in 5 degree steps, then 1 degree around the best
    def rz(deg):
        a = np.radians(deg)
        return np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    # A long object's side silhouette barely changes when it is turned a few degrees (and the fit stretches it back to
    # its box), so the silhouette alone left the bullpup body turned off the barrel's line - a mess from the front
    # (2026-09-27). Its long axis decides the turn: the turn that makes it thinnest from the front; the side
    # silhouette then only picks which end is forward. Objects that are not long fall back to the silhouette sweep.
    def extents(t):
        v = verts @ rz(t).T
        return np.ptp(v[:, 1]), np.ptp(v[:, 0])          # width seen from the front, length seen from the side
    t0 = min(range(0, 180), key=lambda t: extents(t)[0])
    t0 = min((t0 + e * 0.1 for e in range(-10, 11)), key=lambda t: extents(t)[0])
    width, length = extents(t0)
    if length > 1.8 * width:
        scores = score_all([rz(t0), rz(t0 + 180)])      # aligned; the silhouette picks the forward end
        mode = "long_axis"
    else:
        coarse = score_all([rz(d) for d in range(0, 360, 5)])
        b = coarse[0]
        deg0 = next(d for d in range(0, 360, 5) if np.allclose(rz(d), b[3]))
        fine = score_all([rz(deg0 + d) for d in range(-4, 5)])
        scores = sorted(fine + coarse[1:], key=lambda s: -s[0])
        mode = "yaw_sweep"
best = scores[0]
bpy.data.objects.remove(probe, do_unlink=True)
R = Matrix([list(r) + [0] for r in best[3]] + [[0, 0, 0, 1]])
ob.data.transform(R)
shear = 0.0
if mode == "long_axis":
    # a seed made from a three-quarter picture comes out slanted: the picture's perspective (the near end drawn bigger)
    # reads as depth, and the bullpup body's centreline drifted 43% of its width from butt to muzzle, the sights and
    # the grip off the barrel's line (2026-09-27). The drift is a straight line along the length: sheared out.
    co = np.empty(len(ob.data.vertices) * 3, np.float32)
    ob.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    edges = np.linspace(co[:, 0].min(), co[:, 0].max(), 21)
    xs, mids = [], []
    for a, b in zip(edges, edges[1:]):
        sl = (co[:, 0] >= a) & (co[:, 0] <= b)
        if sl.sum() > 20:
            xs.append((a + b) / 2)
            mids.append((co[sl, 1].min() + co[sl, 1].max()) / 2)
    if len(xs) >= 5:
        shear = float(np.polyfit(xs, mids, 1)[0])
        ob.data.transform(Matrix(((1, 0, 0, 0), (-shear, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))))
lo, hi = blib.dims(ob)
ob.data.transform(Matrix.Translation(-(lo + hi) * 0.5))
ob.name = "Part"
bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args["out_blend"]), compress=True)
if args.get("out_render"):
    # the seed as the vendor made it (registered, untouched), for the page next to the picture it was made from
    blib.setup_render(512, 24, look="preview")
    st = blib.Stage(ob, look="preview")
    st.render("iso", os.path.abspath(args["out_render"]))
    st.close()
result = {"mode": mode, "iou": round(float(best[1]), 3), "score": round(float(best[0]), 3), "aspect": round(float(best[2]), 3),
          "target_aspect": round(float(t_aspect), 3), "runner_up_iou": round(float(scores[1][1]), 3), "shear": round(shear, 4),
          "rotation": [[round(float(v), 4) if mode in ("yaw_sweep", "long_axis") else int(v) for v in row] for row in best[3]]}
json.dump(result, open(args["out_json"], "w"), indent=1)
print("[register] best IoU %.3f (aspect %.2f vs %.2f), runner-up %.3f" % (best[1], best[2], t_aspect, scores[1][1]), flush=True)
