"""Allowlist check for builder-written part code, pure Python so both the service and Blender import it.

The builder LLM writes `def build(kit, L, W, H): ...`. It reads the customer's pictures, so its code is untrusted:
only `kit`, `math`, the part's dimensions, its own local names and a few builtins are reachable, no imports, no
dunder or private attributes, no while loops, no exec. Blender runs the same check again before executing."""
import ast

SAFE_BUILTINS = ("range", "len", "min", "max", "abs", "float", "int", "round", "sum", "enumerate", "zip", "list",
                 "tuple", "reversed", "sorted", "any", "all", "bool", "hasattr", "dict", "set")
MODULES = ("kit", "math")
PARAMS = ("L", "W", "H")
LIST_METHODS = ("append", "extend", "insert", "pop", "index", "count", "sort", "reverse", "copy", "items", "keys", "values", "get")
ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr,
    ast.Call, ast.keyword, ast.Name, ast.Load, ast.Store, ast.Attribute, ast.Constant, ast.Tuple, ast.List, ast.Dict,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.If, ast.IfExp, ast.For, ast.Subscript, ast.Slice,
    ast.ListComp, ast.GeneratorExp, ast.comprehension, ast.Starred, ast.Pass, ast.Break, ast.Continue,
    ast.Lambda, ast.Is, ast.IsNot, ast.While, ast.Nonlocal, ast.DictComp, ast.SetComp, ast.JoinedStr, ast.FormattedValue, ast.Set,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)
MAX_SOURCE = 20000


class CodeRejected(ValueError):
    pass


def strip_math_imports(source):
    """`import math` and `from math import pi, sin` are harmless (math is provided) and the builder writes them out of
    habit: the first is dropped, the second becomes `pi = math.pi; sin = math.sin`, both kept at their indentation."""
    import re
    out = []
    for line in source.splitlines():
        m = re.match(r"^(\s*)import\s+math\s*$", line)
        if m:
            if m.group(1):
                out.append(m.group(1) + "pass")       # inside a block: keep the block non-empty
            continue                                  # at the top: drop it, build() stays the only statement
        m = re.match(r"^(\s*)from\s+math\s+import\s+([A-Za-z_][\w, ]*)$", line.rstrip())
        if m:
            names = [n.strip() for n in m.group(2).split(",") if n.strip()]
            if m.group(1):
                out.append(m.group(1) + "; ".join("%s = math.%s" % (n, n) for n in names))
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def check_code(source):
    """Raises CodeRejected with the reason; returns the parsed module when the code may run."""
    if isinstance(source, str):
        source = strip_math_imports(source)
    if not isinstance(source, str) or not source.strip():
        raise CodeRejected("no code")
    if len(source) > MAX_SOURCE:
        raise CodeRejected("the code is longer than %d characters" % MAX_SOURCE)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CodeRejected("syntax error on line %s: %s" % (exc.lineno, exc.msg))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(tree.body) != 1 or len(funcs) != 1 or funcs[0].name != "build":
        raise CodeRejected("the code must be exactly one function, def build(kit, L, W, H)")
    if [a.arg for a in funcs[0].args.args] != ["kit", "L", "W", "H"] or funcs[0].decorator_list:
        raise CodeRejected("the signature must be def build(kit, L, W, H) with no decorators")
    assigned = set(PARAMS) | {"kit"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned.add(node.id)
        elif isinstance(node, ast.arg):
            assigned.add(node.arg)
        elif isinstance(node, ast.FunctionDef) and node is not funcs[0]:
            assigned.add(node.name)          # a helper the builder defines inside build()
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise CodeRejected("%s is not allowed in part code (line %s)" % (type(node).__name__, getattr(node, "lineno", "?")))
        if isinstance(node, ast.FunctionDef) and node is not funcs[0] and (node.decorator_list or node.name.startswith("_")):
            raise CodeRejected("helper functions may not be decorated or private (line %s)" % node.lineno)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise CodeRejected("private attribute %s (line %s)" % (node.attr, node.lineno))
            base = node.value
            if not ((isinstance(base, ast.Name) and base.id in MODULES) or node.attr in LIST_METHODS):
                raise CodeRejected("only kit.* and math.* may be called (line %s: .%s)" % (node.lineno, node.attr))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id.startswith("__"):
                raise CodeRejected("dunder name %s (line %s)" % (node.id, node.lineno))
            if node.id not in assigned and node.id not in MODULES and node.id not in SAFE_BUILTINS:
                raise CodeRejected("unknown name %s (line %s)" % (node.id, node.lineno))
    return tree


def safe_globals(kit_obj, math_mod):
    import builtins
    return {"__builtins__": {n: getattr(builtins, n) for n in SAFE_BUILTINS}, "math": math_mod, "kit": kit_obj}


KIT_DOC = """Part-local frame: +X forward (towards the muzzle / nose), +Y left, +Z up, metres, centred on the part's box:
x in [-L/2, L/2], y in [-W/2, W/2], z in [-H/2, H/2]. Keep every piece inside that box.
""" + """
Calls (every length in metres; sizes are full sizes, not half sizes):
  kit.box(center, size, bevel=None)                         bevelled box
  kit.cylinder(center, radius, length, axis="X", sides=32, radius2=None, bevel=None)   cylinder, or cone with radius2
  kit.tube(center, r_outer, r_inner, length, axis="X", sides=32)   hollow tube
  kit.profile(points, width, offset=0.0, plane="XZ", bevel=None)   closed outline extruded: XZ points (x, z) extruded
        across Y by width; XY points (x, y) extruded up Z; YZ points (y, z) extruded along X. Points go around the
        outline once, no self-crossing; concave outlines are fine (a trigger guard, a stock, a sight blade)
  kit.cut(target, *cutters)                                 boolean difference; the cutters are consumed. A cut that
        removes most of the target is refused with an error
  kit.union(target, *others)                                boolean union into one closed shell
  kit.hole(target, center, radius, depth, axis="Y")         round hole through
  kit.slot(target, center, size)                            box-shaped cut
  kit.array(piece, count, offset)                           count copies, each offset (dx, dy, dz) further; joined
  kit.mirror(piece, axis="Y")                               add a mirrored copy
  kit.move(piece, offset)  kit.rotate(piece, degrees, axis="Z", pivot=(0,0,0))  kit.join(*pieces)
Shapes beyond straight extrusions:
  kit.revolve(points, center=(0,0,0), axis="X", sides=48)   lathe: (a, r) points along the axis spun round it -
        stepped barrels, muzzle devices, knobs, scope bodies, wheel rims, domes; an end with r > 0 is capped
  kit.loft(sections, axis="X", samples=48)                  skin through cross-sections [(position, outline), ...];
        axis X outlines are (y, z) points, Y: (x, z), Z: (x, y). Outlines may differ (a rectangle to a rounded nose) -
        a receiver or stock that changes section along its length, a tapering nose, a fuselage
  kit.sweep(points, radius, sides=16)                       round rod along a 3D path: handles, loops, bent pipes
Shaping a piece (returns the same piece):
  kit.fillet(piece, radius, segments=4, region=None)        real rounded edges; make the piece with bevel=0 first.
        region=(x0, y0, z0, x1, y1, z1) rounds only the edges inside that box
  kit.smooth(piece, levels=2, crease_angle=None)            subdivision: a blocky bevel=0 cage becomes moulded curves
        (grips, stocks, rounded housings); edges sharper than crease_angle degrees stay crisp
  kit.bend(piece, degrees, along="X", toward="Z", fixed=None)   curve the piece's length (a banana magazine:
        along="Z", toward="X"); the section at coordinate `fixed` stays put (default the middle)
  kit.taper(piece, scale, along="X", keep="min")            scale the cross-section from 1 at the keep end to scale
  kit.shell(piece, thickness)                               hollow it inwards; cut an opening to show the wall
bevel=None picks a width from the piece's smallest side (6%, 0.2-1.5 mm); bevel=0 keeps a razor edge.
Build the shape the picture shows, not a stack of boxes: moulded housings and stocks are lofts or smoothed cages with
fillets, round parts are revolves, curved parts are bent. Details (ribs, vents, screws, seams, grooves, texture
panels) are cut or added as real geometry, the picture's surface is not painted on later.
"""
