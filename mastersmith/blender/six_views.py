"""The delivered asset from all six sides, orthographic, for the final review (inside Blender).
    blender -b -Y --python six_views.py -- <args.json>
args: {"glb", "out_dir", "size"}. Writes view_<left|right|front|back|top|bottom>.png, each framed on the asset.

An assembly that looked right from the side was a mess from the front (the bullpup of 2026-09-27): the review sees every
side before it may call an asset good."""
import json
import os
import sys

import bpy
from mathutils import Matrix

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blib  # noqa: E402

args = json.load(open(sys.argv[sys.argv.index("--") + 1]))
VIEWS = ("left", "right", "front", "back", "top", "bottom")

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=os.path.abspath(args["glb"]))
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
for o in [o for o in bpy.data.objects if o.type not in ("MESH", "EMPTY")]:
    bpy.data.objects.remove(o, do_unlink=True)
blib.select_only(meshes)
if len(meshes) > 1:
    bpy.ops.object.join()
target = bpy.context.view_layer.objects.active
lo, hi = blib.dims(target)
blib.setup_render(int(args.get("size", 768)), 32, look="preview")
stage = blib.Stage(target, look="preview")           # its lights; the camera is replaced by an orthographic one
cam = bpy.data.objects.new("ViewCam", bpy.data.cameras.new("ViewCam"))
bpy.context.collection.objects.link(cam)
scn = bpy.context.scene
os.makedirs(args["out_dir"], exist_ok=True)
out = {}
# each view also gets a soft light from its own direction: lit from one side, the right and bottom views came out
# nearly black and the review could not judge them (2026-09-28)
head = bpy.data.objects.new("ViewLight", bpy.data.lights.new("ViewLight", "SUN"))
head.data.energy = 0.6
head.data.angle = 0.9
bpy.context.collection.objects.link(head)
for view in VIEWS:
    blib.ortho_camera(cam, view, lo, hi, margin=1.08)
    scn.camera = cam
    # about 20 degrees off the camera's axis: straight along it, flat metal (a rail, a muzzle face) mirrored it back
    # as a white blow-out
    head.matrix_world = cam.matrix_world @ Matrix.Rotation(0.35, 4, "X") @ Matrix.Rotation(0.2, 4, "Y")
    path = os.path.join(args["out_dir"], "view_%s.png" % view)
    scn.render.filepath = path
    bpy.ops.render.render(write_still=True)
    out[view] = os.path.basename(path)
stage.close()
json.dump(out, open(os.path.join(args["out_dir"], "views.json"), "w"))
print("[six_views] %s" % ", ".join(VIEWS), flush=True)
