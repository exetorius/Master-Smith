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
                 name="cylinder", height=None, depth=None):
        length = length if length is not None else (height if height is not None else depth)   # the builder's other words
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

    def tube(self, center=(0, 0, 0), r_outer=None, r_inner=None, length=None, axis="X", sides=32, bevel=None, name="tube",
             height=None, depth=None):
        length = length if length is not None else (height if height is not None else depth)
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
        (to under a quarter) is undone and reported: a cutter that swallows the part is a mistake, not a design (the bullpup handguard's
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
            if f1 == 0 or (vol0 > 0 and vol1 < vol0 * 0.25):     # halving a cylinder is a design; erasing it is not
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

    # ------------------------------------------------------------------ shapes beyond straight extrusions
    def revolve(self, points, center=(0, 0, 0), axis="X", sides=48, bevel=None, name="revolve"):
        """A lathe: an outline of (a, r) points - a along the axis, r the radius - spun round the axis. Barrels with
        steps, muzzle devices, knobs, scope bodies, wheel rims, domes. The outline runs from one end to the other; an
        end with r > 0 is capped. A closed outline (first point = last) with every r > 0 makes a ring."""
        ax = _axis(axis)
        c = _vec3(center, "center")
        pts = []
        for p in points:
            try:
                u, r = float(p[0]), float(p[1])
            except (TypeError, IndexError, ValueError):
                raise KitError("revolve points must be (a, r) pairs, got %r" % (p,))
            if r < 0:
                raise KitError("revolve radii must be 0 or more, got %r" % (r,))
            if not pts or abs(u - pts[-1][0]) > 1e-7 or abs(r - pts[-1][1]) > 1e-7:
                pts.append((u, r))
        closed = len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) < 1e-7 and abs(pts[0][1] - pts[-1][1]) < 1e-7
        if closed:
            pts.pop()
            if min(r for _, r in pts) <= 1e-6:
                raise KitError("a closed revolve outline must stay off the axis (every r > 0)")
        if len(pts) < 2 or max(r for _, r in pts) <= 0:
            raise KitError("revolve needs at least two points and some radius")
        sides = int(max(8, min(int(sides), 128)))

        def place(u, r, ang):
            y, z = r * math.cos(ang), r * math.sin(ang)
            if ax == "X":
                return c + Vector((u, y, z))
            if ax == "Y":
                return c + Vector((z, u, y))
            return c + Vector((y, z, u))
        bm = bmesh.new()
        rings = []
        for u, r in pts:
            if r <= 1e-6:
                rings.append([bm.verts.new(place(u, 0, 0))])
            else:
                rings.append([bm.verts.new(place(u, r, 2 * math.pi * k / sides)) for k in range(sides)])
        pairs = list(zip(rings, rings[1:])) + ([(rings[-1], rings[0])] if closed else [])
        for r0, r1 in pairs:
            if len(r0) == 1 and len(r1) == 1:
                continue
            for k in range(sides):
                k1 = (k + 1) % sides
                if len(r0) == 1:
                    bm.faces.new((r0[0], r1[k], r1[k1]))
                elif len(r1) == 1:
                    bm.faces.new((r0[k], r1[0], r0[k1]))
                else:
                    bm.faces.new((r0[k], r1[k], r1[k1], r0[k1]))
        if not closed:
            for ring in (rings[0], rings[-1]):
                if len(ring) > 1:
                    bm.faces.new(ring)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        us, rs = [u for u, _ in pts], [r for _, r in pts]
        smallest = min((max(us) - min(us)) or max(rs), 2 * max(rs))
        return self._bevel(self._link(bm, name), bevel, smallest)

    @staticmethod
    def _resample(pts, n):
        """A closed outline as n points evenly spaced by length, counter-clockwise, starting where the outline crosses
        the +u ray from its centre (so lofted sections line up without twisting)."""
        area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
        if area < 0:
            pts = list(reversed(pts))
        cu = sum(p[0] for p in pts) / len(pts)
        cv = sum(p[1] for p in pts) / len(pts)
        best = None
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            if (a[1] - cv) * (b[1] - cv) <= 0 and a[1] != b[1]:
                t = (cv - a[1]) / (b[1] - a[1])
                u = a[0] + t * (b[0] - a[0])
                if u > cu and (best is None or u > best[1]):
                    best = (i, u)
        if best is not None:
            i = best[0]
            pts = [(best[1], cv)] + pts[i + 1:] + pts[:i + 1]
        segs = [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
        lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs]
        total = sum(lens)
        out, i, acc = [], 0, 0.0
        for k in range(n):
            t = total * k / n
            while i < len(segs) - 1 and acc + lens[i] < t:
                acc += lens[i]
                i += 1
            f = (t - acc) / lens[i] if lens[i] > 0 else 0.0
            a, b = segs[i]
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
        return out

    def loft(self, sections, axis="X", samples=48, bevel=None, name="loft"):
        """A solid skinned through cross-sections along an axis: sections is a list of (position, outline), outline a
        closed list of points in the plane across the axis - axis "X": (y, z); "Y": (x, z); "Z": (x, y). Outlines
        may differ in shape and point count (a rectangle to a rounded nose, a stock thinning to its butt). Positions
        must increase. Both ends are capped."""
        ax = _axis(axis)
        secs = []
        for s in sections:
            try:
                pos, outline = float(s[0]), [(float(p[0]), float(p[1])) for p in s[1]]
            except (TypeError, IndexError, ValueError):
                raise KitError("loft sections must be (position, [(u, v), ...]), got %r" % (s,))
            if len(outline) > 2 and abs(outline[0][0] - outline[-1][0]) < 1e-7 and abs(outline[0][1] - outline[-1][1]) < 1e-7:
                outline.pop()
            if len(outline) < 3:
                raise KitError("each loft outline needs at least three points")
            secs.append((pos, outline))
        if len(secs) < 2:
            raise KitError("loft needs at least two sections")
        if any(b[0] <= a[0] for a, b in zip(secs, secs[1:])):
            raise KitError("loft section positions must increase")
        n = int(max(8, min(int(samples), 128)))

        def place(pos, u, v):
            if ax == "X":
                return Vector((pos, u, v))
            if ax == "Y":
                return Vector((u, pos, v))
            return Vector((u, v, pos))
        bm = bmesh.new()
        rings = [[bm.verts.new(place(pos, u, v)) for u, v in self._resample(outline, n)] for pos, outline in secs]
        for r0, r1 in zip(rings, rings[1:]):
            for k in range(n):
                k1 = (k + 1) % n
                bm.faces.new((r0[k], r0[k1], r1[k1], r1[k]))
        bm.faces.new(rings[0])
        bm.faces.new(list(reversed(rings[-1])))
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        o = self._link(bm, name)
        e = [max(v.co[i] for v in o.data.vertices) - min(v.co[i] for v in o.data.vertices) for i in range(3)]
        return self._bevel(o, bevel, min(e))          # the bevel's 30 degree limit leaves the curved facets alone

    def sweep(self, points, radius, sides=16, name="sweep"):
        """A round rod along a 3D path of points (x, y, z): handles, carry loops, bent pipes, roll bars, wire. Corners
        stay as given; add points to round them."""
        pts = [_vec3(p, "sweep point") for p in points]
        if len(pts) < 2:
            raise KitError("sweep needs at least two points")
        r = float(radius)
        if r <= 0:
            raise KitError("sweep radius must be positive")
        sides = int(max(8, min(int(sides), 64)))
        cu = bpy.data.curves.new(name, "CURVE")
        cu.dimensions = "3D"
        cu.bevel_depth = r
        cu.bevel_resolution = max(0, sides // 4 - 2)
        cu.use_fill_caps = True
        sp = cu.splines.new("POLY")
        sp.points.add(len(pts) - 1)
        for i, p in enumerate(pts):
            sp.points[i].co = (p.x, p.y, p.z, 1.0)
        tmp = bpy.data.objects.new(name + "_curve", cu)
        bpy.context.collection.objects.link(tmp)
        dg = bpy.context.evaluated_depsgraph_get()
        me = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg))
        bpy.data.objects.remove(tmp, do_unlink=True)
        bpy.data.curves.remove(cu)
        bm = bmesh.new()
        bm.from_mesh(me)
        bpy.data.meshes.remove(me)
        bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        return self._link(bm, name)

    # ------------------------------------------------------------------ shaping a piece
    def fillet(self, obj, radius, segments=4, region=None, angle=35):
        """Round the piece's sharp edges with a real radius (several segments), instead of the small default bevel.
        Make the piece with bevel=0 first. region=(x0, y0, z0, x1, y1, z1) limits it to edges inside that box (round the
        top edges of a housing, keep its bottom sharp). Only edges sharper than `angle` degrees are rounded."""
        self._check(obj)
        rad = float(radius)
        if rad <= 0:
            raise KitError("fillet radius must be positive")
        lo = hi = None
        if region is not None:
            try:
                reg = [float(x) for x in region]
            except (TypeError, ValueError):
                raise KitError("region must be (x0, y0, z0, x1, y1, z1)")
            if len(reg) != 6:
                raise KitError("region must be (x0, y0, z0, x1, y1, z1)")
            lo = Vector([min(reg[i], reg[i + 3]) for i in range(3)])
            hi = Vector([max(reg[i], reg[i + 3]) for i in range(3)])
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        limit = math.radians(float(angle))
        edges = []
        for e in bm.edges:
            if len(e.link_faces) != 2 or e.calc_face_angle(0) < limit:
                continue
            if lo is not None:
                m = (e.verts[0].co + e.verts[1].co) / 2
                if not all(lo[i] - 1e-6 <= m[i] <= hi[i] + 1e-6 for i in range(3)):
                    continue
            edges.append(e)
        if edges:
            bmesh.ops.bevel(bm, geom=edges, offset=rad, offset_type="OFFSET", segments=int(max(1, min(int(segments), 12))),
                            profile=0.5, affect="EDGES", clamp_overlap=True)
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        return obj

    def smooth(self, obj, levels=2, crease_angle=None):
        """Subdivision surface: turns a rough, blocky cage into smooth moulded curves (a grip, a stock, a rounded
        housing, a helmet). Edges sharper than crease_angle degrees stay crisp (creased); None rounds everything.
        Model the cage with bevel=0 and few faces - every level multiplies the faces by four."""
        self._check(obj)
        levels = int(max(1, min(int(levels), 3)))
        if crease_angle is not None:
            limit = math.radians(float(crease_angle))
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            layer = bm.edges.layers.float.get("crease_edge") or bm.edges.layers.float.new("crease_edge")
            for e in bm.edges:
                if len(e.link_faces) != 2 or e.calc_face_angle(0) >= limit:
                    e[layer] = 1.0
            bm.to_mesh(obj.data)
            bm.free()
        m = obj.modifiers.new("smooth", "SUBSURF")
        m.levels = levels
        m.render_levels = levels
        m.boundary_smooth = "PRESERVE_CORNERS"
        self._apply(obj, m)
        return obj

    def _slice(self, obj, axis_i, count):
        """Cut the piece into count slabs across an axis, so a bend or taper has vertices to move."""
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        co = [v.co[axis_i] for v in bm.verts]
        lo, hi = min(co), max(co)
        n = Vector((0, 0, 0))
        n[axis_i] = 1.0
        for k in range(1, count):
            p = Vector((0, 0, 0))
            p[axis_i] = lo + (hi - lo) * k / count
            bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], plane_co=p, plane_no=n)
        bm.to_mesh(obj.data)
        bm.free()
        return lo, hi

    def bend(self, obj, degrees, along="X", toward="Z", fixed=None, segments=None, axis=None):
        """Bend the piece so its length (along an axis) curves toward another axis, turning `degrees` in total: a
        banana magazine (along="Z", toward="X"), a drooping barrel, a curved grip. The cross-section at `fixed` (a
        coordinate along the axis; default the piece's middle) stays put; negative degrees curve the other way."""
        self._check(obj)
        a, b = _axis(axis or along), _axis(toward)
        if a == b:
            raise KitError("bend: along and toward must be different axes")
        deg = float(degrees)
        if abs(deg) < 1e-6:
            return obj
        ai, bi = "XYZ".index(a), "XYZ".index(b)
        count = int(segments) if segments else int(max(8, min(64, abs(deg) / 3)))
        lo, hi = self._slice(obj, ai, max(1, min(count, 128)))
        f = (lo + hi) / 2 if fixed is None else float(fixed)
        span = max(hi - f, f - lo, 1e-6)
        k = math.radians(deg) / span
        R = 1.0 / k
        for v in obj.data.vertices:
            t, u = v.co[ai] - f, v.co[bi]
            phi, r = k * t, R - u
            co = v.co.copy()
            co[ai] = f + r * math.sin(phi)
            co[bi] = R - r * math.cos(phi)
            v.co = co
        obj.data.update()
        return obj

    def taper(self, obj, scale, along="X", keep="min", segments=8, axis=None):
        """Narrow (or widen) the piece along an axis: its cross-section is scaled from 1 at the `keep` end ("min" or
        "max") to `scale` at the other, about the piece's centre line. scale is one number or a pair for the two other
        axes in XYZ order (along X: (y, z)). A stock thinning to its butt, a tapered barrel, a wedge nose."""
        self._check(obj)
        ai = "XYZ".index(_axis(axis or along))           # the builder writes axis= as for every other call
        others = [i for i in range(3) if i != ai]
        try:
            sc = (float(scale[0]), float(scale[1])) if isinstance(scale, (list, tuple)) else (float(scale), float(scale))
        except (TypeError, ValueError, IndexError):
            raise KitError("taper scale must be a number or a pair")
        if min(sc) <= 0:
            raise KitError("taper scale must be positive")
        lo, hi = self._slice(obj, ai, int(max(1, min(int(segments), 64))))
        vs = obj.data.vertices
        ctr = [(min(v.co[i] for v in vs) + max(v.co[i] for v in vs)) / 2 for i in range(3)]
        for v in vs:
            t = (v.co[ai] - lo) / max(hi - lo, 1e-9)
            if str(keep).lower() == "max":
                t = 1 - t
            co = v.co.copy()
            for j, i in enumerate(others):
                co[i] = ctr[i] + (co[i] - ctr[i]) * (1 + (sc[j] - 1) * t)
            v.co = co
        obj.data.update()
        return obj

    def shell(self, obj, thickness):
        """Hollow the piece to a wall of this thickness (inwards). Cut an opening afterwards to show it: a hood, a
        shroud, a cowling, a bucket."""
        self._check(obj)
        t = float(thickness)
        if t <= 0:
            raise KitError("shell thickness must be positive")
        m = obj.modifiers.new("shell", "SOLIDIFY")
        m.thickness = t
        m.offset = -1.0
        m.use_even_offset = True
        m.use_quality_normals = True
        self._apply(obj, m)
        return obj

    # the builder reaches for other libraries' names; these are the same calls
    def translate(self, obj, offset):
        return self.move(obj, offset)

    def difference(self, target, *cutters):
        return self.cut(target, *cutters)

    subtract = difference

    def extrude(self, points, width=None, offset=0.0, plane="XZ", bevel=None, name="profile"):
        return self.profile(points, width, offset, plane, bevel, name)

    def lathe(self, points, center=(0, 0, 0), axis="X", sides=48, bevel=None, name="revolve"):
        return self.revolve(points, center, axis, sides, bevel, name)

    def round_edges(self, obj, radius, segments=4, region=None, angle=35):
        return self.fillet(obj, radius, segments, region, angle)

    def subdivide(self, obj, levels=2, crease_angle=None):
        return self.smooth(obj, levels, crease_angle)

    def solidify(self, obj, thickness):
        return self.shell(obj, thickness)

    def __getattr__(self, name):
        raise KitError("kit.%s does not exist. The calls are: box, cylinder, tube, profile, revolve, loft, sweep, cut, "
                       "union, hole, slot, array, mirror, move, rotate, join, fillet, smooth, bend, taper, shell" % name)

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
