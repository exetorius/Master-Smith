"""Assembly builds: the code allowlist, the plan's numbers, and (with Blender) a part built and assembled."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mastersmith", "blender"))
import codecheck  # noqa: E402

from mastersmith import config  # noqa: E402
from mastersmith.stages.plan import object_dims, to_metres, validate_plan  # noqa: E402

GOOD = '''def build(kit, L, W, H):
    body = kit.cylinder((0, 0, 0), min(W, H) / 2, L, axis="X", sides=32)
    parts = []
    for i in range(3):
        parts.append(kit.box((-L / 4 + i * L / 4, 0, H / 2 * 0.8), (L * 0.05, W * 0.5, H * 0.2)))
    body = kit.hole(body, (0, 0, 0), min(W, H) * 0.1, W * 1.2, axis="Y")
    return [body] + parts
'''


def test_part_code_allowlist_accepts_kit_code():
    codecheck.check_code(GOOD)
    # what the builder writes naturally: a helper, a lambda, an "is not None" test
    codecheck.check_code(HELPERS)


HELPERS = '''def build(kit, L, W, H):
    def rib(x):
        return kit.box((x, 0, 0), (L * 0.02, W, H))
    f = lambda v: v * 0.5
    parts = [rib(f(x)) for x in (0, L / 4)]
    extra = None
    if extra is not None:
        parts.append(extra)
    return parts
'''


@pytest.mark.parametrize("code, why", [
    ("import os\ndef build(kit, L, W, H):\n    return kit.box()", "exactly one function"),
    ("def build(kit, L, W, H):\n    return __import__('os')", "dunder"),
    ("def build(kit, L, W, H):\n    return open('x')", "unknown name open"),
    ("def build(kit, L, W, H):\n    return kit.box().__class__", "private attribute"),
    ("def build(kit, L, W, H):\n    return kit._link(None, 'x')", "private attribute"),
    ("def build(kit, L, W, H):\n    import bpy\n    return kit.box()", "Import is not allowed"),
    ("def build(kit, L, W, H):\n    x = kit.box()\n    return x.data", "only kit.* and math.*"),
    ("def build(kit, L, W, H):\n    return eval('1')", "unknown name eval"),
    ("def build(kit, L, W):\n    return kit.box()", "signature"),
    ("def build(kit, L, W, H):\n    def _inner():\n        return 1\n    return kit.box()", "private"),
    ("def build(kit, L, W, H):\n    class A:\n        pass\n    return kit.box()", "ClassDef is not allowed"),
])
def test_part_code_allowlist_refuses_escapes(code, why):
    with pytest.raises(codecheck.CodeRejected) as exc:
        codecheck.check_code(code)
    assert why in str(exc.value)


def test_percent_boxes_become_metres_in_the_asset_frame():
    dims = object_dims(0.68, (1000, 400), (200, 400))          # side 1000x400 px, front 200x400 px
    assert dims == pytest.approx((0.68, 0.136, 0.272))
    lo, hi = to_metres([80, 100, 40, 60], [40, 60], dims)      # the front fifth, a middle band, the middle across
    assert lo == pytest.approx([0.204, -0.0136, -0.0272]) and hi == pytest.approx([0.34, 0.0136, 0.0272])
    lo, hi = to_metres([0, 10, 0, 100], [0, 100], dims)        # the rear tenth, full height and width
    assert lo[0] == pytest.approx(-0.34) and hi[2] == pytest.approx(0.136) and lo[1] == pytest.approx(-0.068)
    lo, hi = to_metres([50, 50, 50, 50], [50, 50], dims)       # a degenerate box gets the minimum size
    assert all(h - l >= 0.68 * 0.004 - 1e-9 for l, h in zip(lo, hi))


def test_views_for_a_plan_fall_back_to_the_side_alone():
    from mastersmith.stages.plan import pick_views
    assert pick_views({"views": ["side", "muzzle", "mirror"]}, "weapon") == ("side", "muzzle", False)
    assert pick_views({"views": ["side"]}, "weapon") == ("side", None, False)          # the muzzle view was refused
    assert pick_views({"views": ["tq"], "seed_views": ["f", "l", "b", "r"]}, "vehicle") == ("l", "f", True)
    assert pick_views({"views": ["tq"]}, "vehicle") is None and pick_views({"views": ["a", "b"]}, "prop") is None


def test_contact_faces_come_from_the_boxes():
    from mastersmith.stages.assembly import contacts
    frame = {"name": "Frame", "box_min": [-0.08, -0.016, 0.016], "box_max": [0.08, 0.016, 0.040]}
    guard = {"name": "Guard", "box_min": [-0.013, -0.009, -0.012], "box_max": [0.039, 0.008, 0.017]}
    sight = {"name": "Sight", "box_min": [0.06, -0.004, 0.040], "box_max": [0.07, 0.004, 0.050]}
    far = {"name": "Far", "box_min": [0.5, 0.5, 0.5], "box_max": [0.6, 0.6, 0.6]}
    parts = [frame, guard, sight, far]
    dims = (0.185, 0.038, 0.132)
    assert contacts(guard, parts, dims) == ["the top (+z) face meets Frame"]
    assert contacts(sight, parts, dims) == ["the bottom (-z) face meets Frame"]
    assert set(contacts(frame, parts, dims)) == {"the bottom (-z) face meets Guard", "the top (+z) face meets Sight"}
    assert contacts(far, parts, dims) == []


def test_plan_validation_cleans_names_methods_and_materials():
    dims = (0.68, 0.14, 0.27)
    raw = {"parts": [
        {"name": "barrel", "method": "code", "side_box": [80, 100, 45, 55], "front_span": [45, 55],
         "material": {"color": "#222222", "metal": True, "roughness": 0.4}},
        {"name": "barrel", "method": "sculpt", "side_box": [70, 80, 40, 60], "front_span": [40, 60], "material": {"color": "black"}},
        {"name": "Grip!", "method": "vendor", "side_box": [30, 45, 50, 100], "front_span": [35, 65]},
        {"name": "Broken", "side_box": [1, 2], "front_span": [0, 100]},
    ], "notes": "n"}
    plan = validate_plan(raw, dims)
    names = [p["name"] for p in plan["parts"]]
    assert names == ["Barrel", "Barrel2", "Grip"]
    assert plan["parts"][1]["method"] == "code" and plan["parts"][2]["method"] == "vendor"
    assert plan["parts"][1]["material"]["color"] == "#808080" and plan["parts"][0]["material"]["metal"] is True
    assert [d["name"] for d in plan["dropped"]] == ["Broken"]
    with pytest.raises(ValueError):
        validate_plan({"parts": raw["parts"][:1]}, dims)


BLENDER = pytest.mark.skipif(os.environ.get("MASTERSMITH_BLENDER_TESTS") != "1" or not os.path.exists(config.BLENDER_BIN),
                             reason="set MASTERSMITH_BLENDER_TESTS=1 with Blender installed")


def _blender(script, args_path):
    r = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(config.ROOT / "mastersmith" / "blender" / script),
                        "--", args_path], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


@BLENDER
def test_code_parts_build_and_assemble_into_a_game_ready_asset():
    with tempfile.TemporaryDirectory() as d:
        parts = []
        for name, code, size, box in (
                ("Body", GOOD, [0.3, 0.05, 0.08], ([-0.15, -0.025, -0.04], [0.15, 0.025, 0.04])),
                ("Sight", "def build(kit, L, W, H):\n    return kit.profile([(-L/2, -H/2), (L/2, -H/2), (0, H/2)], W)",
                 [0.04, 0.01, 0.03], ([0.0, -0.005, 0.04], [0.04, 0.005, 0.07]))):
            a = os.path.join(d, name + ".json")
            json.dump({"name": name, "code": code, "size": size, "material": {"color": "#303030", "metal": True, "roughness": 0.4},
                       "out_dir": os.path.join(d, "parts"), "render_size": 128}, open(a, "w"))
            _blender("build_part.py", a)
            res = json.load(open(os.path.join(d, "parts", name + ".json")))
            assert res["ok"], res.get("error")
            parts.append({"name": name, "kind": "code", "glb": res["glb"], "box_min": box[0], "box_max": box[1]})
        a = os.path.join(d, "asm.json")
        json.dump({"name": "T", "out_dir": os.path.join(d, "out"), "tri_budget": 20000, "atlas_size": 512, "render_size": 128,
                   "check_size": 256, "spec": {"category": "weapon"}, "parts": parts}, open(a, "w"))
        _blender("assemble.py", a)
        rep = json.load(open(os.path.join(d, "out", "report.json")))
        assert {m["role"] for m in rep["maps"]} == {"BC", "N", "ORM"}
        assert rep["dimensions_m"][0] == pytest.approx(0.3, abs=0.005) and rep["dimensions_m"][2] == pytest.approx(0.095, abs=0.004)
        assert [l["lod"] for l in rep["lods"]] == [0, 1, 2] and rep["collision"]["triangles"] <= 256
        assert set(rep["check_renders"]) == {"left", "front"} and len(rep["detail_renders"]) == 2
        assert {s["name"] for s in rep["sockets"]} >= {"Muzzle", "Sight"}
        for f in rep["files"]:
            assert os.path.exists(os.path.join(d, "out", f)), f
        bad = os.path.join(d, "bad.json")
        json.dump({"name": "Bad", "code": "def build(kit, L, W, H):\n    return kit.box((0, 0, 0), (L * 2, W, H))",
                   "size": [0.1, 0.1, 0.1], "out_dir": os.path.join(d, "parts")}, open(bad, "w"))
        _blender("build_part.py", bad)
        res = json.load(open(os.path.join(d, "parts", "Bad.json")))
        assert not res["ok"] and "of its box along X" in res["error"]
