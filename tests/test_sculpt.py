"""Headless brushes and silhouette fitting on synthetic meshes: numpy only, no Blender."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mastersmith import sculpt, sdfkit  # noqa: E402


def sphere_mesh(r=0.05, voxel=0.004):
    return sdfkit.mesh(sdfkit.sphere(r), [-r] * 3, [r] * 3, voxel)


def test_normals_point_outwards_on_a_sphere():
    v, f = sphere_mesh()
    n = sculpt.vertex_normals(v, f)
    assert (np.einsum("ij,ij->i", n, v / np.linalg.norm(v, axis=1, keepdims=True)) > 0.8).mean() > 0.95


def test_inflate_moves_only_the_vertices_inside_the_radius():
    v, f = sphere_mesh()
    top = np.array([0, 0, 0.05])
    out = sculpt.inflate(v, f, top, 0.02, 0.003)
    moved = np.linalg.norm(out - v, axis=1)
    near = np.linalg.norm(v - top, axis=1) < 0.02
    assert moved[~near].max() < 1e-9 and moved[near].max() > 0.002
    assert out[:, 2].max() > v[:, 2].max() + 0.002


def test_move_smooth_flatten_and_crease_stay_local():
    v, f = sphere_mesh()
    at = np.array([0.05, 0, 0])
    for op in ({"op": "move", "at": at, "radius": 0.015, "delta": [0.004, 0, 0]},
               {"op": "smooth", "at": at, "radius": 0.015, "strength": 0.8},
               {"op": "flatten", "at": at, "radius": 0.015, "strength": 1.0},
               {"op": "crease", "at": [0.05, -0.01, 0], "to": [0.05, 0.01, 0], "radius": 0.01, "strength": 0.5}):
        out = sculpt.stroke(v, f, op)
        moved = np.linalg.norm(out - v, axis=1)
        far = np.linalg.norm(v - at, axis=1) > 0.03
        assert moved[far].max() < 1e-9, op["op"]
        assert moved.max() > 1e-5, op["op"]
    v, f = sphere_mesh(voxel=0.002)
    flat = sculpt.flatten(v, at, 0.015, 1.0, normal=[1, 0, 0])
    near = np.linalg.norm(v - at, axis=1) < 0.008
    assert np.ptp(flat[near, 0]) < 0.6 * np.ptp(v[near, 0])    # pressed towards one plane across x


def test_diffuse_keeps_the_constrained_vertices_and_spreads_to_the_rest():
    v, f = sphere_mesh()
    adj = sculpt.neighbours(f, len(v))
    field = np.zeros_like(v)
    keep = np.zeros(len(v), bool)
    keep[0] = True
    field[0] = [0, 0, 0.01]
    out = sculpt.diffuse(field, adj, iters=4, keep=keep)
    assert np.allclose(out[0], [0, 0, 0.01])
    a, b = adj
    ring = b[a == 0]
    assert (out[ring, 2] > 0).all() and out[ring, 2].max() < 0.01


def test_view_basis_side_and_quarter():
    R = sculpt.view_basis(0, 0)
    assert np.allclose(R, [[1, 0, 0], [0, 0, 1], [0, -1, 0]])      # right = forward, up = up, camera at -Y
    Rq = sculpt.view_basis(35, 20)
    assert np.allclose(Rq @ Rq.T, np.eye(3)) and Rq[2, 0] > 0 and Rq[2, 2] > 0   # turned towards +X, raised


def test_fit_carves_a_box_to_a_notched_outline():
    v, f = sdfkit.mesh(sdfkit.box((0.1, 0.05, 0.05)), [-0.05, -0.025, -0.025], [0.05, 0.025, 0.025], 0.0025)
    h, w = 100, 200
    mask = np.ones((h, w), bool)
    mask[0:40, 150:200] = False                                   # a notch out of the top-right (front-top) corner
    view = sculpt.make_view(v, 0, 0, mask)
    before = sculpt.silhouette_iou(v, f, view, size=100)
    out = sculpt.fit_silhouette(v, f, [view], iters=25, step=0.7)
    after = sculpt.silhouette_iou(out, f, view, size=100)
    assert after > before + 0.05, (before, after)
    corner = (v[:, 0] > 0.03) & (v[:, 2] > 0.02)                  # the vertices that were in the notch
    still_in_notch = (out[:, 0] > 0.0265) & (out[:, 2] > 0.0065)   # each slid to the nearest wall of the notch
    assert still_in_notch.sum() < 0.02 * corner.sum(), (still_in_notch.sum(), corner.sum())
    untouched = (v[:, 0] < -0.02)                                 # the back half keeps its shape
    assert np.linalg.norm(out[untouched] - v[untouched], axis=1).max() < 0.003


def test_mask_sdf_is_signed_and_zero_at_the_edge():
    m = np.zeros((20, 30), bool)
    m[5:15, 8:22] = True
    sdf, gx, gy = sculpt.mask_sdf(m)
    assert sdf[10, 15] < -3 and sdf[0, 0] > 5 and abs(sdf[5, 15]) <= 1.0
    assert gx[10, 25] > 0 and gx[10, 3] < 0                       # the gradient points away from the object
