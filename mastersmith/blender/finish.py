"""Pass B (inside Blender): work.blend + decision.json -> engine-ready asset.
    blender -b --python finish.py -- <args.json>
Applies the facing yaw, projects segmentation masks onto faces (glass slot, wheel tagging), sanity-checks
roughness, exports maps with engine names, builds LOD0/1/2 by decimation, a UCX convex hull, preview
renders, and writes FBX + GLB + .blend + report.json."""
import json
import math
import os
import re
import sys

import bmesh
import bpy
import numpy as np
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blib  # noqa: E402
from finish_policy import detail_bake_skip_reason  # noqa: E402
from surface_normals import smooth_organic_normals  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
# Scripted texture repairs the brief asked for (Spec.texture_fixes): deterministic, free, applied on a re-finish of
# the same seed. "delight" = strong removal of baked shading; "clear_glass_highlights" = painted reflections under
# the canopy darkened (forced, whatever the env says); "dark_canopy" = an opaque dark canopy instead of a clear one.
TEXTURE_FIXES = set(args.get("texture_fixes") or [])
OUT, WORK, NAME = args["out_dir"], args["work_dir"], args["name"]
os.makedirs(OUT, exist_ok=True)
report = {"name": NAME, "lods": [], "maps": [], "files": [], "notes": []}
probe = json.load(open(os.path.join(WORK, "probe.json")))
decision = json.load(open(os.path.join(WORK, "decision.json")))
report["notes"] += probe.get("notes", [])


def log(msg):
    print("[finish] " + msg, flush=True)
    report["notes"].append(msg)


bpy.ops.wm.open_mainfile(filepath=os.path.join(WORK, "work.blend"))
ob = next(o for o in bpy.data.objects if o.type == "MESH")


def drop_floaties(obj):
    """Delete disconnected specks (below 0.05% of the faces AND 1% of the object's size); recalculate normals.
    Legitimate separate parts - magazines, sights, wheels - are far bigger than that and stay."""
    me = obj.data
    nv, nf = len(me.vertices), len(me.polygons)
    if nf < 1000:
        return 0
    ev = np.empty(len(me.edges) * 2, np.int32)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    label = np.arange(nv, dtype=np.int64)
    for _ in range(64):                      # label propagation + pointer jumping: islands in a few dozen rounds
        lo = np.minimum(label[ev[:, 0]], label[ev[:, 1]])
        before = label.copy()
        np.minimum.at(label, ev[:, 0], lo)
        np.minimum.at(label, ev[:, 1], lo)
        label = label[label]
        if np.array_equal(label, before):
            break
    fv = np.empty(nf, np.int32)
    me.polygons.foreach_get("vertices", np.empty(sum(p.loop_total for p in me.polygons), np.int32)) if False else None
    first = np.empty(nf, np.int32)
    me.polygons.foreach_get("loop_start", first)
    lv = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get("vertex_index", lv)
    face_label = label[lv[first]]
    co = np.empty(nv * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    diag = float(np.linalg.norm(co.max(axis=0) - co.min(axis=0)))
    labels, counts = np.unique(face_label, return_counts=True)
    small = labels[counts < max(8, 0.0005 * nf)]
    kill = np.zeros(nf, bool)
    for lb in small:
        vs = np.nonzero(label == lb)[0]
        ext = co[vs].max(axis=0) - co[vs].min(axis=0)
        if float(np.linalg.norm(ext)) < 0.01 * diag:
            kill |= face_label == lb
    removed = int(kill.sum())
    if removed:
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.nonzero(kill)[0]], context="FACES")
        bm.to_mesh(me)
        bm.free()
    # NO global "recalculate normals outside" here: image-to-3D meshes are overlapping, non-manifold shells and the
    # operator flipped whole patches of the M4 receiver and the Glock frame, which single-sided web viewers then
    # culled as holes (wave 8, 2026-09-17). The vendor's normals are the better guess; leave them.
    if removed:
        log("removed %d faces in %d floating fragment(s)" % (removed, len(small)))
    return removed


try:
    report["floaties_removed"] = drop_floaties(ob)
except Exception as exc:  # noqa: BLE001
    log("floatie clean-up skipped: %s" % str(exc)[:120])
raw_tris = blib.tri_count(ob)

# ---------------------------------------------------------------- masks -> faces (in the probe's coordinates, before the yaw)
def load_mask(path):
    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    m = px.reshape(h, w, 4)[:, :, 0] > 0.5        # Blender images are bottom-up, like camera view coords
    bpy.data.images.remove(img)
    return m


def pixel_ray(scn, cam, frame, u, v):
    """World-space direction of the camera ray through normalized image coords (u right, v up)."""
    xs = [c.x for c in frame]
    ys = [c.y for c in frame]
    p = Vector((min(xs) + u * (max(xs) - min(xs)), min(ys) + v * (max(ys) - min(ys)), frame[0].z))
    return (cam.matrix_world.to_3x3() @ p).normalized()


def dilate(mask, px):
    out = mask.copy()
    for _ in range(px):
        m = out
        out = m.copy()
        out[1:, :] |= m[:-1, :]
        out[:-1, :] |= m[1:, :]
        out[:, 1:] |= m[:, :-1]
        out[:, :-1] |= m[:, 1:]
    return out


def convex_hull_2d(pts):
    pts = sorted(set(map(tuple, pts)))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for pt in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], pt) <= 0:
            lower.pop()
        lower.append(pt)
    for pt in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], pt) <= 0:
            upper.pop()
        upper.append(pt)
    return lower[:-1] + upper[:-1]


def film_patch(scn, cam, frame, mask, ring, radius, cam_pos, cam_np, grid=56):
    """A smooth surface spanning an empty frame: depth along each mask pixel's ray is solved as a
    Laplace problem with the rim's measured depths as boundary (a soap film), then meshed on the pixel
    grid. Flat frames come out flat, curved cockpit openings come out curved. Returns
    {"centre", "normal", "verts", "faces"} or None."""
    dg = bpy.context.evaluated_depsgraph_get()
    h, w = mask.shape
    ys, xs = np.nonzero(mask | ring)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    step = max(1, int(math.ceil(max(y1 - y0, x1 - x0) / float(grid))))
    sub_mask = mask[y0:y1:step, x0:x1:step]
    sub_ring = ring[y0:y1:step, x0:x1:step]
    gh, gw = sub_mask.shape
    depth = np.zeros((gh, gw), np.float64)
    known = np.zeros((gh, gw), bool)
    for gy in range(gh):
        for gx in range(gw):
            if not sub_ring[gy, gx]:
                continue
            u = (x0 + gx * step + 0.5) / w
            v = (y0 + gy * step + 0.5) / h
            d = pixel_ray(scn, cam, frame, u, v)
            ok, loc, _n, _i, _o, _m = scn.ray_cast(dg, cam_pos, d, distance=radius * 20)
            if ok:
                depth[gy, gx] = (loc - cam_pos).length
                known[gy, gx] = True
    if known.sum() < 8:
        return None
    rim_pts = []
    for gy in range(gh):
        for gx in range(gw):
            if known[gy, gx]:
                d = pixel_ray(scn, cam, frame, (x0 + gx * step + 0.5) / w, (y0 + gy * step + 0.5) / h)
                rim_pts.append(np.array(cam_pos) + np.array(d) * depth[gy, gx])
    P = np.array(rim_pts)
    _u, sv, _vt = np.linalg.svd(P - P.mean(axis=0))
    if sv[0] > 1e-6 and sv[2] / sv[0] > 0.2:
        log("open frame rejected: its rim is not planar (%.2f)" % (sv[2] / sv[0]))
        return None
    # rim depths that are wildly off the median are the far side of the opening, not the frame
    med = np.median(depth[known])
    good = known & (np.abs(depth - med) < 0.25 * radius)
    if good.sum() < 8:
        return None
    interior = sub_mask & ~good
    depth[~good] = med
    for _ in range(600):                              # Jacobi relaxation; small grid, converges fast
        nb = (np.roll(depth, 1, 0) + np.roll(depth, -1, 0) + np.roll(depth, 1, 1) + np.roll(depth, -1, 1)) * 0.25
        depth = np.where(interior, nb, depth)
    verts, index = [], -np.ones((gh, gw), np.int64)
    for gy in range(gh):
        for gx in range(gw):
            if not sub_mask[gy, gx]:
                continue
            u = (x0 + gx * step + 0.5) / w
            v = (y0 + gy * step + 0.5) / h
            d = pixel_ray(scn, cam, frame, u, v)
            index[gy, gx] = len(verts)
            verts.append(np.array(cam_pos) + np.array(d) * depth[gy, gx] * 0.995)
    faces = []
    for gy in range(gh - 1):
        for gx in range(gw - 1):
            a, b, c, d_ = index[gy, gx], index[gy, gx + 1], index[gy + 1, gx + 1], index[gy + 1, gx]
            if min(a, b, c, d_) >= 0:
                faces.append((a, b, c))
                faces.append((a, c, d_))
            elif min(a, b, c) >= 0:
                faces.append((a, b, c))
            elif min(a, c, d_) >= 0:
                faces.append((a, c, d_))
    if len(faces) < 2:
        return None
    V = np.array(verts)
    centre = V.mean(axis=0)
    _u, _s, vt = np.linalg.svd(V - centre)
    nrm = vt[2]
    if np.dot(nrm, cam_np - centre) < 0:
        nrm = -nrm
    return {"centre": centre, "normal": nrm, "verts": V, "faces": faces}


def faces_under_masks(region_masks, allow_panes=False, min_votes=1):
    """Returns (face_mask, panes). A face is marked when its centre projects into a mask, faces the
    camera and is not occluded. With allow_panes, a mask whose interior rays fly PAST the rim (an open
    window frame with nothing in it) yields a pane polygon fitted to the rim instead of marking whatever
    lies behind the opening."""
    scn = bpy.context.scene
    dg = bpy.context.evaluated_depsgraph_get()
    mesh = ob.data
    n = len(mesh.polygons)
    centres = np.empty(n * 3, np.float32)
    normals = np.empty(n * 3, np.float32)
    mesh.polygons.foreach_get("center", centres)
    mesh.polygons.foreach_get("normal", normals)
    centres = centres.reshape(-1, 3)
    normals = normals.reshape(-1, 3)
    votes = np.zeros(n, np.int32)
    panes = []
    lo0, hi0 = blib.dims(ob)
    radius = max((hi0 - lo0).length * 0.5, 1e-4)
    for view, masks in region_masks.items():
        rec = next((r for r in probe["views"] if r["view"] == view), None)
        if rec is None or not masks:
            continue
        cam = blib.camera_from_record(rec["camera"], "Cam_" + view)
        scn.camera = cam
        frame = cam.data.view_frame(scene=scn)
        cam_pos = cam.matrix_world.translation
        cam_np = np.array(cam_pos)
        for m in masks:
            arr = load_mask(os.path.join(WORK, m["file"]))
            h, w = arr.shape
            if allow_panes and arr.sum() > 200:
                ring = dilate(arr, 5) & ~arr
                rim_pts, rim_d, inner_d = [], [], []
                ring_samples = 0
                ys, xs = np.nonzero(ring)
                for k in range(0, len(ys), max(1, len(ys) // 400)):
                    ring_samples += 1
                    d = pixel_ray(scn, cam, frame, (xs[k] + 0.5) / w, (ys[k] + 0.5) / h)
                    ok, loc, _n, _i, _o, _m = scn.ray_cast(dg, cam_pos, d, distance=radius * 20)
                    if ok:
                        rim_pts.append(np.array(loc))
                        rim_d.append((loc - cam_pos).length)
                ys, xs = np.nonzero(arr)
                for k in range(0, len(ys), max(1, len(ys) // 400)):
                    d = pixel_ray(scn, cam, frame, (xs[k] + 0.5) / w, (ys[k] + 0.5) / h)
                    ok, loc, _n, _i, _o, _m = scn.ray_cast(dg, cam_pos, d, distance=radius * 20)
                    inner_d.append((loc - cam_pos).length if ok else radius * 20)
                rim_hit = len(rim_d) / max(1, ring_samples)
                if len(rim_d) >= 12 and inner_d:
                    rim_med = float(np.median(rim_d))
                    behind = float(np.mean(np.array(inner_d) > rim_med + 0.06 * radius))
                    log("%s/%s: %.0f%% of the mask looks through an empty frame, rim hit %.0f%%" % (
                        view, m["file"], behind * 100, rim_hit * 100))
                    if behind > 0.6 and rim_hit >= 0.75:
                        patch = film_patch(scn, cam, frame, arr, ring, radius, cam_pos, cam_np)
                        if patch is not None:
                            c = patch["centre"]
                            dup = any(np.linalg.norm(pn["centre"] - c) < 0.08 * radius for pn in panes)
                            if not dup:
                                panes.append({**patch, "view": view})
                        continue
            facing = ((cam_np - centres) * normals).sum(axis=1) > 0
            for i in np.nonzero(facing)[0]:
                cpt = Vector(centres[i].tolist())
                u, v, depth = world_to_camera_view(scn, cam, cpt)
                if depth <= 0 or not (0 <= u < 1 and 0 <= v < 1):
                    continue
                if not arr[min(int(v * h), h - 1), min(int(u * w), w - 1)]:
                    continue
                d = cpt - cam_pos
                ok, loc, _nrm, _idx, _obj, _mm = scn.ray_cast(dg, cam_pos, d.normalized(), distance=d.length * 1.01)
                if ok and (loc - cpt).length > d.length * 0.01:
                    continue
                votes[i] += 1
        bpy.data.objects.remove(cam, do_unlink=True)
    return votes >= max(1, min_votes), panes


def smooth_face_selection(me, sel, rounds=1):
    """Morphological closing then opening of a face selection over shared vertices: fills the notches and shaves the
    spikes a projected mask leaves along a boundary (the sawtooth canopy edge, 2026-09-18). rounds = ring width."""
    n = len(me.polygons)
    if sel is None or n == 0 or sel.sum() == 0:
        return sel
    ls = np.empty(n, np.int32); me.polygons.foreach_get("loop_start", ls)
    lt = np.empty(n, np.int32); me.polygons.foreach_get("loop_total", lt)
    lv = np.empty(len(me.loops), np.int32); me.loops.foreach_get("vertex_index", lv)
    face_of_loop = np.repeat(np.arange(n), lt)
    nv = len(me.vertices)

    def grow(s):
        vs = np.zeros(nv, bool)
        vs[lv[s[face_of_loop]]] = True
        hit = vs[lv]                                   # per loop: its vertex is touched by the selection
        return np.bitwise_or.reduceat(hit, ls) if n else s

    def shrink(s):
        return ~grow(~s)

    out = sel.copy()
    for _ in range(rounds):
        out = shrink(grow(out))                        # closing: fill notches
    for _ in range(rounds):
        out = grow(shrink(out))                        # opening: shave spikes
    return out


regions = decision.get("regions", {})

# Repair before anything else is measured: parts the customer wants gone (an extra cylinder the vendor grew on the
# magazine, a stand, a sling fused to the stock) are deleted under their masks and the hole is closed. Blender does
# this for nothing; the alternative was buying a new mesh and hoping. Face arrays computed below see the final mesh.
if args.get("remove_parts"):
    kill_all = None
    n_all = len(ob.data.polygons)
    for k in sorted(k for k in regions if k.startswith("remove")):
        idx = int(k[6:])
        phrase = args["remove_parts"][idx] if idx < len(args["remove_parts"]) else k
        f, _ = faces_under_masks(regions[k], min_votes=2 if len(regions[k]) >= 3 else 1)
        if f.sum() < 20:
            log("remove %s: no faces found under its masks; nothing deleted" % phrase)
            continue
        if f.mean() > 0.3:
            log("remove %s: the mask covers %.0f%% of the object - a spill, nothing deleted" % (phrase, f.mean() * 100))
            continue
        sel = smooth_face_selection(ob.data, f, rounds=1)          # shave stray spill, fill notches
        log("remove %s: %d faces under the masks, %d after smoothing" % (phrase, int(f.sum()), int(sel.sum())))
        kill_all = sel if kill_all is None else (kill_all | sel)
        report.setdefault("removed_parts", []).append({"phrase": phrase, "faces": int(sel.sum())})
    if kill_all is not None and kill_all.any():
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.nonzero(kill_all)[0]], context="FACES")
        bm.verts.ensure_lookup_table()
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        boundary = [e for e in bm.edges if e.is_boundary]
        if boundary:
            try:
                bmesh.ops.holes_fill(bm, edges=boundary, sides=0)          # close the opening the part left
            except Exception as exc:  # noqa: BLE001 - an open hole beats a crash
                log("remove: hole fill failed (%s); the opening stays" % str(exc)[:120])
        bm.to_mesh(ob.data)
        bm.free()
        ob.data.update()
        bpy.context.view_layer.update()
        log("removed %d faces of unwanted part(s); mesh now %d faces" % (int(kill_all.sum()), len(ob.data.polygons)))

glass_faces, panes = None, []
if regions.get("glass"):
    glass_faces, panes = faces_under_masks(regions["glass"], allow_panes=True)
    before_n = int(glass_faces.sum())
    glass_faces = smooth_face_selection(ob.data, glass_faces, rounds=2)
    log("glass: %d faces marked, %d pane(s) to build, from %d view(s); boundary smoothed %d -> %d faces" % (
        before_n, len(panes), len(regions["glass"]), before_n, int(glass_faces.sum())))
part_faces = None
part_keys = [k for k in regions if k.startswith("part")]
part_faces_each = {}


def face_colours(obj):
    """Each face's base colour sampled at its UV centre from the material's base colour image (sRGB 0-1)."""
    me = obj.data
    n = len(me.polygons)
    out = np.full((n, 3), np.nan, np.float32)
    uv_layer = me.uv_layers.active
    if uv_layer is None:
        return out
    uv = np.empty(len(me.loops) * 2, np.float32)
    uv_layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    loop_start = np.empty(n, np.int32)
    loop_total = np.empty(n, np.int32)
    mat_idx = np.empty(n, np.int32)
    me.polygons.foreach_get("loop_start", loop_start)
    me.polygons.foreach_get("loop_total", loop_total)
    me.polygons.foreach_get("material_index", mat_idx)
    loop_poly = np.repeat(np.arange(n), loop_total)
    cen = np.zeros((n, 2), np.float32)
    np.add.at(cen, loop_poly, uv)
    cen /= np.maximum(loop_total, 1)[:, None]
    for si, slot in enumerate(obj.material_slots):
        m = slot.material
        if not m or not m.node_tree:
            continue
        bsdf = next((nd for nd in m.node_tree.nodes if nd.type == "BSDF_PRINCIPLED"), None)
        node, _ch = image_feeding(bsdf.inputs["Base Color"]) if bsdf else (None, None)
        if node is None or not node.image:
            continue
        px = pixels(node.image)
        h, w = px.shape[:2]
        sel = np.nonzero(mat_idx == si)[0]
        u = np.clip((cen[sel, 0] % 1.0) * (w - 1), 0, w - 1).astype(np.int64)
        v = np.clip((cen[sel, 1] % 1.0) * (h - 1), 0, h - 1).astype(np.int64)
        out[sel] = px[v, u, :3]
    return out


def hex_rgb(h):
    h = (h or "").lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32) if len(h) == 6 else None


def same_colour_only(faces, colours, tag, protect=None, anchor=None):
    """Drop faces whose colour sits far from the selection's median: the mask for a white stock must not take the
    orange magazine next to it (Raven2 v6, 2026-09-17). A protected neighbour's mask only removes faces that do
    NOT share the part's colour - SAM's protect masks spill onto the part itself (Raven2 v7)."""
    sel = np.nonzero(faces)[0]
    cols = colours[sel]
    ok = ~np.isnan(cols).any(axis=1)
    if ok.sum() < 20:
        return faces
    med = np.median(cols[ok], axis=0)
    if anchor is not None:
        # the planner's "this is how the part looks now": a spilled mask must not shift the reference colour
        # (the stock mask took half the rifle and its median went amber, Raven2 v8)
        med = anchor
    dist = np.abs(cols - med).max(axis=1)
    keep = np.zeros_like(faces)
    keep[sel[ok & (dist < (0.28 if anchor is not None else 0.22))]] = True
    keep[sel[~ok]] = True
    dropped = int(faces.sum() - keep.sum())
    if dropped:
        log("%s: %d faces dropped as another colour than the part (median rgb %.2f %.2f %.2f)" % (tag, dropped, *med))
    # a protected neighbour takes back only the faces that look like IT (closer to its colour than to the part's);
    # without a known colour, only faces clearly not the part's colour. Its mask spills onto the part otherwise.
    for pf, phex in (protect or []):
        if len(pf) < len(faces):
            continue
        pcol = hex_rgb(phex)
        if pcol is not None:
            d_prot = np.abs(cols - pcol).max(axis=1)
            theirs = ok & (d_prot < dist)
        else:
            theirs = ok & (dist >= 0.2)
        cut = np.zeros_like(faces)
        cut[sel[theirs]] = True
        cut &= keep & pf[:len(faces)]
        if cut.any():
            log("%s: %d faces given back to a protected neighbour" % (tag, int(cut.sum())))
        keep &= ~cut
    return keep


seed_faces_each = {}
for k in sorted(k for k in regions if k.startswith("seed")):
    f, _ = faces_under_masks(regions[k], min_votes=2 if len(regions[k]) >= 3 else 1)
    if f.sum() >= 30:
        seed_faces_each[int(k[4:])] = f
cyl_faces_each = {}
for k in sorted(k for k in regions if k.startswith("cyl")):
    f, _ = faces_under_masks(regions[k], min_votes=2 if len(regions[k]) >= 3 else 1)
    if f.sum() >= 50:
        cyl_faces_each[int(k[3:])] = f
        log("cylinder candidate %s: %d faces under the masks" % ((args.get("repair_cylinders") or [{}])[int(k[3:])].get("phrase", k), int(f.sum())))
part_protect = None
protect_each = []          # (faces, hex) per protected neighbour
if args.get("recolor_parts") and part_keys:
    # masks -> faces here (the probe's coordinates); the colour and protect guards run later, once the
    # texture helpers exist and the mesh is final
    for k in sorted(k for k in regions if k.startswith("protect")):
        f, _ = faces_under_masks(regions[k])
        pi = int(k[7:])
        prot = (args.get("protect_parts") or [None] * 9)[pi] if pi < len(args.get("protect_parts") or []) else None
        protect_each.append((f, (prot or {}).get("hex") if isinstance(prot, dict) else None))
        part_protect = f if part_protect is None else (part_protect | f)
    if part_protect is not None:
        log("protected parts: %d faces under their masks" % int(part_protect.sum()))
    for k in part_keys:
        f, _ = faces_under_masks(regions[k])
        idx = int(k[4:])
        if f.sum() >= 20:
            part_faces_each[idx] = f
            log("recolour %s: %d faces under the masks" % (args["recolor_parts"][idx]["phrase"], int(f.sum())))
        else:
            log("WARNING: no faces found for %s; it is left as it was" % args["recolor_parts"][idx]["phrase"])
if args.get("retexture_parts") and part_keys:
    merged = {}
    for k in part_keys:
        for view, masks in regions[k].items():
            merged.setdefault(view, []).extend(masks)
    part_faces, _ = faces_under_masks(merged)
    log("repaint limited to %s: %d faces under the masks" % (", ".join(args["retexture_parts"]), int(part_faces.sum())))
    if part_faces.sum() < 20:
        log("WARNING: the part masks matched almost nothing; repainting the whole object instead")
        part_faces = None
wheel_faces = None
if regions.get("wheel"):
    wheel_faces, _ = faces_under_masks(regions["wheel"])
    log("wheels: %d faces selected from %d view(s)" % (int(wheel_faces.sum()), len(regions["wheel"])))

def main_cluster(pts, cell):
    """Points of the biggest group of mutually touching grid cells (26-neighbourhood) - drops stray marks."""
    keys = np.floor(pts / cell).astype(np.int64)
    cells = {}
    for i, k in enumerate(map(tuple, keys)):
        cells.setdefault(k, []).append(i)
    seen, best = set(), []
    for start in cells:
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            c = stack.pop()
            comp.extend(cells[c])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        nb = (c[0] + dx, c[1] + dy, c[2] + dz)
                        if nb in cells and nb not in seen:
                            seen.add(nb)
                            stack.append(nb)
        if len(comp) > len(best):
            best = comp
    return pts[np.array(best)]


def main_cluster_idx(pts, cell):
    """Indices (into pts) of the biggest touching group - the same as main_cluster, for callers that need the rows."""
    keys = np.floor(pts / cell).astype(np.int64)
    cells = {}
    for i, k in enumerate(map(tuple, keys)):
        cells.setdefault(k, []).append(i)
    seen, best = set(), []
    for start in cells:
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            c = stack.pop()
            comp.extend(cells[c])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        nb = (c[0] + dx, c[1] + dy, c[2] + dz)
                        if nb in cells and nb not in seen:
                            seen.add(nb)
                            stack.append(nb)
        if len(comp) > len(best):
            best = comp
    return np.array(sorted(best), np.int64)


def canopy_shell_from_points(points, glass_mat, cut_frac=0.35, name="Canopy"):
    """Convex hull over a cloud of points, keeping only its upper, outward faces: a glass dome. Returns the new object
    (linked, matrix = identity) or None when the hull is degenerate."""
    import bmesh as _bm
    pts = np.asarray(points, np.float64)
    if len(pts) < 8 or (pts.max(axis=0) - pts.min(axis=0)).max() < 1e-4:
        return None
    bm = _bm.new()
    hv = [bm.verts.new(tuple(map(float, q))) for q in pts]
    bm.verts.ensure_lookup_table()
    res = _bm.ops.convex_hull(bm, input=hv)
    _bm.ops.delete(bm, geom=[g for g in res["geom_unused"] if isinstance(g, _bm.types.BMVert)], context="VERTS")
    bm.faces.ensure_lookup_table()
    zmin, zmax = pts[:, 2].min(), pts[:, 2].max()
    cut = zmin + (zmax - zmin) * cut_frac
    drop = [f for f in bm.faces if f.calc_center_median().z < cut or f.normal.z < -0.15]
    _bm.ops.delete(bm, geom=drop, context="FACES")
    if not len(bm.faces):
        bm.free()
        return None
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(glass_mat)
    for pgn in me.polygons:
        pgn.use_smooth = True
    o = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(o)
    return o



# glass gets its own material slot; the base material keeps the vendor maps
if (glass_faces is not None and glass_faces.sum() > 20) or panes:
    glass = bpy.data.materials.new("MI_%s_Glass" % NAME)
    glass.use_nodes = True
    bsdf = next(n for n in glass.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    # One glass look that survives every viewer (2026-09-18, checked in Google's model-viewer, the forge's viewer):
    # a plain dark tint with alpha blending and NO transmission extension. Transmission + 25% alpha made canopies
    # vanish (the cockpit tub looked uncovered) and the vehicle variant's texture mix exported the BODY atlas onto the
    # windows (opaque white panes). Unreal's FBX path ignores both anyway; the README names the material for an
    # opacity setup. A canopy with a cockpit under it is clearer than a car window over a hollow shell.
    _cat = (args.get("spec") or {}).get("category")
    _dark = _cat == "vehicle" or "dark_canopy" in TEXTURE_FIXES
    _alpha = 0.7 if _dark else 0.2                    # a contained tub lets the canopy be clear (F-16 wave 19/20: "opaque and dark")
    # near-black tint and a modest specular: at 0.09 grey with specular 0.8 the panes mirrored the backdrop and read
    # as opaque light grey in both Blender and model-viewer (wave 18 F-150 / Humvee "windows are opaque white")
    bsdf.inputs["Base Color"].default_value = (0.03, 0.04, 0.05, 1.0) if _dark else (0.16, 0.20, 0.26, 1.0)   # a canopy is clear: light enough to see in, not milky (wave 21)
    bsdf.inputs["Roughness"].default_value = 0.08
    bsdf.inputs["Metallic"].default_value = 0.0
    bsdf.inputs["Alpha"].default_value = _alpha
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.45
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 0.0
    try:
        glass.surface_render_method = "BLENDED"
    except AttributeError:
        pass
    log("glass: dark tinted panes, alpha %.2f, no transmission (%s)" % (_alpha, _cat or "generic"))
    ob.data.materials.append(glass)
    slot = len(ob.data.materials) - 1
    marked = 0
    if glass_faces is not None and glass_faces.sum() > 20:
        idx = np.empty(len(ob.data.polygons), np.int32)
        ob.data.polygons.foreach_get("material_index", idx)
        idx[glass_faces] = slot
        ob.data.polygons.foreach_set("material_index", idx)
        marked = int(glass_faces.sum())
    if panes:
        # each pane is one n-gon in the frame, slightly inset so it does not z-fight the rim
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        uv = bm.loops.layers.uv.verify()
        built = 0
        for pn in panes:
            bverts = [bm.verts.new(v.tolist()) for v in pn["verts"]]
            made = 0
            for tri in pn["faces"]:
                try:
                    f = bm.faces.new([bverts[i] for i in tri])
                except ValueError:
                    continue
                f.material_index = slot
                f.smooth = True
                if np.dot(np.array(f.normal), pn["normal"]) < 0:
                    f.normal_flip()
                for lp in f.loops:
                    lp[uv].uv = (0.5, 0.5)
                made += 1
            if made:
                built += 1
        bm.to_mesh(ob.data)
        bm.free()
        added = len(ob.data.polygons) - len(glass_faces)
        if added > 0:
            glass_faces = np.concatenate([glass_faces, np.ones(added, bool)])
            if wheel_faces is not None:
                wheel_faces = np.concatenate([wheel_faces, np.zeros(added, bool)])
            if part_faces is not None:
                part_faces = np.concatenate([part_faces, np.zeros(added, bool)])
            part_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in part_faces_each.items()}
            cyl_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in cyl_faces_each.items()}
            seed_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in seed_faces_each.items()}
            if part_protect is not None:
                part_protect = np.concatenate([part_protect, np.zeros(added, bool)])
                protect_each = [(np.concatenate([f, np.zeros(added, bool)]), hx) for f, hx in protect_each]
        log("glass: built %d pane(s) into empty frame(s)" % built)
    # An OPEN canopy (issue: Havoc 2026-09-18): image-to-3D sometimes models a cockpit as a recess with no glass
    # surface at all. The marked faces then line a cavity (their normals point INTO the cluster) and there is nothing
    # to make transparent. Close it with a shell: the convex hull of the cavity's rim, keeping only its upper faces.
    if (args.get("spec") or {}).get("cockpit") and glass_faces is not None and glass_faces.sum() > 200:
        n_all = len(ob.data.polygons)
        if len(glass_faces) != n_all:
            glass_faces = np.concatenate([glass_faces, np.zeros(n_all - len(glass_faces), bool)])[:n_all]
        cen_g = np.empty(n_all * 3, np.float32); ob.data.polygons.foreach_get("center", cen_g); cen_g = cen_g.reshape(-1, 3)
        nrm_g = np.empty(n_all * 3, np.float32); ob.data.polygons.foreach_get("normal", nrm_g); nrm_g = nrm_g.reshape(-1, 3)
        lo_o, hi_o = blib.dims(ob)
        gi_all = np.nonzero(glass_faces)[0]
        cl = main_cluster_idx(cen_g[gi_all], max((hi_o - lo_o).length * 0.02, 1e-3))
        pts = cen_g[gi_all[cl]]
        if len(pts) >= 50:
            c0 = pts.mean(axis=0)
            # only the canopy cluster: side windows and stray marks elsewhere would drag the hull into a fin
            gi = gi_all[cl]
            # ... and only its core: the 1st-99th percentile box drops the last strays that touch the cluster
            lo_c, hi_c = np.percentile(pts, 1, axis=0), np.percentile(pts, 99, axis=0)
            pad = (hi_c - lo_c) * 0.03
            inside = np.all((pts >= lo_c - pad) & (pts <= hi_c + pad), axis=1)
            gi = gi[inside]
            inward = ((c0 - cen_g[gi]) * nrm_g[gi]).sum(axis=1) > 0
            frac_in = float(inward.mean())
            log("canopy: %.0f%% of the glass faces point into the cluster (a closed dome is ~0%%, an open cavity is high)" % (frac_in * 100))
            shell = None
            if frac_in > 0.35 and os.environ.get("MASTERSMITH_CAVITY_SHELL", "1") != "0":
                # hull over the cavity faces' vertices
                me = ob.data
                lstart = np.empty(n_all, np.int32); me.polygons.foreach_get("loop_start", lstart)
                ltot = np.empty(n_all, np.int32); me.polygons.foreach_get("loop_total", ltot)
                lv = np.empty(len(me.loops), np.int32); me.loops.foreach_get("vertex_index", lv)
                vids = set()
                for fi in gi:
                    vids.update(lv[lstart[fi]:lstart[fi] + ltot[fi]].tolist())
                co = np.empty(len(me.vertices) * 3, np.float32); me.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
                vids = np.array(sorted(vids), np.int64)
                vin = np.all((co[vids] >= lo_c - pad * 2) & (co[vids] <= hi_c + pad * 2), axis=1)
                vids = vids[vin]
                shell = canopy_shell_from_points(co[vids], glass, 0.35)
                if shell is None:
                    log("canopy: the cavity rim is degenerate; no shell")
                    gi = gi[:0]
            if frac_in > 0.35 and shell is not None:
                shell.matrix_world = ob.matrix_world.copy()
                n_before = len(ob.data.polygons)
                blib.select_only([ob, shell]); bpy.context.view_layer.objects.active = ob
                bpy.ops.object.join()
                added = len(ob.data.polygons) - n_before
                glass_faces = np.concatenate([glass_faces, np.ones(added, bool)])
                if wheel_faces is not None:
                    wheel_faces = np.concatenate([wheel_faces, np.zeros(added, bool)])
                if part_faces is not None:
                    part_faces = np.concatenate([part_faces, np.zeros(added, bool)])
                part_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in part_faces_each.items()}
                cyl_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in cyl_faces_each.items()}
                seed_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in seed_faces_each.items()}
                # the cavity lining is the interior now, not glass: give it back to the body material
                idx = np.empty(len(ob.data.polygons), np.int32); ob.data.polygons.foreach_get("material_index", idx)
                idx[gi] = 0
                ob.data.polygons.foreach_set("material_index", idx)
                marked = added
                report["canopy_shell"] = {"faces": int(added), "cavity_faces": int(len(gi)), "inward": round(frac_in, 3),
                                          "hull_box_m": [round(float(v), 3) for v in (hi_c - lo_c)]}
                log("canopy: open cavity closed with a %d-face glass shell over its rim" % added)
    report["glass"] = {"faces": marked, "panes": len(panes), "material": glass.name}
def build_parametric_tub(seats=1):
    """Issue #12: a cockpit tub with no vendor - floor, side walls, seat(s) with backrest and headrest, an angled
    instrument panel with a glare shield, control stick(s) and pedals. Unit size (1 long along +X, 0.6 wide, 0.5 tall);
    the caller scales it into the canopy box like a seed. Dark, matte, slightly varied greys."""
    def box(name, cx, cy, cz, sx, sy, sz, mat):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(cx, cy, cz))
        o = bpy.context.active_object
        o.scale = (sx, sy, sz)
        o.name = name
        o.data.materials.append(mat)
        return o

    def solid(name, rgb, rough, metal=0.0):
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        b = next(nd for nd in m.node_tree.nodes if nd.type == "BSDF_PRINCIPLED")
        b.inputs["Base Color"].default_value = (*rgb, 1.0)
        b.inputs["Roughness"].default_value = rough
        b.inputs["Metallic"].default_value = metal
        return m

    dark = solid("MI_%s_Cockpit" % NAME, (0.05, 0.05, 0.055), 0.72)
    seat_m = solid("MI_%s_Cockpit_Seat" % NAME, (0.09, 0.09, 0.10), 0.85)
    panel_m = solid("MI_%s_Cockpit_Panel" % NAME, (0.03, 0.03, 0.035), 0.45)
    metal_m = solid("MI_%s_Cockpit_Metal" % NAME, (0.30, 0.30, 0.32), 0.4, 1.0)
    # furniture only: side walls and a bulkhead read as a grey BOX filling the canopy from outside (Havoc wave 18)
    parts = [box("floor", 0, 0, 0.03, 1.0, 0.6, 0.06, dark)]
    pitch = 1.0 / seats
    for i in range(seats):
        x = 0.5 - pitch * (i + 0.62)          # seats sit in the rear part of each bay, the panel ahead of them
        parts += [box("seat%d" % i, x, 0, 0.14, 0.26, 0.30, 0.10, seat_m),
                  box("back%d" % i, x - 0.12, 0, 0.30, 0.06, 0.30, 0.34, seat_m),
                  box("head%d" % i, x - 0.11, 0, 0.50, 0.06, 0.16, 0.08, seat_m),
                  box("pedals%d" % i, x + 0.36, 0, 0.09, 0.06, 0.22, 0.05, metal_m)]
        px = x + 0.30
        panel = box("panel%d" % i, px, 0, 0.30, 0.10, 0.50, 0.22, panel_m)
        panel.rotation_euler = (0, math.radians(-25), 0)
        parts += [panel, box("shield%d" % i, px + 0.03, 0, 0.42, 0.14, 0.54, 0.03, dark)]
        bpy.ops.mesh.primitive_cylinder_add(radius=0.012, depth=0.22, location=(x + 0.16, 0, 0.17))
        stick = bpy.context.active_object
        stick.name = "stick%d" % i
        stick.data.materials.append(metal_m)
        parts.append(stick)
    blib.select_only(parts)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    tub = bpy.context.view_layer.objects.active
    tub.name = "Cockpit"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for pgn in tub.data.polygons:
        pgn.use_smooth = False
    return tub


# ---------------------------------------------------------------- cockpit: a second model fitted under the canopy glass
cockpit_glb = args.get("cockpit_glb")
cockpit_parametric = bool(args.get("cockpit_parametric")) and not (cockpit_glb and os.path.exists(cockpit_glb))
if ((cockpit_glb and os.path.exists(cockpit_glb)) or cockpit_parametric) and glass_faces is not None and glass_faces.sum() > 20:
    n = len(ob.data.polygons)
    if len(glass_faces) != n:
        glass_faces = np.concatenate([glass_faces, np.zeros(n - len(glass_faces), bool)])[:n]
    centres = np.empty(n * 3, np.float32)
    ob.data.polygons.foreach_get("center", centres)
    gl = centres.reshape(-1, 3)[glass_faces]
    lo_all, hi_all = blib.dims(ob)
    gl = main_cluster(gl, max((hi_all - lo_all).length * 0.02, 1e-3))
    glo, ghi = np.percentile(gl, 2, axis=0), np.percentile(gl, 98, axis=0)
    gext = ghi - glo
    log("cockpit: canopy cluster %d of %d glass faces, box %.2f x %.2f x %.2f m" % (
        len(gl), int(glass_faces.sum()), gext[0], gext[1], gext[2]))
    before = set(bpy.data.objects)
    if cockpit_parametric:
        log("cockpit: no usable seed picture - building the tub parametrically (%d seat%s)" % (
            int(args.get("cockpit_seats") or 1), "" if int(args.get("cockpit_seats") or 1) == 1 else "s"))
        build_parametric_tub(int(args.get("cockpit_seats") or 1))
    else:
        bpy.ops.import_scene.gltf(filepath=os.path.abspath(cockpit_glb))
    new = [o for o in bpy.data.objects if o not in before]
    cps = [o for o in new if o.type == "MESH"]
    for o in cps:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in [o for o in new if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    if cps:
        blib.select_only(cps)
        if len(cps) > 1:
            bpy.ops.object.join()
        cp = bpy.context.view_layer.objects.active
        cp.rotation_mode = "XYZ"
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        clo, chi = blib.dims(cp)
        cext = chi - clo
        # the tub's long axis follows the canopy's long axis
        if (gext[0] >= gext[1]) != (cext.x >= cext.y):
            blib.apply_yaw(cp, 90)
            clo, chi = blib.dims(cp)
            cext = chi - clo
        # fit: the tub FILLS the cockpit cavity. A uniform scale (the smallest of the three ratios) left a small cube
        # floating in the middle of a hollow canopy (wave 20). Length and width follow the canopy footprint separately,
        # with the aspect allowed to move up to 1.6x; height runs from the cavity floor to the sill line.
        cen_all = centres.reshape(-1, 3)
        # the sill: the highest BODY (non-glass) faces under the canopy footprint. A seat's headrest rises to about
        # the middle of the canopy above the sill; a tub fitted to the glass box top of an open or tall canopy stood
        # on the fuselage like a crate (F-16 wave 18).
        sill = None
        body_sel = (~glass_faces) & (cen_all[:, 0] > glo[0] + 0.1 * gext[0]) & (cen_all[:, 0] < ghi[0] - 0.1 * gext[0])             & (np.abs(cen_all[:, 1] - (glo[1] + ghi[1]) * 0.5) < 0.6 * gext[1])             & (cen_all[:, 2] <= ghi[2]) & (cen_all[:, 2] >= glo[2] - 0.5 * gext[2])
        if body_sel.sum() >= 30:
            sill = float(np.percentile(cen_all[body_sel][:, 2], 85))
        # the cavity floor: the lowest body faces under the footprint, but never deeper below the sill than the canopy
        # is tall above it (a closed canopy has no cavity - the body faces under it are the belly, F-16 wave 20)
        foot = body_sel & (cen_all[:, 2] < (sill if sill is not None else ghi[2]))
        floor_z = float(np.percentile(cen_all[foot][:, 2], 8)) if foot.sum() >= 30 else glo[2] - 0.6 * gext[2]
        top_ref = sill if sill is not None else glo[2]
        floor_z = max(floor_z, top_ref - 1.0 * max(ghi[2] - top_ref, 0.15 * gext[2]))
        sx = 0.94 * gext[0] / max(cext.x, 1e-6)
        sy = 0.90 * gext[1] / max(cext.y, 1e-6)
        base = min(sx, sy)
        sx, sy = min(sx, base * 1.6), min(sy, base * 1.6)
        sz = max(ghi[2] - 0.12 * gext[2] - floor_z, 0.15 * gext[0]) / max(cext.z, 1e-6)
        sz = min(max(sz, 0.6 * base), 1.6 * base)
        cp.scale = (sx, sy, sz)
        bpy.ops.object.transform_apply(scale=True)
        s = base
        clo, chi = blib.dims(cp)
        target_top = ghi[2] - 0.08 * gext[2]
        log("cockpit: tub scaled %.2f x %.2f x %.2f, cavity floor %.2f m (glass %.2f..%.2f)" % (sx, sy, sz, floor_z, glo[2], ghi[2]))
        if sill is not None:
            clamp = sill + 0.45 * max(ghi[2] - sill, 0.05 * gext[2])
            if clamp < target_top:
                log("cockpit: tub top lowered from %.2f to %.2f m (sill %.2f, glass top %.2f)" % (target_top, clamp, sill, ghi[2]))
                target_top = clamp
        # place: centred on the footprint, standing on the cavity floor; the top must still respect target_top
        bottom_target = floor_z + 0.02 * gext[2]
        dz = bottom_target - clo.z
        if clo.z + dz + (chi.z - clo.z) > target_top:
            dz = target_top - chi.z
        shift = Vector(((glo[0] + ghi[0]) * 0.5 - (clo.x + chi.x) * 0.5, (glo[1] + ghi[1]) * 0.5 - (clo.y + chi.y) * 0.5, dz))
        cp.location += shift
        bpy.ops.object.transform_apply(location=True)
        for slot in cp.material_slots:
            if slot.material and not cockpit_parametric:
                slot.material.name = "MI_%s_Cockpit" % NAME
        cp.name = "Cockpit"
        # (a) a tub is open on top: the picture model keeps drawing a windscreen and the seed vendor closes it into a
        # roof, which then shows through the canopy as an opaque white shell (Havoc, 2026-09-18)
        clo, chi = blib.dims(cp)
        h_cp = max(chi.z - clo.z, 1e-6)
        bm_cp = bmesh.new()
        bm_cp.from_mesh(cp.data)
        roof = [f for f in bm_cp.faces if (f.calc_center_median().z > clo.z + 0.45 * h_cp and f.normal.z > 0.25)
                or f.calc_center_median().z > clo.z + 0.85 * h_cp]
        if 0 < len(roof) < 0.45 * len(bm_cp.faces) and not cockpit_parametric:
            bmesh.ops.delete(bm_cp, geom=roof, context="FACES")
            bm_cp.to_mesh(cp.data)
            log("cockpit: %d roof / windscreen faces removed from the tub" % len(roof))
        bm_cp.free()
        cp.data.update()
        # (b) is the tub visible from outside? Ray-cast its upper vertices up and outward against the BODY; hits mean
        # a canopy surface covers them. An exposed tub gets a glass dome whatever the glass mask votes said.
        try:
            from mathutils.bvhtree import BVHTree
            deps = bpy.context.evaluated_depsgraph_get()
            bvh = BVHTree.FromObject(ob, deps)
            inv = ob.matrix_world.inverted()
            inv3 = inv.to_3x3()
            vco = np.empty(len(cp.data.vertices) * 3, np.float32)
            cp.data.vertices.foreach_get("co", vco)
            vco = vco.reshape(-1, 3)
            upper = vco[vco[:, 2] > clo.z + 0.5 * h_cp]
            if len(upper) > 400:
                upper = upper[np.random.RandomState(0).choice(len(upper), 400, replace=False)]
            dirs = [Vector((0, 0, 1)), Vector((0, 0.7, 0.7)), Vector((0, -0.7, 0.7)), Vector((0.7, 0, 0.7)), Vector((-0.7, 0, 0.7))]
            reach = 4.0 * max(h_cp, gext[2])
            miss = 0
            total = 0
            for q in upper:
                o_l = inv @ Vector((float(q[0]), float(q[1]), float(q[2])))
                for d in dirs:
                    total += 1
                    hit = bvh.ray_cast(o_l, inv3 @ d, reach)
                    if hit[0] is None:
                        miss += 1
            exposed = miss / max(total, 1)
            log("cockpit: %.0f%% of the tub's upper vertices see the sky through the body" % (exposed * 100))
            # (c) a tub is interior geometry: faces whose every vertex sees the sky are OUTSIDE the airframe (a tub
            # longer than the canopy pokes through the spine - F-16 wave 18) and are deleted rather than domed over.
            # The rays go up and outward; a face survives if any vertex is covered by body geometry.
            vco_all = np.empty(len(cp.data.vertices) * 3, np.float32)
            cp.data.vertices.foreach_get("co", vco_all)
            vco_all = vco_all.reshape(-1, 3)
            covered = np.zeros(len(vco_all), bool)
            zlow = clo.z + 0.25 * h_cp
            for vi, q in enumerate(vco_all):
                if q[2] < zlow:
                    covered[vi] = True              # the floor and the lower walls are always inside the hull
                    continue
                o_l = inv @ Vector((float(q[0]), float(q[1]), float(q[2])))
                for d in dirs:
                    if bvh.ray_cast(o_l, inv3 @ d, reach)[0] is not None:
                        covered[vi] = True
                        break
            if (~covered).any():
                bm_x = bmesh.new()
                bm_x.from_mesh(cp.data)
                bm_x.verts.ensure_lookup_table()
                outside = [f for f in bm_x.faces if all(not covered[v.index] for v in f.verts)]
                if outside and len(outside) < 0.7 * len(bm_x.faces):
                    bmesh.ops.delete(bm_x, geom=outside, context="FACES")
                    bm_x.to_mesh(cp.data)
                    cp.data.update()
                    log("cockpit: %d tub faces stuck out of the airframe and were removed" % len(outside))
                    # re-measure what is still exposed (a genuine opening, not a protrusion)
                    vco2 = np.empty(len(cp.data.vertices) * 3, np.float32)
                    cp.data.vertices.foreach_get("co", vco2)
                    vco2 = vco2.reshape(-1, 3)
                    up2 = vco2[vco2[:, 2] > clo.z + 0.5 * h_cp]
                    if len(up2) > 400:
                        up2 = up2[np.random.RandomState(1).choice(len(up2), 400, replace=False)]
                    miss2 = tot2 = 0
                    for q in up2:
                        o_l = inv @ Vector((float(q[0]), float(q[1]), float(q[2])))
                        for d in dirs:
                            tot2 += 1
                            if bvh.ray_cast(o_l, inv3 @ d, reach)[0] is None:
                                miss2 += 1
                    exposed = miss2 / max(tot2, 1)
                    upper = up2
                    log("cockpit: %.0f%% of the remaining tub still sees the sky" % (exposed * 100))
                bm_x.free()
            # a cavity shell already covers the top; rays escaping under its rim do not justify a second dome
            need = 0.4 if report.get("canopy_shell") else 0.25
            if exposed > need:
                ctr = np.array([(glo[0] + ghi[0]) * 0.5, (glo[1] + ghi[1]) * 0.5, glo[2]])
                pushed = ctr + (upper - ctr) * 1.04
                pts = np.vstack([gl, pushed])
                dome = canopy_shell_from_points(pts, glass, 0.30, "CanopyDome")
                if dome is not None:
                    n_before = len(ob.data.polygons)
                    blib.select_only([ob, dome])
                    bpy.context.view_layer.objects.active = ob
                    bpy.ops.object.join()
                    ob = bpy.context.view_layer.objects.active
                    added = len(ob.data.polygons) - n_before
                    glass_faces = np.concatenate([glass_faces, np.ones(added, bool)])
                    if wheel_faces is not None:
                        wheel_faces = np.concatenate([wheel_faces, np.zeros(added, bool)])
                    if part_faces is not None:
                        part_faces = np.concatenate([part_faces, np.zeros(added, bool)])
                    part_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in part_faces_each.items()}
                    cyl_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in cyl_faces_each.items()}
                    seed_faces_each = {k: np.concatenate([v, np.zeros(added, bool)]) for k, v in seed_faces_each.items()}
                    n = len(ob.data.polygons)
                    report["glass"]["faces"] = int(report["glass"].get("faces", 0)) + added
                    report["canopy_dome"] = {"faces": int(added), "exposed": round(exposed, 3)}
                    log("canopy: the cockpit was exposed - a %d-face glass dome built over it" % added)
        except Exception as exc:  # noqa: BLE001 - the dome is a guard, never a reason to fail the build
            log("WARNING: cockpit exposure check skipped: %s" % str(exc)[:160])
        blib.select_only([ob, cp])
        bpy.context.view_layer.objects.active = ob
        bpy.ops.object.join()
        ob = bpy.context.view_layer.objects.active
        # material indices and the glass selection survive the join (the cockpit's faces are appended)
        glass_faces = np.concatenate([glass_faces, np.zeros(len(ob.data.polygons) - len(glass_faces), bool)])
        raw_tris = blib.tri_count(ob)     # the LOD budget applies to the aircraft WITH its cockpit
        report["cockpit"] = {"faces": int(len(ob.data.polygons) - n), "fit_scale": round(float(s), 3),
                             "canopy_box_m": [round(float(v), 3) for v in gext], "parametric": bool(cockpit_parametric)}
        log("cockpit fitted under the canopy (%d faces, scale %.2f)" % (report["cockpit"]["faces"], s))

# wheel faces are remembered as a face attribute so the rig pass can pick them up after decimation
if wheel_faces is not None and wheel_faces.sum() > 20:
    attr = ob.data.attributes.new("ms_wheel", "INT", "FACE")
    attr.data.foreach_set("value", wheel_faces.astype(np.int32))
    report["wheel_faces"] = int(wheel_faces.sum())

# ---------------------------------------------------------------- facing: bring the front round to +X
yaw = int(decision.get("yaw", 0))
YAW_PIVOT = ob.location.copy()
if yaw:
    blib.apply_yaw(ob, yaw)
    log("yawed %d deg so the front faces +X (%s)" % (yaw, decision.get("facing", {}).get("reason", "")))
lo, hi = blib.dims(ob)
if args["origin"] == "bottom":
    shift = Vector(((lo.x + hi.x) * 0.5, (lo.y + hi.y) * 0.5, lo.z))
else:
    shift = (lo + hi) * 0.5
if shift.length > 1e-6:
    ob.location -= shift
    blib.select_only([ob])
    bpy.ops.object.transform_apply(location=True)
    bpy.context.scene.cursor.location = (0, 0, 0)
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
lo, hi = blib.dims(ob)
report["dimensions_m"] = [round(v, 4) for v in (hi - lo)]
# the probe cameras were recorded before the yaw and the origin shift; this carries them into the final frame
PROBE_TO_NOW = (Matrix.Translation(-shift) if shift.length > 1e-6 else Matrix.Identity(4)) @     Matrix.Translation(YAW_PIVOT) @ Matrix.Rotation(math.radians(yaw), 4, "Z") @ Matrix.Translation(-YAW_PIVOT)

# ---------------------------------------------------------------- materials: maps + roughness sanity
def image_feeding(sock):
    if not sock.is_linked:
        return None, None
    node = sock.links[0].from_node
    channel = None
    if node.type == "SEPARATE_COLOR" and node.inputs[0].is_linked:
        channel = sock.links[0].from_socket.name
        node = node.inputs[0].links[0].from_node
    if node.type == "NORMAL_MAP" and node.inputs["Color"].is_linked:
        node = node.inputs["Color"].links[0].from_node
    hops = 0
    while node is not None and node.type != "TEX_IMAGE" and hops < 4:
        linked = [i for i in node.inputs if i.is_linked]
        node = linked[0].links[0].from_node if linked else None
        hops += 1
    if node is not None and node.type == "TEX_IMAGE" and node.image is not None:
        return node, channel
    return None, None


def pixels(img):
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(h, w, 4)


def box_blur(a, r):
    """Separable box blur, radius r pixels, no scipy."""
    if r < 1:
        return a
    k = 2 * r + 1
    pad = np.pad(a, ((r, r), (r, r)), mode="edge")
    c = np.cumsum(pad, axis=0)
    a1 = (c[k - 1:] - np.concatenate([np.zeros((1, c.shape[1]), c.dtype), c[:-k]], axis=0)) / k
    c = np.cumsum(a1, axis=1)
    return (c[:, k - 1:] - np.concatenate([np.zeros((c.shape[0], 1), c.dtype), c[:, :-k]], axis=1)) / k


def gauss(a, r):
    return box_blur(box_blur(box_blur(a, max(1, r // 2)), max(1, r // 2)), max(1, r // 2))


def put_channel(px, cols, val):
    """Write a grey (H, W) array into one channel of px, or into RGB when the map is a grey image of its own."""
    if cols is not None:
        px[:, :, cols] = val
    else:
        px[:, :, :3] = val[..., None]


def channel_of(px, channel):
    cols = {"Red": 0, "Green": 1, "Blue": 2}.get(channel)
    return (px[:, :, cols] if cols is not None else px[:, :, :3].mean(axis=2)), cols


def reference_foreground(rp):
    """Foreground mask of a reference picture. The backdrop is whatever is CONNECTED to the border through smooth
    colour changes: a white sweep, a grey studio floor, a sky gradient. A border-median threshold counted the F-150's
    grey floor as foreground and the tone match lifted a dark blue truck to light blue (2026-09-18). Region grow on a
    256-wide copy (pure numpy, iterative dilation), then upsampled."""
    h, w = rp.shape[:2]
    step = max(1, int(round(max(h, w) / 256.0)))
    small = rp[::step, ::step, :3]
    sh, sw = small.shape[:2]
    bg = np.zeros((sh, sw), bool)
    bg[0, :] = bg[-1, :] = True
    bg[:, 0] = bg[:, -1] = True
    # a pixel joins the backdrop when a backdrop neighbour is within 0.035 of it (smooth gradients pass, edges stop)
    for _ in range(sh + sw):
        grew = False
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            src = np.roll(bg, (dy, dx), axis=(0, 1))
            nb = np.roll(small, (dy, dx), axis=(0, 1))
            close = np.abs(small - nb).max(axis=2) < 0.035
            add = src & close & ~bg
            if dy == 1:
                add[0, :] = False
            if dy == -1:
                add[-1, :] = False
            if dx == 1:
                add[:, 0] = False
            if dx == -1:
                add[:, -1] = False
            if add.any():
                bg |= add
                grew = True
        if not grew:
            break
    fg_small = ~bg
    if fg_small.mean() < 0.02 or fg_small.mean() > 0.9:
        # degenerate (busy backdrop or object touching every border): fall back to the border-median rule
        border = np.concatenate([rp[:8, :, :3].reshape(-1, 3), rp[-8:, :, :3].reshape(-1, 3),
                                 rp[:, :8, :3].reshape(-1, 3), rp[:, -8:, :3].reshape(-1, 3)])
        back = np.median(border, axis=0)
        lum = 0.2126 * rp[:, :, 0] + 0.7152 * rp[:, :, 1] + 0.0722 * rp[:, :, 2]
        return (np.abs(rp[:, :, :3] - back).max(axis=2) > 0.06) & (lum < 0.97)
    fg = np.repeat(np.repeat(fg_small, step, axis=0), step, axis=1)[:h, :w]
    if fg.shape != (h, w):
        pad = np.zeros((h, w), bool)
        pad[:fg.shape[0], :fg.shape[1]] = fg
        fg = pad
    return fg


def material_pass(found, mat, profile, reference_path):
    """Tripo's PBR atlas is a soft painting: a flat roughness, a metallic mask speckled per UV island, and a base
    colour with the studio lighting baked in. A flat 0.6 roughness on a dark grey reads as plastic. This rebuilds
    roughness per material family (gunmetal vs matte dielectric) with variation from the vendor map and the colour
    detail, despeckles the metallic mask, and pulls the base colour's tones towards the reference picture when they
    are far off. Deterministic; no vendor call."""
    out = {}
    px_m, cols_m, img_m = None, None, None
    if "M" in found:
        node, channel = found["M"]
        img_m = node.image
        px_m = pixels(img_m)
        m_raw, cols_m = channel_of(px_m, channel)
        m_raw = m_raw.copy()
        m_smooth = gauss(m_raw, 12)
        m_clean = gauss((m_smooth > 0.5).astype(np.float32), 3)
        # paint is a dielectric: painted vehicles and aircraft keep only a little of the vendor's "metal"
        m_clean = (m_clean * profile.get("metallic_scale", 1.0)).astype(np.float32)
        out["metallic_mean"] = round(float(m_raw.mean()), 3)
        out["metallic_rebuilt_mean"] = round(float(m_clean.mean()), 3)
        out["metallic_speckle_removed"] = round(float(np.abs(m_clean - m_raw).mean()), 3)
    else:
        m_clean = None
    # ---- base colour tones follow the reference when they are far off (washed or over-lit vendor atlas)
    if "BC" in found and reference_path and os.path.exists(reference_path):
        node, _ch = found["BC"]
        img_bc = node.image
        px_bc = pixels(img_bc)
        ref = bpy.data.images.load(os.path.abspath(reference_path), check_existing=True)
        rp = pixels(ref)
        ref_l = 0.2126 * rp[:, :, 0] + 0.7152 * rp[:, :, 1] + 0.0722 * rp[:, :, 2]
        # the object is whatever is not the backdrop: a product shot's white, or a render's flat grey
        # (a reference taken from an earlier version's render, 2026-09-17). The backdrop colour is the
        # median of the picture's border.
        fg = reference_foreground(rp)
        src_l = 0.2126 * px_bc[:, :, 0] + 0.7152 * px_bc[:, :, 1] + 0.0722 * px_bc[:, :, 2]
        if fg.mean() > 0.02:
            q = np.linspace(0, 1, 256)
            s_q = np.quantile(src_l, q)
            t_q = np.quantile(ref_l[fg], q)
            matched = np.interp(src_l, s_q, t_q)
            gap = float(np.abs(matched - src_l).mean())
            out["basecolor_tone_gap"] = round(gap, 3)
            # a quantile match assumes the atlas and the photo show the same mix of tones. An atlas with a far larger
            # share of black texels (rail, barrel, bore) than the photo's foreground maps its mid-grey body onto the
            # photo's light tones and LIGHTENS it (the Hi3D bullpup: 30% vs 8% dark, lum 0.27 -> 0.35, 2026-09-25)
            _used = src_l > 0.03
            dark_src = float((src_l[_used] < 0.12).mean()) if _used.any() else 0.0
            dark_ref = float((ref_l[fg] < 0.12).mean())
            out["basecolor_dark_share"] = [round(dark_src, 3), round(dark_ref, 3)]
            if gap > 0.06 and profile.get("tone_match", 0.6) is not None and abs(dark_src - dark_ref) > 0.15:
                log("base colour tone pull skipped: the atlas is %.0f%% dark texels, the photo %.0f%%; a quantile match would "
                    "lighten the body instead of matching it" % (dark_src * 100, dark_ref * 100))
            elif gap > 0.06 and profile.get("tone_match", 0.6) is not None:
                # the further off the atlas is, the harder it is pulled (a 0.5 pull left the Havoc light blue against
                # a slate-grey reference, 2026-09-18); the profile value is the floor
                strength = float(min(0.85, profile.get("tone_match", 0.6) + max(0.0, gap - 0.06) * 2.0))
                new_l = (1 - strength) * src_l + strength * matched
                gain = np.clip(new_l / np.maximum(src_l, 1e-3), 0.3, 2.5)
                px_bc[:, :, :3] = np.clip(px_bc[:, :, :3] * gain[..., None], 0, 1)
                # saturation follows the reference too: hue-preserving chroma scale by the ratio of the mean chroma of
                # the reference foreground to that of the atlas (the vendor over-saturates pale paints)
                lum2 = 0.2126 * px_bc[:, :, 0] + 0.7152 * px_bc[:, :, 1] + 0.0722 * px_bc[:, :, 2]
                # saturation = chroma / luminance: absolute chroma scales with the lighting (a dark blue truck reads
                # as 'less colourful' than the same blue under the probe lights) and halved the F-150's colour
                chroma_ref = (np.abs(rp[:, :, :3] - ref_l[..., None]).sum(axis=2) / np.maximum(ref_l, 0.08))[fg]
                c_ref = float(chroma_ref.mean())
                # the atlas is measured through the PROBE RENDERS (visible pixels, area-weighted, white light), not
                # the atlas itself: unused atlas areas and padding made the atlas read as desaturated and the first
                # version pushed the Havoc's saturation UP (2026-09-18)
                c_src, n_src = 0.0, 0
                for view in ("posy", "negy", "iso"):
                    pr_path = os.path.join(WORK, "probe_%s.png" % view)
                    if not os.path.exists(pr_path):
                        continue
                    pr = pixels(bpy.data.images.load(pr_path, check_existing=True))
                    pr_l = 0.2126 * pr[:, :, 0] + 0.7152 * pr[:, :, 1] + 0.0722 * pr[:, :, 2]
                    pb = np.median(np.concatenate([pr[:8, :, :3].reshape(-1, 3), pr[-8:, :, :3].reshape(-1, 3)]), axis=0)
                    pfg = np.abs(pr[:, :, :3] - pb).max(axis=2) > 0.06
                    if pfg.mean() > 0.01:
                        c_src += float((np.abs(pr[:, :, :3] - pr_l[..., None]).sum(axis=2) / np.maximum(pr_l, 0.08))[pfg].mean())
                        n_src += 1
                if n_src:
                    c_src /= n_src
                else:
                    chroma_src = np.abs(px_bc[:, :, :3] - lum2[..., None]).sum(axis=2) / np.maximum(lum2, 0.08)
                    used = (lum2 > 0.05) & (chroma_src > 0.005)
                    c_src = float(chroma_src[used].mean()) if used.any() else 0.0
                sat = float(np.clip(c_ref / max(c_src, 1e-3), 0.5, 1.4)) if c_src > 0.01 else 1.0
                if abs(sat - 1.0) > 0.08:
                    px_bc[:, :, :3] = np.clip(lum2[..., None] + (px_bc[:, :, :3] - lum2[..., None]) * sat, 0, 1)
                    out["basecolor_saturation_scale"] = round(sat, 3)
                img_bc.pixels.foreach_set(px_bc.ravel())
                img_bc.pack()
                img_bc.update()
                out["basecolor_tone_matched"] = round(float(new_l.mean()), 3)
                out["basecolor_tone_strength"] = round(strength, 2)
                log("base colour tones pulled towards the reference (gap %.2f, strength %.2f, lum %.2f -> %.2f, saturation x%.2f)" % (
                    gap, strength, src_l.mean(), new_l.mean(), sat))
        L = src_l
    else:
        L = None
    # ---- de-light: the vendor bakes its studio lighting into the colour (soft shading across panels, dark hollows,
    # bright tops). Divide the colour by its own LOW-FREQUENCY luminance, normalised over used texels, so only the
    # material and the fine detail stay; the engine's lights then do the shading. That baked shading under a
    # mid-roughness dielectric is most of the "melted plastic" read (owner, 2026-09-18).
    if "BC" in found and profile.get("delight", True):
        node, _ch = found["BC"]
        img_bc = node.image
        px_bc = pixels(img_bc)
        lum = 0.2126 * px_bc[:, :, 0] + 0.7152 * px_bc[:, :, 1] + 0.0722 * px_bc[:, :, 2]
        used = (lum > 0.03).astype(np.float32)
        if used.sum() > 1000:
            rad = max(24, int(px_bc.shape[0] * 0.02))
            low = gauss(lum * used, rad) / np.maximum(gauss(used, rad), 1e-3)
            um = used > 0
            target = float(lum[um].mean())
            gain = np.clip(target / np.maximum(low, 0.05), 0.7, 1.5)
            strength = float(profile.get("delight_strength", 0.6))
            gain = 1.0 + (gain - 1.0) * strength
            before_std = float(low[um].std())
            px_bc[:, :, :3] = np.clip(px_bc[:, :, :3] * gain[..., None], 0, 1)
            lum2 = 0.2126 * px_bc[:, :, 0] + 0.7152 * px_bc[:, :, 1] + 0.0722 * px_bc[:, :, 2]
            low2 = gauss(lum2 * used, rad) / np.maximum(gauss(used, rad), 1e-3)
            img_bc.pixels.foreach_set(px_bc.ravel())
            img_bc.pack()
            img_bc.update()
            out["delight_low_std"] = [round(before_std, 4), round(float(low2[um].std()), 4)]
            log("de-light: low-frequency shading std %.3f -> %.3f (radius %d px, strength %.1f)" % (before_std, float(low2[um].std()), rad, strength))
            if L is not None:
                L = lum2
    # ---- painted specular: bright, colourless speckles and streaks the vendor painted where its lights hit (rails,
    # receiver, barrel of the M4A1, 2026-09-24). The low-frequency de-light cannot reach them. Each is replaced by the
    # colour around it (a normalised blur that ignores the speckles), so the engine's lights make the highlights
    # instead. Asked for with texture_fixes "kill_highlights".
    if "BC" in found and "kill_highlights" in TEXTURE_FIXES:
        node, _ch = found["BC"]
        img_bc = node.image
        px_bc = pixels(img_bc)
        rgb = px_bc[:, :, :3]
        lum = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
        sat = rgb.max(axis=2) - rgb.min(axis=2)
        used = lum > 0.03
        ref = float(np.percentile(lum[used], 60)) if used.sum() > 1000 else 0.3
        hot = used & (lum > max(0.45, ref * 1.8)) & (sat < 0.22)
        if hot.sum() > 50:
            keep = (used & ~hot).astype(np.float32)
            rad = 6
            fill = np.stack([gauss(rgb[:, :, c] * keep, rad) / np.maximum(gauss(keep, rad), 1e-3) for c in range(3)], axis=2)
            rgb[hot] = np.clip(fill[hot] * 0.92, 0, 1)             # a touch darker than the surround: a highlight was there
            px_bc[:, :, :3] = rgb
            img_bc.pixels.foreach_set(px_bc.ravel())
            img_bc.pack()
            img_bc.update()
            out["highlights_killed"] = int(hot.sum())
            log("kill highlights: %d painted-specular texels (%.1f%% of the used atlas) replaced by their surround (ref lum %.2f)" % (
                int(hot.sum()), hot.sum() / max(used.sum(), 1) * 100, ref))
            if L is not None:
                L = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    # ---- roughness
    if "R" in found:
        node, channel = found["R"]
        img_r = node.image
        px_r = pixels(img_r) if (img_m is None or node.image != img_m) else px_m
        r_raw, cols_r = channel_of(px_r, channel)
        r_raw = r_raw.copy()                  # a view into px_r otherwise, and the stats would read the rebuilt map
        out["roughness_mean"] = round(float(r_raw.mean()), 3)
        p5, p95 = np.percentile(r_raw, [5, 95])
        r_norm = np.clip((r_raw - p5) / (p95 - p5), 0, 1) if (p95 - p5) > 0.05 else np.full_like(r_raw, 0.5)
        H, W = r_raw.shape
        if L is not None and L.shape == r_raw.shape:
            hp = L - gauss(L, 8)
            hp = np.clip(hp / (np.percentile(np.abs(hp), 95) + 1e-4), -1, 1)
        else:
            hp = np.zeros_like(r_raw)
        rng = np.random.default_rng(7)
        grain = rng.random((H, W), dtype=np.float32) - 0.5
        grain = grain - gauss(grain, 2)
        metal = m_clean if m_clean is not None else np.zeros_like(r_raw)
        r_metal, r_diel = profile.get("roughness_metal", 0.5), profile.get("roughness_dielectric", 0.68)
        target = metal * r_metal + (1 - metal) * r_diel
        r_new = target + 0.12 * (r_norm - 0.5) + 0.06 * hp + 0.03 * grain * (1 - metal) + 0.02 * grain * metal
        r_new = np.clip(r_new, 0.3, 0.92).astype(np.float32)
        if cols_r is not None:
            px_r[:, :, cols_r] = r_new
        else:
            px_r[:, :, :3] = r_new[..., None]
        if m_clean is not None and img_m is not None and node.image == img_m:
            put_channel(px_r, cols_m, m_clean)
        img_r.pixels.foreach_set(px_r.ravel())
        img_r.pack()
        img_r.update()
        out["roughness_rebuilt_mean"] = round(float(r_new.mean()), 3)
        log("roughness rebuilt %.2f (flat p10-p90 %.2f-%.2f) -> %.2f (%.2f-%.2f); metal %.2f dielectric %.2f" % (
            r_raw.mean(), *np.percentile(r_raw, [10, 90]), r_new.mean(), *np.percentile(r_new, [10, 90]), r_metal, r_diel))
        if m_clean is not None and img_m is not None and node.image != img_m:
            put_channel(px_m, cols_m, m_clean)
            img_m.pixels.foreach_set(px_m.ravel())
            img_m.pack()
            img_m.update()
    elif m_clean is not None and img_m is not None:
        put_channel(px_m, cols_m, m_clean)
        img_m.pixels.foreach_set(px_m.ravel())
        img_m.pack()
        img_m.update()
    return out


def bake_face_mask(obj, faces, size=2048):
    """A UV-space image (H, W) in [0, 1]: 1 where a selected face's UVs land. Cycles EMIT bake of a per-corner
    colour attribute, through a temporary emission shader on every material slot."""
    me = obj.data
    if "ms_mask" in me.color_attributes:
        me.color_attributes.remove(me.color_attributes["ms_mask"])
    attr = me.color_attributes.new("ms_mask", "BYTE_COLOR", "CORNER")
    loop_poly = np.repeat(np.arange(len(me.polygons)), [p.loop_total for p in me.polygons])
    col = np.zeros((len(me.loops), 4), np.float32)
    col[:, 3] = 1.0
    col[faces[loop_poly], :3] = 1.0
    attr.data.foreach_set("color", col.ravel())
    img = bpy.data.images.new("ms_mask_bake", size, size, alpha=False, float_buffer=False)
    img.colorspace_settings.name = "Non-Color"
    restore = []
    for slot in obj.material_slots:
        m = slot.material
        if not m or not m.node_tree:
            continue
        nt = m.node_tree
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None) or \
            next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if out is None:
            continue
        prev = out.inputs["Surface"].links[0].from_socket if out.inputs["Surface"].is_linked else None
        a = nt.nodes.new("ShaderNodeVertexColor")
        a.layer_name = "ms_mask"
        e = nt.nodes.new("ShaderNodeEmission")
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = img
        nt.links.new(a.outputs["Color"], e.inputs["Color"])
        for l in list(out.inputs["Surface"].links):
            nt.links.remove(l)
        nt.links.new(e.outputs["Emission"], out.inputs["Surface"])
        nt.nodes.active = t
        restore.append((nt, out, prev, [a, e, t]))
    scn = bpy.context.scene
    scn.render.engine = "CYCLES"
    scn.cycles.device = "CPU"
    scn.cycles.samples = 1
    blib.select_only([obj])
    bpy.ops.object.bake(type="EMIT", margin=4, use_clear=True, target="IMAGE_TEXTURES")
    px = np.empty(size * size * 4, np.float32)
    img.pixels.foreach_get(px)
    mask = px.reshape(size, size, 4)[:, :, 0]
    for nt, out, prev, nodes in restore:
        for l in list(out.inputs["Surface"].links):
            nt.links.remove(l)
        if prev is not None:
            nt.links.new(prev, out.inputs["Surface"])
        for n in nodes:
            nt.nodes.remove(n)
    bpy.data.images.remove(img)
    me.color_attributes.remove(me.color_attributes["ms_mask"])
    return mask


def resample_nearest(a, h, w):
    ys = (np.arange(h) * a.shape[0] / h).astype(np.int64)
    xs = (np.arange(w) * a.shape[1] / w).astype(np.int64)
    return a[ys][:, xs]


def blend_repaint(obj, faces, old_maps):
    """New maps only where the mask is: everywhere else the previous version's maps come back."""
    mask = bake_face_mask(obj, faces)
    cover = float(mask.mean())
    log("repaint mask covers %.0f%% of the atlas" % (cover * 100))
    for slot in obj.material_slots:
        m = slot.material
        if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME):
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf:
            continue
        for role, sock in (("BC", bsdf.inputs["Base Color"]), ("R", bsdf.inputs["Roughness"]),
                           ("M", bsdf.inputs["Metallic"]), ("N", bsdf.inputs["Normal"])):
            old = old_maps.get(role)
            node, channel = image_feeding(sock)
            if node is None or not node.image or not old or not os.path.exists(old["file"]):
                continue
            new_img = node.image
            new_px = pixels(new_img)
            old_img = bpy.data.images.load(old["file"], check_existing=True)
            old_px = pixels(old_img)
            h, w = new_px.shape[:2]
            if old_px.shape[:2] != (h, w):
                old_px = np.stack([resample_nearest(old_px[:, :, c], h, w) for c in range(4)], axis=2)
            mk = resample_nearest(mask, h, w)[..., None] if mask.shape != (h, w) else mask[..., None]
            if role in ("R", "M") and old.get("channel"):
                # the old value lived in one channel of a packed ORM; the new map is a grey image
                c = {"Red": 0, "Green": 1, "Blue": 2}[old["channel"]]
                old_val = old_px[:, :, c:c + 1]
                new_px[:, :, :3] = mk * new_px[:, :, :3] + (1 - mk) * old_val
            else:
                new_px[:, :, :3] = mk * new_px[:, :, :3] + (1 - mk) * old_px[:, :, :3]
            new_img.pixels.foreach_set(new_px.ravel())
            new_img.pack()
            new_img.update()
    return cover


FINISH_ROUGHNESS = {"matte": 0.78, "satin": 0.55, "glossy": 0.3}


def clear_painted_reflections(obj, glass_faces, tint=(0.10, 0.11, 0.12)):
    """The vendor paints the reference's glass highlights onto whatever it models under the canopy; seen through a
    clear canopy that reads as white blobs (Havoc gunship, 2026-09-23). Inside the canopy's bounding box - the glass
    faces and what lies behind them - bright, colourless texels are pulled toward a dark cockpit tint, the more the
    brighter they are; coloured and dark texels (seat, panels, frame paint) stay. Returns stats or None."""
    me = obj.data
    n = len(me.polygons)
    g = np.zeros(n, bool)
    g[:min(n, len(glass_faces))] = glass_faces[:n]
    if g.sum() < 20:
        return None
    cen = np.empty(n * 3, np.float32)
    me.polygons.foreach_get("center", cen)
    cen = cen.reshape(-1, 3)
    lo, hi = cen[g].min(axis=0), cen[g].max(axis=0)
    pad = 0.06 * float((hi - lo).max()) + 1e-3
    inside = np.all((cen >= lo - pad) & (cen <= hi + pad), axis=1)
    sel = inside | g
    mask = bake_face_mask(obj, sel)
    tint_v = np.array(tint, np.float32)
    texels = 0
    for slot in obj.material_slots:
        m = slot.material
        if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME):
            continue
        bsdf = next((nd for nd in m.node_tree.nodes if nd.type == "BSDF_PRINCIPLED"), None)
        if not bsdf:
            continue
        node, _ch = image_feeding(bsdf.inputs["Base Color"])
        if node is None or not node.image:
            continue
        px = pixels(node.image)
        h, w = px.shape[:2]
        mk = resample_nearest(mask, h, w) if mask.shape != (h, w) else mask
        rgb = px[:, :, :3]
        L = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
        S = rgb.max(axis=2) - rgb.min(axis=2)
        hot = (mk > 0.5) & (L > 0.5) & (S < 0.25)
        if not hot.any():
            continue
        strength = np.where(hot, np.clip((L - 0.5) / 0.3, 0.0, 1.0), 0.0).astype(np.float32)   # brighter -> darker
        px[:, :, :3] = strength[..., None] * tint_v[None, None, :] + (1 - strength[..., None]) * rgb
        node.image.pixels.foreach_set(px.ravel())
        node.image.pack()
        node.image.update()
        texels += int(hot.sum())
    return {"faces": int(sel.sum()), "glass_faces": int(g.sum()), "texels": texels}


def recolor_part(obj, faces, part):
    """Inside the part's UV mask: base colour becomes the wanted flat colour modulated by the old luminance (the
    stippling, wear and panel lines stay), metallic and roughness become the wanted finish."""
    mask = bake_face_mask(obj, faces)
    hexv = (part.get("color") or "").lstrip("#")
    target = np.array([int(hexv[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32) if len(hexv) == 6 else None
    rough = float(part.get("roughness") or FINISH_ROUGHNESS.get(part.get("finish", "matte"), 0.78))
    metal = 1.0 if part.get("metal") else 0.0
    cover = float(mask.mean())
    for slot in obj.material_slots:
        m = slot.material
        if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME):
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf:
            continue
        node, _ch = image_feeding(bsdf.inputs["Base Color"])
        if node is not None and node.image and target is not None:
            px = pixels(node.image)
            h, w = px.shape[:2]
            mk = resample_nearest(mask, h, w) if mask.shape != (h, w) else mask
            L = 0.2126 * px[:, :, 0] + 0.7152 * px[:, :, 1] + 0.0722 * px[:, :, 2]
            inside = mk > 0.5
            ref = float(L[inside].mean()) if inside.any() else float(L.mean())
            detail = np.clip(L / max(ref, 1e-3), 0.55, 1.45)
            new = np.clip(target[None, None, :] * detail[..., None], 0, 1)
            px[:, :, :3] = mk[..., None] * new + (1 - mk[..., None]) * px[:, :, :3]
            node.image.pixels.foreach_set(px.ravel())
            node.image.pack()
            node.image.update()
        for role, sock, val in (("R", bsdf.inputs["Roughness"], rough), ("M", bsdf.inputs["Metallic"], metal)):
            node, channel = image_feeding(sock)
            if node is None or not node.image:
                continue
            px = pixels(node.image)
            cur, cols = channel_of(px, channel)
            h, w = cur.shape
            mk = resample_nearest(mask, h, w) if mask.shape != (h, w) else mask
            put_channel(px, cols, (mk * val + (1 - mk) * cur).astype(np.float32))
            node.image.pixels.foreach_set(px.ravel())
            node.image.pack()
            node.image.update()
    return cover


MATERIAL_PROFILES = {   # per category: roughness targets for metal / dielectric, tone-match strength
    # satin gunmetal, not polished: a Glock's nDLC slide and a parkerized barrel sit near 0.6 (reviewer, wave 7)
    "weapon": {"roughness_metal": 0.58, "roughness_dielectric": 0.75, "tone_match": 0.6, "metallic_scale": 1.0},
    "vehicle": {"roughness_metal": 0.45, "roughness_dielectric": 0.35, "tone_match": 0.5, "metallic_scale": 0.3},   # car paint has a clearcoat: 0.55 read as matte plastic
    "aircraft": {"roughness_metal": 0.45, "roughness_dielectric": 0.5, "tone_match": 0.5, "metallic_scale": 0.2},
    "helicopter": {"roughness_metal": 0.45, "roughness_dielectric": 0.55, "tone_match": 0.5, "metallic_scale": 0.25},
    "prop": {"roughness_metal": 0.5, "roughness_dielectric": 0.7, "tone_match": 0.5, "metallic_scale": 0.8},
    "environment": {"roughness_metal": 0.55, "roughness_dielectric": 0.8, "tone_match": 0.4, "metallic_scale": 0.5},
}
old_maps_path = os.path.join(WORK, "old_maps.json")
if os.path.exists(old_maps_path):
    # a repaint vendor answers metallic 0 everywhere (Meshy, 2026-09-17); the previous version knew where the metal was
    old_m = (json.load(open(old_maps_path)) or {}).get("M")
    for slot in ob.material_slots:
        m = slot.material
        if not m or not m.node_tree or not old_m or not os.path.exists(old_m["file"]):
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        node, channel = image_feeding(bsdf.inputs["Metallic"]) if bsdf else (None, None)
        if node is None or not node.image:
            continue
        px = pixels(node.image)
        cur, cols = channel_of(px, channel)
        if float(cur.mean()) < 0.02:
            old_px = pixels(bpy.data.images.load(old_m["file"], check_existing=True))
            c = {"Red": 0, "Green": 1, "Blue": 2}.get(old_m.get("channel"))
            old_val = old_px[:, :, c] if c is not None else old_px[:, :, :3].mean(axis=2)
            if old_val.shape != cur.shape:
                old_val = resample_nearest(old_val, cur.shape[0], cur.shape[1])
            put_channel(px, cols, old_val.astype(np.float32))
            node.image.pixels.foreach_set(px.ravel())
            node.image.pack()
            node.image.update()
            log("repaint: the vendor's metallic map was empty; the previous version's metal placement is kept (mean %.2f)" % float(old_val.mean()))
if part_faces is not None and os.path.exists(old_maps_path):
    try:
        report["repaint_cover"] = round(blend_repaint(ob, part_faces, json.load(open(old_maps_path))), 3)
    except Exception as exc:  # noqa: BLE001 - a whole-object repaint is still a repaint
        log("part-limited repaint failed (%s); the whole object keeps the new maps" % str(exc)[:160])
GLOSSY_MEAN, FLOOR = 0.40, 0.35
found_by_material = {}
for slot in ob.material_slots:
    m = slot.material
    if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME):
        continue
    bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if not bsdf:
        continue
    roles = {"BC": bsdf.inputs["Base Color"], "N": bsdf.inputs["Normal"], "R": bsdf.inputs["Roughness"],
             "M": bsdf.inputs["Metallic"], "E": bsdf.inputs["Emission Color"]}
    found = {}
    for role, sock in roles.items():
        node, channel = image_feeding(sock)
        if node is not None:
            found[role] = (node, channel)
    spec_d = args.get("spec") or {}
    profile = MATERIAL_PROFILES.get(spec_d.get("category"))
    if profile and spec_d.get("category") == "vehicle":
        # clearcoat for cars, matte for military paint: the hybrid Humvee at 0.35 read "glossy plasticky" (2026-09-18)
        _d = ((spec_d.get("description") or "") + " " + (spec_d.get("name") or "")).lower()
        if any(w in _d for w in ("military", "matte", "desert tan", "olive", "camouflage", "camo", "armoured", "armored",
                                 "tank ", "humvee", "hmmwv", "apc", "utility vehicle", "truck", "pickup")):
            profile = {**profile, "roughness_dielectric": 0.62, "roughness_metal": 0.5}
        elif any(w in _d for w in ("gloss", "metallic paint", "sports car", "muscle car", "sedan", "supercar", "showroom")):
            profile = {**profile, "roughness_dielectric": 0.28}
    if "delight" in TEXTURE_FIXES:
        # the brief asked for the baked shading to go: full-strength de-light whatever the category profile says
        profile = {**(profile or {}), "delight": True, "delight_strength": 0.95}
        log("texture fix: strong de-light requested")
    if "preserve_seed_maps" in TEXTURE_FIXES:
        # the seed's colour IS the asset: no tone pull towards the reference and no de-light. On the Hi3D bullpup
        # (2026-09-25) the pull lifted the atlas from 0.27 to 0.35 and cut saturation to 0.73, turning the black grip,
        # magazine and barrel light grey and the olive panels chalky; roughness and metallic are still rebuilt.
        profile = {**(profile or {}), "delight": False, "tone_match": None}
        log("texture fix: preserve_seed_maps keeps the seed's base colour (no tone pull, no de-light)")
    # a seed with colour but no roughness / metallic maps (Hi3D v3 ships BC + N only) gets a flat ORM-style map so the
    # material pass, the families and the recolour have something to write into and the delivery has an ORM
    if "BC" in found and ("R" not in found or "M" not in found) and profile:
        bc_img = found["BC"][0].image
        w0, h0 = (bc_img.size if bc_img and bc_img.size[0] else (2048, 2048))
        w0, h0 = min(w0, 4096), min(h0, 4096)
        orm = bpy.data.images.new("ms_ORM_%s" % m.name, w0, h0, alpha=False)
        orm.colorspace_settings.name = "Non-Color"
        flat = np.empty((h0, w0, 4), np.float32)
        flat[:, :, 0] = 1.0                                                   # AO
        flat[:, :, 1] = profile.get("roughness_dielectric", 0.6)              # roughness
        flat[:, :, 2] = 0.0                                                   # metallic
        flat[:, :, 3] = 1.0
        orm.pixels.foreach_set(flat.ravel())
        orm.pack()
        nt = m.node_tree
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = orm
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(tex.outputs["Color"], sep.inputs["Color"])
        made = []
        if "R" not in found:
            for l in list(bsdf.inputs["Roughness"].links):
                nt.links.remove(l)
            nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
            found["R"] = image_feeding(bsdf.inputs["Roughness"])
            made.append("roughness")
        if "M" not in found:
            for l in list(bsdf.inputs["Metallic"].links):
                nt.links.remove(l)
            nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
            found["M"] = image_feeding(bsdf.inputs["Metallic"])
            made.append("metallic")
        log("the seed had no %s map: a flat %dx%d ORM was made for the material pass to shape" % (" or ".join(made), w0, h0))
    is_cockpit = m.name.startswith("MI_%s_Cockpit" % NAME)
    if profile and spec_d.get("style", "realistic") == "realistic" and not is_cockpit:
        stats = material_pass(found, m, profile, args.get("reference"))
        report.setdefault("material_pass", {})[m.name] = stats
        if "roughness_rebuilt_mean" in stats:
            report["roughness_mean"] = stats["roughness_rebuilt_mean"]
        if "metallic_mean" in stats:
            report["metallic_mean"] = stats["metallic_mean"]
    else:
        # characters and stylized work keep the vendor's roughness, just not glazed-ceramic low
        if "R" in found:
            node, channel = found["R"]
            img = node.image
            px = pixels(img)
            cols = {"Red": 0, "Green": 1, "Blue": 2}.get(channel)
            sel = px[:, :, cols] if cols is not None else px[:, :, :3]
            mean = float(sel.mean())
            if mean < GLOSSY_MEAN:
                if cols is not None:
                    px[:, :, cols] = FLOOR + (1 - FLOOR) * px[:, :, cols]
                else:
                    px[:, :, :3] = FLOOR + (1 - FLOOR) * px[:, :, :3]
                img.pixels.foreach_set(px.ravel())
                img.pack()
                img.update()
                after = float(px[:, :, cols].mean() if cols is not None else px[:, :, :3].mean())
                log("roughness lifted %.2f -> %.2f (vendor map read as glazed ceramic)" % (mean, after))
                mean = after
            report["roughness_mean"] = round(mean, 3)
        if "M" in found:
            node, channel = found["M"]
            px = pixels(node.image)
            cols = {"Red": 0, "Green": 1, "Blue": 2}.get(channel, 2)
            report["metallic_mean"] = round(float(px[:, :, cols].mean()), 3)
    found_by_material[m.name] = found
    # A vendor material ("tripo_material_<uuid>", "pbr_material") becomes MI_<Name>. Anything ALREADY
    # named MI_<Name>... keeps its own name. Renaming them all collided: Blender suffixes a duplicate
    # (.001, .002), the suffixed name then fails the `MI_<Name>_` test in export_maps, and the maps
    # shipped as T_<Name>_Part1_BC.png instead of under the part's name. Anything with more than one
    # body material hit it - a second vendor material, a re-finished asset, a fitted part (2026-09-22).
    if not m.name.startswith("MI_%s" % NAME):
        m.name = "MI_" + NAME


def export_maps(obj):
    """T_<Name>[_Cockpit]_<BC|N|ORM|E>.png from the materials' images - after every edit (material pass, recolour,
    bake): the files used to be written mid-loop and missed the recolour (2026-09-17)."""
    saved = {}
    written = set()
    for si, slot in enumerate(obj.material_slots):
        m = slot.material
        if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME):
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf:
            continue
        roles = {"BC": bsdf.inputs["Base Color"], "N": bsdf.inputs["Normal"], "R": bsdf.inputs["Roughness"],
                 "M": bsdf.inputs["Metallic"], "E": bsdf.inputs["Emission Color"]}
        # every material gets its own file set: the body is MI_<Name>, a fitted part MI_<Name>_<Part>; anything
        # else (a second vendor material, a stale material from a re-finished asset) is a numbered part. Two
        # materials used to share T_<Name>_BC.png and the last one written won (Havoc re-finish, 2026-09-18).
        if m.name == "MI_%s" % NAME or si == 0:
            part = ""
        elif m.name.startswith("MI_%s_" % NAME):
            part = "_" + re.sub(r"[^A-Za-z0-9]", "", m.name[len("MI_%s_" % NAME):]) or "_Part%d" % si
        else:
            part = "_Part%d" % si
        # Roughness and metallic must ship as ONE ORM image (AO in R, roughness in G, metallic in B): the delivery
        # splits it by channel and Unreal reads it that way. Tripo's quad/FBX seeds carry SEPARATE grey roughness and
        # metallic images, and the old loop kept only the first of them under the ORM name - every quad seed since
        # roll 23 shipped without its metallic (found on the F-150, 2026-09-18). Compose when they differ.
        r_node, r_ch = image_feeding(bsdf.inputs["Roughness"])
        m_node, m_ch = image_feeding(bsdf.inputs["Metallic"])
        if r_node is not None and r_node.image and m_node is not None and m_node.image and (
                r_node.image != m_node.image or r_ch is None or m_ch is None):
            rpx = pixels(r_node.image); mpx = pixels(m_node.image)
            if rpx.shape[:2] != mpx.shape[:2]:
                m_node.image.scale(r_node.image.size[0], r_node.image.size[1]); mpx = pixels(m_node.image)
            rr, _c = channel_of(rpx, r_ch); mm, _c2 = channel_of(mpx, m_ch)
            orm = bpy.data.images.new("ms_ORM_%s" % m.name, rpx.shape[1], rpx.shape[0], alpha=False)
            orm.colorspace_settings.name = "Non-Color"
            comp = np.empty_like(rpx)
            comp[:, :, 0] = rpx[:, :, 0] if r_ch == "Green" else 1.0    # AO lives in R when the source was an ORM
            comp[:, :, 1] = rr; comp[:, :, 2] = mm; comp[:, :, 3] = 1.0
            orm.pixels.foreach_set(comp.ravel()); orm.pack()
            nt = m.node_tree
            tex = nt.nodes.new("ShaderNodeTexImage"); tex.image = orm
            sep = nt.nodes.new("ShaderNodeSeparateColor"); nt.links.new(tex.outputs["Color"], sep.inputs["Color"])
            for sock_, out_ in ((bsdf.inputs["Roughness"], "Green"), (bsdf.inputs["Metallic"], "Blue")):
                for l in list(sock_.links):
                    nt.links.remove(l)
                nt.links.new(sep.outputs[out_], sock_)
            for n in list(nt.nodes):
                if n.type == "TEX_IMAGE" and not any(o.is_linked for o in n.outputs):
                    nt.nodes.remove(n)
            log("roughness + metallic composed into one ORM for %s (they were separate images)" % m.name)
            roles = {"BC": bsdf.inputs["Base Color"], "N": bsdf.inputs["Normal"], "R": bsdf.inputs["Roughness"],
                     "M": bsdf.inputs["Metallic"], "E": bsdf.inputs["Emission Color"]}
        for role, sock in roles.items():
            node, _channel = image_feeding(sock)
            if node is None or not node.image:
                continue
            img = node.image
            tag = "ORM" if role in ("R", "M") else role
            if img.name in saved:
                continue
            path = os.path.join(OUT, "T_%s%s_%s.png" % (NAME, part, tag))
            if path in written:
                log("WARNING: %s would be written twice (material %s); the second image is kept in the blend only" % (os.path.basename(path), m.name))
                continue
            written.add(path)
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
            saved[img.name] = path
            report["maps"].append({"role": tag, "file": os.path.basename(path), "size": list(img.size),
                                   **({"part": part[1:]} if part else {})})
    if not report["maps"]:
        log("WARNING: the seed had no texture maps")

fam_keys = sorted(k for k in regions if k.startswith("family"))
fams = args.get("material_families") or []
if fam_keys and fams:
    face_cols_f = face_colours(ob)
    taken = np.zeros(len(ob.data.polygons), bool)
    for k in fam_keys:
        idx = int(k[6:])
        if idx >= len(fams):
            continue
        fam = fams[idx]
        # a family must be seen from two sides when it was found in three or more: one spilled mask painted a
        # whole rifle "steel" (ScoutRifle, 2026-09-17)
        f, _ = faces_under_masks(regions[k], min_votes=2 if len(regions[k]) >= 3 else 1)
        if len(f) != len(taken):
            f = np.concatenate([f, np.zeros(len(taken) - len(f), bool)])[:len(taken)]
        if f.sum() > 0.6 * len(f) and len(fams) > 1:
            # one mask covering most of the object is the segmenter spilling, not a material family
            f, _ = faces_under_masks(regions[k], min_votes=3)
            if len(f) != len(taken):
                f = np.concatenate([f, np.zeros(len(taken) - len(f), bool)])[:len(taken)]
            log("material family %s: mask covered most of the object; kept only faces seen in 3 views (%d)" % (fam["phrase"], int(f.sum())))
        if fam.get("metal"):
            # metals are greys: a saturated face (olive stock, amber magazine, red paint) is never bare metal
            cols = face_cols_f
            mx, mn = np.nanmax(cols, axis=1), np.nanmin(cols, axis=1)
            sat = np.where(mx > 1e-3, (mx - mn) / np.maximum(mx, 1e-3), 0)
            grey = ~np.isnan(mx) & (sat < 0.22)
            before = int(f.sum())
            f &= grey
            if before - int(f.sum()):
                log("material family %s: %d saturated faces are not metal" % (fam["phrase"], before - int(f.sum())))
        f &= ~taken
        if f.sum() < 20:
            log("material family %s: no faces found" % fam["phrase"])
            continue
        taken |= f
        try:
            rough = float(fam.get("roughness") or FINISH_ROUGHNESS.get(fam.get("finish", "matte"), 0.7))
            cover = recolor_part(ob, f, {"color": "", "metal": bool(fam.get("metal")), "finish": fam.get("finish", "matte"), "roughness": rough})
            report.setdefault("material_families", []).append({"phrase": fam["phrase"], "faces": int(f.sum()), "metal": bool(fam.get("metal")),
                                                                "roughness": rough, "atlas_cover": round(cover, 3)})
            log("material family %s: %d faces -> %s roughness %.2f (%.0f%% of the atlas)" % (
                fam["phrase"], int(f.sum()), "metal" if fam.get("metal") else "dielectric", rough, cover * 100))
        except Exception as exc:  # noqa: BLE001
            log("material family %s failed: %s" % (fam["phrase"], str(exc)[:160]))

if part_faces_each:
    face_cols = face_colours(ob)
for idx, faces in sorted(part_faces_each.items()):
    part = args["recolor_parts"][idx]
    faces = same_colour_only(faces, face_cols, part["phrase"], protect=protect_each, anchor=hex_rgb(part.get("current_hex")))
    if faces.sum() < 20:
        log("WARNING: nothing left to recolour for %s after the colour/protect guards" % part["phrase"])
        continue
    try:
        cover = recolor_part(ob, faces, part)
        report.setdefault("recolor", []).append({**part, "faces": int(faces.sum()), "atlas_cover": round(cover, 3)})
        log("recoloured %s -> %s %s%s (%.0f%% of the atlas)" % (part["phrase"], part["color"], part["finish"],
                                                              " metal" if part.get("metal") else "", cover * 100))
    except Exception as exc:  # noqa: BLE001
        log("recolour of %s failed: %s" % (part["phrase"], str(exc)[:160]))

# Painted glass highlights under a clear canopy (aircraft, helicopters, vehicles with a cabin).
if glass_faces is not None and (args.get("spec") or {}).get("category") in ("aircraft", "helicopter", "vehicle") \
        and (os.environ.get("MASTERSMITH_CLEAR_GLASS_HIGHLIGHTS", "1") != "0" or "clear_glass_highlights" in TEXTURE_FIXES):
    try:
        _cleared = clear_painted_reflections(ob, glass_faces)
        if _cleared:
            report["glass_reflections_cleared"] = _cleared
            log("glass: %d painted-highlight texels under the canopy pulled toward the cockpit tint (%d faces in the canopy box)" % (
                _cleared["texels"], _cleared["faces"]))
    except Exception as exc:  # noqa: BLE001
        log("glass highlight clean-up failed: %s" % str(exc)[:160])

def fit_cylinder(points):
    """Axis (unit), centre, radius, length and the relative residual of a cylinder through `points`."""
    c = points.mean(axis=0)
    q = points - c
    _u, _s, vt = np.linalg.svd(q, full_matrices=False)
    axis = vt[0]
    t = q @ axis
    radial = q - np.outer(t, axis)
    d = np.linalg.norm(radial, axis=1)
    r = float(np.median(d))
    resid = float(np.median(np.abs(d - r)) / max(r, 1e-6))
    t0, t1 = np.percentile(t, [1.5, 98.5])
    centre = c + axis * (t0 + t1) * 0.5
    return axis, centre, r, float(t1 - t0), resid


def make_tube(axis, centre, r, length, wall=0.18, segments=48):
    """A capped tube (outer wall, inner bore, end rings) as a new mesh object."""
    axis = axis / np.linalg.norm(axis)
    ref = np.array([0, 0, 1.0]) if abs(axis[2]) < 0.9 else np.array([1.0, 0, 0])
    u = np.cross(axis, ref); u /= np.linalg.norm(u)
    v = np.cross(axis, u)
    ri = r * (1 - wall)
    ang = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    ring = np.stack([np.cos(ang), np.sin(ang)], axis=1)
    verts = []
    for tval in (-length / 2, length / 2):
        base = centre + axis * tval
        for rad in (r, ri):
            for cx, cy in ring:
                verts.append(base + u * cx * rad + v * cy * rad)
    verts = np.array(verts)
    faces = []
    def idx(end, inner, k):
        return end * 2 * segments + inner * segments + (k % segments)
    for k in range(segments):
        faces.append([idx(0, 0, k), idx(0, 0, k + 1), idx(1, 0, k + 1), idx(1, 0, k)])         # outer wall
        faces.append([idx(1, 1, k), idx(1, 1, k + 1), idx(0, 1, k + 1), idx(0, 1, k)])         # bore (inward)
        faces.append([idx(0, 1, k), idx(0, 1, k + 1), idx(0, 0, k + 1), idx(0, 0, k)])         # end ring 0
        faces.append([idx(1, 0, k), idx(1, 0, k + 1), idx(1, 1, k + 1), idx(1, 1, k)])         # end ring 1
    me = bpy.data.meshes.new("Tube")
    me.from_pydata([tuple(map(float, p)) for p in verts], [], faces)
    me.update()
    for p in me.polygons:
        p.use_smooth = True
    o = bpy.data.objects.new("Tube", me)
    bpy.context.collection.objects.link(o)
    return o


PISTOL_WORDS = ("pistol", "handgun", "revolver", "sidearm", "glock", "beretta", "desert eagle", "colt python", "1911")


def repair_cylinder(obj, faces, spec_part, colours):
    """Replace the faces' wobbly cylinder with a true tube; shrink the old faces inside it."""
    faces_in = faces
    me = obj.data
    # Long guns only. A pistol's barrel is inside the slide; on the wave 18 Glock the tube landed through the trigger
    # guard and grip (3/10). The description names the class; the silhouette (length / height) confirms it.
    desc = ((args.get("spec") or {}).get("description") or "").lower() + " " + ((args.get("spec") or {}).get("name") or "").lower()
    lo0, hi0 = blib.dims(obj)
    aspect = (hi0.x - lo0.x) / max(hi0.z - lo0.z, 1e-6)
    is_pistol = any(w in desc for w in PISTOL_WORDS if w != "pistol") or re.search(r"pistol(?!\s*grip)", desc) is not None
    if is_pistol or aspect < 2.2:      # "pistol grip" on a carbine is not a pistol (M4A1 wave 19 skipped for that word)
        return {"skipped": "not a long gun (aspect %.1f): the barrel is inside the slide, nothing to replace" % aspect}
    n = len(me.polygons)
    cen = np.empty(n * 3, np.float32)
    me.polygons.foreach_get("center", cen)
    cen = cen.reshape(-1, 3)
    lo, hi = blib.dims(obj)
    diag = (hi - lo).length
    # The segmenter's "barrel" mask spills onto the forend, rail and muzzle device, so a fit over the whole mask
    # never passed (residual 0.41-0.55 on every rifle, 2026-09-17). Slice the masked faces along the weapon's axis
    # (+X) and keep the forward run of THIN slices: a barrel is the part whose cross-section stays small and round.
    pts_all = cen[faces]
    nb = 40
    edges = np.linspace(pts_all[:, 0].min(), pts_all[:, 0].max() + 1e-6, nb + 1)
    which = np.clip(np.searchsorted(edges, pts_all[:, 0], side="right") - 1, 0, nb - 1)
    rad = np.full(nb, np.nan)
    for b in range(nb):
        sel = which == b
        if sel.sum() >= 6:
            c_b = pts_all[sel][:, 1:].mean(axis=0)
            rad[b] = np.percentile(np.linalg.norm(pts_all[sel][:, 1:] - c_b, axis=1), 80)
    finite = rad[np.isfinite(rad)]
    thin_faces = faces
    if len(finite) >= 8:
        thin = np.isfinite(rad) & (rad < np.nanpercentile(finite, 30) * 1.5)
        # the longest run of thin slices that touches the front (+X) end, allowing one thick slice (sight, brake)
        run_end = nb - 1
        while run_end >= 0 and not thin[run_end]:
            run_end -= 1
        run_start, gaps = run_end, 0
        while run_start - 1 >= 0 and (thin[run_start - 1] or gaps < 1):
            gaps += 0 if thin[run_start - 1] else 1
            run_start -= 1
        if run_end - run_start + 1 >= 5:
            keep_b = np.zeros(nb, bool)
            keep_b[run_start:run_end + 1] = True
            sub = keep_b[which]
            if sub.sum() >= 50:
                idx_all = np.nonzero(faces)[0]
                thin_faces = np.zeros_like(faces)
                thin_faces[idx_all[sub]] = True
                log("cylinder repair: barrel = slices %d-%d of %d (radius %.4f vs mask %.4f), %d of %d faces" % (
                    run_start, run_end, nb, float(np.nanmedian(rad[keep_b])), float(np.nanmedian(finite)), int(sub.sum()), int(faces.sum())))
    faces = thin_faces
    isolated = int(faces.sum()) < int(faces_in.sum())
    if not isolated:
        # The mask never isolated a thin run (M4A1 wave 16/17: flash hider and rail sit at the front). Fall back to
        # the GEOMETRY: slice the whole weapon along +X from the muzzle end, skip the thick muzzle device (up to 12%
        # of the length), then take the run of thin slices - a barrel is the part whose cross-section is a small
        # fraction of the weapon's. Faces in those slices become the candidate; the fit and wander checks still rule.
        nb2 = 60
        e2 = np.linspace(lo.x, hi.x + 1e-6, nb2 + 1)
        w2 = np.clip(np.searchsorted(e2, cen[:, 0], side="right") - 1, 0, nb2 - 1)
        rad2 = np.full(nb2, np.nan)
        for b in range(nb2):
            sel = w2 == b
            if sel.sum() >= 12:
                c_b = cen[sel][:, 1:].mean(axis=0)
                rad2[b] = np.percentile(np.linalg.norm(cen[sel][:, 1:] - c_b, axis=1), 80)
        fin2 = rad2[np.isfinite(rad2)]
        if len(fin2) >= 20:
            r_med = float(np.nanmedian(fin2))
            thin2 = np.isfinite(rad2) & (rad2 < 0.4 * r_med)
            end = nb2 - 1
            skipped = 0
            while end >= 0 and (not thin2[end]) and skipped < int(0.12 * nb2):
                end -= 1; skipped += 1
            start = end
            while start - 1 >= 0 and thin2[start - 1]:
                start -= 1
            if thin2[end] and end - start + 1 >= int(0.08 * nb2):
                keep2 = np.zeros(nb2, bool); keep2[start:end + 1] = True
                cand = keep2[w2]
                if cand.sum() >= 50:
                    faces = cand
                    isolated = True
                    log("cylinder repair: geometry says the barrel is slices %d-%d of %d from the muzzle (radius %.4f vs weapon %.4f), %d faces" % (
                        start, end, nb2, float(np.nanmedian(rad2[keep2])), r_med, int(cand.sum())))
    # The muzzle device (flash hider, brake) is a defining detail and never a cylinder: the front 8% of the weapon
    # is left alone, and a repair may replace at most 25% of the length (the wave 18 M4A1 lost its A2 flash hider
    # to a plain tube, 4/10).
    front_cut = hi.x - 0.08 * (hi.x - lo.x)
    faces = faces & (cen[:, 0] <= front_cut)
    if faces.sum() < 50:
        return {"skipped": "nothing left to repair behind the muzzle device"}
    span = float(cen[faces][:, 0].max() - cen[faces][:, 0].min())
    if span > 0.25 * (hi.x - lo.x):
        return {"skipped": "the candidate spans %.0f%% of the weapon - a barrel is a short thin run, this is not one" % (100 * span / (hi.x - lo.x))}
    pts = cen[faces]
    axis, centre, r, length, resid = fit_cylinder(pts)
    # the mask carries neighbours (brake, sling loop, forend): trim what sits off the fitted radius and refit
    keep_pts = pts
    for _ in range(3):
        q = keep_pts - centre
        t = q @ axis
        d = np.linalg.norm(q - np.outer(t, axis), axis=1)
        inl = np.abs(d - r) < 0.3 * r
        if inl.sum() < 50 or inl.sum() < 0.5 * len(pts):
            break
        keep_pts = keep_pts[inl]
        axis, centre, r, length, resid = fit_cylinder(keep_pts)
    if len(keep_pts) < 0.5 * len(pts):
        return {"skipped": "only %d%% of the masked faces lie on one cylinder" % round(100 * len(keep_pts) / max(len(pts), 1))}
    if r < diag * 0.004 or length < 3 * r:
        return {"skipped": "not cylindrical: r %.4f length %.4f" % (r, length)}
    # A MELTED barrel is exactly what we are here to replace, so its cross-section residual is high by nature
    # (0.4-0.5 on every rifle). When the slice pass isolated a thin forward run, the test is straightness instead:
    # the slice centroids must sit on the fitted axis. Only an unsliced (spilled) mask keeps the strict residual.
    straight = None
    if isolated:
        cs = []
        t_all = (pts - centre) @ axis
        for lo_t, hi_t in zip(np.linspace(t_all.min(), t_all.max(), 9)[:-1], np.linspace(t_all.min(), t_all.max(), 9)[1:]):
            sel = (t_all >= lo_t) & (t_all < hi_t)
            if sel.sum() >= 5:
                cs.append(pts[sel].mean(axis=0))
        if len(cs) >= 4:
            cs = np.array(cs)
            off = cs - centre
            straight = float(np.max(np.linalg.norm(off - np.outer(off @ axis, axis), axis=1)) / max(r, 1e-6))
    if straight is not None:
        if straight > 2.0 or resid > 0.7:
            return {"skipped": "barrel run found but bent (axis wander %.2f r, residual %.2f)" % (straight, resid)}
    elif resid > 0.35:
        return {"skipped": "residual %.2f - the part is not a cylinder" % resid}
    # colour: the old barrel's average albedo, so the tube matches the rest
    cols = colours[faces]
    ok = ~np.isnan(cols).any(axis=1)
    colour = cols[ok].mean(axis=0) if ok.any() else np.array([0.12, 0.12, 0.13])
    # shrink the old faces toward the axis so the tube covers them
    vidx = set()
    loop_start = np.empty(n, np.int32); me.polygons.foreach_get("loop_start", loop_start)
    loop_total = np.empty(n, np.int32); me.polygons.foreach_get("loop_total", loop_total)
    lv = np.empty(len(me.loops), np.int32); me.loops.foreach_get("vertex_index", lv)
    for fi in np.nonzero(faces)[0]:
        vidx.update(lv[loop_start[fi]:loop_start[fi] + loop_total[fi]].tolist())
    vidx = np.array(sorted(vidx), np.int64)
    co = np.empty(len(me.vertices) * 3, np.float32); me.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
    q = co[vidx] - centre
    t = q @ axis
    radial = q - np.outer(t, axis)
    # a melted barrel wanders off the axis; pull its old faces well inside the tube so none poke through
    co[vidx] = centre + np.outer(t, axis) + radial * (0.6 if isolated else 0.9)
    me.vertices.foreach_set("co", co.ravel())
    me.update()
    tube = make_tube(axis, centre, r, length)
    mat = bpy.data.materials.new("MI_%s_Barrel" % NAME)
    mat.use_nodes = True
    bsdf = next(nd for nd in mat.node_tree.nodes if nd.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (float(colour[0]), float(colour[1]), float(colour[2]), 1.0)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.62
    tube.data.materials.append(mat)
    blib.select_only([obj, tube])
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    return {"radius": round(r, 4), "length": round(length, 4), "residual": round(resid, 3), "faces": int(faces.sum()),
            "axis_wander": None if straight is None else round(straight, 3),
            "colour": [round(float(c), 3) for c in colour]}


def fit_part(obj, faces, glb, name):
    """Import a part seed and fit it into the bounding box of the part's faces on the body (the cockpit path);
    the old faces are shrunk toward the box centre so the seed covers them."""
    me = obj.data
    n = len(me.polygons)
    cen = np.empty(n * 3, np.float32)
    me.polygons.foreach_get("center", cen)
    cen = cen.reshape(-1, 3)
    pts = main_cluster(cen[faces], max(blib.dims(obj)[1].length * 0.02, 1e-3))
    lo_p, hi_p = np.percentile(pts, 2, axis=0), np.percentile(pts, 98, axis=0)
    ext_p = hi_p - lo_p
    if ext_p.max() < 1e-4:
        return {"skipped": "degenerate box"}
    before = set(bpy.data.objects)
    if glb.lower().endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=os.path.abspath(glb))
    else:
        bpy.ops.import_scene.gltf(filepath=os.path.abspath(glb))
    new = [o for o in bpy.data.objects if o not in before]
    parts = [o for o in new if o.type == "MESH"]
    for o in parts:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in [o for o in new if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    if not parts:
        return {"skipped": "empty seed"}
    blib.select_only(parts)
    if len(parts) > 1:
        bpy.ops.object.join()
    p = bpy.context.view_layer.objects.active
    p.rotation_mode = "XYZ"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    plo, phi = blib.dims(p)
    pext = phi - plo
    # the part's long axis follows the box's long axis (horizontal)
    order_box = np.argsort(-ext_p[:2])
    order_part = np.argsort(-np.array([pext.x, pext.y]))
    if order_box[0] != order_part[0]:
        blib.apply_yaw(p, 90)
        plo, phi = blib.dims(p)
        pext = phi - plo
    sc = min(ext_p[0] / max(pext.x, 1e-6), ext_p[1] / max(pext.y, 1e-6), ext_p[2] / max(pext.z, 1e-6))
    p.scale = (sc, sc, sc)
    bpy.ops.object.transform_apply(scale=True)
    plo, phi = blib.dims(p)
    shift = Vector(((lo_p[0] + hi_p[0]) * 0.5 - (plo.x + phi.x) * 0.5, (lo_p[1] + hi_p[1]) * 0.5 - (plo.y + phi.y) * 0.5,
                    (lo_p[2] + hi_p[2]) * 0.5 - (plo.z + phi.z) * 0.5))
    p.location += shift
    bpy.ops.object.transform_apply(location=True)
    for slot in p.material_slots:
        if slot.material:
            slot.material.name = "MI_%s_%s" % (NAME, name)
    p.name = name
    # the part's slice of the budget, as for an added part: an optic seed joined to the 355k-face bullpup took the
    # whole 60k collapse and the body shredded (2026-09-25). Reduced on its own and marked so the LODs spare it.
    part_budget = max(int(int(args["tri_budget"]) * ADDED_PART_SHARE), 3000)
    part_tris = blib.tri_count(p)
    if part_tris > part_budget:
        mod = p.modifiers.new("dec_part", "DECIMATE")
        mod.ratio = part_budget / float(part_tris)
        mod.use_collapse_triangulate = True
        blib.select_only([p])
        bpy.ops.object.modifier_apply(modifier="dec_part")
    vg = p.vertex_groups.new(name=ADDED_GROUP)
    vg.add(list(range(len(p.data.vertices))), 1.0, "REPLACE")
    # old faces shrink toward the box centre
    loop_start = np.empty(n, np.int32); me.polygons.foreach_get("loop_start", loop_start)
    loop_total = np.empty(n, np.int32); me.polygons.foreach_get("loop_total", loop_total)
    lv = np.empty(len(me.loops), np.int32); me.loops.foreach_get("vertex_index", lv)
    vidx = set()
    for fi in np.nonzero(faces)[0]:
        vidx.update(lv[loop_start[fi]:loop_start[fi] + loop_total[fi]].tolist())
    vidx = np.array(sorted(vidx), np.int64)
    co = np.empty(len(me.vertices) * 3, np.float32); me.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
    c = (lo_p + hi_p) * 0.5
    co[vidx] = c + (co[vidx] - c) * 0.85
    me.vertices.foreach_set("co", co.ravel())
    me.update()
    blib.select_only([obj, p])
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    return {"scale": round(float(sc), 3), "box_m": [round(float(v), 3) for v in ext_p], "faces_replaced": int(faces.sum())}


ADDED_GROUP = "ms_added"          # vertex group marking added parts: the LOD decimation spares them
ADDED_PART_SHARE = 0.3            # of the triangle budget, at most, for ALL added parts together (four parts at 30%
                                  # each starved the Havoc's body down to 646 faces, 2026-09-24)
ADDED_MATERIALS = set()           # material names of the parts attached so far: not "body" for the next part's floor


def part_budgets(parts, budget):
    """Each part's slice of the added-parts share, by its longest dimension (a 2.3 m shell needs more triangles than a
    0.45 m pedal set), never under 3,000."""
    sizes = [max(float(p.get("size_m") or 0), 0.3) for p in parts]
    total = sum(sizes) or 1.0
    return [max(3000, int(budget * ADDED_PART_SHARE * s / total)) for s in sizes]


def protect_added(mod, o):
    """The decimate modifier spends its collapses on the body, not on an added part (vertex group weight 1 -> cost x10)."""
    if ADDED_GROUP in o.vertex_groups:
        mod.vertex_group = ADDED_GROUP
        mod.invert_vertex_group = True
        mod.vertex_group_factor = 10.0


def cavity_floor(obj, faces, cen, lo_p, hi_p, ext_p):
    """The floor under a glass anchor: the highest dense 0.2 m layer of body faces (not glass, not added parts) under
    the footprint, never deeper below the glass than the canopy is tall. -> (z, note) or (None, None)."""
    n = len(obj.data.polygons)
    mat_idx = np.empty(n, np.int32)
    obj.data.polygons.foreach_get("material_index", mat_idx)
    added_slots = [i for i, sl in enumerate(obj.material_slots) if sl.material and sl.material.name in ADDED_MATERIALS]
    not_added = ~np.isin(mat_idx, added_slots) if added_slots else np.ones(n, bool)
    body_sel = (~faces) & not_added & (cen[:, 0] > lo_p[0] + 0.1 * ext_p[0]) & (cen[:, 0] < hi_p[0] - 0.1 * ext_p[0]) \
        & (np.abs(cen[:, 1] - (lo_p[1] + hi_p[1]) * 0.5) < 0.4 * ext_p[1]) \
        & (cen[:, 2] < lo_p[2]) & (cen[:, 2] >= lo_p[2] - 1.0 * ext_p[2])
    if body_sel.sum() < 30:
        return None, None
    zs = cen[body_sel][:, 2]
    step = max(0.2, 0.08 * ext_p[2])
    edges = np.arange(zs.min(), lo_p[2] + step, step)
    counts, _ = np.histogram(zs, bins=edges)
    dense = [i for i, c in enumerate(counts) if c >= 0.15 * len(zs)]
    top_band = max(dense) if dense else int(np.argmax(counts))
    floor_z = max(float(edges[top_band + 1]), lo_p[2] - 0.6 * ext_p[2])
    return floor_z, "cavity floor %.2f m under a glass edge at %.2f m (%d body faces, densest layer)" % (floor_z, lo_p[2], int(body_sel.sum()))


def line_cavity(obj, glass, floor_z):
    """Enclose the cabin: the hull is one skin, so through the glass one looked past the seat into the fuselage void
    (the Havoc, 2026-09-24). Walls drop from the glass rim to the cavity floor and a floor caps them, all facing into
    the cabin, in a dark matte material. Deterministic; no vendor."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    # the weight layer first: adding a layer reallocates every vertex and kills the references taken before it
    # ("BMVert has been removed" on the Havoc, whose mesh had no vertex groups yet, 2026-09-24)
    deform = bm.verts.layers.deform.verify()
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    nf = len(bm.faces)
    g = np.zeros(nf, bool)
    g[:min(nf, len(glass))] = np.asarray(glass, bool)[:nf]
    rim = []
    for e in bm.edges:
        fs = e.link_faces
        if len(fs) == 2 and g[fs[0].index] != g[fs[1].index]:
            rim.append(e)
        elif len(fs) == 1 and g[fs[0].index]:
            rim.append(e)
    if len(rim) < 8:
        bm.free()
        return {"skipped": "no glass rim"}
    rim_verts = {v.index: v for e in rim for v in e.verts}
    xy = np.array([[v.co.x, v.co.y] for v in rim_verts.values()], np.float64)
    c = xy.mean(axis=0)
    zr = float(np.mean([v.co.z for v in rim_verts.values()]))
    if floor_z is None or floor_z >= zr - 0.05:
        floor_z = zr - 0.5
    mat = bpy.data.materials.new("MI_%s_CabinLining" % NAME)
    mat.use_nodes = True
    b = next(nd for nd in mat.node_tree.nodes if nd.type == "BSDF_PRINCIPLED")
    b.inputs["Base Color"].default_value = (0.05, 0.05, 0.055, 1.0)
    b.inputs["Roughness"].default_value = 0.8
    me.materials.append(mat)
    slot = len(me.materials) - 1
    vg = obj.vertex_groups.get(ADDED_GROUP) or obj.vertex_groups.new(name=ADDED_GROUP)
    below = {}
    for vi, v in rim_verts.items():
        nx, ny = c + (np.array([v.co.x, v.co.y]) - c) * 0.97      # a hair inside the rim: the wall stays in the hull
        nv = bm.verts.new((float(nx), float(ny), float(floor_z)))
        nv[deform][vg.index] = 1.0
        below[vi] = nv
    walls = []
    for e in rim:
        a, bb = e.verts
        try:
            walls.append(bm.faces.new((a, bb, below[bb.index], below[a.index])))
        except ValueError:
            pass
    newset = set(below.values())
    bottom = [e for e in bm.edges if e.verts[0] in newset and e.verts[1] in newset and len(e.link_faces) == 1]
    floor = []
    try:
        floor = list(bmesh.ops.holes_fill(bm, edges=bottom, sides=0).get("faces", []))
    except Exception:  # noqa: BLE001
        floor = []
    if not floor:
        try:
            floor = list(bmesh.ops.triangle_fill(bm, use_beauty=True, use_dissolve=False, edges=bottom).get("geom", []))
            floor = [f for f in floor if isinstance(f, bmesh.types.BMFace)]
        except Exception:  # noqa: BLE001
            floor = []
    bm.normal_update()
    cc = Vector((float(c[0]), float(c[1]), (zr + floor_z) * 0.5))
    for f in walls + floor:
        f.material_index = slot
        f.smooth = False
        if (cc - f.calc_center_median()).dot(f.normal) < 0:
            f.normal_flip()
    bm.to_mesh(me)
    bm.free()
    me.update()
    ADDED_MATERIALS.add(mat.name)
    return {"walls": len(walls), "floor_faces": len(floor), "rim_vertices": len(rim_verts), "rim_z_mean": round(zr, 3),
            "floor_z": round(float(floor_z), 3)}


def attach_part(obj, faces, glb, name, place="inside", size_m=0.0, yaw=0, blend=None, offset=(0.0, 0.0, 0.0), part_budget=None):
    """Import a separately seeded part and put it where the brief said, relative to the anchor faces' box: inside
    (scaled to fit the box), on_top / below (resting on the box's top / hanging under its bottom), in_front /
    behind (butted against its +X / -X end). size_m sets the part's longest dimension; 0 fits it to the box.
    The body is joined with the part; nothing on the body is removed or shrunk."""
    me = obj.data
    n = len(me.polygons)
    cen = np.empty(n * 3, np.float32)
    me.polygons.foreach_get("center", cen)
    cen = cen.reshape(-1, 3)
    if faces is None or faces.sum() < 3:
        lo_o, hi_o = blib.dims(obj)
        lo_p, hi_p = np.array(lo_o, np.float32), np.array(hi_o, np.float32)
    else:
        pts = main_cluster(cen[faces], max(blib.dims(obj)[1].length * 0.02, 1e-3))
        lo_p, hi_p = np.percentile(pts, 2, axis=0), np.percentile(pts, 98, axis=0)
    ext_p = hi_p - lo_p
    if ext_p.max() < 1e-4:
        return {"skipped": "degenerate anchor box"}
    # inside a canopy: the floor is the body's cavity floor under the glass footprint, not the glass's lower edge. The
    # Havoc's interior stood on the sill line with the cabin hollow for 0.9 m beneath it (2026-09-24). The floor is the
    # lowest body faces under the footprint, never deeper below the glass than the canopy is tall (a closed canopy has
    # no cavity: those faces would be the belly).
    floor_z, floor_note = None, None
    if place == "inside" and faces is not None and faces.sum() >= 3:
        floor_z, floor_note = cavity_floor(obj, faces, cen, lo_p, hi_p, ext_p)
    before = set(bpy.data.objects)
    prepared = bool(blend) and os.path.exists(blend)
    if prepared:
        # the part's own prepare pass already put its long axis on X, scaled it and centred it; the facing check
        # then said which end is the front (yaw). Appending keeps its materials and packed images.
        with bpy.data.libraries.load(os.path.abspath(blend), link=False) as (src, dst):
            dst.objects = list(src.objects)
        for o in dst.objects:
            if o is not None and o.type == "MESH":
                bpy.context.scene.collection.objects.link(o)
    elif glb.lower().endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=os.path.abspath(glb))
    else:
        bpy.ops.import_scene.gltf(filepath=os.path.abspath(glb))
    new = [o for o in bpy.data.objects if o not in before]
    parts = [o for o in new if o.type == "MESH"]
    for o in parts:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in [o for o in new if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    if not parts:
        return {"skipped": "empty seed"}
    blib.select_only(parts)
    if len(parts) > 1:
        bpy.ops.object.join()
    p = bpy.context.view_layer.objects.active
    p.rotation_mode = "XYZ"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    plo, phi = blib.dims(p)
    pext = phi - plo
    # the part's long axis follows the body's (+X), the same turn the prepare pass makes; then the facing check's yaw
    if not prepared and pext.y > pext.x * 1.15:
        blib.apply_yaw(p, -90)
    if yaw:
        blib.apply_yaw(p, yaw)
    plo, phi = blib.dims(p)
    pext = phi - plo
    if size_m and size_m > 0:
        sc = float(size_m) / max(max(pext.x, pext.y, pext.z), 1e-6)
    elif place == "inside":
        sc = min(ext_p[0] / max(pext.x, 1e-6), ext_p[1] / max(pext.y, 1e-6), ext_p[2] / max(pext.z, 1e-6))
    else:
        sc = min(ext_p[0] / max(pext.x, 1e-6), ext_p[1] / max(pext.y, 1e-6))        # as wide/long as the anchor
    p.scale = (sc, sc, sc)
    bpy.ops.object.transform_apply(scale=True)
    plo, phi = blib.dims(p)
    cx, cy, cz = (lo_p + hi_p) * 0.5
    pcx, pcy, pcz = (plo.x + phi.x) * 0.5, (plo.y + phi.y) * 0.5, (plo.z + phi.z) * 0.5
    if place == "inside":
        base = floor_z + 0.02 * ext_p[2] if floor_z is not None else lo_p[2]
        target = (cx, cy, base + (phi.z - plo.z) * 0.5)                    # standing on the cavity floor (else the glass edge)
    elif place == "on_top":
        target = (cx, cy, hi_p[2] + (phi.z - plo.z) * 0.5)
    elif place == "below":
        target = (cx, cy, lo_p[2] - (phi.z - plo.z) * 0.5)
    elif place == "in_front":
        target = (hi_p[0] + (phi.x - plo.x) * 0.5, cy, cz)
    else:  # behind
        target = (lo_p[0] - (phi.x - plo.x) * 0.5, cy, cz)
    ox, oy, oz = [float(v or 0) for v in (list(offset) + [0, 0, 0])[:3]]      # forward, left, up from the placement
    p.location += Vector((target[0] - pcx + ox, target[1] - pcy + oy, target[2] - pcz + oz))
    bpy.ops.object.transform_apply(location=True)
    for slot in p.material_slots:
        if slot.material:
            slot.material.name = "MI_%s_%s" % (NAME, name)
            ADDED_MATERIALS.add(slot.material.name)
    p.name = name
    # the part's share of the triangle budget: a 155k-triangle cockpit interior joined to a 280k body and decimated to
    # 120k as one mesh lost its joystick, throttles and harness (the Havoc, 2026-09-24). It is reduced on its own to
    # at most ADDED_PART_SHARE of the budget, and its vertices are marked so the LOD passes decimate the body instead.
    part_budget = int(part_budget or max(int(int(args["tri_budget"]) * ADDED_PART_SHARE), 3000))
    part_tris = blib.tri_count(p)
    if part_tris > part_budget:
        mod = p.modifiers.new("dec_part", "DECIMATE")
        mod.ratio = part_budget / float(part_tris)
        mod.use_collapse_triangulate = True
        blib.select_only([p])
        bpy.ops.object.modifier_apply(modifier="dec_part")
    vg = p.vertex_groups.new(name=ADDED_GROUP)
    vg.add(list(range(len(p.data.vertices))), 1.0, "REPLACE")
    faces_added = int(len(p.data.polygons))
    tris_kept = blib.tri_count(p)
    blib.select_only([obj, p])
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()                     # p is gone after this; read nothing from it
    return {"place": place, "yaw": yaw, "offset_m": [ox, oy, oz], "scale": round(float(sc), 3),
            "size_m": [round(float(v), 3) for v in (phi - plo)],
            "anchor_box_m": [round(float(v), 3) for v in ext_p], "faces_added": faces_added, "floor": floor_note,
            "triangles": {"seed": int(part_tris), "kept": int(tris_kept), "share_of_budget": int(part_budget)}}


if cyl_faces_each and args.get("repair_cylinders"):
    cols_for_cyl = face_colours(ob)
    for ci, f in sorted(cyl_faces_each.items()):
        part = (args["repair_cylinders"] or [{}])[ci] if ci < len(args["repair_cylinders"]) else {}
        try:
            res = repair_cylinder(ob, f, part, cols_for_cyl)
            report.setdefault("repairs", []).append({"part": part.get("phrase"), **res})
            log("cylinder repair %s: %s" % (part.get("phrase"), json.dumps(res)))
            report.setdefault("cylinders", []).append({"phrase": part.get("phrase"), **res})
            raw_tris = blib.tri_count(ob)
            n_now = len(ob.data.polygons)
            seed_faces_each = {k: np.concatenate([v, np.zeros(max(0, n_now - len(v)), bool)])[:n_now] for k, v in seed_faces_each.items()}
            cyl_faces_each = {k: np.concatenate([v, np.zeros(max(0, n_now - len(v)), bool)])[:n_now] for k, v in cyl_faces_each.items()}
        except Exception as exc:  # noqa: BLE001
            log("cylinder repair failed: %s" % str(exc)[:200])

_part_budgets = part_budgets(args.get("add_parts") or [], int(args["tri_budget"]))
_cabin_lined = False
for _pi, ap in enumerate(args.get("add_parts") or []):
    if ap.get("anchor") == "glass" and ap.get("place", "inside") == "inside" and glass_faces is not None \
            and glass_faces.sum() >= 3 and not _cabin_lined:
        _cabin_lined = True
        try:
            _n = len(ob.data.polygons)
            _cen = np.empty(_n * 3, np.float32)
            ob.data.polygons.foreach_get("center", _cen)
            _cen = _cen.reshape(-1, 3)
            _gf = np.concatenate([glass_faces, np.zeros(max(0, _n - len(glass_faces)), bool)])[:_n]
            _pts = main_cluster(_cen[_gf], max(blib.dims(ob)[1].length * 0.02, 1e-3))
            _lo, _hi = np.percentile(_pts, 2, axis=0), np.percentile(_pts, 98, axis=0)
            _fz, _ = cavity_floor(ob, _gf, _cen, _lo, _hi, _hi - _lo)
            res = line_cavity(ob, _gf, _fz)
            report["cabin_lining"] = res
            log("cabin lining: %s" % json.dumps(res))
            raw_tris = blib.tri_count(ob)
        except Exception as exc:  # noqa: BLE001 - the parts still go in
            import traceback as _tb
            log("cabin lining failed: %s | %s" % (str(exc)[:200], " / ".join(_tb.format_exc().strip().splitlines()[-4:])[:400]))
    # the anchor: glass faces, the whole body, or the faces under the anchor phrase's masks
    if ap.get("anchor") == "glass":
        f = glass_faces
    elif ap.get("anchor") == "body":
        f = None
    else:
        key = "anchor%d" % ap.get("index", -1)
        f = faces_under_masks(regions[key], min_votes=2 if len(regions[key]) >= 3 else 1)[0] if regions.get(key) else None
        if f is not None and f.sum() < 3:
            log("add %s: the anchor '%s' was not found on the renders; placing against the whole body" % (ap.get("name"), ap.get("anchor")))
            f = None
    if f is not None and len(f) != len(ob.data.polygons):
        f = np.concatenate([f, np.zeros(max(0, len(ob.data.polygons) - len(f)), bool)])[:len(ob.data.polygons)]
    if ap.get("place", "inside") == "inside" and ap.get("anchor") != "body" and (f is None or f.sum() < 3):
        log("WARNING: add %s skipped: interior anchor '%s' not found; refusing to fit against the whole body" %
            (ap.get("name"), ap.get("anchor")))
        continue
    if not os.path.exists(ap.get("glb") or "") and not os.path.exists(ap.get("blend") or ""):
        log("add %s: no seed mesh" % ap.get("name"))
        continue
    try:
        res = attach_part(ob, f, ap["glb"], ap.get("name", "Part"), ap.get("place", "inside"), float(ap.get("size_m") or 0),
                          yaw=int(ap.get("yaw") or 0), blend=ap.get("blend"), offset=ap.get("offset_m") or (0, 0, 0),
                          part_budget=_part_budgets[_pi])
        report.setdefault("added_parts", []).append({"name": ap.get("name"), "phrase": ap.get("phrase"), **res})
        log("added %s: %s" % (ap.get("name"), json.dumps(res)))
        raw_tris = blib.tri_count(ob)
    except Exception as exc:  # noqa: BLE001 - the body ships without the part
        log("add %s failed: %s" % (ap.get("name"), str(exc)[:200]))

for ps in (args.get("part_seeds") or []):
    f = seed_faces_each.get(ps.get("index"))
    if f is None or not os.path.exists(ps.get("glb", "")):
        log("part seed %s: no faces found on the body; not fitted" % ps.get("name"))
        continue
    n_now = len(ob.data.polygons)
    if len(f) != n_now:
        f = np.concatenate([f, np.zeros(max(0, n_now - len(f)), bool)])[:n_now]
    # a small attached part covers a small share of the body; a mask that swallowed the object is a spill
    lo_o, hi_o = blib.dims(ob)
    cen_o = np.empty(n_now * 3, np.float32); ob.data.polygons.foreach_get("center", cen_o); cen_o = cen_o.reshape(-1, 3)
    pts_o = main_cluster(cen_o[f], max((hi_o - lo_o).length * 0.02, 1e-3)) if f.sum() else np.zeros((0, 3))
    ext_o = (np.percentile(pts_o, 98, axis=0) - np.percentile(pts_o, 2, axis=0)) if len(pts_o) else np.zeros(3)
    if f.mean() > 0.3 or ext_o.max() > 0.5 * (hi_o - lo_o).length:
        log("part seed %s: its mask covers %.0f%% of the faces / %.0f%% of the length - a spill, not fitted" % (
            ps.get("name"), f.mean() * 100, ext_o.max() / max((hi_o - lo_o).length, 1e-6) * 100))
        continue
    try:
        res = fit_part(ob, f, ps["glb"], ps.get("name", "Part"))
        report.setdefault("part_seeds", []).append({"name": ps.get("name"), **res})
        log("part seed %s fitted: %s" % (ps.get("name"), json.dumps(res)))
        raw_tris = blib.tri_count(ob)
    except Exception as exc:  # noqa: BLE001
        log("part seed %s failed: %s" % (ps.get("name"), str(exc)[:200]))

# ---------------------------------------------------------------- LODs (material indices and face attributes survive decimation)
def decimate_copy(src, ratio, name, protect=True):
    """protect: spare the added parts (LOD0). The lower LODs shrink everything, or LOD2 cannot reach its count."""
    o = src.copy()
    o.data = src.data.copy()
    o.name = name
    o.data.name = name
    bpy.context.collection.objects.link(o)
    if ratio < 0.999:
        mod = o.modifiers.new("dec", "DECIMATE")
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        if protect:
            protect_added(mod, o)
        blib.select_only([o])
        bpy.ops.object.modifier_apply(modifier="dec")
    return o


# Use the same tangent basis for baking and delivery. Smoothing only AFTER the bake encodes flat
# triangle normals into a texture and then displays that texture on a smooth mesh.
if "smooth_organic_normals" in TEXTURE_FIXES:
    report.setdefault("normal_repairs", []).append(smooth_organic_normals(ob))
for polygon in ob.data.polygons:
    polygon.use_smooth = True
ob.data.update()
budget = int(args["tri_budget"])
lod0 = decimate_copy(ob, min(1.0, budget / float(max(raw_tris, 1))), "SM_%s_LOD0" % NAME)
# collapse decimation counts faces, and n-gons triangulate to more than one; and it refuses non-manifold edges, of which a
# mesh with merged coincident vertices can have tens of thousands (the decimation stalled at 45k for a 30k budget on the
# openrouter-only branch, 2026-09-19). Decimate again on the triangle count, splitting those edges when it stalls.
for _pass in range(3):
    have = blib.tri_count(lod0)
    if have <= budget * 1.03:
        break
    if _pass == 1 and have > budget * 1.2:
        bm = bmesh.new()
        bm.from_mesh(lod0.data)
        bad = [e for e in bm.edges if not e.is_manifold]
        if bad:
            bmesh.ops.split_edges(bm, edges=bad)
            bm.to_mesh(lod0.data)
            log("LOD0: %d non-manifold edge(s) split so the decimation can proceed" % len(bad))
        bm.free()
    mod = lod0.modifiers.new("dec2", "DECIMATE")
    mod.ratio = max(0.05, budget / float(have) * 0.98)
    mod.use_collapse_triangulate = True
    if _pass < 2:
        protect_added(mod, lod0)          # the last pass shrinks everything: the budget is a promise
    blib.select_only([lod0])
    bpy.ops.object.modifier_apply(modifier="dec2")
    log("LOD0 decimated again: %d -> %d triangles for a budget of %d" % (have, blib.tri_count(lod0), budget))


if "smooth_organic_normals" in TEXTURE_FIXES:
    report.setdefault("normal_repairs", []).append(smooth_organic_normals(lod0))

# the collapse leaves specks of its own (a dot under the bullpup's rail that no segmenter could target, 2026-09-25)
try:
    _lod0_specks = drop_floaties(lod0)
    if _lod0_specks:
        report["floaties_removed_lod0"] = _lod0_specks
except Exception as exc:  # noqa: BLE001 - a speck is not worth a failed build
    log("LOD0 floater pass skipped: %s" % str(exc)[:120])


def bake_detail(high, low):
    """Tangent normal + AO of the high-poly seed baked onto LOD0 (issue #1). The vendor's normal map is flat, so
    the serrations, stipple and panel lines that survive in the 600k-2M seed were lost at decimation. The AO
    also de-lights the albedo: Tripo bakes cavity shading into the colour, half of the 'ceramic' look."""
    if blib.tri_count(low) >= blib.tri_count(high) * 0.98:
        log("bake skipped: LOD0 keeps nearly all of the seed's triangles")
        return None
    mats = [s.material for s in low.material_slots if s.material and s.material.node_tree]
    if not mats:
        return None
    first = image_feeding(next(n for n in mats[0].node_tree.nodes if n.type == "BSDF_PRINCIPLED").inputs["Base Color"])[0]
    size = 4096 if (first is not None and first.image and max(first.image.size) >= 4096) else 2048
    lo, hi = blib.dims(low)
    diag = (hi - lo).length
    # LOD0 is a decimation of the same surface, so it sits within a hair of the high-poly: a tiny cage keeps the
    # rays off the far side of fins, blades and barrels (a 0.4% cage on a jet baked the wings inside-out, 2026-09-17)
    extr = max(diag * 0.0006, 1e-5)
    n_img = bpy.data.images.new("ms_bake_N", size, size, alpha=False, float_buffer=False)
    n_img.colorspace_settings.name = "Non-Color"
    ao_img = bpy.data.images.new("ms_bake_AO", size, size, alpha=False, float_buffer=False)
    ao_img.colorspace_settings.name = "Non-Color"
    cov_img = bpy.data.images.new("ms_bake_COV", size, size, alpha=False, float_buffer=False)
    cov_img.colorspace_settings.name = "Non-Color"
    temps = []
    for m in mats:
        t = m.node_tree.nodes.new("ShaderNodeTexImage")
        t.image = cov_img
        m.node_tree.nodes.active = t
        temps.append((m, t))
    scn = bpy.context.scene
    scn.render.engine = "CYCLES"
    scn.cycles.device = "CPU"
    blib.select_only([high, low])
    bpy.context.view_layer.objects.active = low
    high.hide_render = False
    low.hide_render = False
    bake_kw = dict(use_selected_to_active=True, cage_extrusion=extr, max_ray_distance=extr * 4, margin=2,
                   use_clear=True, target="IMAGE_TEXTURES")
    scn.cycles.samples = 1
    # pass 1: coverage - where a cage ray actually lands on the high-poly (a bake of "white emission")
    hi_emit = []
    for m in {s.material for s in high.material_slots if s.material and s.material.node_tree}:
        nt = m.node_tree
        outn = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None) or next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
        prev = outn.inputs["Surface"].links[0].from_socket if outn and outn.inputs["Surface"].is_linked else None
        e = nt.nodes.new("ShaderNodeEmission")
        e.inputs["Color"].default_value = (1, 1, 1, 1)
        for l in list(outn.inputs["Surface"].links):
            nt.links.remove(l)
        nt.links.new(e.outputs["Emission"], outn.inputs["Surface"])
        hi_emit.append((nt, outn, prev, e))
    bpy.ops.object.bake(type="EMIT", **bake_kw)
    for nt, outn, prev, e in hi_emit:
        for l in list(outn.inputs["Surface"].links):
            nt.links.remove(l)
        if prev is not None:
            nt.links.new(prev, outn.inputs["Surface"])
        nt.nodes.remove(e)
    # pass 2: tangent normals
    for m, t in temps:
        t.image = n_img
        m.node_tree.nodes.active = t
    bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT", **bake_kw)
    # pass 3: AO with a short reach - cavities and seams, not the underside of the wing
    for m, t in temps:
        t.image = ao_img
        m.node_tree.nodes.active = t
    scn.cycles.samples = 16
    scn.world.light_settings.distance = max(diag * 0.02, 0.005)
    # the low-poly target must not occlude the AO rays cast from the high-poly surface (whole islands went black)
    vis = {k: getattr(low, k) for k in ("visible_camera", "visible_diffuse", "visible_glossy", "visible_transmission", "visible_volume_scatter", "visible_shadow") if hasattr(low, k)}
    for k in vis:
        setattr(low, k, False)
    try:
        bpy.ops.object.bake(type="AO", **bake_kw)
    finally:
        for k, v in vis.items():
            setattr(low, k, v)
    for m, t in temps:
        m.node_tree.nodes.remove(t)
    npx = pixels(n_img)[:, :, :3]
    aopx = pixels(ao_img)[:, :, 0]
    covpx = pixels(cov_img)[:, :, 0]
    # a hit from the right side gives a normal that points mostly out of the surface (z high); back-face hits do not
    covered = (covpx > 0.5) & (npx[:, :, 2] > 0.6) & (aopx > 0.02)
    log("bake coverage %.0f%% of the atlas (cage %.4f)" % (covered.mean() * 100, extr))
    return {"N": npx, "AO": aopx, "covered": covered, "size": size, "images": (n_img, ao_img, cov_img)}


def whiteout(nv, nb):
    """Blend two tangent normal maps (both 0-1 encoded): detail on top of the vendor's."""
    a = nv * 2 - 1
    b = nb * 2 - 1
    n = np.stack([a[..., 0] + b[..., 0], a[..., 1] + b[..., 1], a[..., 2] * b[..., 2]], axis=2)
    n /= np.maximum(np.linalg.norm(n, axis=2, keepdims=True), 1e-6)
    return (n + 1) * 0.5


def apply_bake(low, baked):
    """Vendor normal <- whiteout(vendor, baked); ORM red <- baked AO; base colour de-lit by the AO."""
    stats = {}
    for slot in low.material_slots:
        m = slot.material
        if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME) or m.name.startswith("MI_%s_Cockpit" % NAME):
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf:
            continue
        # normal
        node, _c = image_feeding(bsdf.inputs["Normal"])
        if node is not None and node.image:
            px = pixels(node.image)
            h, w = px.shape[:2]
            nb = baked["N"] if baked["N"].shape[:2] == (h, w) else np.stack([resample_nearest(baked["N"][:, :, c], h, w) for c in range(3)], axis=2)
            cov = baked["covered"] if baked["covered"].shape == (h, w) else resample_nearest(baked["covered"], h, w)
            flat_vendor = float(np.abs(px[:, :, :3] - np.array([0.5, 0.5, 1.0])).mean()) < 0.03
            nb = nb.copy()
            nb[:, :, :2] = 0.5 + (nb[:, :, :2] - 0.5) * 0.7            # a decimated cage exaggerates slopes; keep 70%
            blended = nb if flat_vendor else whiteout(px[:, :, :3], nb)
            px[:, :, :3] = np.where(cov[..., None], blended, px[:, :, :3]).astype(np.float32)
            node.image.pixels.foreach_set(px.ravel())
            node.image.pack()
            node.image.update()
            stats["normal_detail_std"] = round(float(np.std(px[:, :, :2][cov])), 4) if cov.any() else 0.0
        else:
            img = bpy.data.images.new("ms_N_%s" % m.name, baked["size"], baked["size"], alpha=False)
            img.colorspace_settings.name = "Non-Color"
            arr = np.ones((baked["size"], baked["size"], 4), np.float32)
            arr[:, :, :3] = baked["N"]
            img.pixels.foreach_set(arr.ravel())
            img.pack()
            tex = m.node_tree.nodes.new("ShaderNodeTexImage")
            tex.image = img
            nm = m.node_tree.nodes.new("ShaderNodeNormalMap")
            m.node_tree.links.new(tex.outputs["Color"], nm.inputs["Color"])
            m.node_tree.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
            stats["normal_detail_std"] = round(float(np.std(baked["N"][:, :, :2][baked["covered"]])), 4)
        # AO into the ORM red channel (Tripo's is 1.0 everywhere) and albedo de-light
        node_r, ch_r = image_feeding(bsdf.inputs["Roughness"])
        if node_r is not None and node_r.image and ch_r in ("Green", "Blue"):
            px = pixels(node_r.image)
            h, w = px.shape[:2]
            ao = baked["AO"] if baked["AO"].shape == (h, w) else resample_nearest(baked["AO"], h, w)
            cov = baked["covered"] if baked["covered"].shape == (h, w) else resample_nearest(baked["covered"], h, w)
            px[:, :, 0] = np.where(cov, ao, px[:, :, 0]).astype(np.float32)
            node_r.image.pixels.foreach_set(px.ravel())
            node_r.image.pack()
            node_r.image.update()
            stats["ao_mean"] = round(float(ao[cov].mean()), 3) if cov.any() else None
        node_bc, _c = image_feeding(bsdf.inputs["Base Color"])
        if node_bc is not None and node_bc.image:
            px = pixels(node_bc.image)
            h, w = px.shape[:2]
            ao = baked["AO"] if baked["AO"].shape == (h, w) else resample_nearest(baked["AO"], h, w)
            cov = baked["covered"] if baked["covered"].shape == (h, w) else resample_nearest(baked["covered"], h, w)
            L0 = float((0.2126 * px[:, :, 0] + 0.7152 * px[:, :, 1] + 0.0722 * px[:, :, 2])[cov].mean()) if cov.any() else 0
            gain = 1.0 / np.clip(np.power(np.clip(ao, 0, 1), 0.5), 0.75, 1.0)         # cavities lifted a little, open faces untouched
            lifted = np.clip(px[:, :, :3] * gain[..., None], 0, 1)
            L1 = float((0.2126 * lifted[:, :, 0] + 0.7152 * lifted[:, :, 1] + 0.0722 * lifted[:, :, 2])[cov].mean()) if cov.any() else L0
            if L1 > 1e-4:
                lifted = np.clip(lifted * (L0 / L1), 0, 1)                                 # same overall tone as before
            px[:, :, :3] = np.where(cov[..., None], lifted, px[:, :, :3]).astype(np.float32)
            node_bc.image.pixels.foreach_set(px.ravel())
            node_bc.image.pack()
            node_bc.image.update()
            stats["delight_gain_mean"] = round(float(gain[cov].mean()), 3) if cov.any() else None
    return stats


_used_slots = {p.material_index for p in lod0.data.polygons}


def atlas_domains(obj, used_slots):
    """One entry per distinct base-colour IMAGE among the used materials. The finish's own materials (the glass slot,
    a repaired barrel's flat colour, a recoloured family) share the body's atlas and must not stop the bake: on the
    bullpup (2026-09-25) glass + barrel + body counted as three "domains" and no build ever got its normal map.
    Only a joined vendor part brings an atlas of its own, and it shows up here as a second image."""
    images, names = [], []
    for i in sorted(used_slots):
        if i >= len(obj.material_slots) or not obj.material_slots[i].material:
            continue
        m = obj.material_slots[i].material
        names.append(m.name)
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if m.node_tree else None
        node = image_feeding(bsdf.inputs["Base Color"])[0] if bsdf is not None else None
        if node is not None and getattr(node, "image", None) is not None and node.image.name not in images:
            images.append(node.image.name)
    return images or names[:1], names


_material_domains, _material_names = atlas_domains(lod0, _used_slots)
_bake_skip = detail_bake_skip_reason(_material_domains, "preserve_seed_maps" in TEXTURE_FIXES)
if args.get("bake_detail", True) and _bake_skip:
    report["bake"] = {"status": "skipped", "reason": _bake_skip, "material_domains": _material_domains, "materials": _material_names}
    log("detail bake skipped: " + _bake_skip + "; preserving source texture maps")
elif args.get("bake_detail", True):
    try:
        baked = bake_detail(ob, lod0)
        if baked:
            report["bake"] = apply_bake(lod0, baked)
            for img in baked["images"]:
                bpy.data.images.remove(img)
            log("baked high-poly normal + AO onto LOD0 at %d: %s" % (baked["size"], json.dumps(report["bake"])))
    except Exception as exc:  # noqa: BLE001
        log("detail bake failed: %s" % str(exc)[:200])

def harmonise_second_model(obj):
    """Issue #9: a second model (cockpit, attachment) is textured in its own run and its white balance drifts from
    the body's. Match its chroma (R/G and B/G ratios of the mean colour) to the body's, luminance untouched -
    a dark instrument panel stays dark, it just stops being a different colour temperature."""
    body = cock = None
    for slot in obj.material_slots:
        m = slot.material
        if not m or not m.node_tree:
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        node, _c = image_feeding(bsdf.inputs["Base Color"]) if bsdf else (None, None)
        if node is None or not node.image:
            continue
        if m.name.startswith("MI_%s_Cockpit" % NAME):
            cock = node.image
        elif not m.name.startswith("MI_%s_Glass" % NAME) and body is None:
            body = node.image
    if body is None or cock is None:
        return None
    bpx, cpx = pixels(body), pixels(cock)
    bm = bpx[:, :, :3].reshape(-1, 3).mean(axis=0)
    cm = cpx[:, :, :3].reshape(-1, 3).mean(axis=0)
    if bm[1] < 1e-3 or cm[1] < 1e-3:
        return None
    gain = np.array([(bm[0] / bm[1]) / (cm[0] / cm[1]), 1.0, (bm[2] / bm[1]) / (cm[2] / cm[1])], np.float32)
    gain = np.clip(gain, 0.8, 1.25)
    if np.abs(gain - 1).max() < 0.03:
        return {"gain": [round(float(g), 3) for g in gain], "applied": False}
    L0 = 0.2126 * cpx[:, :, 0] + 0.7152 * cpx[:, :, 1] + 0.0722 * cpx[:, :, 2]
    new = np.clip(cpx[:, :, :3] * gain[None, None, :], 0, 1)
    L1 = 0.2126 * new[:, :, 0] + 0.7152 * new[:, :, 1] + 0.0722 * new[:, :, 2]
    new = np.clip(new * (L0 / np.maximum(L1, 1e-4))[..., None], 0, 1)
    cpx[:, :, :3] = new.astype(np.float32)
    cock.pixels.foreach_set(cpx.ravel())
    cock.pack()
    cock.update()
    return {"gain": [round(float(g), 3) for g in gain], "applied": True}


if args.get("reproject") and args.get("reference") and "preserve_seed_maps" not in TEXTURE_FIXES:
    try:
        import reproject as reproject_mod
        rp = reproject_mod.run_with_work(lod0, probe, decision, args["reference"], PROBE_TO_NOW, WORK, log)
        if rp:
            report["reproject"] = rp
            log("reprojected the reference onto the mesh: %s" % json.dumps(rp))
    except Exception as exc:  # noqa: BLE001
        import traceback
        log("reprojection failed: %s" % str(exc)[:200])
        print(traceback.format_exc()[-1500:])

try:
    h = harmonise_second_model(lod0)
    if h:
        report["harmonise"] = h
        log("cockpit colour balance matched to the body: gain %s%s" % (h["gain"], "" if h["applied"] else " (already close)"))
except Exception as exc:  # noqa: BLE001
    log("colour harmonisation skipped: %s" % str(exc)[:120])

export_maps(lod0)
lod1 = decimate_copy(lod0, 0.5, "SM_%s_LOD1" % NAME, protect=False)
lod2 = decimate_copy(lod1, 0.5, "SM_%s_LOD2" % NAME, protect=False)
bpy.data.objects.remove(ob, do_unlink=True)
for i, o in enumerate((lod0, lod1, lod2)):
    if i > 0 and "smooth_organic_normals" in TEXTURE_FIXES:
        report.setdefault("normal_repairs", []).append(smooth_organic_normals(o))
    for p in o.data.polygons:
        p.use_smooth = True
    report["lods"].append({"lod": i, "triangles": blib.tri_count(o)})
log("LODs: %s" % ", ".join(format(l["triangles"], ",") for l in report["lods"]))

# ---------------------------------------------------------------- collision: convex hull of extreme points (UE UCX_ convention)
def extreme_points(o, directions=48):
    n = len(o.data.vertices)
    co = np.empty(n * 3, np.float32)
    o.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    i = np.arange(directions) + 0.5
    phi = np.arccos(1 - 2 * i / directions)
    theta = math.pi * (1 + 5 ** 0.5) * i
    dirs = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)
    proj = co @ dirs.T
    idx = np.unique(np.concatenate([proj.argmax(axis=0), proj.argmin(axis=0)]))
    return co[idx]


hull = bpy.data.objects.new("UCX_SM_%s_01" % NAME, bpy.data.meshes.new("UCX_SM_%s_01" % NAME))
bpy.context.collection.objects.link(hull)
bm = bmesh.new()
for pnt in extreme_points(lod2):
    bm.verts.new(pnt.tolist())
bm.verts.ensure_lookup_table()
bmesh.ops.convex_hull(bm, input=bm.verts)
bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
bmesh.ops.triangulate(bm, faces=bm.faces)
bm.to_mesh(hull.data)
bm.free()
report["collision"] = {"type": "convex", "triangles": blib.tri_count(hull)}

# ---------------------------------------------------------------- sockets (weapons): attach points a game needs
def weapon_sockets(o):
    """Muzzle at the +X end of the barrel, grip under the receiver, sight on top: centroids of the
    vertices in those regions. Positions in metres, object space (origin at the body centre)."""
    n = len(o.data.vertices)
    co = np.empty(n * 3, np.float32)
    o.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    lo, hi = co.min(axis=0), co.max(axis=0)
    ext = hi - lo
    out = {}
    tip = co[co[:, 0] >= hi[0] - 0.03 * ext[0]]
    if len(tip):
        out["Muzzle"] = tip.mean(axis=0)
    body_x = (co[:, 0] > lo[0] + 0.25 * ext[0]) & (co[:, 0] < lo[0] + 0.7 * ext[0])
    grip = co[body_x & (co[:, 2] <= lo[2] + 0.2 * ext[2])]
    if len(grip):
        g = grip.mean(axis=0)
        out["Grip"] = np.array([g[0], g[1], lo[2] + 0.1 * ext[2]])
    top = co[body_x & (co[:, 2] >= hi[2] - 0.08 * ext[2])]
    if len(top):
        out["Sight"] = top.mean(axis=0)
    return {k: [round(float(x), 4) for x in v] for k, v in out.items()}


MELEE = ("sword", "blade", "axe", "knife", "dagger", "mace", "hammer", "spear", "katana", "club", "staff", "bat", "machete")
spec_cat = (args.get("spec") or {}).get("category")
sk_objects = []
if spec_cat == "weapon":
    sockets = weapon_sockets(lod0)
    desc = ((args.get("spec") or {}).get("description") or "").lower() + " " + NAME.lower()
    if any(w in desc for w in MELEE):
        # a blade has a tip, a grip and a guard, not a muzzle and a sight
        sockets = {{"Muzzle": "Tip", "Grip": "Grip", "Sight": "Guard"}[k]: v for k, v in sockets.items()}
    report["sockets"] = [{"name": k, "position_m": v, "forward": "+X"} for k, v in sockets.items()]
    log("sockets: %s" % ", ".join(sockets))
    if (args.get("spec") or {}).get("rig") and sockets:
        # a skeletal version: root + one bone per socket, the whole mesh bound to root, so the engine can
        # attach effects and hands to named bones without editing the asset
        arm_data = bpy.data.armatures.new("SK_%s_Skeleton" % NAME)
        arm = bpy.data.objects.new("SK_" + NAME, arm_data)
        bpy.context.collection.objects.link(arm)
        blib.select_only([arm])
        bpy.ops.object.mode_set(mode="EDIT")
        root = arm_data.edit_bones.new("root")
        root.head, root.tail = (0, 0, 0), (0, 0, 0.05)
        for k, v in sockets.items():
            b = arm_data.edit_bones.new(k)
            b.head = Vector(v)
            b.tail = Vector(v) + Vector((0.05, 0, 0))
            b.parent = root
        bpy.ops.object.mode_set(mode="OBJECT")
        sk_mesh = lod0.copy()
        sk_mesh.data = lod0.data.copy()
        sk_mesh.name = "SK_%s_Mesh" % NAME
        bpy.context.collection.objects.link(sk_mesh)
        sk_mesh.parent = arm
        vg = sk_mesh.vertex_groups.new(name="root")
        vg.add(list(range(len(sk_mesh.data.vertices))), 1.0, "REPLACE")
        mod = sk_mesh.modifiers.new("Armature", "ARMATURE")
        mod.object = arm
        sk_objects = [arm, sk_mesh]
        report["skeleton"] = {"bones": ["root"] + list(sockets)}

# ---------------------------------------------------------------- previews (LOD0)
blib.setup_render(int(args.get("render_size", 768)), 48, look="preview")
stage = blib.Stage(lod0, extra_hidden=[hull] + sk_objects)
report["renders"] = [stage.render(v, os.path.join(OUT, "preview_%s.png" % v))["file"] for v in ("iso", "side", "front")]
stage.close()

# Havoc's 2 m cockpit disappeared in the 12.5 m aircraft's review thumbnails. Review the delivered LOD0
# close-up, with its real glass/hull intact: occlusion must be reported as unverified, not hidden for a pass.
if (args.get("spec") or {}).get("add_parts") or (args.get("spec") or {}).get("cockpit") or args.get("add_parts"):
    try:
        slots = {i for i, slot in enumerate(lod0.material_slots) if slot.material and (
            slot.material.name in ADDED_MATERIALS or slot.material.name.startswith("MI_%s_Cockpit" % NAME))}
        indices = {vi for poly in lod0.data.polygons if poly.material_index in slots for vi in poly.vertices}
        if not indices:
            log("WARNING: no requested assembly geometry remains on LOD0 for focused review")
        else:
            points = [lod0.matrix_world @ lod0.data.vertices[i].co for i in indices]
            lo = Vector(tuple(min(p[i] for p in points) for i in range(3)))
            hi = Vector(tuple(max(p[i] for p in points) for i in range(3)))
            margin = max((hi - lo).length * 0.08, 0.02)
            pad = Vector((margin, margin, margin))
            focus = blib.Stage(lod0, extra_hidden=[hull] + sk_objects, focus_bounds=(lo - pad, hi + pad))
            try:
                report["review_renders"] = [focus.render(v, os.path.join(OUT, "preview_assembly_%s.png" % v))["file"]
                                           for v in ("iso", "top")]
                report["renders"].extend(report["review_renders"])
            finally:
                focus.close()
            # Diagnostic view only: glass is removed from a disposable copy, never the exported asset.
            inspect = lod0.copy()
            inspect.data = lod0.data.copy()
            bpy.context.collection.objects.link(inspect)
            try:
                glass_slots = {i for i, s in enumerate(inspect.material_slots)
                               if s.material and s.material.name.startswith("MI_%s_Glass" % NAME)}
                bm = bmesh.new()
                try:
                    bm.from_mesh(inspect.data)
                    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index in glass_slots], context="FACES")
                    bm.to_mesh(inspect.data)
                finally:
                    bm.free()
                inspection_stage = blib.Stage(inspect, focus_bounds=(lo - pad, hi + pad))
                try:
                    report["inspection_renders"] = [inspection_stage.render(
                        "top", os.path.join(OUT, "preview_inspection_canopy_hidden.png"))["file"]]
                finally:
                    inspection_stage.close()
            finally:
                inspect_mesh = inspect.data
                bpy.data.objects.remove(inspect, do_unlink=True)
                bpy.data.meshes.remove(inspect_mesh)
    except Exception as exc:  # a missing review view blocks acceptance, not delivery of the files
        log("WARNING: focused assembly renders failed: %s" % str(exc)[:200])

# ---------------------------------------------------------------- exports
fbx_kw = dict(use_selection=True, apply_unit_scale=True, apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z",
              axis_up="Y", mesh_smooth_type="FACE", use_mesh_modifiers=True, path_mode="STRIP", embed_textures=False,
              add_leaf_bones=False, bake_anim=False)
lod0.name = "SM_" + NAME
blib.select_only([lod0, hull])
p = os.path.join(OUT, "SM_%s.fbx" % NAME)
bpy.ops.export_scene.fbx(filepath=p, **fbx_kw)
report["files"].append(os.path.basename(p))
for o in (lod1, lod2):
    blib.select_only([o])
    p = os.path.join(OUT, o.name + ".fbx")
    bpy.ops.export_scene.fbx(filepath=p, **fbx_kw)
    report["files"].append(os.path.basename(p))
if sk_objects:
    blib.select_only(sk_objects)
    p = os.path.join(OUT, "SK_%s.fbx" % NAME)
    bpy.ops.export_scene.fbx(filepath=p, use_selection=True, object_types={"ARMATURE", "MESH"}, apply_unit_scale=True,
                             apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z", axis_up="Y", mesh_smooth_type="FACE",
                             use_mesh_modifiers=False, path_mode="STRIP", embed_textures=False, add_leaf_bones=False,
                             bake_anim=False, use_armature_deform_only=True)
    report["files"].append(os.path.basename(p))
    for o in sk_objects:
        bpy.data.objects.remove(o, do_unlink=True)
blib.select_only([lod0])
p = os.path.join(OUT, "SM_%s.glb" % NAME)
bpy.ops.export_scene.gltf(filepath=p, use_selection=True, export_format="GLB", export_yup=True)
report["files"].append(os.path.basename(p))
if args.get("spec"):
    txt = bpy.data.texts.get("ms_spec.json") or bpy.data.texts.new("ms_spec.json")
    txt.clear()
    txt.write(json.dumps(args["spec"]))
if args.get("reference") and os.path.exists(args["reference"]):
    # the picture the mesh was built from rides along, so a later re-finish can still be reviewed against it
    ref_img = bpy.data.images.load(os.path.abspath(args["reference"]))
    ref_img.name = "ms_reference"
    ref_img.pack()
    ref_img.use_fake_user = True      # an image nobody uses is orphan data and would be dropped on save
p = os.path.join(OUT, "SM_%s.blend" % NAME)
bpy.ops.wm.save_as_mainfile(filepath=p, compress=True)
report["files"].append(os.path.basename(p))
report["files"] += [m["file"] for m in report["maps"]]
report["engine"] = args["engine"]
report["materials"] = [m.name for m in lod0.data.materials if m]
with open(os.path.join(OUT, "report.json"), "w") as f:
    json.dump(report, f, indent=1)
log("done")
