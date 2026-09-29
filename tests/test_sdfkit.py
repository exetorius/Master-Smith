"""The SDF kit: every primitive and operator by sampling, and marching cubes to a closed mesh."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mastersmith import sdfkit  # noqa: E402
from mastersmith.sdfkit import box, capsule, cone, cylinder, mm, plane, rounded_box, smooth_union, sphere, torus, wedge  # noqa: E402


def d(s, *pts):
    return s(np.array(pts, dtype=float))


def test_primitives_measure_distance():
    assert np.allclose(d(sphere(0.01), [0, 0, 0], [0.02, 0, 0]), [-0.01, 0.01])
    assert np.allclose(d(box((0.02, 0.04, 0.06)), [0, 0, 0], [0.02, 0, 0], [0, 0.03, 0]), [-0.01, 0.01, 0.01])
    assert np.allclose(d(cylinder(0.01, 0.1, "x"), [0, 0, 0], [0, 0.02, 0], [0.06, 0, 0]), [-0.01, 0.01, 0.01])
    assert np.allclose(d(cylinder(0.01, 0.1, "z"), [0, 0, 0.06]), [0.01])
    assert d(cone(0.02, 0.01, 0.1, "x"), [-0.05, 0.02, 0])[0] < 1e-6 < d(cone(0.02, 0.01, 0.1, "x"), [0.05, 0.02, 0])[0]
    assert np.allclose(d(capsule([0, 0, 0], [0.1, 0, 0], 0.01), [0.05, 0, 0], [0.12, 0, 0]), [-0.01, 0.01])
    assert np.allclose(d(torus(0.05, 0.01, "z"), [0.05, 0, 0], [0.07, 0, 0]), [-0.01, 0.01])
    assert np.allclose(d(plane([0, 0, 1], 0.01), [0, 0, 0], [0, 0, 0.02]), [-0.01, 0.01])
    assert d(rounded_box((0.02, 0.02, 0.02), 0.002), [0.01, 0.01, 0.01])[0] > 0 > d(rounded_box((0.02, 0.02, 0.02), 0.002), [0, 0, 0])[0]
    w = wedge((0.1, 0.04, 0.02), axis="z", across="y", taper=0.5)
    assert d(w, [0, 0.015, -0.009])[0] < 0 < d(w, [0, 0.015, 0.009])[0]     # narrow at the top, full at the bottom
    assert mm(5.56) == 0.00556


def test_operators_and_transforms():
    a, b = sphere(0.01), sphere(0.01).translate([0.015, 0, 0])
    assert d(a | b, [0.0075, 0, 0])[0] < 0 and d(a & b, [0.0075, 0, 0])[0] < 0 and d(a & b, [-0.005, 0, 0])[0] > 0
    assert d(a - b, [0.008, 0, 0])[0] > 0 and d(a - b, [-0.005, 0, 0])[0] < 0
    su = smooth_union(a, b, 0.005)
    assert d(su, [0.0075, 0.009, 0])[0] < d(a | b, [0.0075, 0.009, 0])[0]     # the fillet fills the notch
    r = cylinder(0.01, 0.1, "x").rotate("z", 90)
    assert d(r, [0, 0.04, 0])[0] < 0 and d(r, [0.04, 0, 0])[0] > 0
    assert np.allclose(d(sphere(0.01).scale(2), [0, 0, 0]), [-0.02])
    m = sphere(0.005).translate([0, 0.02, 0]).mirror("y")
    assert d(m, [0, -0.02, 0])[0] < 0 and d(m, [0, 0.02, 0])[0] < 0
    sh = sphere(0.01).shell(0.002)
    assert d(sh, [0.01, 0, 0])[0] < 0 and d(sh, [0, 0, 0])[0] > 0
    el = sphere(0.005).elongate([0.02, 0, 0])
    assert d(el, [0.02, 0, 0])[0] < 0 and d(el, [0.03, 0, 0])[0] > 0
    rep = box((0.002, 0.01, 0.01)).repeat([0.01, 0, 0], [5, 1, 1])
    assert all(v < 0 for v in d(rep, [-0.02, 0, 0], [0, 0, 0], [0.02, 0, 0])) and d(rep, [0.03, 0, 0])[0] > 0
    assert d(rep, [0.005, 0, 0])[0] > 0                                       # between two slots


def test_mesh_is_closed_and_the_right_size():
    v, f = sdfkit.mesh(cylinder(0.01, 0.1, "x") - cylinder(0.003, 0.2, "x"), [-0.05, -0.01, -0.01], [0.05, 0.01, 0.01], 0.001)
    size = v.max(axis=0) - v.min(axis=0)
    assert abs(size[0] - 0.1) < 0.003 and abs(size[1] - 0.02) < 0.003
    edges = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert (counts == 2).all()                                                # watertight
    assert d(cylinder(0.01, 0.1, "x") - cylinder(0.003, 0.2, "x"), [0, 0, 0])[0] > 0   # the bore is a hole


def test_part_script_runs_in_the_kit_namespace():
    src = "def part(kit, L, W, H):\n    return kit.cylinder(W / 2, L, axis='x') - kit.cylinder(kit.mm(3), L * 2, axis='x')\n"
    s = sdfkit.run_part_script(src, 0.1, 0.02, 0.02)
    assert d(s, [0, 0.006, 0])[0] < 0 and d(s, [0, 0, 0])[0] > 0
    with pytest.raises(ValueError):
        sdfkit.run_part_script("x = 1\n", 1, 1, 1)
    with pytest.raises(Exception):
        sdfkit.run_part_script("def part(kit, L, W, H):\n    import os\n    return kit.sphere(1)\n", 1, 1, 1)
