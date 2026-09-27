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
from mathutils import Matrix

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
        lin.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(lin)


def make_material(spec):
    spec = spec or {}
    mat = bpy.data.materials.new("MI_part_%s" % NAME)
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*hex_rgb(spec.get("color")), 1.0)
    bsdf.inputs["Metallic"].default_value = 1.0 if spec.get("metal") else 0.0
    bsdf.inputs["Roughness"].default_value = float(spec.get("roughness", 0.6))
    return mat


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
    blib.setup_render(int(args.get("render_size", 512)), 32, look="preview")
    stage = blib.Stage(part)
    result["renders"] = {v: stage.render(v, os.path.join(OUT, "%s_%s.png" % (NAME, name)))["file"]
                         for v, name in (("side", "side"), ("front", "front"), ("iso", "iso"))}
    stage.close()
    result["ok"] = True
except (codecheck.CodeRejected, KitError) as exc:
    result["error"] = str(exc)
except Exception as exc:  # noqa: BLE001 - the builder sees the error and tries again
    tb = traceback.format_exc().splitlines()
    result["error"] = "%s: %s | %s" % (type(exc).__name__, str(exc)[:300], " / ".join(l.strip() for l in tb[-4:])[:400])
write_result()
print("[build_part] %s ok=%s %s" % (NAME, result["ok"], result.get("error", "")), flush=True)
