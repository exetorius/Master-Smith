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
        # a code part's box is its tight bounds: a build that falls short of it on an axis by more than 8% is scaled to
        # fill it (at most 1.5x). The bullpup's barrel was built 7 mm across in a 10.6 mm box, every build (2026-09-27)
        lo_c, hi_c = blib.dims(o)
        ext_c = hi_c - lo_c
        fill = [min(2.0, size[i] / ext_c[i]) if ext_c[i] > 1e-9 and size[i] / ext_c[i] > 1.08 else 1.0 for i in range(3)]
        if p.get("centreline"):
            # a round part scales evenly across its section: stretched one way only, the muzzle brake came out oval
            fill[1] = fill[2] = math.sqrt(fill[1] * fill[2])
        if fill != [1.0, 1.0, 1.0]:
            o.data.transform(Matrix.Translation(-(lo_c + hi_c) * 0.5))
            o.data.transform(Matrix.Diagonal(Vector(fill).to_4d()))
        g = float(p.get("cover") or 1.0)          # grown a little over the vendor body's soft copy of this part
        if g != 1.0:
            o.data.transform(Matrix.Diagonal(Vector((g, g, g, 1.0))))
        o.data.transform(Matrix.Translation(centre))
        return {"scale": g, "filled": [round(v, 3) for v in fill]}
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
    if p.get("fill_box"):
        scale = list(ratios)                      # a small part: its box, read off the picture, rules on every side
    if p.get("keep_depth"):
        # seeded from a three-quarter picture, the vendor saw the part's real depth: its width keeps the length and
        # height scale instead of being stretched to the planned front span, which counts every protrusion (the
        # bullpup body was squeezed to 76% of its width and was a mess from the front, 2026-09-27)
        scale[1] = math.sqrt(scale[0] * scale[2])
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
    for m in (mats if mats is not None else {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}):
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


def planned_linear(h, metal=False):
    """The plan's colour (#rrggbb) in linear RGB with the same albedo floor the code parts use (sRGB 45). A metal's
    base colour is how much it reflects: blued or black steel still reflects about a fifth of the light, so a metal is
    lifted to that luminance, keeping its hue - near-black "black steel" rendered as dull graphite (2026-09-28)."""
    h = str(h or "").lstrip("#")
    if len(h) != 6:
        return None
    out = []
    for i in (0, 2, 4):
        c = max(int(h[i:i + 2], 16) / 255.0, 45 / 255.0)
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    if metal:
        lum = 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
        if lum < METAL_MIN_REFLECTANCE:
            k = METAL_MIN_REFLECTANCE / max(lum, 1e-4)
            out = [min(1.0, v * k) for v in out]
    return tuple(out)


METAL_MIN_REFLECTANCE = 0.07       # blued or black steel: clearly metal, still dark (0.12 read as silver on a pistol slide)


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
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    # a mesher's atlas is mostly black padding: the mean over the whole image read 0.04 for a mid-grey receiver and
    # the brightness correction pushed its texels to white (2026-09-28). Only texels with some light count.
    used = lum[lum > 0.004]
    return float(used.mean()) if len(used) > 100 else float(lum.mean())


def tint_to_plan(o, colour, mats=None, metal=False, luminance_only=False):
    """A vendor part takes its planned colour, keeping its own light and dark variation: base colour = planned colour x
    (texel luminance / the texture's mean luminance), clamped. Tripo keeps a washed-out grey where the plan says matte
    black (the pistol frame, 2026-09-27); this is the part's material being set as planned while it is assembled, the
    shape and the texture's detail are the vendor's."""
    lin = planned_linear(colour, metal)
    if lin is None:
        return False
    done = False
    for m in (mats if mats is not None else {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}):
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
        want = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        if luminance_only:
            # a kept texture (its own colours are right, e.g. an olive panel on a grey body) is only brought to the
            # picture's brightness: a part drawn alone on white came out near-white (2026-09-28)
            k = max(0.25, min(4.0, want / max(mean, 1e-4)))
            if 0.85 <= k <= 1.18:
                t.nodes.remove(rgb)
                continue
            scale = t.nodes.new("ShaderNodeVectorMath")
            scale.operation = "SCALE"
            K_ = t.links
            K_.new(src, scale.inputs[0])
            scale.inputs["Scale"].default_value = k
            for l in list(base.links):
                t.links.remove(l)
            K_.new(scale.outputs["Vector"], base)
            t.nodes.remove(rgb)
            done = True
            continue
        if 0.7 <= mean / max(want, 1e-4) <= 1.4:
            # the vendor's texture is already about the planned tone: keep it, with its own colour separation (the
            # TRELLIS pistol's black slide against its frame, its stippled grip) - the tint is for washed-out seeds
            # like Tripo's grey frame
            t.nodes.remove(rgb)
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


def surface_detail(o, mat, strength=1.0, mats=None, vendor=False):
    """The material pass a texture artist gives a moulded or cast part, on a vendor seed before the bake: worn, lighter
    raised edges and darker recesses from the mesh's curvature (Cycles pointiness on the full-detail seed), a fine grain
    in the normal, and roughness that varies with it and polishes on the worn edges. A vendor's texture alone read as
    soft grey plastic on the bullpup (owner, 2026-09-27). Everything is procedural in object space, so it bakes into
    the atlas like the seed's own texture."""
    metal = bool(mat.get("metal"))
    finish = mat.get("finish") or ("metal" if metal else "polymer")
    rubber = finish == "rubber"
    # a mesher's surface is bumpy everywhere, so curvature finds "edges" all over it: a vendor part gets a third of
    # the wear a clean code part gets (white flecks over the pistol's slide, 2026-09-28)
    wear_k = 0.3 if vendor else 1.0
    for m in (mats if mats is not None else {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}):
        t = m.node_tree
        b = next((n for n in t.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if b is None:
            continue
        N, K = t.nodes, t.links
        geo = N.new("ShaderNodeNewGeometry")
        coord = N.new("ShaderNodeTexCoord")
        # curvature: pointiness is ~0.5 on flat areas, higher on convex edges, lower in concave recesses
        edge = N.new("ShaderNodeMapRange")
        edge.inputs["From Min"].default_value = 0.52       # a dense seed's curvature sits close to 0.5: only the
        edge.inputs["From Max"].default_value = 0.57       # sharpest edges wear (0.505 turned the whole body chalky)
        K.new(geo.outputs["Pointiness"], edge.inputs["Value"])
        cavity = N.new("ShaderNodeMapRange")
        cavity.inputs["From Min"].default_value = 0.485
        cavity.inputs["From Max"].default_value = 0.44
        K.new(geo.outputs["Pointiness"], cavity.inputs["Value"])
        # a broken-up wear mask: edges wear unevenly
        wear_noise = N.new("ShaderNodeTexNoise")
        wear_noise.inputs["Scale"].default_value = 60.0
        wear_noise.inputs["Detail"].default_value = 8.0
        K.new(coord.outputs["Object"], wear_noise.inputs["Vector"])
        patches = N.new("ShaderNodeMapRange")           # wear in broken patches, not along every edge
        patches.inputs["From Min"].default_value = 0.48
        patches.inputs["From Max"].default_value = 0.66
        K.new(wear_noise.outputs["Fac"], patches.inputs["Value"])
        wear = N.new("ShaderNodeMath")
        wear.operation = "MULTIPLY"
        wear.use_clamp = True
        K.new(edge.outputs["Result"], wear.inputs[0])
        K.new(patches.outputs["Result"], wear.inputs[1])
        wear2 = N.new("ShaderNodeMath")
        wear2.operation = "MULTIPLY"
        wear2.use_clamp = True
        K.new(wear.outputs[0], wear2.inputs[0])
        wear2.inputs[1].default_value = 0.8 * strength * wear_k
        # base colour: lighter on the worn edges, darker in the recesses
        base = b.inputs["Base Color"]
        if base.is_linked:
            src = base.links[0].from_socket
        else:
            rgb = N.new("ShaderNodeRGB")
            rgb.outputs[0].default_value = tuple(base.default_value)
            src = rgb.outputs[0]
        lighten = N.new("ShaderNodeMix")
        lighten.data_type = "RGBA"
        lighten.blend_type = "SCREEN"
        K.new(wear2.outputs[0], lighten.inputs["Factor"])
        K.new(src, lighten.inputs["A"])
        lighten.inputs["B"].default_value = (0.50, 0.50, 0.47, 1.0) if metal else (0.26, 0.26, 0.25, 1.0)
        grime = N.new("ShaderNodeMix")
        grime.data_type = "RGBA"
        grime.blend_type = "MULTIPLY"
        gf = N.new("ShaderNodeMath")
        gf.operation = "MULTIPLY"
        gf.use_clamp = True
        K.new(cavity.outputs["Result"], gf.inputs[0])
        gf.inputs[1].default_value = 0.55 * strength * (0.6 if vendor else 1.0)
        K.new(gf.outputs[0], grime.inputs["Factor"])
        K.new(lighten.outputs["Result"], grime.inputs["A"])
        grime.inputs["B"].default_value = (0.45, 0.44, 0.43, 1.0)
        K.new(grime.outputs["Result"], base)
        # fine grain in the normal (polymer texture / cast or machined metal), chained onto any normal map the seed has
        grain = N.new("ShaderNodeTexNoise")
        grain.inputs["Scale"].default_value = 1600.0 if rubber else 900.0 if not metal else 2500.0
        grain.inputs["Detail"].default_value = 4.0
        if metal:
            # brushed: a fine grain drawn out along the part's length, 6x not 20x (20x made 14 mm stripes over a
            # pistol slide, 2026-09-28)
            stretch = N.new("ShaderNodeMapping")
            stretch.inputs["Scale"].default_value = (0.16, 1.0, 1.0)
            K.new(coord.outputs["Object"], stretch.inputs["Vector"])
            K.new(stretch.outputs["Vector"], grain.inputs["Vector"])
        else:
            K.new(coord.outputs["Object"], grain.inputs["Vector"])
        bump = N.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = (0.7 if rubber else 0.12 if metal else 0.35) * strength
        bump.inputs["Distance"].default_value = 0.001
        K.new(grain.outputs["Fac"], bump.inputs["Height"])
        nrm = b.inputs["Normal"]
        if nrm.is_linked:
            K.new(nrm.links[0].from_socket, bump.inputs["Normal"])
        K.new(bump.outputs["Normal"], nrm)
        # roughness: varied by the grain, polished where the edges are worn
        rough = b.inputs["Roughness"]
        r_src = rough.links[0].from_socket if rough.is_linked else None
        r_base = float(mat.get("roughness", rough.default_value if not rough.is_linked else 0.6))
        if rubber:
            r_base = max(r_base, 0.85)                 # rubber: dead matte
        elif metal:
            r_base = min(r_base, 0.4)                  # bare metal: a real sheen
        var = N.new("ShaderNodeMapRange")
        var.inputs["To Min"].default_value = max(0.05, r_base - 0.12)
        var.inputs["To Max"].default_value = min(1.0, r_base + 0.08)
        K.new(wear_noise.outputs["Fac"], var.inputs["Value"])
        if r_src is not None:
            avg = N.new("ShaderNodeMix")
            avg.data_type = "FLOAT"
            avg.inputs["Factor"].default_value = 0.5
            K.new(r_src, avg.inputs["A"])
            K.new(var.outputs["Result"], avg.inputs["B"])
            r_out = avg.outputs["Result"]
        else:
            r_out = var.outputs["Result"]
        polish = N.new("ShaderNodeMix")
        polish.data_type = "FLOAT"
        K.new(wear2.outputs[0], polish.inputs["Factor"])
        K.new(r_out, polish.inputs["A"])
        polish.inputs["B"].default_value = r_base if rubber else max(0.05, r_base - 0.35)   # rubber does not polish
        K.new(polish.outputs["Result"], rough)
    return True


def split_zones(o, zones):
    """A vendor part's material zones (plan: a rubber grip, bare steel, a coloured panel) as material slots of their
    own: the faces whose centre lies in a zone's box get a copy of their material. -> [(zone, set of materials)], in
    plan order; a later zone wins where two overlap. One material for a whole body read as graphite (2026-09-28)."""
    if not zones:
        return []
    me = o.data
    n = len(me.polygons)
    centres = np.empty(n * 3, np.float32)
    me.polygons.foreach_get("center", centres)
    centres = centres.reshape(-1, 3)
    idx = np.empty(n, np.int32)
    me.polygons.foreach_get("material_index", idx)
    if not o.material_slots:
        return []
    base = [sl.material for sl in o.material_slots]
    out = []
    # face adjacency (shared vertices), to smooth a zone's ragged box edge by neighbour majority. Built in numpy
    # (Blender's Python has no scipy): every pair of faces meeting at a vertex, made unique, then a vote is a
    # bincount instead of a Python loop over 150k faces (issue #6).
    loop_total = np.empty(n, np.int64)
    me.polygons.foreach_get("loop_total", loop_total)
    loop_vert = np.empty(len(me.loops), np.int64)
    me.loops.foreach_get("vertex_index", loop_vert)
    face_of_loop = np.repeat(np.arange(n, dtype=np.int64), loop_total)
    order = np.argsort(loop_vert, kind="stable")
    fs = face_of_loop[order]                          # faces grouped by the vertex they meet at
    _, start, k = np.unique(loop_vert[order], return_index=True, return_counts=True)
    k_e = np.repeat(k, k)                             # each corner's group size
    start_e = np.repeat(start, k)
    rep = np.repeat(np.arange(len(fs)), k_e)          # each corner repeated once per face in its group
    within = np.arange(len(rep)) - np.repeat(np.cumsum(k_e) - k_e, k_e)
    pairs = np.unique(fs[rep] * n + fs[np.repeat(start_e, k_e) + within])
    face_a, face_b = pairs // n, pairs % n
    degree = np.bincount(face_a, minlength=n).astype(np.float32)
    for z in zones:
        lo, hi = np.array(z["box_min"]), np.array(z["box_max"])
        inside = np.all((centres >= lo) & (centres <= hi), axis=1)
        for _ in range(2):
            votes = np.bincount(face_a, weights=inside[face_b].astype(np.float32), minlength=n) / np.maximum(degree, 1.0)
            inside = votes > 0.5
        if inside.sum() < 20:
            continue
        made = {}
        for src in sorted(set(idx[inside])):
            if src >= len(base) or base[src] is None:
                continue
            m = base[src].copy()
            m.name = "%s_zone_%s" % (base[src].name, z.get("name", "zone"))
            me.materials.append(m)
            made[src] = len(me.materials) - 1
        for src, dst in made.items():
            sel = inside & (idx == src)
            idx[sel] = dst
        out.append((z, {me.materials[d] for d in made.values()}))
    me.polygons.foreach_set("material_index", idx)
    me.update()
    return out


def glass_zone(mats):
    """A vendor body's window or canopy area as smoked glass in the baked atlas: the vendor's own colour darkened and
    desaturated, no roughness or metal, a mirror-smooth finish. The atlas cannot carry transparency, so this is the
    look of tinted glass seen from outside (the aircraft skill's rule)."""
    for m in mats:
        t = m.node_tree
        b = next((n for n in t.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if b is None:
            continue
        base = b.inputs["Base Color"]
        if base.is_linked:
            src = base.links[0].from_socket
            hsv = t.nodes.new("ShaderNodeHueSaturation")
            hsv.inputs["Saturation"].default_value = 0.5
            hsv.inputs["Value"].default_value = 0.45
            t.links.new(src, hsv.inputs["Color"])
            t.links.new(hsv.outputs["Color"], base)
        else:
            base.default_value = (0.05, 0.07, 0.09, 1.0)
        for name, v in (("Roughness", 0.05), ("Metallic", 0.0)):
            inp = b.inputs[name]
            for l in list(inp.links):
                t.links.remove(l)
            inp.default_value = v
        if "Specular IOR Level" in b.inputs:
            b.inputs["Specular IOR Level"].default_value = 0.8


def surface_to_plan(o, mat, mats=None):
    """A vendor part takes its planned roughness and metalness too: Tripo's maps made the bullpup's polymer body shine
    like metal (2026-09-27). The texture's roughness variation is kept, squeezed into planned +-0.1."""
    if "roughness" not in mat and "metal" not in mat:
        return False
    for m in (mats if mats is not None else {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree}):
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
    if p["kind"] == "code" and args.get("detail") and p.get("reference_detail", True) and not (p.get("material") or {}).get("glass"):
        rec["reference_detail"] = add_reference_detail(o, args["detail"])
    if p["kind"] == "vendor" and not (p.get("material") or {}).get("glass") and args.get("tint_vendor", True):
        zoned = split_zones(o, p.get("zones") or [])
        in_zone = set().union(*[m for _z, m in zoned]) if zoned else set()
        rest = {sl.material for sl in o.material_slots if sl.material and sl.material.node_tree} - in_zone
        pm = p.get("material") or {}
        # a multi-coloured body (grey with an olive panel) keeps the vendor's colours; its surface is still the plan's
        # the sampled colour with the texture's own light and dark: the brightness-only path left a receiver near-white
        # twice (2026-09-28); zones carry any second colour, so nothing is lost by tinting the rest
        rec["tinted"] = tint_to_plan(o, pm.get("color"), rest, metal=bool(pm.get("metal")))
        rec["surface_planned"] = surface_to_plan(o, pm, rest)
        if args.get("surface_detail", True):
            rec["surface_detail"] = surface_detail(o, pm, mats=rest, vendor=True)
        for z, mats in zoned:
            zm = z.get("material") or {}
            if zm.get("glass") or zm.get("finish") == "glass":
                glass_zone(mats)                   # smooth tinted glass; the wear pass turned a windshield to snow
                continue
            tint_to_plan(o, zm.get("color"), mats, metal=bool(zm.get("metal")))
            surface_to_plan(o, zm, mats)
            if args.get("surface_detail", True):
                surface_detail(o, zm, mats=mats, vendor=True)
        rec["zones"] = [z.get("name") for z, _m in zoned]
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


def sharpen_planar(o, band=None, flat_deg=10.0, min_area_frac=0.002, crease_deg=25.0):
    """Hard-surface edges back on a vendor seed (owner, 2026-09-27: "why can't Blender sharpen edges"). An image-to-3D
    mesh rounds every edge of a faceted body. This finds its large near-flat panels (region growing on face normals),
    flattens each onto its best-fit plane, and pulls the vertices of the rounded band between two panels onto the
    line where the planes meet: the fillet becomes a crease. Curved areas (a grip, a magazine) have no large flat
    panel and are left alone; vertices only move, so the UVs and texture stay. `band` is the widest rounding pulled
    in (metres). -> {"panels", "flattened", "creased"}"""
    from mathutils import kdtree
    me = o.data
    if band is None:
        # the rounding a mesher puts on an edge grows with the object: 0.4% of its length (3 mm on the 68 cm bullpup),
        # never under 0.8 mm or over 5 mm. A fixed 3 mm tore the edges of an 18.5 cm pistol (2026-09-28).
        lo_b, hi_b = blib.dims(o)
        band = min(0.005, max(0.0008, 0.004 * max((hi_b - lo_b)[:])))
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    nf = len(bm.faces)
    if nf < 200:
        bm.free()
        return {"panels": 0, "flattened": 0, "creased": 0}
    normals = np.array([f.normal[:] for f in bm.faces], np.float64)
    centres = np.array([f.calc_center_median()[:] for f in bm.faces], np.float64)
    areas = np.array([f.calc_area() for f in bm.faces], np.float64)
    total = areas.sum()
    cos_flat = math.cos(math.radians(flat_deg))
    region = np.full(nf, -1, np.int64)
    panels = []                                   # (normal, point, face ids)
    order = np.argsort(-areas)
    for seed in order:
        if region[seed] != -1:
            continue
        n0 = normals[seed]
        stack, members = [seed], []
        region[seed] = -2
        while stack:
            i = stack.pop()
            members.append(i)
            for e in bm.faces[i].edges:
                for g in e.link_faces:
                    j = g.index
                    if region[j] == -1 and normals[j].dot(n0) > cos_flat and abs(n0.dot(centres[j] - centres[seed])) < band * 0.4:
                        region[j] = -2
                        stack.append(j)
        members = np.array(members)
        if areas[members].sum() < min_area_frac * total or len(members) < 12:
            region[members] = -3                   # too small to be a panel: left as it is
            continue
        w = areas[members][:, None]
        c = (centres[members] * w).sum(0) / w.sum()
        cov = ((centres[members] - c) * w).T @ (centres[members] - c)
        n = np.linalg.eigh(cov)[1][:, 0]
        if n.dot(n0) < 0:
            n = -n
        region[members] = len(panels)
        panels.append((n, c, members))
    if not panels:
        bm.free()
        return {"panels": 0, "flattened": 0, "creased": 0}
    # which panels each vertex touches
    vert_panels = [set() for _ in bm.verts]
    for pi, (_n, _c, members) in enumerate(panels):
        for fi in members:
            for v in bm.faces[fi].verts:
                vert_panels[v.index].add(pi)
    tree = kdtree.KDTree(sum(len(m) for _n, _c, m in panels))
    k = 0
    for pi, (_n, _c, members) in enumerate(panels):
        for fi in members:
            tree.insert(centres[fi], pi)
            k += 1
    tree.balance()
    flattened = creased = 0
    cos_crease = math.cos(math.radians(crease_deg))
    new_co = {}
    for v in bm.verts:
        p = np.array(v.co[:], np.float64)
        own = vert_panels[v.index]
        inner = len(own) == 1 and all(region[f.index] == next(iter(own)) for f in v.link_faces)
        if inner:                                  # wholly inside one panel: flattened onto it
            n, c, _m = panels[next(iter(own))]
            d = n.dot(p - c)
            if abs(d) < band:
                new_co[v.index] = p - n * d
                flattened += 1
            continue
        if own:
            continue                               # a panel's border vertex that is not in a rounding: left alone
        # a vertex of the rounded band (or a seam between panels): the two nearest panels that meet at an edge
        near = {}
        for (_co, pi, dist) in tree.find_range(v.co, band * 1.5):
            if pi not in near or dist < near[pi]:
                near[pi] = dist
        cand = sorted(near, key=near.get)
        pair = None
        for a in range(len(cand)):
            for b in range(a + 1, len(cand)):
                if panels[cand[a]][0].dot(panels[cand[b]][0]) < cos_crease:
                    pair = (cand[a], cand[b])
                    break
            if pair:
                break
        if not pair:
            continue
        (n1, c1, _), (n2, c2, _) = panels[pair[0]], panels[pair[1]]
        if abs(n1.dot(p - c1)) > band or abs(n2.dot(p - c2)) > band:
            continue                               # not in the rounding between these two panels
        dirn = np.cross(n1, n2)
        if np.linalg.norm(dirn) < 1e-6:
            continue
        dirn /= np.linalg.norm(dirn)
        # a point on the line: solve n1.x = n1.c1, n2.x = n2.c2, dirn.x = dirn.p
        A = np.array([n1, n2, dirn])
        rhs = np.array([n1.dot(c1), n2.dot(c2), dirn.dot(p)])
        try:
            q = np.linalg.solve(A, rhs)
        except np.linalg.LinAlgError:
            continue
        if np.linalg.norm(q - p) < band:
            new_co[v.index] = q
            creased += 1
    old = {i: bm.verts[i].co.copy() for i in new_co}
    before = {f.index: f.normal.copy() for i in new_co for f in bm.verts[i].link_faces}
    for i, co in new_co.items():
        bm.verts[i].co = co
    for _ in range(3):                             # a move that flips or badly tilts a face is undone
        bm.normal_update()
        bad = set()
        for fi, n0 in before.items():
            f = bm.faces[fi]
            if f.calc_area() > 1e-14 and f.normal.dot(n0) < 0.3:
                bad.update(v.index for v in f.verts if v.index in old)
        if not bad:
            break
        for i in bad:
            bm.verts[i].co = old.pop(i)
            new_co.pop(i, None)
    flattened = sum(1 for i in new_co if len(vert_panels[i]) == 1)
    creased = len(new_co) - flattened
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    sharp_by_angle(o, degrees=crease_deg + 5)
    return {"panels": len(panels), "flattened": flattened, "creased": creased}


def find_bore(body_co, body_faces, centre, radius):
    """The body's own bore near `centre` (y, z) at its front end: rays cast straight back over a window around it; the
    bore is a round, enclosed region where they run deep (a shroud or muzzle opening), nearest the planned axis. The
    bullpup's TRELLIS body had a barrel shroud with a clean bore 20 mm from where the picture put the barrel, and the
    code barrel entered beside it (2026-09-28). -> (y, z) or None"""
    from mathutils.bvhtree import BVHTree
    bvh = BVHTree.FromPolygons([Vector(v) for v in body_co], body_faces)
    front = float(body_co[:, 0].max()) + radius
    half, step = 4.0 * radius, radius / 6.0
    ys = np.arange(centre[0] - half, centre[0] + half, step)
    zs = np.arange(centre[1] - half, centre[1] + half, step)
    deep = np.zeros((len(zs), len(ys)), bool)
    for i, z in enumerate(zs):
        for j, y in enumerate(ys):
            hit = bvh.ray_cast(Vector((front, y, z)), Vector((-1.0, 0.0, 0.0)), 12 * radius)
            deep[i, j] = hit[0] is None or (front - hit[0].x) > 4 * radius
    seen = np.zeros_like(deep)
    best = None
    for i0 in range(deep.shape[0]):
        for j0 in range(deep.shape[1]):
            if not deep[i0, j0] or seen[i0, j0]:
                continue
            stack, cells, edge = [(i0, j0)], [], False
            seen[i0, j0] = True
            while stack:
                i, j = stack.pop()
                cells.append((i, j))
                if i in (0, deep.shape[0] - 1) or j in (0, deep.shape[1] - 1):
                    edge = True                    # open to the window's border: not an enclosed bore
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    a2, b2 = i + di, j + dj
                    if 0 <= a2 < deep.shape[0] and 0 <= b2 < deep.shape[1] and deep[a2, b2] and not seen[a2, b2]:
                        seen[a2, b2] = True
                        stack.append((a2, b2))
            if edge:
                continue
            cells = np.array(cells)
            area = len(cells) * step * step
            circle = math.pi * radius * radius
            ext = (np.ptp(cells[:, 0]) + 1, np.ptp(cells[:, 1]) + 1)
            roundness = len(cells) / (math.pi * (max(ext) / 2.0) ** 2)
            if not (0.1 * circle <= area <= 6 * circle) or roundness < 0.5:
                continue
            cy, cz = float(ys[int(round(cells[:, 1].mean()))]), float(zs[int(round(cells[:, 0].mean()))])
            dist = math.hypot(cy - centre[0], cz - centre[1])
            if dist <= 3.5 * radius and (best is None or dist < best[0]):
                best = (dist, cy, cz)
    return (best[1], best[2]) if best else None


def align_to_body(parts, specs):
    """Centreline parts (a barrel, a muzzle device) put on the vendor body's own axis where they enter it: their place
    came from the pictures, the body's from the mesher, and the two disagree by millimetres - enough that a bullet
    could not pass (owner, 2026-09-28). First the body's bore (find_bore): the parts move onto it in both directions.
    Without one, the middle of the body's front section decides the sideways place. Every centreline part moves by the
    same amount, so a barrel and its muzzle device stay on one axis."""
    line = [(o, r) for o, r in parts if (specs.get(r["name"]) or {}).get("centreline")]
    vend = [(o, r) for o, r in parts if r["kind"] == "vendor" and (o, r) not in line]
    if not vend or not line:
        return []
    # the part the barrel enters (its rear end inside that part's box: the handguard), not the largest part (the
    # receiver: its "bore" put the barrel 3 mm off the handguard's, 2026-09-28)
    lead0 = min(line, key=lambda t: blib.dims(t[0])[0].x)[0]
    l0, h0 = blib.dims(lead0)
    rear = Vector((l0.x + 0.02 * (h0.x - l0.x), (l0.y + h0.y) / 2, (l0.z + h0.z) / 2))
    entered = [(o, r) for o, r in vend if all(r["box_min"][i] - 0.005 <= rear[i] <= r["box_max"][i] + 0.005 for i in range(3))]
    body = (min(entered, key=lambda t: (t[1]["box_max"][0] - t[1]["box_min"][0])) if entered
            else max(vend, key=lambda t: (t[1]["box_max"][0] - t[1]["box_min"][0])))[0]
    co = np.empty(len(body.data.vertices) * 3, np.float32)
    body.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    # the body's faces as triangles, read in one call (issue #6)
    body.data.calc_loop_triangles()
    tri = np.empty(len(body.data.loop_triangles) * 3, np.int32)
    body.data.loop_triangles.foreach_get("vertices", tri)
    faces = tri.reshape(-1, 3).tolist()
    # the part that enters the body: the rearmost centreline part (a barrel before its muzzle device)
    lead = min(line, key=lambda t: blib.dims(t[0])[0].x)[0]
    lo, hi = blib.dims(lead)
    yc, zc = (lo.y + hi.y) / 2, (lo.z + hi.z) / 2
    radius = max(min(hi.y - lo.y, hi.z - lo.z) / 2, 1e-4)
    bore = find_bore(co, faces, (yc, zc), radius)
    if bore:
        dy, dz, how = bore[0] - yc, bore[1] - zc, "onto the body's bore"
    else:
        front = co[:, 0].max()
        near = co[(co[:, 0] > front - 6 * radius) & (np.abs(co[:, 2] - zc) < 5 * radius)]
        if len(near) < 20:
            return []
        dy, dz, how = float((near[:, 1].min() + near[:, 1].max()) / 2) - yc, 0.0, "onto the body's centreline"
    moved = []
    if abs(dy) > 1e-5 or abs(dz) > 1e-5:
        for o, r in line:
            o.data.transform(Matrix.Translation((0.0, dy, dz)))
            r["centred_on_body_m"] = [round(dy, 5), round(dz, 5)]
            moved.append((r["name"], dy, dz, how))
    return moved


for name, dy, dz, how in align_to_body(parts, {p["name"]: p for p in args["parts"]}):
    log("%s: moved %.1f mm sideways and %.1f mm up %s" % (name, dy * 1000, dz * 1000, how))

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


# hard-surface edges back on the vendor seeds before anything is copied, decimated or baked
if args.get("sharpen", True):
    for o, r in parts:
        if r["kind"] == "vendor":
            r["sharpened"] = sharpen_planar(o)
            log("%s: sharpened %s" % (r["name"], r["sharpened"]))

# the bake source keeps every part at full detail: decimating first and baking from the decimated mesh threw away all
# of a seed's fine detail (the free pistol's 280k-face TRELLIS body came out melted at 40k with nothing to bake back,
# 2026-09-27). The decimated parts below become LOD0; these copies are what its maps are baked from.
full_parts = []
for o, _r in parts:
    c = o.copy()
    c.data = o.data.copy()
    bpy.context.collection.objects.link(c)
    c.name = o.name + "_full"
    full_parts.append(c)

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
for o in [o for o, _r in parts] + full_parts:
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
lod0 = bpy.context.view_layer.objects.active                  # the decimated parts, each in its atlas tile
lod0.name = "SM_" + NAME
blib.select_only(full_parts)
bpy.ops.object.join()
high = bpy.context.view_layer.objects.active                  # the same parts at full detail: the bake source
high.name = "MS_high"
high.data.uv_layers["UVMap"].active = True
high.data.uv_layers["UVMap"].active_render = True           # the source textures read their own unwrap
report["bake_source_triangles"] = blib.tri_count(high)
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
for view in ("left", "front", "top"):                   # top: parts off the centreline show only from above
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
