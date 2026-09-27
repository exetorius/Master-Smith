"""Assemble planned parts into one game-ready asset (inside Blender). No repairs: every part arrives built and
checked; this only places, budgets, bakes and packages.
    blender -b -Y --python assemble.py -- <args.json>
args: {"name", "out_dir", "tri_budget", "engine", "atlas_size", "render_size", "spec", "reference",
       "parts": [{"name", "kind": "code"|"vendor", "glb" | "blend", "yaw", "box_min": [x,y,z], "box_max": [x,y,z],
                  "material": {"color", "metal", "roughness", "glass"}}]}
Frame: +X forward, +Y left, +Z up, metres, origin at the centre of the whole asset."""
import json
import math
import os
import sys

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blib  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
NAME = args["name"]
OUT = os.path.abspath(args["out_dir"])
os.makedirs(OUT, exist_ok=True)
report = {"name": NAME, "notes": [], "files": [], "maps": [], "lods": [], "parts": []}


def log(msg):
    print("[assemble] " + msg, flush=True)
    report["notes"].append(msg)


def import_part(p):
    before = set(bpy.data.objects)
    if p.get("blend"):
        with bpy.data.libraries.load(os.path.abspath(p["blend"]), link=False) as (src, dst):
            dst.objects = [n for n in src.objects]
        for o in dst.objects:
            if o is not None and o.type == "MESH":
                bpy.context.collection.objects.link(o)
    else:
        bpy.ops.import_scene.gltf(filepath=os.path.abspath(p["glb"]))
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in [o for o in new if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    if not meshes:
        raise RuntimeError("part %s has no mesh" % p["name"])
    blib.select_only(meshes)
    if len(meshes) > 1:
        bpy.ops.object.join()
    o = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    o.name = "Part_" + p["name"]
    return o


def fit(o, p):
    """Code parts were built in their box's own frame: moved to the box centre. Vendor parts arrive at their own size,
    long axis along X and upright: turned by the facing yaw, then scaled to fill the box on every side (within 1.8x of
    the median scale) and centred."""
    bmin, bmax = Vector(p["box_min"]), Vector(p["box_max"])
    centre, size = (bmin + bmax) * 0.5, bmax - bmin
    if p["kind"] == "code":
        g = float(p.get("cover") or 1.0)          # grown a little over the vendor body's soft copy of this part
        if g != 1.0:
            o.data.transform(Matrix.Diagonal(Vector((g, g, g, 1.0))))
        o.data.transform(Matrix.Translation(centre))
        return {"scale": g}
    if p.get("yaw"):
        o.data.transform(Matrix.Rotation(math.radians(float(p["yaw"])), 4, "Z"))
    lo, hi = blib.dims(o)
    ext = hi - lo
    ratios = [size[i] / max(ext[i], 1e-9) for i in range(3)]
    # the box was measured from the same picture the part was drawn from: the part FILLS it on every side, so it meets
    # its neighbours. A uniform fit left the pistol's grip and the shotgun's stock short of the frame and receiver
    # (2026-09-27). A part more than 1.8x out of proportion with its box is kept in proportion on that axis instead.
    s = sorted(ratios)[1]
    scale = [r if r / s <= 1.8 and s / r <= 1.8 else s for r in ratios]
    o.data.transform(Matrix.Translation(-(lo + hi) * 0.5))
    o.data.transform(Matrix.Diagonal(Vector(scale).to_4d()))
    o.data.transform(Matrix.Translation(centre))
    return {"scale": [round(v, 4) for v in scale], "proportion": [round(v / s, 3) for v in scale]}


def decimate_to(o, target):
    have = blib.tri_count(o)
    if have <= target:
        return have
    m = o.modifiers.new("dec", "DECIMATE")
    m.ratio = max(0.02, target / float(have))
    m.use_collapse_triangulate = True
    blib.select_only([o])
    bpy.ops.object.modifier_apply(modifier="dec")
    return blib.tri_count(o)


def surface_area(o):
    return sum(p.area for p in o.data.polygons)


def add_reference_detail(o, det):
    """Project the reference's fine detail (a grey high-pass map, 0.5 = none) onto a code part's materials, from the
    side picture on faces that look sideways and the front picture on faces that look forward: it darkens and lightens
    the base colour and drives a bump, so the colour and normal bakes both carry it. Parts sit in the asset frame
    (identity transforms), the frame the plan's pictures were cropped to."""
    L_, W_, H_ = (float(v) for v in det["dims"])
    s = float(det.get("strength", 0.6))
    images = {}
    for view in ("side", "front"):
        if det.get(view) and os.path.exists(det[view]):
            img = bpy.data.images.load(os.path.abspath(det[view]), check_existing=True)
            img.colorspace_settings.name = "Non-Color"
            images[view] = img
    if "side" not in images:
        return False
    for m in {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}:
        t = m.node_tree
        b = next((n for n in t.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if b is None:
            continue
        N, K = t.nodes, t.links

        def op(kind, a, c=None):
            n = N.new("ShaderNodeMath")
            n.operation = kind
            for i, v in enumerate((a, c)):
                if v is None:
                    continue
                if isinstance(v, (int, float)):
                    n.inputs[i].default_value = float(v)
                else:
                    K.new(v, n.inputs[i])
            return n.outputs[0]
        geo = N.new("ShaderNodeNewGeometry")
        pos = N.new("ShaderNodeSeparateXYZ")
        nrm = N.new("ShaderNodeSeparateXYZ")
        K.new(geo.outputs["Position"], pos.inputs[0])
        K.new(geo.outputs["Normal"], nrm.inputs[0])

        def look(img, u_sock, u_len):
            uv = N.new("ShaderNodeCombineXYZ")
            K.new(op("ADD", op("DIVIDE", u_sock, u_len), 0.5), uv.inputs[0])
            K.new(op("ADD", op("DIVIDE", pos.outputs["Z"], H_), 0.5), uv.inputs[1])
            tex = N.new("ShaderNodeTexImage")
            tex.image = img
            tex.extension = "EXTEND"
            K.new(uv.outputs[0], tex.inputs["Vector"])
            return op("SUBTRACT", tex.outputs["Color"], 0.5)
        term = op("MULTIPLY", look(images["side"], pos.outputs["X"], L_), op("MULTIPLY", nrm.outputs["Y"], nrm.outputs["Y"]))
        if "front" in images:
            term = op("ADD", term, op("MULTIPLY", look(images["front"], pos.outputs["Y"], W_),
                                      op("MULTIPLY", nrm.outputs["X"], nrm.outputs["X"])))
        base = b.inputs["Base Color"]
        if base.is_linked:
            src = base.links[0].from_socket
        else:
            rgb = N.new("ShaderNodeRGB")
            rgb.outputs[0].default_value = tuple(base.default_value)
            src = rgb.outputs[0]
        scale = N.new("ShaderNodeVectorMath")
        scale.operation = "SCALE"
        K.new(src, scale.inputs[0])
        K.new(op("ADD", op("MULTIPLY", term, 2.0 * s), 1.0), scale.inputs["Scale"])
        K.new(scale.outputs["Vector"], base)
        if not b.inputs["Normal"].is_linked:
            bump = N.new("ShaderNodeBump")
            bump.inputs["Strength"].default_value = min(1.0, 0.5 * s)
            bump.inputs["Distance"].default_value = 0.0006
            K.new(term, bump.inputs["Height"])
            K.new(bump.outputs["Normal"], b.inputs["Normal"])
    return True


def planned_linear(h):
    """The plan's colour (#rrggbb) in linear RGB with the same albedo floor the code parts use (sRGB 45)."""
    h = str(h or "").lstrip("#")
    if len(h) != 6:
        return None
    out = []
    for i in (0, 2, 4):
        c = max(int(h[i:i + 2], 16) / 255.0, 45 / 255.0)
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)


def image_mean_luminance(img):
    """Mean linear luminance of an image's opaque texels, on a subsample."""
    w, h = img.size
    if not w or not h:
        return None
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    a = a.reshape(-1, 4)[:: max(1, (w * h) // 65536)]
    a = a[a[:, 3] > 0.5] if (a[:, 3] > 0.5).any() else a
    if img.colorspace_settings.name == "sRGB":
        rgb = np.where(a[:, :3] <= 0.04045, a[:, :3] / 12.92, ((a[:, :3] + 0.055) / 1.055) ** 2.4)
    else:
        rgb = a[:, :3]
    return float((rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)).mean())


def tint_to_plan(o, colour):
    """A vendor part takes its planned colour, keeping its own light and dark variation: base colour = planned colour x
    (texel luminance / the texture's mean luminance), clamped. Tripo keeps a washed-out grey where the plan says matte
    black (the pistol frame, 2026-09-27); this is the part's material being set as planned while it is assembled, the
    shape and the texture's detail are the vendor's."""
    lin = planned_linear(colour)
    if lin is None:
        return False
    done = False
    for m in {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}:
        t = m.node_tree
        b = next((n for n in t.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if b is None:
            continue
        base = b.inputs["Base Color"]
        rgb = t.nodes.new("ShaderNodeRGB")
        rgb.outputs[0].default_value = (*lin, 1.0)
        if not base.is_linked:
            t.links.new(rgb.outputs[0], base)
            done = True
            continue
        src = base.links[0].from_socket
        img, todo, seen = None, [src.node], set()
        while todo and img is None:                        # the image upstream of the base colour
            n = todo.pop()
            if n.name in seen:
                continue
            seen.add(n.name)
            if n.type == "TEX_IMAGE" and n.image:
                img = n.image
            todo.extend(l.from_node for i in n.inputs for l in i.links)
        mean = image_mean_luminance(img) if img is not None else None
        if not mean or mean <= 1e-4:
            continue
        bw = t.nodes.new("ShaderNodeRGBToBW")
        t.links.new(src, bw.inputs[0])
        ratio = t.nodes.new("ShaderNodeMath")
        ratio.operation = "DIVIDE"
        t.links.new(bw.outputs[0], ratio.inputs[0])
        ratio.inputs[1].default_value = mean
        clamp = t.nodes.new("ShaderNodeMapRange")          # keep the detail, not the vendor's blown highlights
        clamp.clamp = True
        clamp.inputs["From Min"].default_value = 0.0
        clamp.inputs["From Max"].default_value = 2.0
        clamp.inputs["To Min"].default_value = 0.0
        clamp.inputs["To Max"].default_value = 2.0
        t.links.new(ratio.outputs[0], clamp.inputs["Value"])
        soft = t.nodes.new("ShaderNodeMath")               # halve the swing: 0.5 + 0.5 x ratio, 0.5..1.5
        soft.operation = "MULTIPLY_ADD"
        t.links.new(clamp.outputs["Result"], soft.inputs[0])
        soft.inputs[1].default_value = 0.5
        soft.inputs[2].default_value = 0.5
        scale = t.nodes.new("ShaderNodeVectorMath")
        scale.operation = "SCALE"
        t.links.new(rgb.outputs[0], scale.inputs[0])
        t.links.new(soft.outputs[0], scale.inputs["Scale"])
        t.links.new(scale.outputs["Vector"], base)
        done = True
    return done


def surface_to_plan(o, mat):
    """A vendor part takes its planned roughness and metalness too: Tripo's maps made the bullpup's polymer body shine
    like metal (2026-09-27). The texture's roughness variation is kept, squeezed into planned +-0.1."""
    if "roughness" not in mat and "metal" not in mat:
        return False
    for m in {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}:
        t = m.node_tree
        b = next((n for n in t.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if b is None:
            continue
        if "metal" in mat:
            for l in list(b.inputs["Metallic"].links):
                t.links.remove(l)
            b.inputs["Metallic"].default_value = 1.0 if mat.get("metal") else 0.0
        if "roughness" in mat:
            r = min(1.0, max(0.05, float(mat["roughness"])))
            inp = b.inputs["Roughness"]
            if inp.is_linked:
                src = inp.links[0].from_socket
                mr = t.nodes.new("ShaderNodeMapRange")
                mr.clamp = True
                mr.inputs["To Min"].default_value = max(0.05, r - 0.1)
                mr.inputs["To Max"].default_value = min(1.0, r + 0.1)
                t.links.new(src, mr.inputs["Value"])
                t.links.new(mr.outputs["Result"], inp)
            else:
                inp.default_value = r
    return True


# ---------------------------------------------------------------- parts in their boxes
bpy.ops.wm.read_factory_settings(use_empty=True)
parts, glass_parts = [], []
for p in args["parts"]:
    o = import_part(p)
    rec = {"name": p["name"], "kind": p["kind"], "box_min": p["box_min"], "box_max": p["box_max"], **fit(o, p)}
    # code parts carry the reference's fine detail; vendor parts already have their own texture
    if p["kind"] == "code" and args.get("detail") and not (p.get("material") or {}).get("glass"):
        rec["reference_detail"] = add_reference_detail(o, args["detail"])
    if p["kind"] == "vendor" and not (p.get("material") or {}).get("glass") and args.get("tint_vendor", True):
        # a multi-coloured body (grey with an olive panel) keeps the vendor's colours; its surface is still the plan's
        if not (p.get("material") or {}).get("keep_texture"):
            rec["tinted"] = tint_to_plan(o, (p.get("material") or {}).get("color"))
        rec["surface_planned"] = surface_to_plan(o, p.get("material") or {})
    for slot in o.material_slots:
        if slot.material:
            slot.material.name = "MS_src_%s_%s" % (p["name"], slot.material.name)
    if (p.get("material") or {}).get("glass"):
        glass_parts.append((o, rec))
    else:
        parts.append((o, rec))
    report["parts"].append(rec)
if not parts:
    raise RuntimeError("no opaque parts to assemble")

# ---------------------------------------------------------------- the triangle budget: code parts as built, vendor parts share the rest
budget = int(args["tri_budget"])
vendor = [(o, r) for o, r in parts if r["kind"] == "vendor"]


def sharp_by_angle(o, degrees=30):
    bm = bmesh.new()
    bm.from_mesh(o.data)
    lim = math.radians(degrees)
    for f in bm.faces:
        f.smooth = True
    for e in bm.edges:
        e.smooth = not (e.is_manifold and e.calc_face_angle(0.0) > lim)
    bm.to_mesh(o.data)
    bm.free()


# code parts over their share (the truck's tyre treads came to 120k triangles of a 100k budget, 2026-09-27): flat areas
# are dissolved first, which changes no shape, then the part is collapsed to its share by surface area
code = [(o, r) for o, r in parts if r["kind"] == "code"]
code_allow = int(budget * (0.6 if vendor else 0.9))
code_tris = sum(blib.tri_count(o) for o, _r in code)
if code_tris > code_allow:
    c_areas = [surface_area(o) for o, _r in code]
    for (o, r), a in zip(code, c_areas):
        share = max(300, int(code_allow * a / max(sum(c_areas), 1e-9)))
        have = blib.tri_count(o)
        if have <= share:
            continue
        m = o.modifiers.new("flat", "DECIMATE")
        m.decimate_type = "DISSOLVE"
        m.angle_limit = math.radians(0.5)
        m.delimit = {"UV", "MATERIAL", "SHARP"}
        blib.select_only([o])
        bpy.ops.object.modifier_apply(modifier="flat")
        bm = bmesh.new()
        bm.from_mesh(o.data)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.to_mesh(o.data)
        bm.free()
        decimate_to(o, share)
        sharp_by_angle(o)
        r["triangles_built"] = have
        r["triangles"] = blib.tri_count(o)
    log("code parts over their %d allowance: %d -> %d triangles" % (code_allow, code_tris, sum(blib.tri_count(o) for o, _r in code)))
    code_tris = sum(blib.tri_count(o) for o, _r in code)
left = max(budget - code_tris, int(budget * 0.3))
areas = [surface_area(o) for o, _r in vendor]
for (o, r), a in zip(vendor, areas):
    share = max(2000, int(left * a / max(sum(areas), 1e-9)))
    r["triangles_seed"] = blib.tri_count(o)
    r["triangles"] = decimate_to(o, share)
for o, r in parts:
    r.setdefault("triangles", blib.tri_count(o))
log("parts: %d code (%d tris), %d vendor (%d tris after their share of %d)" % (
    len(parts) - len(vendor), code_tris, len(vendor), sum(r["triangles"] for _o, r in vendor), left))

# ---------------------------------------------------------------- HIGH (parts as they are) and LOD0 (one atlas)
blib.select_only([o for o, _r in parts])
for o, _r in parts:
    if not o.data.uv_layers:
        blib.select_only([o])
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.02)
        bpy.ops.object.mode_set(mode="OBJECT")
    o.data.uv_layers[0].name = "UVMap"
    while len(o.data.uv_layers) > 1:
        o.data.uv_layers.remove(o.data.uv_layers[1])


def tile_layout(sizes, gap=0.004):
    """Shelf-pack squares of relative side `sizes` into the unit square at the largest common scale that fits.
    -> [(u0, v0, side)] in input order."""
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i])
    lo, hi, best = 0.0, 4.0 / max(max(sizes), 1e-9), None
    for _ in range(40):
        k = (lo + hi) / 2
        x = y = row = 0.0
        pos, fits = {}, True
        for i in order:
            s = sizes[i] * k
            if x + s > 1.0 + 1e-9:
                x, y, row = 0.0, y + row + gap, 0.0
            if s > 1.0 or y + s > 1.0 + 1e-9:
                fits = False
                break
            pos[i] = (x, y, s)
            x += s + gap
            row = max(row, s)
        if fits:
            lo, best = k, pos
        else:
            hi = k
    return [best[i] for i in range(len(sizes))]


# the atlas: each part keeps its own unwrap inside a square tile sized by its surface area. Packing every island of every
# part together failed on the truck: thousands of tread islands, each with a margin, shrank to dots (7% of the atlas used)
tiles = tile_layout([math.sqrt(max(surface_area(o), 1e-12)) for o, _r in parts])
for (o, r), (u0, v0, side) in zip(parts, tiles):
    src = o.data.uv_layers["UVMap"]
    dst = o.data.uv_layers.new(name="Atlas")
    n = len(src.data)
    uv = np.empty(n * 2, np.float32)
    src.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    lo_uv, hi_uv = uv.min(axis=0), uv.max(axis=0)
    span = np.maximum(hi_uv - lo_uv, 1e-9)
    pad = side * 0.01
    uv = (uv - lo_uv) / span.max() * (side - 2 * pad) + np.array([u0 + pad, v0 + pad])
    dst.data.foreach_set("uv", uv.ravel())
    r["atlas_tile"] = [round(u0, 4), round(v0, 4), round(side, 4)]
blib.select_only([o for o, _r in parts])
bpy.ops.object.join()
high = bpy.context.view_layer.objects.active
high.name = "MS_high"
high.data.uv_layers["UVMap"].active = True
high.data.uv_layers["UVMap"].active_render = True           # the source textures read their own unwrap
lod0 = high.copy()
lod0.data = high.data.copy()
bpy.context.collection.objects.link(lod0)
lod0.name = "SM_" + NAME
blib.select_only([lod0])
lod0.data.uv_layers.remove(lod0.data.uv_layers["UVMap"])
lod0.data.uv_layers["Atlas"].name = "UVMap"
lod0.data.uv_layers["UVMap"].active = True
lod0.data.uv_layers["UVMap"].active_render = True

size = int(args.get("atlas_size", 2048))
lo, hi = blib.dims(high)
diag = (hi - lo).length


def new_image(tag, colour=True):
    img = bpy.data.images.new("T_%s_%s" % (NAME, tag), size, size, alpha=False, float_buffer=False)
    img.colorspace_settings.name = "sRGB" if colour else "Non-Color"
    return img


final = bpy.data.materials.new("MI_" + NAME)
nt = final.node_tree
bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
lod0.data.materials.clear()
lod0.data.materials.append(final)
target = nt.nodes.new("ShaderNodeTexImage")
nt.nodes.active = target
scn = bpy.context.scene
scn.render.engine = "CYCLES"
scn.cycles.device = "CPU"
scn.cycles.samples = 1
blib.select_only([high, lod0])
bpy.context.view_layer.objects.active = lod0
bake_kw = dict(use_selected_to_active=True, cage_extrusion=diag * 0.0015, max_ray_distance=diag * 0.006, margin=4,
               use_clear=True, target="IMAGE_TEXTURES")


def bake(tag, kind, colour, **extra):
    img = new_image(tag, colour)
    target.image = img
    bpy.ops.object.bake(type=kind, **bake_kw, **extra)
    return img


def route_to_emission(what):
    """Temporarily feed each HIGH material's `what` input (Base Color, Metallic) into an emission, so EMIT bakes it."""
    undo = []
    for m in {s.material for s in high.material_slots if s.material and s.material.node_tree}:
        t = m.node_tree
        b = next((n for n in t.nodes if n.type == "BSDF_PRINCIPLED"), None)
        outn = next((n for n in t.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if b is None or outn is None:
            continue
        prev = outn.inputs["Surface"].links[0].from_socket if outn.inputs["Surface"].is_linked else None
        e = t.nodes.new("ShaderNodeEmission")
        src = b.inputs[what]
        if src.is_linked:
            t.links.new(src.links[0].from_socket, e.inputs["Color"])
        else:
            v = src.default_value
            e.inputs["Color"].default_value = tuple(v)[:3] + (1,) if hasattr(v, "__len__") else (float(v),) * 3 + (1,)
        for l in list(outn.inputs["Surface"].links):
            t.links.remove(l)
        t.links.new(e.outputs["Emission"], outn.inputs["Surface"])
        undo.append((t, outn, prev, e))
    return undo


def restore(undo):
    for t, outn, prev, e in undo:
        for l in list(outn.inputs["Surface"].links):
            t.links.remove(l)
        if prev is not None:
            t.links.new(prev, outn.inputs["Surface"])
        t.nodes.remove(e)


# base colour through an emission: Cycles' diffuse colour pass is zero on metal, and every part planned as metal (the
# truck's painted body, the bullpup's steel) baked black (2026-09-27)
undo = route_to_emission("Base Color")
try:
    bc = bake("BC", "EMIT", True)
finally:
    restore(undo)
rough = bake("R", "ROUGHNESS", False)
undo = route_to_emission("Metallic")
try:
    metal = bake("M", "EMIT", False)
finally:
    restore(undo)
normal = bake("N", "NORMAL", False, normal_space="TANGENT")
scn.cycles.samples = 16
scn.world = scn.world or bpy.data.worlds.new("World")
scn.world.light_settings.distance = max(diag * 0.015, 0.003)
ao = bake("AO", "AO", False)
nt.nodes.remove(target)


def pixels(img):
    a = np.empty(img.size[0] * img.size[1] * 4, np.float32)
    img.pixels.foreach_get(a)
    return a.reshape(img.size[1], img.size[0], 4)


orm = new_image("ORM", False)
px = np.ones((size, size, 4), np.float32)
px[:, :, 0] = np.clip(0.35 + 0.65 * pixels(ao)[:, :, 0], 0, 1)          # contact shadow, never black
px[:, :, 1] = pixels(rough)[:, :, 0]
px[:, :, 2] = pixels(metal)[:, :, 0]
orm.pixels.foreach_set(px.ravel())
covered = pixels(ao)[:, :, 0] > 0.001            # texels a part landed on; the empty atlas is not the surface
report["roughness_mean"] = round(float(px[:, :, 1][covered].mean()), 3) if covered.any() else None
report["metallic_mean"] = round(float(px[:, :, 2][covered].mean()), 3) if covered.any() else None
report["atlas_coverage"] = round(float(covered.mean()), 3)
for img, tag in ((bc, "BC"), (normal, "N"), (orm, "ORM")):
    img.filepath_raw = os.path.join(OUT, "T_%s_%s.png" % (NAME, tag))
    img.file_format = "PNG"
    img.save()
    img.pack()
    report["maps"].append({"role": tag, "file": os.path.basename(img.filepath_raw), "size": [size, size]})
for img in (rough, metal, ao):
    bpy.data.images.remove(img)
t_bc = nt.nodes.new("ShaderNodeTexImage")
t_bc.image = bc
nt.links.new(t_bc.outputs["Color"], bsdf.inputs["Base Color"])
t_orm = nt.nodes.new("ShaderNodeTexImage")
t_orm.image = orm
sep = nt.nodes.new("ShaderNodeSeparateColor")
nt.links.new(t_orm.outputs["Color"], sep.inputs["Color"])
nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
t_n = nt.nodes.new("ShaderNodeTexImage")
t_n.image = normal
nm = nt.nodes.new("ShaderNodeNormalMap")
nt.links.new(t_n.outputs["Color"], nm.inputs["Color"])
nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
bpy.data.objects.remove(high, do_unlink=True)

# glass parts keep a glass slot of their own, outside the atlas
for o, r in glass_parts:
    g = bpy.data.materials.get("MI_%s_Glass" % NAME) or bpy.data.materials.new("MI_%s_Glass" % NAME)
    gb = next(n for n in g.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    gb.inputs["Base Color"].default_value = (0.05, 0.07, 0.08, 1)
    gb.inputs["Roughness"].default_value = 0.05
    gb.inputs["Alpha"].default_value = 0.25
    o.data.materials.clear()
    o.data.materials.append(g)
    blib.select_only([lod0, o])
    bpy.context.view_layer.objects.active = lod0
    bpy.ops.object.join()
report["glass"] = {"parts": [r["name"] for _o, r in glass_parts]} if glass_parts else None
log("atlas %d baked: roughness %s, metallic %s, %.0f%% of the atlas used" % (size, report["roughness_mean"], report["metallic_mean"],
                                                                         report["atlas_coverage"] * 100))

# ---------------------------------------------------------------- LODs, collision, sockets
lo, hi = blib.dims(lod0)
report["dimensions_m"] = [round(v, 4) for v in (hi - lo)]


def lod_copy(src, ratio, name):
    o = src.copy()
    o.data = src.data.copy()
    o.name = name
    bpy.context.collection.objects.link(o)
    decimate_to(o, int(blib.tri_count(src) * ratio))
    return o


lod1 = lod_copy(lod0, 0.5, "SM_%s_LOD1" % NAME)
lod2 = lod_copy(lod1, 0.5, "SM_%s_LOD2" % NAME)
for i, o in enumerate((lod0, lod1, lod2)):
    report["lods"].append({"lod": i, "triangles": blib.tri_count(o)})
log("LODs: %s" % ", ".join(format(l["triangles"], ",") for l in report["lods"]))

co = np.empty(len(lod2.data.vertices) * 3, np.float32)
lod2.data.vertices.foreach_get("co", co)
co = co.reshape(-1, 3)
k = np.arange(48) + 0.5
phi, theta = np.arccos(1 - 2 * k / 48), math.pi * (1 + 5 ** 0.5) * k
dirs = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)
proj = co @ dirs.T
pts = co[np.unique(np.concatenate([proj.argmax(axis=0), proj.argmin(axis=0)]))]
hull = bpy.data.objects.new("UCX_SM_%s_01" % NAME, bpy.data.meshes.new("UCX_SM_%s_01" % NAME))
bpy.context.collection.objects.link(hull)
bm = bmesh.new()
for pnt in pts:
    bm.verts.new(pnt.tolist())
bm.verts.ensure_lookup_table()
bmesh.ops.convex_hull(bm, input=bm.verts)
bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
bmesh.ops.triangulate(bm, faces=bm.faces)
bm.to_mesh(hull.data)
bm.free()
report["collision"] = {"type": "convex", "triangles": blib.tri_count(hull)}


def part_box(*words):
    for r in report["parts"]:
        if any(w in r["name"].lower() for w in words):
            return Vector(r["box_min"]), Vector(r["box_max"])
    return None


if (args.get("spec") or {}).get("category") == "weapon":
    sockets = []
    b = part_box("brake", "muzzle", "suppressor", "flash", "barrel")
    sockets.append({"name": "Muzzle", "location": [round(v, 4) for v in ((hi.x if b is None else b[1].x), 0.0,
                                                                          (0.0 if b is None else (b[0].z + b[1].z) / 2))]})
    g = part_box("grip")
    if g is not None:
        sockets.append({"name": "Grip", "location": [round(v, 4) for v in ((g[0] + g[1]) / 2)]})
    s = part_box("rail", "sight", "optic", "scope")
    if s is not None:
        sockets.append({"name": "Sight", "location": [round((s[0].x + s[1].x) / 2, 4), 0.0, round(s[1].z, 4)]})
    report["sockets"] = sockets

# ---------------------------------------------------------------- renders: previews, detail views, the check views
blib.setup_render(int(args.get("render_size", 768)), 48, look="preview")
stage = blib.Stage(lod0, extra_hidden=[hull, lod1, lod2])
report["renders"] = [stage.render(v, os.path.join(OUT, "preview_%s.png" % v))["file"] for v in ("iso", "side", "front")]
stage.close()
span = hi.x - lo.x
detail = []
for tag, x0, x1 in (("front", hi.x - span * 0.4, hi.x), ("rear", lo.x, lo.x + span * 0.4)):
    st = blib.Stage(lod0, extra_hidden=[hull, lod1, lod2], focus_bounds=(Vector((x0, lo.y, lo.z)), Vector((x1, hi.y, hi.z))))
    name = "preview_detail_%s.png" % tag
    st.render("iso", os.path.join(OUT, name))
    st.close()
    detail.append(name)
report["detail_renders"] = detail
# orthographic side and front on white, framed on the silhouette like the reference pictures, for the check step
blib.setup_render(int(args.get("check_size", 1024)), 16, look="probe")
cam = bpy.data.objects.new("CheckCam", bpy.data.cameras.new("CheckCam"))
bpy.context.collection.objects.link(cam)
scn.camera = cam
for o in (hull, lod1, lod2):
    o.hide_render = True
checks = {}
for view in ("left", "front"):
    rec = blib.ortho_camera(cam, view, lo, hi, margin=1.08)
    path = os.path.join(OUT, "check_%s.png" % view)
    scn.render.filepath = path
    bpy.ops.render.render(write_still=True)
    checks[view] = {"file": os.path.basename(path), "camera": rec}
report["check_renders"] = checks
bpy.data.objects.remove(cam, do_unlink=True)
for o in (hull, lod1, lod2):
    o.hide_render = False

# ---------------------------------------------------------------- exports
fbx_kw = dict(use_selection=True, apply_unit_scale=True, apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z",
              axis_up="Y", mesh_smooth_type="FACE", use_mesh_modifiers=True, path_mode="STRIP", embed_textures=False,
              add_leaf_bones=False, bake_anim=False)
blib.select_only([lod0, hull])
p = os.path.join(OUT, "SM_%s.fbx" % NAME)
bpy.ops.export_scene.fbx(filepath=p, **fbx_kw)
report["files"].append(os.path.basename(p))
for o in (lod1, lod2):
    blib.select_only([o])
    p = os.path.join(OUT, o.name + ".fbx")
    bpy.ops.export_scene.fbx(filepath=p, **fbx_kw)
    report["files"].append(os.path.basename(p))
blib.select_only([lod0])
p = os.path.join(OUT, "SM_%s.glb" % NAME)
bpy.ops.export_scene.gltf(filepath=p, use_selection=True, export_format="GLB", export_yup=True)
report["files"].append(os.path.basename(p))
if args.get("spec"):
    txt = bpy.data.texts.new("ms_spec.json")
    txt.write(json.dumps(args["spec"]))
if args.get("reference") and os.path.exists(args["reference"]):
    ref_img = bpy.data.images.load(os.path.abspath(args["reference"]))
    ref_img.name = "ms_reference"
    ref_img.pack()
    ref_img.use_fake_user = True
p = os.path.join(OUT, "SM_%s.blend" % NAME)
bpy.ops.wm.save_as_mainfile(filepath=p, compress=True)
report["files"].append(os.path.basename(p))
report["files"] += [m["file"] for m in report["maps"]]
report["engine"] = args.get("engine", "unreal")
report["materials"] = [m.name for m in lod0.data.materials if m]
with open(os.path.join(OUT, "report.json"), "w") as f:
    json.dump(report, f, indent=1)
log("done")
