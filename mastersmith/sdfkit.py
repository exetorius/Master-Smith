"""Exact parts as signed distance functions (issue #12). A barrel is `cylinder(r, L) - cylinder(bore, L)`, a rail is a
box with `repeat`ed slots subtracted, a moulded blend is `smooth_union(a, b, k)`: the shape is text, the edges are
as sharp as written, round is round, and no topology can break. Units are metres; `mm(x)` converts.

    def part(kit, L, W, H):                          # the part's box, metres, centred at the origin
        body = kit.cylinder(W / 2, L, axis="x")
        return body - kit.cylinder(kit.mm(5.56) / 2, L * 1.1, axis="x")

`mesh(sdf, lo, hi, voxel)` samples the field on a grid and runs marching cubes (scikit-image) -> (verts, faces);
`write_glb` saves it (trimesh). Every function below takes and returns an SDF; an SDF is called with points (n, 3)
and answers distances (n,), negative inside."""
import numpy as np


def mm(x):
    return float(x) / 1000.0


class SDF:
    def __init__(self, f):
        self.f = f

    def __call__(self, p):
        return self.f(np.asarray(p, dtype=np.float64).reshape(-1, 3))

    # set operations as operators
    def __or__(self, other):
        return union(self, other)

    def __and__(self, other):
        return intersect(self, other)

    def __sub__(self, other):
        return subtract(self, other)

    # transforms
    def translate(self, v):
        v = np.asarray(v, dtype=np.float64)
        return SDF(lambda p: self.f(p - v))

    def rotate(self, axis, degrees):
        """About the origin; axis "x" | "y" | "z" or a vector."""
        a = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}.get(axis, axis)
        a = np.asarray(a, dtype=np.float64)
        a /= max(np.linalg.norm(a), 1e-12)
        t = np.radians(degrees)
        K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
        R = np.eye(3) + np.sin(t) * K + (1 - np.cos(t)) * (K @ K)
        Rinv = R.T
        return SDF(lambda p: self.f(p @ Rinv.T))

    def scale(self, s):
        """Uniform scale (a non-uniform scale is not a distance any more; stretch the primitives instead)."""
        s = float(s)
        return SDF(lambda p: self.f(p / s) * s)

    def mirror(self, axis="y"):
        """The shape and its mirror image across the plane through the origin."""
        i = "xyz".index(axis)

        def f(p):
            q = p.copy()
            q[:, i] = np.abs(q[:, i])
            return self.f(q)
        return SDF(f)

    def shell(self, thickness):
        """Hollow: the surface as a skin of `thickness`."""
        return SDF(lambda p: np.abs(self.f(p)) - thickness / 2.0)

    def round(self, r):
        """Round every edge by `r` (grows the shape by r; shrink the primitive by r to keep the size)."""
        return SDF(lambda p: self.f(p) - r)

    def elongate(self, h):
        """Stretch the shape by inserting a flat section of half-lengths h (3-vector) at its middle."""
        h = np.asarray(h, dtype=np.float64)
        return SDF(lambda p: self.f(p - np.clip(p, -h, h)))

    def repeat(self, pitch, count):
        """Copies along the axes: pitch (3-vector, 0 = no repeat on that axis), count (3-vector of copies, odd or
        even; the copies are centred on the origin)."""
        pitch = np.asarray(pitch, dtype=np.float64)
        count = np.asarray(count, dtype=np.float64)
        lo, hi = -(count - 1) / 2.0, (count - 1) / 2.0

        def f(p):
            q = p.copy()
            for i in range(3):
                if pitch[i] > 0 and count[i] > 1:
                    k = np.clip(np.round(q[:, i] / pitch[i]), lo[i], hi[i])
                    q[:, i] = q[:, i] - k * pitch[i]
            return self.f(q)
        return SDF(f)


# ---------------------------------------------------------------- primitives (centred at the origin)
def _axes(axis):
    i = "xyz".index(axis)
    others = [j for j in range(3) if j != i]
    return i, others


def sphere(r):
    return SDF(lambda p: np.linalg.norm(p, axis=1) - r)


def box(size):
    """size = (Lx, Ly, Lz) full lengths."""
    h = np.asarray(size, dtype=np.float64) / 2.0

    def f(p):
        q = np.abs(p) - h
        return np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0)
    return SDF(f)


def rounded_box(size, r):
    h = np.asarray(size, dtype=np.float64) / 2.0 - r
    return box(h * 2).round(r)


def cylinder(r, length, axis="x"):
    i, others = _axes(axis)

    def f(p):
        d = np.stack([np.linalg.norm(p[:, others], axis=1) - r, np.abs(p[:, i]) - length / 2.0], axis=1)
        return np.linalg.norm(np.maximum(d, 0), axis=1) + np.minimum(d.max(axis=1), 0)
    return SDF(f)


def cone(r0, r1, length, axis="x"):
    """A truncated cone: radius r0 at the -axis end, r1 at the +axis end."""
    i, others = _axes(axis)
    hl = length / 2.0

    def f(p):
        t = np.clip((p[:, i] + hl) / max(length, 1e-12), 0, 1)
        r = r0 + (r1 - r0) * t
        radial = np.linalg.norm(p[:, others], axis=1) - r
        # the slant makes the radial distance a little short; scale by the cone's half-angle cosine
        cos_a = length / np.hypot(length, r1 - r0)
        d = np.stack([radial * cos_a, np.abs(p[:, i]) - hl], axis=1)
        return np.linalg.norm(np.maximum(d, 0), axis=1) + np.minimum(d.max(axis=1), 0)
    return SDF(f)


def capsule(a, b, r):
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    ab = b - a

    def f(p):
        t = np.clip(((p - a) @ ab) / max(ab @ ab, 1e-12), 0, 1)
        return np.linalg.norm(p - (a + t[:, None] * ab), axis=1) - r
    return SDF(f)


def torus(R, r, axis="z"):
    i, others = _axes(axis)

    def f(p):
        q = np.stack([np.linalg.norm(p[:, others], axis=1) - R, p[:, i]], axis=1)
        return np.linalg.norm(q, axis=1) - r
    return SDF(f)


def plane(normal, offset=0.0):
    """The half-space below the plane n.p = offset (inside where n.p < offset)."""
    n = np.asarray(normal, dtype=np.float64)
    n /= max(np.linalg.norm(n), 1e-12)
    return SDF(lambda p: p @ n - offset)


def wedge(size, axis="z", across="y", taper=0.5):
    """A box whose +`axis` end is narrowed to `taper` of its full width measured `across` (a stock's comb, a ramp)."""
    i, _others = _axes(axis)
    j = "xyz".index(across)
    h = np.asarray(size, dtype=np.float64) / 2.0

    def f(p):
        t = np.clip((p[:, i] + h[i]) / max(size[i], 1e-12), 0, 1)
        wj = h[j] * (1 - (1 - taper) * t)
        q = np.abs(p) - h
        q[:, j] = np.abs(p[:, j]) - wj
        return np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0)
    return SDF(f)


# ---------------------------------------------------------------- operators
def union(*shapes):
    return SDF(lambda p: np.min(np.stack([s(p) for s in shapes]), axis=0))


def intersect(*shapes):
    return SDF(lambda p: np.max(np.stack([s(p) for s in shapes]), axis=0))


def subtract(a, b):
    return SDF(lambda p: np.maximum(a(p), -b(p)))


def _smin(a, b, k):
    h = np.clip(0.5 + 0.5 * (b - a) / max(k, 1e-12), 0, 1)
    return b + (a - b) * h - k * h * (1 - h)


def smooth_union(a, b, k):
    """Union with the joint blended over `k` metres: a moulded fillet."""
    return SDF(lambda p: _smin(a(p), b(p), k))


def smooth_subtract(a, b, k):
    return SDF(lambda p: -_smin(-a(p), b(p), k))


def smooth_intersect(a, b, k):
    return SDF(lambda p: -_smin(-a(p), -b(p), k))


KIT = {name: obj for name, obj in globals().items()
       if name in ("mm", "sphere", "box", "rounded_box", "cylinder", "cone", "capsule", "torus", "plane", "wedge",
                   "union", "intersect", "subtract", "smooth_union", "smooth_subtract", "smooth_intersect")}


class Kit:
    """The namespace a part script gets: `kit.cylinder(...)`, `kit.mm(5)`, ..."""

    def __init__(self):
        for k, v in KIT.items():
            setattr(self, k, v)


# ---------------------------------------------------------------- meshing
def mesh(sdf, lo, hi, voxel, chunk=2_000_000):
    """Marching cubes over the box [lo, hi] at `voxel` metres. -> (verts (n,3) float64 in metres, faces (m,3) int)"""
    from skimage import measure
    lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
    lo, hi = lo - 2 * voxel, hi + 2 * voxel                 # a margin so the surface closes at the box
    axes = [np.arange(lo[i], hi[i] + voxel, voxel) for i in range(3)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)
    vals = np.empty(len(pts))
    for i in range(0, len(pts), chunk):
        vals[i:i + chunk] = sdf(pts[i:i + chunk])
    vol = vals.reshape(gx.shape)
    if vol.min() > 0 or vol.max() < 0:
        raise ValueError("the field has no surface inside the box (all %s)" % ("outside" if vol.min() > 0 else "inside"))
    verts, faces, _normals, _values = measure.marching_cubes(vol, level=0.0, spacing=(voxel, voxel, voxel))
    verts = verts + lo
    faces = faces.astype(np.int64)                           # skimage's winding faces outward for a field negative inside
    return verts, faces


def write_glb(verts, faces, path):
    import trimesh
    m = trimesh.Trimesh(vertices=np.asarray(verts, dtype=np.float64), faces=np.asarray(faces, dtype=np.int64), process=True)
    m.export(path)
    return path


def run_part_script(source, L, W, H):
    """Run a part script (`def part(kit, L, W, H)` returning an SDF) in a namespace that holds only the kit."""
    ns = {"__builtins__": {"range": range, "len": len, "min": min, "max": max, "abs": abs, "float": float, "int": int,
                           "round": round, "list": list, "tuple": tuple, "zip": zip, "enumerate": enumerate}}
    exec(compile(source, "part.py", "exec"), ns)
    if "part" not in ns:
        raise ValueError("the script defines no `part(kit, L, W, H)`")
    out = ns["part"](Kit(), float(L), float(W), float(H))
    if not isinstance(out, SDF):
        raise ValueError("part() must return a kit shape")
    return out
