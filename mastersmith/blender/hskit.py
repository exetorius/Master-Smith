"""The hard-surface kit (inside Blender): the calls a builder-written `build(kit, L, W, H)` may make.

Part-local frame: +X forward, +Y left, +Z up, metres, centred on the part's box: x in [-L/2, L/2],
y in [-W/2, W/2], z in [-H/2, H/2]. Every piece is a closed, bevelled shell; cuts are exact booleans. The kit keeps
every object it made so the runner can join what `build` returns and delete the rest."""
import math

import bmesh
import bpy
from mathutils import Matrix, Vector

AXES = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}


class KitError(ValueError):
    pass


def _vec3(v, what):
    try:
        t = tuple(float(c) for c in v)
    except TypeError:
        raise KitError("%s must be three numbers, got %r" % (what, v))
    if len(t) != 3:
        raise KitError("%s must be three numbers, got %r" % (what, v))
    return Vector(t)


def _axis(axis):
    a = str(axis).upper()
    if a not in AXES:
        raise KitError("axis must be X, Y or Z, got %r" % (axis,))
    return a


def _segments_cross(p1, p2, p3, p4):
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2, d3, d4 = orient(p3, p4, p1), orient(p3, p4, p2), orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


class Kit:
    def __init__(self, L, W, H):
        self.L, self.W, self.H = float(L), float(W), float(H)
        self.made = []

    # ------------------------------------------------------------------ internals
    def _link(self, bm, name):
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        o = bpy.data.objects.new(name, me)
        bpy.context.collection.objects.link(o)
        self.made.append(o)
        return o

    def _bevel(self, o, bevel, smallest):
        if bevel is None:
            bevel = min(max(smallest * 0.06, 0.0002), 0.0015)
        bevel = float(bevel)
        if bevel <= 0:
            return o
        bevel = min(bevel, smallest * 0.3)
        m = o.modifiers.new("bevel", "BEVEL")
        m.width = bevel
        m.segments = 2
        m.limit_method = "ANGLE"
        m.angle_limit = math.radians(30)
        m.use_clamp_overlap = True
        self._apply(o, m)
        return o

    @staticmethod
    def _deselect():
        bpy.ops.object.select_all(action="DESELECT")

    def _apply(self, o, mod):
        self._deselect()
        bpy.context.view_layer.objects.active = o
        o.select_set(True)
        bpy.ops.object.modifier_apply(modifier=mod.name)

    def _check(self, o):
        if not isinstance(o, bpy.types.Object) or o not in self.made:
            raise KitError("expected a piece the kit made, got %r" % (o,))
        return o

    # ------------------------------------------------------------------ pieces
    def box(self, center=(0, 0, 0), size=None, bevel=None, name="box"):
        c = _vec3(center, "center")
        s = _vec3(size if size is not None else (self.L, self.W, self.H), "size")
        if min(s) <= 0:
            raise KitError("box size must be positive, got %r" % (tuple(s),))
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * s.x, v.co.y * s.y, v.co.z * s.z)) + c
        return self._bevel(self._link(bm, name), bevel, min(s))

    def cylinder(self, center=(0, 0, 0), radius=None, length=None, axis="X", sides=32, radius2=None, bevel=None,
                 name="cylinder"):
        a = _axis(axis)
        c = _vec3(center, "center")
        r1 = float(radius if radius is not None else min(self.W, self.H) / 2)
        r2 = float(radius2) if radius2 is not None else r1
        ln = float(length if length is not None else self.L)
        sides = int(max(6, min(int(sides), 128)))
        if r1 < 0 or r2 < 0 or max(r1, r2) <= 0 or ln <= 0:
            raise KitError("cylinder needs a positive radius and length")
        bm = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=sides, radius1=r1, radius2=r2, depth=ln)
        rot = {"X": Matrix.Rotation(math.radians(90), 4, "Y"), "Y": Matrix.Rotation(math.radians(-90), 4, "X"),
               "Z": Matrix.Identity(4)}[a]
        bmesh.ops.transform(bm, matrix=rot, verts=bm.verts)
        bmesh.ops.translate(bm, vec=c, verts=bm.verts)
        return self._bevel(self._link(bm, name), bevel, min(2 * max(r1, r2), ln))

    def tube(self, center=(0, 0, 0), r_outer=None, r_inner=None, length=None, axis="X", sides=32, bevel=None, name="tube"):
        r_outer = float(r_outer if r_outer is not None else min(self.W, self.H) / 2)
        r_inner = float(r_inner if r_inner is not None else r_outer * 0.6)
        if not 0 < r_inner < r_outer:
            raise KitError("tube needs 0 < r_inner < r_outer")
        ln = float(length if length is not None else self.L)
        outer = self.cylinder(center, r_outer, ln, axis, sides, bevel=bevel, name=name)
        inner = self.cylinder(center, r_inner, ln * 1.02, axis, sides, bevel=0, name=name + "_bore")
        return self.cut(outer, inner)

    def profile(self, points, width=None, offset=0.0, plane="XZ", bevel=None, name="profile"):
        """A closed 2D outline extruded straight. plane "XZ": points are (x, z), extruded across Y (width, centred on
        y=offset); "XY": points (x, y), extruded up Z centred on z=offset; "YZ": points (y, z), extruded along X."""
        plane = str(plane).upper()
        if plane not in ("XZ", "XY", "YZ"):
            raise KitError("plane must be XZ, XY or YZ")
        pts = []
        for p in points:
            try:
                u, v = float(p[0]), float(p[1])
            except (TypeError, IndexError, ValueError):
                raise KitError("profile points must be (u, v) pairs, got %r" % (p,))
            if not pts or (abs(u - pts[-1][0]) > 1e-7 or abs(v - pts[-1][1]) > 1e-7):
                pts.append((u, v))
        if len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) < 1e-7 and abs(pts[0][1] - pts[-1][1]) < 1e-7:
            pts.pop()
        if len(pts) < 3:
            raise KitError("a profile needs at least three distinct points")
        area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts))) / 2
        if abs(area) < 1e-10:
            raise KitError("the profile has no area")
        n = len(pts)
        for i in range(n):
            for j in range(i + 1, n):
                if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                    continue
                if _segments_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                    raise KitError("the profile outline crosses itself (edges %d and %d)" % (i, j))
        if area < 0:
            pts.reverse()
        depth = float(width if width is not None else {"XZ": self.W, "XY": self.H, "YZ": self.L}[plane])
        if depth <= 0:
            raise KitError("profile width must be positive")
        off = float(offset)

        def place(u, v, w):
            if plane == "XZ":
                return Vector((u, w, v))
            if plane == "XY":
                return Vector((u, v, w))
            return Vector((w, u, v))
        bm = bmesh.new()
        a = [bm.verts.new(place(u, v, off - depth / 2)) for u, v in pts]
        b = [bm.verts.new(place(u, v, off + depth / 2)) for u, v in pts]
        bm.faces.new(a)
        bm.faces.new(list(reversed(b)))
        for i in range(n):
            bm.faces.new((a[i], a[(i + 1) % n], b[(i + 1) % n], b[i]))
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        us, vs = [p[0] for p in pts], [p[1] for p in pts]
        smallest = min(max(us) - min(us), max(vs) - min(vs), depth)
        return self._bevel(self._link(bm, name), bevel, smallest)

    # ------------------------------------------------------------------ operations
    @staticmethod
    def _extent(o):
        if not len(o.data.vertices):
            return 0.0, 0
        xs = [v.co for v in o.data.vertices]
        lo = Vector((min(v.x for v in xs), min(v.y for v in xs), min(v.z for v in xs)))
        hi = Vector((max(v.x for v in xs), max(v.y for v in xs), max(v.z for v in xs)))
        e = hi - lo
        return e.x * e.y * e.z, len(o.data.polygons)

    def cut(self, target, *cutters):
        """Boolean difference, one cutter at a time. Cutters may be pieces joined together even when they overlap
        (exact solver with self-intersection on). A cut that empties the target or shrinks its bounds to under half
        is undone and reported: a cutter that swallows the part is a mistake, not a design (the bullpup handguard's
        joined slot cutters erased the whole shroud, 2026-09-27)."""
        self._check(target)
        for k, c in enumerate(cutters):
            self._check(c)
            before_mesh = target.data.copy()
            vol0, _f0 = self._extent(target)
            m = target.modifiers.new("cut", "BOOLEAN")
            m.operation = "DIFFERENCE"
            m.solver = "EXACT"
            m.use_self = True
            m.use_hole_tolerant = True
            m.object = c
            c.hide_render = True
            c.hide_set(True)
            self._apply(target, m)
            self.made.remove(c)
            bpy.data.objects.remove(c, do_unlink=True)
            vol1, f1 = self._extent(target)
            if f1 == 0 or (vol0 > 0 and vol1 < vol0 * 0.5):
                broken = target.data
                target.data = before_mesh
                bpy.data.meshes.remove(broken)
                raise KitError("cut number %d removed most of %s (its bounds fell to %.0f%%): make each cutter a closed "
                               "solid that overlaps only what it should remove" % (k + 1, target.name,
                                                                                  100.0 * vol1 / max(vol0, 1e-12)))
            bpy.data.meshes.remove(before_mesh)
        return target

    def union(self, target, *others):
        """Boolean union into one closed shell (for cutters built from several pieces, or solid features)."""
        self._check(target)
        for o in others:
            self._check(o)
            m = target.modifiers.new("union", "BOOLEAN")
            m.operation = "UNION"
            m.solver = "EXACT"
            m.use_self = True
            m.use_hole_tolerant = True
            m.object = o
            o.hide_render = True
            o.hide_set(True)
            self._apply(target, m)
            self.made.remove(o)
            bpy.data.objects.remove(o, do_unlink=True)
        return target

    def hole(self, target, center, radius, depth, axis="Y", sides=24):
        return self.cut(target, self.cylinder(center, radius, depth, axis, sides, bevel=0, name="hole"))

    def slot(self, target, center, size):
        return self.cut(target, self.box(center, size, bevel=0, name="slot"))

    def array(self, obj, count, offset):
        self._check(obj)
        count = int(max(1, min(int(count), 400)))
        off = _vec3(offset, "offset")
        copies = []
        for i in range(1, count):
            c = obj.copy()
            c.data = obj.data.copy()
            bpy.context.collection.objects.link(c)
            c.data.transform(Matrix.Translation(off * i))
            self.made.append(c)
            copies.append(c)
        return self._join(obj, copies)

    def mirror(self, obj, axis="Y"):
        self._check(obj)
        a = _axis(axis)
        c = obj.copy()
        c.data = obj.data.copy()
        bpy.context.collection.objects.link(c)
        scale = Vector((-1 if a == "X" else 1, -1 if a == "Y" else 1, -1 if a == "Z" else 1))
        c.data.transform(Matrix.Diagonal(scale.to_4d()))
        c.data.flip_normals()
        self.made.append(c)
        return self._join(obj, [c])

    def move(self, obj, offset):
        self._check(obj)
        obj.data.transform(Matrix.Translation(_vec3(offset, "offset")))
        return obj

    def rotate(self, obj, degrees, axis="Z", pivot=(0, 0, 0)):
        self._check(obj)
        p = _vec3(pivot, "pivot")
        m = Matrix.Translation(p) @ Matrix.Rotation(math.radians(float(degrees)), 4, _axis(axis)) @ Matrix.Translation(-p)
        obj.data.transform(m)
        return obj

    def join(self, *objs):
        objs = [self._check(o) for o in objs]
        if not objs:
            raise KitError("join needs at least one piece")
        return self._join(objs[0], objs[1:])

    def _join(self, first, others):
        if not others:
            return first
        self._deselect()
        for o in [first] + list(others):
            o.select_set(True)
        bpy.context.view_layer.objects.active = first
        bpy.ops.object.join()
        for o in others:
            if o in self.made:
                self.made.remove(o)
        return first


from codecheck import KIT_DOC as API_DOC  # noqa: E402,F401  (one text for the prompt and the kit)
