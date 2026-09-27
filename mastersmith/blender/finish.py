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

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
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

glass_faces, panes = None, []
if regions.get("glass"):
    glass_faces, panes = faces_under_masks(regions["glass"], allow_panes=True)
    before_n = int(glass_faces.sum())
    glass_faces = smooth_face_selection(ob.data, glass_faces, rounds=2)
    log("glass: %d faces marked, %d pane(s) to build, from %d view(s); boundary smoothed %d -> %d faces" % (
        before_n, len(panes), len(regions["glass"]), before_n, int(glass_faces.sum())))
wheel_faces = None
if regions.get("wheel"):
    wheel_faces, _ = faces_under_masks(regions["wheel"])
    log("wheels: %d faces selected from %d view(s)" % (int(wheel_faces.sum()), len(regions["wheel"])))


# glass gets its own material slot; the base material keeps the vendor maps
if (glass_faces is not None and glass_faces.sum() > 20) or panes:
    glass = bpy.data.materials.new("MI_%s_Glass" % NAME)
    glass.use_nodes = True
    bsdf = next(n for n in glass.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    # One glass look that survives every viewer (2026-09-18, checked in Google's model-viewer, the forge's viewer):
    # a plain dark tint with alpha blending and NO transmission extension. Transmission + 25% alpha made canopies
    # vanish (the cockpit tub looked uncovered) and the vehicle variant's texture mix exported the BODY atlas onto the
    # windows (opaque white panes). Unreal's FBX path ignores both anyway; the README names the material for an
    # opacity setup. A canopy over the seed's own cockpit is clearer than a car window over a hollow shell.
    _cat = (args.get("spec") or {}).get("category")
    _dark = _cat == "vehicle"
    _alpha = 0.7 if _dark else 0.2                    # a canopy is clear (F-16 wave 19/20: "opaque and dark")
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
        log("glass: built %d pane(s) into empty frame(s)" % built)
    report["glass"] = {"faces": marked, "panes": len(panes), "material": glass.name}

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


def material_pass(found, mat, profile):
    """Tripo's PBR atlas is a soft painting: a flat roughness and a metallic mask speckled per UV island. A flat 0.6
    roughness on a dark grey reads as plastic. This rebuilds roughness per material class (gunmetal vs matte
    dielectric) with variation from the vendor map and the colour detail, and despeckles the metallic mask. The base
    colour is the seed's and is not touched (2026-09-26: no tone pull, no de-light). Deterministic; no vendor call."""
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
    # ---- base colour luminance: its high-pass is the colour detail the rebuilt roughness follows
    L = None
    if "BC" in found:
        px_bc = pixels(found["BC"][0].image)
        L = 0.2126 * px_bc[:, :, 0] + 0.7152 * px_bc[:, :, 1] + 0.0722 * px_bc[:, :, 2]
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


def resample_nearest(a, h, w):
    ys = (np.arange(h) * a.shape[0] / h).astype(np.int64)
    xs = (np.arange(w) * a.shape[1] / w).astype(np.int64)
    return a[ys][:, xs]


MATERIAL_PROFILES = {   # per category: roughness targets for metal / dielectric, how much of the vendor's metal is kept
    # satin gunmetal, not polished: a Glock's nDLC slide and a parkerized barrel sit near 0.6 (reviewer, wave 7)
    "weapon": {"roughness_metal": 0.58, "roughness_dielectric": 0.75, "metallic_scale": 1.0},
    "vehicle": {"roughness_metal": 0.45, "roughness_dielectric": 0.35, "metallic_scale": 0.3},   # car paint has a clearcoat: 0.55 read as matte plastic
    "aircraft": {"roughness_metal": 0.45, "roughness_dielectric": 0.5, "metallic_scale": 0.2},
    "helicopter": {"roughness_metal": 0.45, "roughness_dielectric": 0.55, "metallic_scale": 0.25},
    "prop": {"roughness_metal": 0.5, "roughness_dielectric": 0.7, "metallic_scale": 0.8},
    "environment": {"roughness_metal": 0.55, "roughness_dielectric": 0.8, "metallic_scale": 0.5},
}
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
        # clearcoat for cars, matte for military paint: a Humvee at 0.35 read "glossy plasticky" (2026-09-18)
        _d = ((spec_d.get("description") or "") + " " + (spec_d.get("name") or "")).lower()
        if any(w in _d for w in ("military", "matte", "desert tan", "olive", "camouflage", "camo", "armoured", "armored",
                                 "tank ", "humvee", "hmmwv", "apc", "utility vehicle", "truck", "pickup")):
            profile = {**profile, "roughness_dielectric": 0.62, "roughness_metal": 0.5}
        elif any(w in _d for w in ("gloss", "metallic paint", "sports car", "muscle car", "sedan", "supercar", "showroom")):
            profile = {**profile, "roughness_dielectric": 0.28}
    # a seed with colour but no roughness / metallic maps (Hi3D v3 ships BC + N only) gets a flat ORM-style map so the
    # material pass has something to write into and the delivery has an ORM
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
    if profile and spec_d.get("style", "realistic") == "realistic":
        stats = material_pass(found, m, profile)
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
    # body material hit it - a second vendor material, a re-finished asset (2026-09-22).
    if not m.name.startswith("MI_%s" % NAME):
        m.name = "MI_" + NAME


def export_maps(obj):
    """T_<Name>[_<Part>]_<BC|N|ORM|E>.png from the materials' images - after every edit (material pass, bake): the
    files used to be written mid-loop and missed the later edits (2026-09-17)."""
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
        # every material gets its own file set: the body is MI_<Name>, another MI_<Name>_<Part> keeps its name; anything
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

# ---------------------------------------------------------------- LODs (material indices and face attributes survive decimation)
def decimate_copy(src, ratio, name):
    o = src.copy()
    o.data = src.data.copy()
    o.name = name
    o.data.name = name
    bpy.context.collection.objects.link(o)
    if ratio < 0.999:
        mod = o.modifiers.new("dec", "DECIMATE")
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        blib.select_only([o])
        bpy.ops.object.modifier_apply(modifier="dec")
    return o


# Use the same tangent basis for baking and delivery. Smoothing only AFTER the bake encodes flat
# triangle normals into a texture and then displays that texture on a smooth mesh.
for polygon in ob.data.polygons:
    polygon.use_smooth = True
ob.data.update()
budget = int(args["tri_budget"])
raw_tris = blib.tri_count(ob)     # the finished body with its glass panes, before LOD0
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
    blib.select_only([lod0])
    bpy.ops.object.modifier_apply(modifier="dec2")
    log("LOD0 decimated again: %d -> %d triangles for a budget of %d" % (have, blib.tri_count(lod0), budget))


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
        if not m or not m.node_tree or m.name.startswith("MI_%s_Glass" % NAME):
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
    """One entry per distinct base-colour IMAGE among the used materials. The finish's own material (the glass slot)
    shares the body's atlas and must not stop the bake: on the bullpup (2026-09-25) the finish's slots and the body
    counted as three "domains" and no build ever got its normal map.
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
_bake_skip = detail_bake_skip_reason(_material_domains)
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

export_maps(lod0)
lod1 = decimate_copy(lod0, 0.5, "SM_%s_LOD1" % NAME)
lod2 = decimate_copy(lod1, 0.5, "SM_%s_LOD2" % NAME)
bpy.data.objects.remove(ob, do_unlink=True)
for i, o in enumerate((lod0, lod1, lod2)):
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
