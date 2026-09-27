"""Local synthetic regression for organic normal repair; no providers or game project."""
import os
from pathlib import Path
import subprocess

import pytest

from mastersmith.spec import Spec


def test_organic_repair_is_explicit_and_roundtrips():
    assert "smooth_organic_normals" not in Spec(name="Panel", description="metal panel").texture_fixes
    brief = Spec(name="Tissue", description="continuous tissue", texture_fixes=["smooth_organic_normals"])
    assert Spec.from_dict(brief.to_dict()).texture_fixes == ["smooth_organic_normals"]


def test_shared_corner_repair_preserves_surface_data(tmp_path):
    executable = os.environ.get("BLENDER_BIN", "")
    if not Path(executable).is_file():
        pytest.skip("Set BLENDER_BIN to run the isolated Blender regression")
    script = tmp_path / "normal_fixture.py"
    script.write_text(r'''
import sys
import bpy
sys.path.insert(0, sys.argv[sys.argv.index('--') + 1])
from surface_normals import corner_normal_splits, smooth_organic_normals

bpy.ops.wm.read_factory_settings(use_empty=True)
m = bpy.data.meshes.new('Tissue')
# Last vertex deliberately duplicates a position: repair must not weld it.
m.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,0)], [], [(0,1,2),(0,2,3)])
o = bpy.data.objects.new('Tissue', m)
bpy.context.collection.objects.link(o)
uv = m.uv_layers.new(name='UVMap')
for i, loop in enumerate(uv.data): loop.uv = (i / 10, i / 20)
for p in m.polygons: p.use_smooth = True
for e in m.edges: e.use_edge_sharp = True
m.normals_split_custom_set([(0,0,1)]*3 + [(0,.8,.6)]*3)
positions = [tuple(v.co) for v in m.vertices]
faces = [tuple(p.vertices) for p in m.polygons]
uvs = [tuple(v.uv) for v in uv.data]
assert corner_normal_splits(m)['split_corners'] > 0
report = smooth_organic_normals(o)
assert report['before']['split_corners'] > 0
assert corner_normal_splits(m)['split_corners'] == 0
assert positions == [tuple(v.co) for v in m.vertices]
assert faces == [tuple(p.vertices) for p in m.polygons]
assert uvs == [tuple(v.uv) for v in m.uv_layers[0].data]
assert not any(e.use_edge_sharp for e in m.edges)
print('ORGANIC_NORMAL_REGRESSION_PASSED')
''')
    helper_dir = Path(__file__).resolve().parents[1] / "mastersmith/blender"
    result = subprocess.run([executable, "-b", "--factory-startup", "--python-exit-code", "1",
                             "--python", str(script), "--", str(helper_dir)],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1500:]
    assert "ORGANIC_NORMAL_REGRESSION_PASSED" in result.stdout
