"""Build one code part (inside Blender): run the builder's `build(kit, L, W, H)`, finish the pieces as one clean
hard-surface mesh, check it against its box, export a GLB and render it for the builder's self-check.
    blender -b -Y --python build_part.py -- <args.json>
args: {"name", "code", "size": [L, W, H], "material": {"color", "metal", "roughness"}, "out_dir", "render_size"}
Writes <out_dir>/<name>.glb, <name>_side.png, <name>_front.png, <name>_iso.png and <name>.json (the result or the
error the builder is shown)."""
import json
import math
import os
import sys
import traceback

import bmesh
import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blib  # noqa: E402
import codecheck  # noqa: E402
from hskit import Kit, KitError  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
NAME = args["name"]
OUT = os.path.abspath(args["out_dir"])
os.makedirs(OUT, exist_ok=True)
L, W, H = (float(v) for v in args["size"])
result = {"name": NAME, "ok": False, "size": [L, W, H]}


def write_result():
    with open(os.path.join(OUT, NAME + ".json"), "w") as f:
        json.dump(result, f, indent=1)


def hex_rgb(h):
    h = str(h or "").lstrip("#")
    if len(h) != 6:
        return (0.5, 0.5, 0.5)
    lin = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        c = max(c, 45 / 255.0)     # the darkest real paint or polymer; a black read off a shadowed photo is darker than
        lin.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)   # any albedo (the pistol, 2026-09-27)
    return tuple(lin)


def make_material(spec):
    """The planned colour, metal and roughness, with the variation a real surface has: a fine noise in the roughness
    (+-0.07) and a faint one in the colour (+-4% value), and worn, lighter edges on metal (Cycles pointiness, so the
    assembly's bake keeps them). A flat fill read as 'untextured shader fills' to the reviewer (2026-09-27)."""
    spec = spec or {}
    mat = bpy.data.materials.new("MI_part_%s" % NAME)
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    base = hex_rgb(spec.get("color"))
    rough = float(spec.get("roughness", 0.6))
    metal = bool(spec.get("metal"))
    finish = spec.get("finish") or ("metal" if metal else "polymer")
    if metal:
        # a metal's base colour is its reflectance: black steel still reflects about a fifth of the light
        lum = 0.2126 * base[0] + 0.7152 * base[1] + 0.0722 * base[2]
        if lum < 0.12:                              # blued or black steel: clearly metal, still dark (0.20 read as chrome)
            base = tuple(min(1.0, c * 0.12 / max(lum, 1e-4)) for c in base)
        rough = min(rough, 0.4)
    elif finish == "rubber":
        rough = max(rough, 0.85)
    bsdf.inputs["Metallic"].default_value = 1.0 if metal else 0.0
    coord = nt.nodes.new("ShaderNodeTexCoord")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 180.0                  # object space, metres: grain of a few millimetres
    noise.inputs["Detail"].default_value = 6.0
    nt.links.new(coord.outputs["Object"], noise.inputs["Vector"])
    r_map = nt.nodes.new("ShaderNodeMapRange")
    r_map.inputs["To Min"].default_value = max(0.05, rough - 0.07)
    r_map.inputs["To Max"].default_value = min(1.0, rough + 0.07)
    nt.links.new(noise.outputs["Fac"], r_map.inputs["Value"])
    nt.links.new(r_map.outputs["Result"], bsdf.inputs["Roughness"])
    c_mix = nt.nodes.new("ShaderNodeMix")
    c_mix.data_type = "RGBA"
    c_mix.inputs["A"].default_value = (*[c * 0.96 for c in base], 1.0)
    c_mix.inputs["B"].default_value = (*[min(1.0, c * 1.04) for c in base], 1.0)
    nt.links.new(noise.outputs["Fac"], c_mix.inputs["Factor"])
    colour = c_mix.outputs["Result"]
    if metal:
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        edge = nt.nodes.new("ShaderNodeMapRange")
        edge.inputs["From Min"].default_value = 0.52
        edge.inputs["From Max"].default_value = 0.62
        nt.links.new(geo.outputs["Pointiness"], edge.inputs["Value"])
        wear = nt.nodes.new("ShaderNodeMix")
        wear.data_type = "RGBA"
        nt.links.new(edge.outputs["Result"], wear.inputs["Factor"])
        nt.links.new(colour, wear.inputs["A"])
        wear.inputs["B"].default_value = (*[min(1.0, c * 1.6 + 0.04) for c in base], 1.0)
        colour = wear.outputs["Result"]
    nt.links.new(colour, bsdf.inputs["Base Color"])
    return mat


def box_render(part, view, path, size):
    """Orthographic, framed on the part's BOX (not on what was built), on transparent film: laid next to the reference
    cropped to the same box, any misfit in outline or proportion shows at once."""
    lo, hi = Vector((-L / 2, -W / 2, -H / 2)), Vector((L / 2, W / 2, H / 2))
    scn = bpy.context.scene
    # in clay: the check is about shape, and a black slide rendered black hid its serrations from the builder
    clay = bpy.data.materials.get("MS_clay") or bpy.data.materials.new("MS_clay")
    cb = next(n for n in clay.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    cb.inputs["Base Color"].default_value = (0.55, 0.55, 0.55, 1)
    cb.inputs["Roughness"].default_value = 0.5
    cb.inputs["Metallic"].default_value = 0.0
    own = [s.material for s in part.material_slots]
    for s in part.material_slots:
        s.material = clay
    cam = bpy.data.objects.new("BoxCam", bpy.data.cameras.new("BoxCam"))
    bpy.context.collection.objects.link(cam)
    scn.camera = cam
    blib.ortho_camera(cam, view, lo, hi, margin=1.0)
    scn.render.resolution_x = scn.render.resolution_y = size
    scn.render.film_transparent = True
    scn.render.image_settings.color_mode = "RGBA"
    scn.render.filepath = path
    bpy.ops.render.render(write_still=True)
    scn.render.film_transparent = False
    scn.render.image_settings.color_mode = "RGB"
    for s, m in zip(part.material_slots, own):
        s.material = m
    bpy.data.objects.remove(cam, do_unlink=True)
    return os.path.basename(path)


def finish(o):
    """One closed hard-surface shell: coincident vertices welded, outward normals, smooth shading split at creases
    (the geometry is clean, so a crease IS a crease), weighted normals so flat faces stay flat, a UV unwrap."""
    bm = bmesh.new()
    bm.from_mesh(o.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    for f in bm.faces:
        f.smooth = True
    lim = math.radians(30)
    for e in bm.edges:
        e.smooth = not (e.is_manifold and e.calc_face_angle(0.0) > lim) if e.is_manifold else False
    bm.to_mesh(o.data)
    bm.free()
    blib.select_only([o])
    m = o.modifiers.new("wn", "WEIGHTED_NORMAL")
    m.keep_sharp = True
    m.weight = 50
    bpy.ops.object.modifier_apply(modifier="wn")
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")


bpy.ops.wm.read_factory_settings(use_empty=True)
try:
    tree = codecheck.check_code(args["code"])
    kit = Kit(L, W, H)
    env = codecheck.safe_globals(kit, math)
    exec(compile(tree, "<part %s>" % NAME, "exec"), env)
    pieces = env["build"](kit, L, W, H)
    if isinstance(pieces, bpy.types.Object):
        pieces = [pieces]
    if not isinstance(pieces, (list, tuple)) or not pieces:
        raise KitError("build must return a piece or a list of pieces")
    pieces = [p for p in pieces if isinstance(p, bpy.types.Object) and p in kit.made]
    if not pieces:
        raise KitError("build returned nothing the kit made")
    for o in list(kit.made):
        if o not in pieces:
            bpy.data.objects.remove(o, do_unlink=True)
    part = kit.join(*pieces)
    part.name = NAME
    if not len(part.data.polygons):
        raise KitError("the part has no faces")
    lo, hi = blib.dims(part)
    ext = hi - lo
    result["built_size"] = [round(v, 5) for v in ext]
    over = [ext[i] / max((L, W, H)[i], 1e-9) for i in range(3)]
    if max(over) > 1.3:
        axis = "XYZ"[over.index(max(over))]
        raise KitError("the part is %.0f%% of its box along %s (%.4f m vs %.4f m): keep every piece inside "
                       "x in [-L/2, L/2], y in [-W/2, W/2], z in [-H/2, H/2]" % (max(over) * 100, axis, ext["XYZ".index(axis)],
                                                                                    (L, W, H)["XYZ".index(axis)]))
    if max(over) > 1.0:
        s = 1.0 / max(over)                    # a small overshoot (a bevel, a sight blade) is scaled back into the box
        part.data.transform(Matrix.Scale(s, 4))
    small = [ext[i] / max((L, W, H)[i], 1e-9) for i in range(3)]
    result["fill"] = [round(v, 3) for v in small]
    finish(part)
    part.data.materials.clear()
    part.data.materials.append(make_material(args.get("material")))
    result["triangles"] = blib.tri_count(part)
    blib.select_only([part])
    glb = os.path.join(OUT, NAME + ".glb")
    bpy.ops.export_scene.gltf(filepath=glb, use_selection=True, export_format="GLB", export_yup=True)
    result["glb"] = glb
    # the assembler reads the .blend: glTF cannot carry the procedural material (noise, worn edges), and the parts of
    # the second bullpup assembly arrived white (2026-09-27)
    for o in list(bpy.data.objects):
        if o is not part:
            bpy.data.objects.remove(o, do_unlink=True)
    blend = os.path.join(OUT, NAME + ".blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend, compress=True)
    result["blend"] = blend
    blib.setup_render(int(args.get("render_size", 512)), 32, look="preview")
    stage = blib.Stage(part)
    result["renders"] = {v: stage.render(v, os.path.join(OUT, "%s_%s.png" % (NAME, name)))["file"]
                         for v, name in (("side", "side"), ("front", "front"), ("iso", "iso"))}
    stage.close()
    blib.setup_render(int(args.get("render_size", 512)), 16, look="probe")
    result["box_renders"] = {v: box_render(part, v, os.path.join(OUT, "%s_box_%s.png" % (NAME, v)), int(args.get("render_size", 512)))
                             for v in ("left", "front")}
    result["ok"] = True
except (codecheck.CodeRejected, KitError) as exc:
    result["error"] = str(exc)
except Exception as exc:  # noqa: BLE001 - the builder sees the error and tries again
    tb = traceback.format_exc().splitlines()
    result["error"] = "%s: %s | %s" % (type(exc).__name__, str(exc)[:300], " / ".join(l.strip() for l in tb[-4:])[:400])
write_result()
print("[build_part] %s ok=%s %s" % (NAME, result["ok"], result.get("error", "")), flush=True)
