"""Headless sculpting in numpy (issues #11 and #13). Blender's sculpt brushes need a viewport and a mouse; a brush
is only a displacement of the vertices near a point with a falloff, and fitting a mesh to a picture is only pushing
its outline onto the picture's. Everything here takes (verts (n,3) float64, faces (m,3) int) and returns new verts,
so it runs inside Blender (numpy only) and in the tests without it.

Frames: the asset frame (X forward, Y left, Z up), metres. A "view" looks at the part like the pictures do: the
side view from -Y (forward end to the right, up is up), the three-quarter view turned `yaw` degrees about Z towards
the forward end and raised `pitch` degrees."""
import numpy as np


# ---------------------------------------------------------------- mesh helpers
def vertex_normals(verts, faces):
    """Area-weighted vertex normals, unit length."""
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    n = np.zeros_like(verts, dtype=np.float64)
    for k in range(3):
        for c in range(3):
            n[:, c] += np.bincount(faces[:, k], weights=fn[:, c], minlength=len(verts))
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def neighbours(faces, n):
    """Directed edges (a, b) of the mesh, each undirected edge once each way, no duplicates."""
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]).astype(np.int64)
    e = np.concatenate([e, e[:, ::-1]])
    key = np.unique(e[:, 0] * n + e[:, 1])
    return key // n, key % n


def neighbour_mean(field, adj, n):
    """Mean of each vertex's neighbours' values; a vertex without neighbours keeps its own."""
    a, b = adj
    deg = np.bincount(a, minlength=n).astype(np.float64)
    out = np.empty_like(field, dtype=np.float64)
    for c in range(field.shape[1]):
        s = np.bincount(a, weights=field[b, c], minlength=n)
        out[:, c] = np.where(deg > 0, s / np.maximum(deg, 1), field[:, c])
    return out


def diffuse(field, adj, iters=5, keep=None):
    """Spread a per-vertex field over the mesh (average with the neighbours, `iters` times); vertices in `keep`
    hold their value, so the constrained vertices drive the rest."""
    n = len(field)
    field = field.astype(np.float64).copy()
    fixed = field[keep].copy() if keep is not None else None
    for _ in range(iters):
        field = 0.5 * field + 0.5 * neighbour_mean(field, adj, n)
        if keep is not None:
            field[keep] = fixed
    return field


def falloff(dist, radius):
    """1 at the centre, 0 at `radius`, smooth in between (Wendland-like)."""
    t = np.clip(dist / max(radius, 1e-12), 0.0, 1.0)
    return (1.0 - t * t) ** 2


# ---------------------------------------------------------------- brushes (#13)
def inflate(verts, faces, centre, radius, strength):
    """Push the vertices within `radius` of `centre` out along their normals, `strength` metres at the centre
    (negative deflates)."""
    w = falloff(np.linalg.norm(verts - np.asarray(centre), axis=1), radius)
    return verts + vertex_normals(verts, faces) * (w * strength)[:, None]


def move(verts, centre, radius, delta):
    """Drag the vertices within `radius` of `centre` by `delta` (metres) at the centre, less towards the rim."""
    w = falloff(np.linalg.norm(verts - np.asarray(centre), axis=1), radius)
    return verts + w[:, None] * np.asarray(delta, dtype=np.float64)


def smooth(verts, faces, centre, radius, strength=0.5, iters=3):
    """Relax the vertices within `radius` towards their neighbours' mean; `strength` 0-1 per pass."""
    adj = neighbours(faces, len(verts))
    w = falloff(np.linalg.norm(verts - np.asarray(centre), axis=1), radius) * float(strength)
    out = verts.astype(np.float64).copy()
    for _ in range(iters):
        out = out + w[:, None] * (neighbour_mean(out, adj, len(out)) - out)
    return out


def flatten(verts, centre, radius, strength=1.0, normal=None):
    """Press the vertices within `radius` onto one plane: the region's best-fit plane (or the given `normal`
    through its weighted centroid); `strength` 0-1 is how far towards the plane."""
    d = np.linalg.norm(verts - np.asarray(centre), axis=1)
    w = falloff(d, radius)
    if w.sum() < 1e-9:
        return verts
    c = (verts * w[:, None]).sum(axis=0) / w.sum()
    if normal is None:
        q = (verts - c) * np.sqrt(w)[:, None]
        _, _, vt = np.linalg.svd(q[w > 0], full_matrices=False)
        normal = vt[-1]
    normal = np.asarray(normal, dtype=np.float64)
    normal = normal / max(np.linalg.norm(normal), 1e-12)
    dist = (verts - c) @ normal
    return verts - normal[None, :] * (w * float(strength) * dist)[:, None]


def crease(verts, faces, a, b, radius, strength=0.5):
    """Sharpen along the line a-b: the vertices within `radius` of it are pinched towards the line (in the plane
    across it) and lifted a little along their normals, so a soft rounded edge becomes a crease."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    ab = b - a
    t = np.clip(((verts - a) @ ab) / max(ab @ ab, 1e-12), 0.0, 1.0)
    foot = a + t[:, None] * ab
    off = verts - foot
    w = falloff(np.linalg.norm(off, axis=1), radius) * float(strength)
    normals = vertex_normals(verts, faces)
    return verts - off * (w * 0.6)[:, None] + normals * (w * radius * 0.15)[:, None]


BRUSHES = ("inflate", "move", "smooth", "flatten", "crease")


def stroke(verts, faces, op):
    """One logged stroke: {"op", "at": [x,y,z], "radius", "strength", "delta"?, "to"?, "normal"?}. -> new verts"""
    kind = op["op"]
    at, r = op.get("at", (0, 0, 0)), float(op.get("radius", 0.01))
    s = float(op.get("strength", 0.5))
    if kind == "inflate":
        return inflate(verts, faces, at, r, s)
    if kind == "move":
        return move(verts, at, r, op.get("delta", (0, 0, 0)))
    if kind == "smooth":
        return smooth(verts, faces, at, r, s, int(op.get("iters", 3)))
    if kind == "flatten":
        return flatten(verts, at, r, s, op.get("normal"))
    if kind == "crease":
        return crease(verts, faces, at, op.get("to", at), r, s)
    raise ValueError("unknown brush %r (one of %s)" % (kind, ", ".join(BRUSHES)))


# ---------------------------------------------------------------- views and silhouette fitting (#11)
def view_basis(yaw_deg=0.0, pitch_deg=0.0):
    """Rows (right, up, towards-camera) in the asset frame for a camera at the side view (-Y, forward end to the
    right) turned `yaw` degrees about Z towards the forward end and raised `pitch` degrees. Camera coords of a point
    p are p @ R.T: (u right, v up, w towards the camera)."""
    yaw, pitch = np.radians(yaw_deg), np.radians(pitch_deg)
    c = np.array([np.cos(pitch) * np.sin(yaw), -np.cos(pitch) * np.cos(yaw), np.sin(pitch)])   # object -> camera
    f = -c
    up = np.array([0.0, 0.0, 1.0])
    right = np.cross(f, up)
    right /= max(np.linalg.norm(right), 1e-12)
    up2 = np.cross(right, f)
    return np.stack([right, up2, c])


def mask_sdf(mask):
    """A picture mask (bool, rows top-down, cropped to the object) -> signed distance in pixels (+ outside, - inside)
    and its gradient (gx, gy). Pure numpy: the exact EDT is scipy's, this is a chamfer-style two-pass distance,
    close enough for pushing vertices towards a boundary."""
    # the picture is cropped tight to the object, so the array's edge is a boundary too: pad one pixel of outside
    mask = np.pad(np.asarray(mask, bool), 1, constant_values=False)
    h, w = mask.shape
    big = float(h + w)

    def dist_to(target):
        d = np.where(target, 0.0, big)
        for _ in range(2):
            for y in range(1, h):
                d[y] = np.minimum(d[y], d[y - 1] + 1.0)
            for y in range(h - 2, -1, -1):
                d[y] = np.minimum(d[y], d[y + 1] + 1.0)
            for x in range(1, w):
                d[:, x] = np.minimum(d[:, x], d[:, x - 1] + 1.0)
            for x in range(w - 2, -1, -1):
                d[:, x] = np.minimum(d[:, x], d[:, x + 1] + 1.0)
        return d
    sdf = (dist_to(mask) - dist_to(~mask))[1:-1, 1:-1]
    gy, gx = np.gradient(sdf)
    return sdf, gx, gy


def project(verts, view):
    """Vertex pixel coordinates (px right, py down) in the view's mask: the view carries the mapping from the
    camera-plane box of the mesh as first seen to the mask's box, so the mesh may move inside it."""
    P = verts @ view["R"].T
    u0, u1, v0, v1 = view["uv_box"]
    h, w = view["sdf"].shape
    px = (P[:, 0] - u0) / max(u1 - u0, 1e-12) * (w - 1)
    py = (v1 - P[:, 1]) / max(v1 - v0, 1e-12) * (h - 1)
    return px, py, P


def make_view(verts, yaw_deg, pitch_deg, mask, weight=1.0, sdf=None):
    """A fitting target: the picture mask seen from (yaw, pitch), mapped to where the mesh sits now."""
    R = view_basis(yaw_deg, pitch_deg)
    P = verts @ R.T
    if sdf is None:
        s, gx, gy = mask_sdf(mask)
    else:
        s, gx, gy = sdf
    return {"R": R, "sdf": s, "gx": gx, "gy": gy, "weight": float(weight),
            "uv_box": (P[:, 0].min(), P[:, 0].max(), P[:, 1].min(), P[:, 1].max())}


def silhouette_targets(verts, normals, view, band=0.45, margin=0.5):
    """Where each vertex should move, in metres in the asset frame, for this view: every vertex outside the mask onto
    its boundary (the visual hull), every outline vertex (normal across the view) that sits inside the mask out to the
    boundary. -> (displacement (n,3), constrained mask)"""
    px, py, P = project(verts, view)
    h, w = view["sdf"].shape
    ix = np.clip(np.rint(px), 0, w - 1).astype(int)
    iy = np.clip(np.rint(py), 0, h - 1).astype(int)
    s = view["sdf"][iy, ix]
    gx, gy = view["gx"][iy, ix], view["gy"][iy, ix]
    # off the mask's edge the sampled gradient is zero: aim back at the picture box instead
    off_x = np.clip(px, 0, w - 1) - px
    off_y = np.clip(py, 0, h - 1) - py
    n_cam = normals @ view["R"].T
    outline = np.abs(n_cam[:, 2]) < band
    outside = s > margin
    inside_edge = outline & (s < -margin)
    constrained = outside | inside_edge
    step_px = np.zeros((len(verts), 2))
    step_px[constrained, 0] = (-s * gx)[constrained] + off_x[constrained]
    step_px[constrained, 1] = (-s * gy)[constrained] + off_y[constrained]
    u0, u1, v0, v1 = view["uv_box"]
    du = step_px[:, 0] * (u1 - u0) / max(w - 1, 1)
    dv = -step_px[:, 1] * (v1 - v0) / max(h - 1, 1)
    disp_cam = np.stack([du, dv, np.zeros(len(verts))], axis=1)
    return disp_cam @ view["R"], constrained


def fit_silhouette(verts, faces, views, iters=8, step=0.6, diffuse_iters=6, max_step=None):
    """Deform the mesh until its outline in every view lies on that view's mask. Each round: the constrained
    vertices get their target displacement, the rest follow by diffusion, and a fraction `step` is applied. `max_step`
    (metres) caps one round's move; default 2% of the bounding-box diagonal. -> new verts"""
    verts = verts.astype(np.float64).copy()
    adj = neighbours(faces, len(verts))
    diag = np.linalg.norm(verts.max(axis=0) - verts.min(axis=0))
    cap = max_step if max_step is not None else 0.02 * diag
    for _ in range(iters):
        normals = vertex_normals(verts, faces)
        total = np.zeros_like(verts)
        keep = np.zeros(len(verts), bool)
        for v in views:
            disp, constrained = silhouette_targets(verts, normals, v)
            total += disp * v["weight"]
            keep |= constrained
        if not keep.any():
            break
        field = diffuse(total, adj, diffuse_iters, keep=keep)
        norm = np.linalg.norm(field, axis=1, keepdims=True)
        field = np.where(norm > cap, field * (cap / np.maximum(norm, 1e-12)), field)
        verts += step * field
    return verts


def raster(verts2d, faces, size):
    """Fill the projected triangles (pixel coords, rows top-down) into a (size, size) mask. Meant for a few thousand
    faces (a decimated probe): a Python loop per triangle."""
    mask = np.zeros((size, size), bool)
    ys, xs = np.mgrid[0:size, 0:size]
    px, py = xs + 0.5, ys + 0.5
    for f in faces:
        (x0, y0), (x1, y1), (x2, y2) = verts2d[f]
        minx, maxx = int(max(0, np.floor(min(x0, x1, x2)))), int(min(size - 1, np.ceil(max(x0, x1, x2))))
        miny, maxy = int(max(0, np.floor(min(y0, y1, y2)))), int(min(size - 1, np.ceil(max(y0, y1, y2))))
        if maxx < minx or maxy < miny:
            continue
        sx, sy = px[miny:maxy + 1, minx:maxx + 1], py[miny:maxy + 1, minx:maxx + 1]
        d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(d) < 1e-12:
            continue
        a = ((y1 - y2) * (sx - x2) + (x2 - x1) * (sy - y2)) / d
        b = ((y2 - y0) * (sx - x2) + (x0 - x2) * (sy - y2)) / d
        mask[miny:maxy + 1, minx:maxx + 1] |= (a >= -1e-6) & (b >= -1e-6) & (a + b <= 1 + 1e-6)
    return mask


def silhouette_iou(verts, faces, view, size=128):
    """Overlap of the mesh's outline with the view's mask, both resampled to (size, size) over the mask's box."""
    px, py, _ = project(verts, view)
    h, w = view["sdf"].shape
    pts = np.stack([px / max(w - 1, 1) * (size - 1e-3), py / max(h - 1, 1) * (size - 1e-3)], axis=1)
    got = raster(pts, faces, size)
    yi = (np.arange(size) * h / size).astype(int)
    xi = (np.arange(size) * w / size).astype(int)
    want = (view["sdf"] <= 0)[yi][:, xi]
    return float((got & want).sum()) / float(max((got | want).sum(), 1))
