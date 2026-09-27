"""Free end-to-end check of the two Blender passes on a synthetic textured mesh: no vendors, no keys.
Runs only when Blender is installed and MASTERSMITH_BLENDER_TESTS=1 (about a minute).
    set MASTERSMITH_BLENDER_TESTS=1 && python -m pytest -q tests/test_blender_synthetic.py"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mastersmith import config  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("MASTERSMITH_BLENDER_TESTS") != "1" or not os.path.exists(config.BLENDER_BIN),
                                reason="set MASTERSMITH_BLENDER_TESTS=1 with Blender installed")

MAKE_GLB = r'''
import bpy, sys, numpy as np
out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
# a long box (a "rifle") with a textured PBR material, long axis along Y so the prepare pass must rotate it
bpy.ops.mesh.primitive_cube_add(size=1)
ob = bpy.context.active_object
ob.scale = (0.1, 0.6, 0.08)
bpy.ops.object.transform_apply(scale=True)
bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.subdivide(number_cuts=12); bpy.ops.uv.smart_project(); bpy.ops.object.mode_set(mode="OBJECT")
m = bpy.data.materials.new("mat"); m.use_nodes = True; ob.data.materials.append(m)
bsdf = m.node_tree.nodes["Principled BSDF"]
def img(name, rgba, noncolor=False):
    im = bpy.data.images.new(name, 256, 256, alpha=True)
    px = np.tile(np.array(rgba, np.float32), 256 * 256); im.pixels.foreach_set(px); im.pack()
    if noncolor: im.colorspace_settings.name = "Non-Color"
    n = m.node_tree.nodes.new("ShaderNodeTexImage"); n.image = im; return n
m.node_tree.links.new(img("Color", (0.5, 0.3, 0.1, 1)).outputs["Color"], bsdf.inputs["Base Color"])
orm = img("ORM", (1.0, 0.2, 0.0, 1), True)            # roughness 0.2 in G: the lift must fire
sep = m.node_tree.nodes.new("ShaderNodeSeparateColor"); m.node_tree.links.new(orm.outputs["Color"], sep.inputs[0])
m.node_tree.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"]); m.node_tree.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
nm = m.node_tree.nodes.new("ShaderNodeNormalMap"); m.node_tree.links.new(img("NormalGL", (0.5, 0.5, 1.0, 1), True).outputs["Color"], nm.inputs["Color"])
m.node_tree.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
bpy.ops.export_scene.gltf(filepath=out, export_format="GLB")
print("MADE", out)
'''


def blender(script_path, args_path):
    r = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", script_path, "--", args_path], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-1500:])
    return r.stdout


def test_prepare_and_finish_on_a_synthetic_box():
    with tempfile.TemporaryDirectory() as d:
        make = os.path.join(d, "make.py")
        open(make, "w").write(MAKE_GLB)
        glb = os.path.join(d, "seed.glb")
        blender(make, glb)
        work, out = os.path.join(d, "work"), os.path.join(d, "delivery")
        common = {"name": "TestBox", "work_dir": work, "out_dir": out, "tri_budget": 1200, "size_m": 1.0,
                  "engine": "unreal", "forward_axis": "long", "origin": "center", "glb": glb, "probe_size": 256, "render_size": 256}
        a1 = os.path.join(d, "prepare.json")
        json.dump(common, open(a1, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "prepare.py"), a1)
        probe = json.load(open(os.path.join(work, "probe.json")))
        assert {v["view"] for v in probe["views"]} == {"posx", "negx", "posy", "negy", "iso"}
        assert any("long axis was Y" in n for n in probe["notes"])
        # a decision as the probe stage would write it: flip 180, no masks
        json.dump({"yaw": 180, "facing": {"reason": "test"}, "regions": {}}, open(os.path.join(work, "decision.json"), "w"))
        a2 = os.path.join(d, "finish.json")
        json.dump(common, open(a2, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "finish.py"), a2)
        rep = json.load(open(os.path.join(out, "report.json")))
        assert rep["lods"][0]["triangles"] <= 1200 and len(rep["lods"]) == 3
        assert {m["role"] for m in rep["maps"]} == {"BC", "N", "ORM"}
        assert abs(rep["dimensions_m"][0] - 1.0) < 0.01                    # long axis now X, scaled to 1 m
        assert rep["roughness_mean"] > 0.4 and any("lifted" in n for n in rep["notes"])
        assert rep["collision"]["triangles"] <= 128
        for f in ("SM_TestBox.fbx", "SM_TestBox_LOD1.fbx", "SM_TestBox.glb", "SM_TestBox.blend", "preview_iso.png"):
            assert os.path.exists(os.path.join(out, f)), f


def test_add_parts_fits_a_seed_onto_the_body():
    """A second copy of the box is fitted on top of the body at 0.3 m; the body keeps its size and gains faces."""
    with tempfile.TemporaryDirectory() as d:
        make = os.path.join(d, "make.py")
        open(make, "w").write(MAKE_GLB)
        glb = os.path.join(d, "seed.glb")
        blender(make, glb)
        work, out = os.path.join(d, "work"), os.path.join(d, "delivery")
        common = {"name": "TestBox", "work_dir": work, "out_dir": out, "tri_budget": 4000, "size_m": 1.0,
                  "engine": "unreal", "forward_axis": "long", "origin": "center", "glb": glb, "probe_size": 256, "render_size": 256}
        a1 = os.path.join(d, "prepare.json")
        json.dump(common, open(a1, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "prepare.py"), a1)
        json.dump({"yaw": 0, "facing": {"reason": "test"}, "regions": {}}, open(os.path.join(work, "decision.json"), "w"))
        a2 = os.path.join(d, "finish.json")
        json.dump({**common, "add_parts": [{"index": 0, "name": "Rack", "phrase": "a roof rack", "anchor": "body",
                                            "place": "on_top", "size_m": 0.3, "glb": glb}]}, open(a2, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "finish.py"), a2)
        rep = json.load(open(os.path.join(out, "report.json")))
        added = rep.get("added_parts") or []
        assert added and added[0]["name"] == "Rack" and added[0]["place"] == "on_top" and added[0]["faces_added"] > 0
        assert rep["review_renders"] == ["preview_assembly_iso.png", "preview_assembly_top.png"]
        assert all(os.path.exists(os.path.join(out, p)) for p in rep["review_renders"])
        assert rep["bake"]["status"] == "skipped" and "UV domains" in rep["bake"]["reason"]
        assert rep["inspection_renders"] == ["preview_inspection_canopy_hidden.png"]
        assert os.path.exists(os.path.join(out, rep["inspection_renders"][0]))
        assert abs(max(added[0]["size_m"]) - 0.3) < 0.02                  # the spec's size wins
        assert rep["dimensions_m"][0] > 0.99                                # the body was not shrunk
        assert rep["dimensions_m"][2] > 0.08 + 0.03                         # taller: the rack sits on top


def test_add_parts_from_a_prepared_part_with_a_yaw():
    """The part goes through its own prepare pass (as the stage does), is appended from that .blend and turned by the
    facing yaw; it still lands on top at the asked size."""
    with tempfile.TemporaryDirectory() as d:
        make = os.path.join(d, "make.py")
        open(make, "w").write(MAKE_GLB)
        glb = os.path.join(d, "seed.glb")
        blender(make, glb)
        work, out = os.path.join(d, "work"), os.path.join(d, "delivery")
        common = {"name": "TestBox", "work_dir": work, "out_dir": out, "tri_budget": 4000, "size_m": 1.0,
                  "engine": "unreal", "forward_axis": "long", "origin": "center", "glb": glb, "probe_size": 256, "render_size": 256}
        a1 = os.path.join(d, "prepare.json")
        json.dump(common, open(a1, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "prepare.py"), a1)
        part_dir = os.path.join(work, "part_Rack")
        a_part = os.path.join(d, "part_prepare.json")
        json.dump({"name": "Rack", "work_dir": part_dir, "glb": glb, "size_m": 0.3, "forward_axis": "long", "origin": "center",
                   "probe_size": 128}, open(a_part, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "prepare.py"), a_part)
        assert os.path.exists(os.path.join(part_dir, "work.blend")) and os.path.exists(os.path.join(part_dir, "probe_negx.png"))
        json.dump({"yaw": 0, "facing": {"reason": "test"}, "regions": {}}, open(os.path.join(work, "decision.json"), "w"))
        a2 = os.path.join(d, "finish.json")
        json.dump({**common, "add_parts": [{"index": 0, "name": "Rack", "phrase": "a roof rack", "anchor": "body", "place": "on_top",
                                            "size_m": 0.3, "glb": glb, "blend": os.path.join(part_dir, "work.blend"), "yaw": 180}]},
                  open(a2, "w"))
        blender(str(config.ROOT / "mastersmith" / "blender" / "finish.py"), a2)
        rep = json.load(open(os.path.join(out, "report.json")))
        added = rep.get("added_parts") or []
        assert added and added[0]["yaw"] == 180 and added[0]["faces_added"] > 0
        assert abs(max(added[0]["size_m"]) - 0.3) < 0.02
        assert rep["dimensions_m"][2] > 0.08 + 0.03
