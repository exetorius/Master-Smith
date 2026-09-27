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
    long axis along X and upright: turned by the facing yaw, scaled uniformly until the tightest side fits, then
    stretched at most 15% on the other sides towards the box, and centred."""
    bmin, bmax = Vector(p["box_min"]), Vector(p["box_max"])
    centre, size = (bmin + bmax) * 0.5, bmax - bmin
    if p["kind"] == "code":
        o.data.transform(Matrix.Translation(centre))
        return {"scale": 1.0}
    if p.get("yaw"):
        o.data.transform(Matrix.Rotation(math.radians(float(p["yaw"])), 4, "Z"))
    lo, hi = blib.dims(o)
    ext = hi - lo
    ratios = [size[i] / max(ext[i], 1e-9) for i in range(3)]
    s = min(ratios)
    stretch = [min(r / s, 1.15) for r in ratios]
    o.data.transform(Matrix.Translation(-(lo + hi) * 0.5))
    o.data.transform(Matrix.Diagonal(Vector([s * stretch[i] for i in range(3)]).to_4d()))
    o.data.transform(Matrix.Translation(centre))
    return {"scale": round(s, 4), "stretch": [round(v, 3) for v in stretch]}


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


# ---------------------------------------------------------------- parts in their boxes
bpy.ops.wm.read_factory_settings(use_empty=True)
parts, glass_parts = [], []
for p in args["parts"]:
    o = import_part(p)
    rec = {"name": p["name"], "kind": p["kind"], "box_min": p["box_min"], "box_max": p["box_max"], **fit(o, p)}
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
code_tris = sum(blib.tri_count(o) for o, r in parts if r["kind"] == "code")
vendor = [(o, r) for o, r in parts if r["kind"] == "vendor"]
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
blib.select_only([o for o, _r in parts])
bpy.ops.object.join()
high = bpy.context.view_layer.objects.active
high.name = "MS_high"
lod0 = high.copy()
lod0.data = high.data.copy()
bpy.context.collection.objects.link(lod0)
lod0.name = "SM_" + NAME
# the atlas: every part's own islands, scaled to one texel density and packed together
blib.select_only([lod0])
atlas = lod0.data.uv_layers.new(name="Atlas")
lod0.data.uv_layers.active = atlas
for i in range(len(lod0.data.uv_layers[0].data)):
    atlas.data[i].uv = lod0.data.uv_layers[0].data[i].uv
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.select_all(action="SELECT")
bpy.ops.uv.average_islands_scale()
bpy.ops.uv.pack_islands(margin=0.004, rotate=True)
bpy.ops.object.mode_set(mode="OBJECT")
lod0.data.uv_layers.remove(lod0.data.uv_layers[0])
lod0.data.uv_layers["Atlas"].name = "UVMap"

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
    """Temporarily feed each HIGH material's `what` input (Metallic) into an emission, so EMIT bakes it."""
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
            v = float(src.default_value)
            e.inputs["Color"].default_value = (v, v, v, 1)
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


bc = bake("BC", "DIFFUSE", True, pass_filter={"COLOR"})
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
report["roughness_mean"] = round(float(px[:, :, 1].mean()), 3)
report["metallic_mean"] = round(float(px[:, :, 2].mean()), 3)
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
log("atlas %d baked: roughness %.2f, metallic %.2f" % (size, report["roughness_mean"], report["metallic_mean"]))

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
