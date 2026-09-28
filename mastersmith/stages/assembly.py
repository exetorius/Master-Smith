"""Assembly builds (docs/ASSEMBLY.md): plan the parts, build each one right, assemble, check against the pictures.
Nothing here repairs a finished mesh: a part that is wrong is rebuilt, a part in the wrong place is moved."""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from .. import config, pricing
from ..fal import first_url, image_url
from ..llm import extract_json
from .finish import BLENDER_DIR, _blender
from .partorient import PART_CHECK, orient_part
from .plan import crop_to_object, draw_grid, make_plan, pick_views, to_metres

sys.path.insert(0, str(BLENDER_DIR))
import codecheck  # noqa: E402  (pure Python, shared with Blender)

CODE_PROMPT = """You are modelling ONE part of a game-ready {category} in Blender, in Python, with a hard-surface kit.
The whole object: {description}
This part: {name} - {what}
Its box is L={L_mm:.1f} mm long (x), W={W_mm:.1f} mm wide (y), H={H_mm:.1f} mm tall (z).

Picture 1: the part's box from the SIDE (forward to the RIGHT, up is up), cropped exactly to the box and centred on a
square, with a scale in millimetres from the part's centre: read positions off it (x across, z up).
Picture 2: the same from the FRONT (the part's left on the right of the picture): y across, z up, millimetres.
Picture 3: the part in context with its neighbours, its box in blue.
Model what is inside the box at the level of detail the reference shows: the outline, the proportions and every
visible hole, slot, groove, recess, raised panel, step, facet and chamfer. A plain slab where the reference has
recesses and facets is wrong. Large parts carry most of the object's look: build them in layers (the main outline,
then raised panels and bosses, then recesses and cuts, then small features). Crisp hard-surface geometry, nothing
melted or blobby. A cutter must be one closed solid: for a rounded slot, kit.union a box and two end cylinders into
one cutter first.
Pick the kit call by the shape, not boxes for everything: a round part is kit.revolve (one outline gives every step,
groove and chamfer), a section that changes along the length is kit.loft, a moulded or rounded body is a bevel=0 cage
with kit.smooth or kit.fillet, a curved part is kit.bend, a rod or loop is kit.sweep.

Before the code, write a numbered list of every feature you can see in pictures 1 and 2: the outline, each opening,
recess, rib, groove, step, raised panel, screw or pin head, seam and chamfer, each with its position and size in mm
read off the scale. Then model every item on the list.

The kit:
{kit}

Write exactly one function and nothing else:
def build(kit, L, W, H):
    ...
    return [pieces]
Use only kit.*, math.* and the numbers L, W, H (metres). No imports. Answer with the code in one ```python block."""

REFINE_PROMPT = """The picture compares what your code built for {name} ({what}) with the reference. Top row: the SIDE,
bottom row: the FRONT. In each row the LEFT is the reference cropped to the part's box and the RIGHT is your build
rendered orthographically in the same box, same scale, same framing - so they should line up. In the FRONT
reference, parts in front of this one (a barrel, a muzzle device) can hide it; judge what is visible.
Be critical. First list, numbered, every feature the REFERENCE shows (the outline, each opening, recess, rib,
groove, step, raised panel, screw or pin head, seam, chamfer, rounded edge) and after each write FOUND or MISSING for
your build, and whether its position and size match. Then compare the outlines: where does yours stick out or fall
short, in mm?
Then the last line: VERDICT: OK only if nothing is MISSING and the outline matches within a few percent of the box.
A missing feature, a wrong outline, a part that does not fill its box like the reference does, or a plain slab where
the reference has recesses and facets is VERDICT: FIX - then give the corrected complete function, with every
MISSING feature added, in one ```python block after the verdict. Your previous code:
```python
{code}
```"""

CHECK_PROMPT = """You are checking an assembled game asset against its reference. Pictures 1 and 2: the reference side
and front views. Pictures 3 and 4: the assembly, same framing, same grid. Blue rectangles are the planned part boxes
(percent of the pictures), labelled by name.
The parts: {parts}
Find parts that are in the wrong place or the wrong size by more than 2% of the picture, parts whose shape is
clearly wrong, and GAPS: parts that should touch (a guard and the frame, a grip and the receiver) with background
showing between them - move the part so its box overlaps the neighbour's. Answer JSON only:
{{"ok": true/false, "moves": [{{"name": "...", "side_box": [x_left, x_right, z_top, z_bottom], "front_span": [y_left, y_right]}}],
  "rebuild": [{{"name": "...", "why": "what is wrong with its shape"}}], "notes": "..."}}
Coordinates in percent of the REFERENCE pictures, like the plan. Empty lists when nothing needs changing.
The LAST picture shows the assembly from ABOVE (the forward end at the top of the picture). Parts that sit on the
object's centreline (a barrel, a muzzle device, a rail, sights) must line up with the body along one straight line; a
part off to one side, or a body that runs at a slant to them, is wrong: move the part's front_span, or rebuild the
body when it is the slanted one.
Use common sense about how the object works, not only the pictures: a firearm's chamber, barrel, the bore through its
handguard and its muzzle device lie on ONE straight axis - a bullet has to pass straight through. A barrel that sits
off that axis, points at a slant or misses the handguard's opening is wrong even where the picture is unclear."""


def _code_from(text):
    """The fenced block that holds def build (a reply can quote other snippets first), else the last block. A reply cut
    off inside its block (the feature list ahead of the code uses tokens) or without fences gives the text from
    `def build(` on, so the error the builder sees is about its code, not about the prose before it."""
    text = text or ""
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n?(.*?)```", text, re.S)
    for b in blocks:
        if "def build(" in b:
            return b.strip()
    at = text.rfind("def build(")
    if at >= 0:
        return text[at:].split("```")[0].strip()
    return (blocks[-1] if blocks else text).strip()


def _crop_part(src, box_pct, dst, margin=0.18):
    """The part's region of a silhouette-cropped picture with a margin, the part's box drawn in blue."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    x0, x1, y0, y1 = box_pct
    bx0, bx1, by0, by1 = x0 / 100 * w, x1 / 100 * w, y0 / 100 * h, y1 / 100 * h
    mx, my = max((bx1 - bx0) * margin, 12), max((by1 - by0) * margin, 12)
    cx0, cy0, cx1, cy1 = max(0, bx0 - mx), max(0, by0 - my), min(w, bx1 + mx), min(h, by1 + my)
    crop = im.crop((int(cx0), int(cy0), int(cx1), int(cy1)))
    s = 512.0 / max(crop.size)
    crop = crop.resize((max(1, int(crop.width * s)), max(1, int(crop.height * s))), Image.LANCZOS)
    from PIL import ImageDraw
    d = ImageDraw.Draw(crop)
    d.rectangle([(bx0 - cx0) * s, (by0 - cy0) * s, (bx1 - cx0) * s, (by1 - cy0) * s], outline=(0, 90, 255), width=2)
    crop.save(dst)
    return dst


def _box_square(src, box_pct, dst, size=512):
    """The reference cropped EXACTLY to a box (percent of a silhouette-cropped picture) and centred on a white square,
    the framing the part's orthographic box renders use (the longer side of the box fills the square)."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    x0, x1, y0, y1 = box_pct
    crop = im.crop((int(x0 / 100 * w), int(y0 / 100 * h), max(int(x0 / 100 * w) + 1, int(x1 / 100 * w)),
                    max(int(y0 / 100 * h) + 1, int(y1 / 100 * h))))
    s = size / float(max(crop.size))
    crop = crop.resize((max(1, round(crop.width * s)), max(1, round(crop.height * s))), Image.LANCZOS)
    sq = Image.new("RGB", (size, size), (255, 255, 255))
    sq.paste(crop, ((size - crop.width) // 2, (size - crop.height) // 2))
    sq.save(dst)
    return dst


def _backdrop(src):
    """The picture's backdrop colour: the median of its four corners (a silhouette-cropped picture still has backdrop
    in its corners)."""
    import numpy as np
    a = np.asarray(Image.open(src).convert("RGB")).astype(np.float32) / 255.0
    k = max(2, min(a.shape[0], a.shape[1]) // 40)
    return np.median(np.concatenate([a[:k, :k].reshape(-1, 3), a[:k, -k:].reshape(-1, 3), a[-k:, :k].reshape(-1, 3),
                                     a[-k:, -k:].reshape(-1, 3)]), axis=0)


def fit_score(src, box_pct, render, size=256):
    """How well a part's side render covers what the reference shows inside its box: the IoU of the two silhouettes in
    the same framing (the render is orthographic, framed on the box, longer side filling a transparent square). The
    reference box may include parts passing through it, so the number is for comparing builds of one part, not an
    absolute grade. -> 0..1, or None when it cannot be measured."""
    import numpy as np
    try:
        back = _backdrop(src)
        im = Image.open(src).convert("RGB")
        w, h = im.size
        x0, x1, y0, y1 = box_pct
        crop = im.crop((int(x0 / 100 * w), int(y0 / 100 * h), max(int(x0 / 100 * w) + 1, int(x1 / 100 * w)),
                        max(int(y0 / 100 * h) + 1, int(y1 / 100 * h))))
        cw, ch = crop.size
        ref = np.abs(np.asarray(crop).astype(np.float32) / 255.0 - back).max(axis=2) > 0.08
        r = Image.open(render).convert("RGBA")
        sq = r.width
        s_ = sq / float(max(cw, ch))
        rw, rh = max(1, round(cw * s_)), max(1, round(ch * s_))
        ox, oy = (sq - rw) // 2, (sq - rh) // 2
        alpha = np.asarray(r.crop((ox, oy, ox + rw, oy + rh)).getchannel("A")) > 20
        tw, th = (size, max(1, round(size * ch / cw))) if cw >= ch else (max(1, round(size * cw / ch)), size)
        a = np.asarray(Image.fromarray(ref.astype(np.uint8) * 255).resize((tw, th))) > 127
        b = np.asarray(Image.fromarray(alpha.astype(np.uint8) * 255).resize((tw, th))) > 127
        union = np.logical_or(a, b).sum()
        return round(float(np.logical_and(a, b).sum()) / float(union), 3) if union else None
    except Exception:
        return None


def _mm_scale(src, dst, span_m, axes):
    """Tick marks every tenth of the square with millimetre labels from the centre: `span_m` is the side of the square
    in metres, `axes` the two axis names ("x", "z")."""
    from PIL import ImageDraw, ImageFont
    im = Image.open(src).convert("RGB")
    s = im.width
    pad = 40
    out = Image.new("RGB", (s + pad, s + pad), (255, 255, 255))
    out.paste(im, (pad, 0))
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 11)
    except OSError:
        font = ImageFont.load_default()
    for i in range(11):
        t = i / 10.0
        mm = (t - 0.5) * span_m * 1000
        x = pad + t * s
        y = s - t * s
        d.line([(x, s), (x, s + 8)], fill=(200, 0, 0), width=1)
        d.line([(pad - 8, y), (pad, y)], fill=(200, 0, 0), width=1)
        d.line([(x, 0), (x, s)], fill=(230, 120, 120), width=1) if i in (0, 5, 10) else None
        d.line([(pad, y), (pad + s, y)], fill=(230, 120, 120), width=1) if i in (0, 5, 10) else None
        if i % 2 == 0:
            d.text((x - 12, s + 10), "%+.0f" % mm, fill=(200, 0, 0), font=font)
            d.text((1, y - 6), "%+.0f" % mm, fill=(200, 0, 0), font=font)
    d.text((pad + s - 60, s + 26), "%s mm" % axes[0], fill=(0, 0, 0), font=font)
    d.text((2, 2), "%s mm" % axes[1], fill=(0, 0, 0), font=font)
    out.save(dst)
    return dst


def _compare(pairs, dst):
    """Rows of [reference square | build render on white], each 384 px."""
    tile = 384
    out = Image.new("RGB", (tile * 2 + 12, tile * len(pairs) + 6 * (len(pairs) - 1)), (255, 255, 255))
    for r, (ref, render) in enumerate(pairs):
        a = Image.open(ref).convert("RGB").resize((tile, tile), Image.LANCZOS)
        b = Image.open(render).convert("RGBA").resize((tile, tile), Image.LANCZOS)
        white = Image.new("RGBA", b.size, (255, 255, 255, 255))
        b = Image.alpha_composite(white, b).convert("RGB")
        out.paste(a, (0, r * (tile + 6)))
        out.paste(b, (tile + 12, r * (tile + 6)))
    out.save(dst)
    return dst


SIDES = {(0, 0): "rear (-x)", (0, 1): "front (+x)", (1, 0): "right (-y)", (1, 1): "left (+y)", (2, 0): "bottom (-z)",
         (2, 1): "top (+z)"}


def contacts(part, parts, dims):
    """Which neighbours this part's box meets, and on which face: boxes that overlap or nearly touch (within 1.5% of the
    object) along one axis while overlapping on the other two. -> ["the top (+z) face meets FrameReceiver", ...]"""
    tol = 0.015 * max(dims)
    out = []
    a0, a1 = part["box_min"], part["box_max"]
    for q in parts:
        if q is part or q["name"] == part["name"]:
            continue
        b0, b1 = q["box_min"], q["box_max"]
        over = [min(a1[i], b1[i]) - max(a0[i], b0[i]) for i in range(3)]
        if sum(o > 0 for o in over) < 2 or any(o < -tol for o in over):
            continue
        # the contact axis: the one with the least overlap (or a small gap); the face is the side the neighbour is on
        axis = min(range(3), key=lambda i: over[i])
        a_len = a1[axis] - a0[axis]
        if over[axis] > 0.5 * a_len:
            continue                             # the neighbour sits inside this box, not against a face of it
        side = 1 if (b0[axis] + b1[axis]) > (a0[axis] + a1[axis]) else 0
        out.append("the %s face meets %s" % (SIDES[(axis, side)], q["name"]))
    return out


def inside(part, parts):
    """The smaller parts whose boxes lie mostly (over half their volume) inside this part's box: they are modelled
    separately and sit in or on it. The bullpup's olive handguard panel was planned as its own part and the handguard,
    not told, closed its surface over it (2026-09-27)."""
    a0, a1 = part["box_min"], part["box_max"]
    va = max(1e-12, (a1[0] - a0[0]) * (a1[1] - a0[1]) * (a1[2] - a0[2]))
    out = []
    for q in parts:
        if q is part or q["name"] == part["name"]:
            continue
        b0, b1 = q["box_min"], q["box_max"]
        vb = max(1e-12, (b1[0] - b0[0]) * (b1[1] - b0[1]) * (b1[2] - b0[2]))
        over = 1.0
        for i in range(3):
            over *= max(0.0, min(a1[i], b1[i]) - max(a0[i], b0[i]))
        if vb < va and over > 0.5 * vb:
            out.append(q["name"])
    return out


def _size(part):
    return [round(part["box_max"][i] - part["box_min"][i], 5) for i in range(3)]


def build_code_part(job, spec, part, plan):
    """The builder writes the part, Blender builds it, errors go back to the builder (two retries), then the builder
    compares its render with the reference crops once and may correct it. -> {"glb", "code", ...} or None."""
    name = part["name"]
    out_dir = os.path.join(job.work_dir, "parts", name)
    os.makedirs(out_dir, exist_ok=True)
    side_src = plan.get("side_hi") or plan["side"]         # the sharpened picture when there is one: same framing
    side_crop = _crop_part(side_src, part["side_box"][:2] + part["side_box"][2:], os.path.join(out_dir, "ref_side.png"))
    L, W, H = _size(part)
    zt, zb = part["side_box"][2], part["side_box"][3]
    side_sq = _box_square(side_src, part["side_box"], os.path.join(out_dir, "box_side.png"))
    side_mm = _mm_scale(side_sq, os.path.join(out_dir, "box_side_mm.png"), max(L, H), ("x", "z"))
    front_sq = front_mm = None
    if plan.get("front"):
        front_sq = _box_square(plan.get("front_hi") or plan["front"], part["front_span"] + [zt, zb],
                               os.path.join(out_dir, "box_front.png"))
        front_mm = _mm_scale(front_sq, os.path.join(out_dir, "box_front_mm.png"), max(W, H), ("y", "z"))
    dims = plan.get("dims_m") or [L, W, H]
    big = L > 0.25 * dims[0] or H > 0.35 * dims[2]           # a housing or stock carries the look: think harder
    effort = "high" if big else "medium"
    model = config.BUILDER_MODEL if big else config.BUILDER_MODEL_SMALL      # pins and levers do not need the big one
    prompt = CODE_PROMPT.format(category=spec.category, description=spec.description[:600], name=name, what=part["what"],
                                L_mm=L * 1000, W_mm=W * 1000, H_mm=H * 1000, kit=codecheck.KIT_DOC)
    joins = contacts(part, plan["parts"], dims)
    if joins:
        # parts are built one at a time: the pistol's trigger guard and grip stopped short of the frame (2026-09-27)
        prompt += ("\n\nWhere it joins: %s. At each of those faces the part must reach the edge of its box, solidly, so it "
                   "closes against the neighbour with no gap (an open loop's top, a grip's upper end, a bracket's foot)." % "; ".join(joins))
    within = inside(part, plan["parts"])
    if within:
        prompt += ("\n\nModelled separately, sitting in or on this part: %s. Do NOT model them; where one is inset "
                   "(a panel, a lever in a slot, a barrel through a shroud) leave the recess, slot or bore it sits in."
                   % ", ".join(within))
    if front_mm is None:
        prompt = prompt.replace("Picture 2: the same from the FRONT (the part's left on the right of the picture): y across, z up, millimetres.\nPicture 3:",
                                "There is no front picture: shape the cross-section (y) from the description and how such a part is made.\nPicture 2:")
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}] + _images([p for p in (side_mm, front_mm, side_crop) if p])}]
    best, code = None, None
    for attempt in range(3):
        reply = job.llm.chat(messages, model=model, max_tokens=16000 if big else 12000, temperature=0.2,
                             effort=effort)
        code = _code_from(reply.get("content"))
        messages.append({"role": "assistant", "content": reply.get("content") or ""})
        res = _run_part(job, part, code, out_dir, attempt)
        if res.get("ok"):
            best = {**res, "code": code}
            best["fit"] = fit_score(plan["side"], part["side_box"], os.path.join(out_dir, best["box_renders"]["left"]))
            break
        job.log("  part %s attempt %d: %s" % (name, attempt + 1, str(res.get("error"))[:160]))
        messages.append({"role": "user", "content": "That failed: %s\nAnswer with the corrected complete function." % res.get("error")})
    if best is None:
        return None
    for round_no in range(2):                    # up to two corrections, each checked against the reference
        rows = [(side_sq, os.path.join(out_dir, best["box_renders"]["left"]))]
        if front_sq:
            rows.append((front_sq, os.path.join(out_dir, best["box_renders"]["front"])))
        cmp = _compare(rows, os.path.join(out_dir, "compare_%d.png" % round_no))
        best["compare"] = cmp
        refine = REFINE_PROMPT.format(name=name, what=part["what"], code=best["code"])
        if best.get("fit") is not None:
            # the number the eye misses: a barrel built half as thick as the picture's still "looks like a barrel"
            refine = ("Measured: your side silhouette covers the reference's inside this box with an overlap of %.2f "
                      "(1.00 = identical). Below about 0.75 the part is too thin, too short or in the wrong place: the box "
                      "is the part's tight bounds and its widest section fills it.\n" % best["fit"]) + refine
        if not front_sq:
            refine = "(There is no front picture: the comparison has only the SIDE row.)\n" + refine
        text = job.llm.vision(refine, [cmp],
                              model=model, max_tokens=16000 if big else 12000, effort=effort, json_only=False)
        verdict = re.findall(r"VERDICT:\W*(OK|FIX)", text.upper())
        if "```" not in text or (verdict and verdict[-1] == "OK") or text.strip().upper().startswith("OK"):
            job.log("  part %s: accepted by its own check after %d correction(s) (%d tris)" % (name, round_no, best.get("triangles", 0)))
            return best
        missing = [ln.strip() for ln in text.split("```")[0].splitlines() if "MISSING" in ln.upper()]
        why = ("; ".join(missing) or text.split("```")[0].strip()).replace("\n", " ")[:200]
        res = _run_part(job, part, _code_from(text), out_dir, "refined%d" % round_no)
        if not res.get("ok"):
            # a correction that crashes is usually one wrong argument: worth one retry told the error (the bullpup's
            # side rail lost its correction to kit.taper(axis=...), 2026-09-27)
            text = job.llm.vision(refine + "\n\nYour corrected code failed: %s\nAnswer with the fixed complete function."
                                  % str(res.get("error"))[:600], [cmp], model=model, max_tokens=16000 if big else 12000,
                                  effort=effort, json_only=False)
            if "```" in text:
                res = _run_part(job, part, _code_from(text), out_dir, "refined%d_retry" % round_no)
        if not res.get("ok"):
            job.log("  part %s: correction %d failed (%s); keeping the previous build" % (name, round_no + 1, str(res.get("error"))[:120]))
            return best
        fit = fit_score(plan["side"], part["side_box"], os.path.join(out_dir, res["box_renders"]["left"]))
        if fit is not None and best.get("fit") is not None and fit < best["fit"] - 0.05:
            # a correction can break a part that was nearly right: on the bullpup of 2026-09-27 the barrel's fit fell
            # from 0.58 to 0.22 and the handguard came back shattered, and both were taken. The silhouette says which
            # is closer to the picture; the next round critiques the better build again.
            job.log("  part %s: correction %d fits the picture worse (%.2f < %.2f); keeping the previous build"
                    % (name, round_no + 1, fit, best["fit"]))
            continue
        best = {**res, "code": _code_from(text), "refined": round_no + 1, "fit": fit}
        job.log("  part %s: corrected (%s)" % (name, why))
    return best


def _images(paths):
    import base64
    out = []
    for p in paths:
        with open(p, "rb") as f:
            out.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(f.read()).decode()}})
    return out


def _run_part(job, part, code, out_dir, tag):
    try:
        codecheck.check_code(code)
    except codecheck.CodeRejected as exc:
        return {"ok": False, "error": "rejected before running: %s" % exc}
    run = "run_%s" % tag
    k = 1
    while os.path.exists(os.path.join(out_dir, run)):     # a rebuild must not overwrite the build it may lose to
        k += 1
        run = "run_%s_%d" % (tag, k)
    run_dir = os.path.join(out_dir, run)
    try:
        _blender(job, "build_part.py", {"name": part["name"], "code": code, "size": _size(part), "material": part["material"],
                                        "out_dir": run_dir, "render_size": 448}, "part_%s_%s" % (part["name"], tag),
                timeout=300)                 # a part is seconds of work; a runaway loop must not stall the job
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)[:600]}
    path = os.path.join(run_dir, part["name"] + ".json")
    if not os.path.exists(path):
        return {"ok": False, "error": "Blender wrote no result"}
    res = json.load(open(path))
    if res.get("ok"):
        res["renders"] = {k: os.path.join(run, v) for k, v in res["renders"].items()}
        res["box_renders"] = {k: os.path.join(run, v) for k, v in (res.get("box_renders") or {}).items()}
    return res


def _mostly_inside(p, q):
    """More than half of p's side-view box lies inside q's."""
    x0, x1, zt, zb = p["side_box"]
    bx0, bx1, bzt, bzb = q["side_box"]
    area = max(0.0, min(x1, bx1) - max(x0, bx0)) * max(0.0, min(zb, bzb) - max(zt, bzt))
    return area > 0.5 * max(1e-9, (x1 - x0) * (zb - zt))


def is_body(part, plan):
    """The vendor body: a vendor part spanning at least half the object's length."""
    return part.get("method") == "vendor" and (part["side_box"][1] - part["side_box"][0]) >= 50


def erased_body_picture(plan, part, dst, pad=0.08, tiny=5.0, grow=0.4):
    """The body's picture made from the APPROVED side picture itself: every code part's box is erased to the backdrop
    (they are modelled in code), then the picture is cropped to the body's box and padded. No picture model redraws
    the design: FLUX.2 klein, asked to leave out the barrel, drew a different gun (an AK magazine, no sights,
    2026-09-27), and any redraw can drift. Tiny code parts (pins, screws: under `tiny` percent both ways) stay: erasing
    them would punch holes in the body. -> (path, [erased part names])"""
    import numpy as np
    im = Image.open(plan["side"]).convert("RGB")
    w, h = im.size
    bk = _backdrop(plan["side"])
    back = tuple(int(round(c * 255)) for c in bk)
    a = np.array(im)
    # one flat backdrop: the photo's own is a soft gradient, and an erased box in a slightly different white reads as
    # an edge to the mesher
    a[np.abs(a.astype(np.float32) / 255.0 - bk).max(axis=2) <= 0.08] = back
    erased = []
    for q in plan["parts"]:
        if q.get("method") != "code":
            continue
        x0, x1, zt, zb = q["side_box"]
        if (x1 - x0) < tiny and (zb - zt) < tiny:
            continue
        # only parts that stick OUT of the body are erased (a barrel, a muzzle brake). A part lying on it (a rail on the
        # receiver) stays: erasing its box took the receiver's top with it and the vendor textured the gap as backdrop
        # (the white strip under the free bullpup's rail, 2026-09-27). The code part is built over the vendor's soft copy.
        bx0, bx1, bzt, bzb = part["side_box"]
        inside_area = max(0.0, min(x1, bx1) - max(x0, bx0)) * max(0.0, min(zb, bzb) - max(zt, bzt))
        if inside_area > 0.5 * max(1e-9, (x1 - x0) * (zb - zt)):
            continue
        g = grow
        c0, c1 = max(0, int((x0 - g) / 100 * w)), min(w, int(np.ceil((x1 + g) / 100 * w)))
        r0, r1 = max(0, int((zt - g) / 100 * h)), min(h, int(np.ceil((zb + g) / 100 * h)))
        a[r0:r1, c0:c1] = back
        erased.append(q["name"])
    x0, x1, zt, zb = part["side_box"]
    crop = Image.fromarray(a).crop((int(x0 / 100 * w), int(zt / 100 * h), int(np.ceil(x1 / 100 * w)), int(np.ceil(zb / 100 * h))))
    m = int(max(crop.size) * pad)
    side = max(crop.size) + 2 * m
    sq = Image.new("RGB", (side, side), back)
    sq.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    sq.save(dst)
    return dst, erased


def build_vendor_part(job, spec, part, plan):
    """The part drawn alone from the side reference, checked, seeded on its own and oriented. -> {"blend", "yaw"} or None."""
    name = part["name"]
    out_dir = os.path.join(job.work_dir, "parts", name)
    os.makedirs(out_dir, exist_ok=True)
    ref = plan["side"]
    picture, fixes = None, ""
    # the parts modelled separately inside or against this one's box are left out of its picture, so the vendor does not
    # model them twice (a soft copy of the barrel under the crisp code barrel)
    others = [q["name"] for q in plan["parts"] if q["name"] != name and q.get("method") == "code"
              and all(min(q["box_max"][i], part["box_max"][i]) - max(q["box_min"][i], part["box_min"][i]) > 0 for i in range(3))]
    leave_out = (" Leave out, they are modelled separately: %s." % ", ".join(others)) if others else ""
    register_to, yaw_sweep = None, False
    body = is_body(part, plan) and not config.ALL_VENDOR      # all-vendor: every part, the biggest too, drawn alone
    if body:
        picture, erased = erased_body_picture(plan, part, os.path.join(out_dir, "picture_erased.png"))
        job.log("  part %s: the approved side picture with the code parts erased (%s)" % (name, ", ".join(erased) or "none"))
    for attempt in range(0 if picture else 2):      # a side picture of the part alone, for the parts that are not the body
        path = os.path.join(out_dir, "picture_%d.png" % attempt)
        # the SAME side view as the reference (forward to the right): its silhouette is what the seed is registered to
        job.images.generate("Show ONLY %s from this exact object, whole and complete, exactly as it looks here (same shape, "
                            "colours and materials), seen from exactly the same side angle as this picture with the forward "
                            "end to the right, isolated on a plain pure white background, nothing else in frame, sharp product "
                            "photograph.%s %s" % (part["what"], leave_out, fixes),
                            path, model=pricing.edit_model(spec), references=[ref], aspect_ratio="1:1")
        j = extract_json(job.llm.vision(PART_CHECK.format(part=part["what"]), [path])) or {}
        if j.get("ok") and int(j.get("score", 0) or 0) >= 6:
            picture = path
            break
        fixes, picture = str(j.get("fixes") or ""), picture or path
    view = config.BODY_SEED_VIEW or ("side" if pricing.edit_model(spec).startswith("local/") else "three_quarter")
    if picture and view == "three_quarter":
        # every vendor part, not only a body: a flat side picture gives the mesher no depth. The side picture still
        # decides where and how the part sits (registration with a yaw sweep).
        quarter = _three_quarter(job, spec, part, plan, picture, erased if body else [], out_dir, body=body)
        if quarter:
            register_to, yaw_sweep, picture = picture, True, quarter
    url = job.fal.upload(picture)
    vendor = pricing.seed_vendor(spec)["key"]
    if vendor.startswith("hitem3d3"):
        model, payload = "hitem3d/hi3d/v3.0/image-to-3d", {"image_url": url, "model": "hi3dv3.0", "resolution": "2048quality",
                                                          "face_count": 200000, "enable_texture": True, "enable_pbr": True,
                                                          "export_format": "glb", "enable_safety_checker": False}
    elif vendor == "local":
        model, payload = config.LOCAL_SEED_MODEL, {"image_url": url}      # TRELLIS.2 on this PC, free
    else:
        model, payload = config.SEED_MODEL, {"image_url": url, "geometry_quality": "detailed", "texture_quality": "detailed",
                                             "pbr": True, "face_limit": 150000}
    out = job.fal.run(model, payload)
    mesh_url = first_url(out, (".glb",)) or first_url(out, (".fbx",))
    if not mesh_url:
        job.log("  part %s: the vendor returned no mesh" % name)
        return None
    ext = ".fbx" if mesh_url.split("?")[0].lower().endswith(".fbx") else ".glb"
    glb = os.path.join(out_dir, "seed" + ext)
    job.fal.download(mesh_url, glb)
    registered = _register(job, name, glb, register_to or picture, out_dir, yaw_sweep) if ext == ".glb" else None
    if registered:
        job.log("  part %s: seeded by %s, registered to its side picture (IoU %.2f, runner-up %.2f)" % (
            name, model.split("/")[0], registered["iou"], registered["runner_up_iou"]))
        return {"blend": registered["blend"], "yaw": 0, "picture": picture, "seed": glb, "registration": registered,
                "keep_depth": bool(yaw_sweep), "side_picture": register_to, "seed_render": registered.get("render")}
    oriented = orient_part(job, spec, {"name": name, "phrase": part["what"], "size_m": max(_size(part))}, glb)
    if not oriented:
        return None
    job.log("  part %s: seeded by %s, facing yaw %s" % (name, model.split("/")[0], oriented.get("yaw")))
    return {"blend": oriented["blend"], "yaw": oriented.get("yaw", 0), "picture": picture, "seed": glb}


THREE_QUARTER_PROMPT = """Picture 1 is a side view of an object. Draw EXACTLY the same object - identical design, shape,
proportions, colours, materials and every detail - seen from a three-quarter view: the camera about 35 degrees round
from that side towards the object's front end and about 20 degrees above it, so its side, its front end and its top
all show. Shot with a long telephoto lens from far away: almost no perspective, the near and far ends drawn at the
same scale, straight lines stay straight. The whole object in frame, isolated on a plain pure white background, even
studio light, sharp product photograph, no shadows on the ground.%s"""


def _three_quarter(job, spec, part, plan, side_picture, erased, out_dir, body=True):
    """The body drawn from a three-quarter angle by the picture model, from its erased side picture (and the approved
    front view when there is one): an image-to-3D model given a flat side profile has to guess all the depth (the
    bullpup came out thin, 2026-09-27). Checked like any part picture; None when no good picture came."""
    refs = [side_picture] + ([plan["front"]] if plan.get("front") else [])
    # every part code builds is left out of the drawing (tiny pins and screws aside): what the mesher does not see it
    # cannot mirror onto the hidden side or model twice
    code = [q for q in plan["parts"] if body and q.get("method") == "code"
            and not ((q["side_box"][1] - q["side_box"][0]) < 5 and (q["side_box"][3] - q["side_box"][2]) < 5)]
    leave = ["%s (%s)" % (q["name"], q["what"].split(",")[0].split(";")[0][:80]) for q in code]
    missing = (" Leave OUT these parts, they are made separately - draw the object without them, with a clean surface "
               "where they attach: %s. Keep EVERYTHING else exactly as in picture 1, including anything mounted on those "
               "parts (sights on a rail stay, standing where they stand)." % "; ".join(leave)) if leave else ""
    if erased:
        missing += " The front end stops where picture 1 stops."
    if plan.get("front"):
        missing = " Picture 2 shows its front end." + missing
    fixes = ""
    for attempt in range(2):
        path = os.path.join(out_dir, "picture_quarter_%d.png" % attempt)
        try:
            job.images.generate(THREE_QUARTER_PROMPT % (missing + (" " + fixes if fixes else "")), path,
                                model=pricing.edit_model(spec), references=refs, aspect_ratio="4:3")
        except Exception as exc:  # noqa: BLE001 - the side picture is the fallback
            job.log("  part %s: no three-quarter picture (%s); seeding from the side" % (part["name"], str(exc)[:120]))
            return None
        j = extract_json(job.llm.vision(QUARTER_CHECK.format(what=part["what"]), [side_picture, path])) or {}
        if j.get("ok") and int(j.get("score", 0) or 0) >= 6:
            job.log("  part %s: seeded from a three-quarter picture (check %s/10)" % (part["name"], j.get("score")))
            if int(j.get("score", 0) or 0) >= 8:
                # a picture this faithful to the approved design carries the design's own colours: the mesher's texture
                # of it beats the planner's hex guess (the Havoc's blue-grey hull came out light grey, 2026-09-28)
                part.setdefault("material", {})["keep_texture"] = True
            return path
        fixes = str(j.get("fixes") or "")
    job.log("  part %s: the three-quarter pictures drifted from the design; seeding from the side" % part["name"])
    return None


QUARTER_CHECK = """Picture 1 is the approved side view of an object: {what}. Picture 2 should be the SAME object from a
three-quarter view. Is it the same design (same shape, proportions, parts, colours), whole, on a plain background,
and does it show the side, the front end and the top? Answer JSON only:
{{"ok": true/false, "score": 0-10, "fixes": "what to change, if anything"}}"""


def _register(job, name, glb, picture, out_dir, yaw_sweep=False, extra_yaw=0.0, extra_pitch=0.0):
    """The seed turned so its side silhouette matches the part's side picture (blender/register_part.py). -> result or None."""
    try:
        import numpy as np
        a = np.asarray(Image.open(picture).convert("RGB")).astype(np.float32) / 255.0
        border = np.concatenate([a[:6].reshape(-1, 3), a[-6:].reshape(-1, 3), a[:, :6].reshape(-1, 3), a[:, -6:].reshape(-1, 3)])
        fg = np.abs(a - np.median(border, axis=0)).max(axis=2) > 0.1
        if fg.mean() < 0.01:
            return None
        mask = os.path.join(out_dir, "side_mask.png")
        Image.fromarray((fg * 255).astype("uint8")).save(mask)
        res_path = os.path.join(out_dir, "registration.json")
        blend = os.path.join(out_dir, "registered.blend")
        render = os.path.join(out_dir, "seed_render.png")
        _blender(job, "register_part.py", {"glb": glb, "mask": mask, "out_blend": blend, "out_json": res_path,
                                           "yaw_sweep": bool(yaw_sweep), "out_render": render,
                                           "extra_yaw": extra_yaw, "extra_pitch": extra_pitch},
                 "register_%s" % name, timeout=600)
        res = json.load(open(res_path))
        if res.get("iou", 0) < 0.35:
            job.log("  part %s: registration too weak (IoU %.2f); asking which side is the front instead" % (name, res.get("iou", 0)))
            return None
        return {**res, "blend": blend, "render": render if os.path.exists(render) else None}
    except Exception as exc:  # noqa: BLE001 - the facing question is the fallback
        job.log("  part %s: registration failed (%s); asking which side is the front instead" % (name, str(exc)[:120]))
        return None


def detail_map(src, dst, blur_frac=0.006):
    """The picture's fine surface detail (panel lines, ribs, knurling, screws, wear) as a grey map centred on 0.5: its
    luminance minus a blurred copy, inside the object only (the silhouette's own edge would print a halo on the part
    behind it). The assembler projects it onto code parts, so their surface carries the reference's detail."""
    import numpy as np
    from PIL import ImageFilter
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.float32) / 255.0
    border = np.concatenate([a[:4].reshape(-1, 3), a[-4:].reshape(-1, 3), a[:, :4].reshape(-1, 3), a[:, -4:].reshape(-1, 3)])
    back = np.median(border, axis=0)
    fg = Image.fromarray(((np.abs(a - back).max(axis=2) > 0.08) * 255).astype(np.uint8))
    grow = max(3, int(max(im.size) * 0.006) | 1)
    inside = np.asarray(fg.filter(ImageFilter.MinFilter(grow))).astype(np.float32) / 255.0
    grey = im.convert("L")
    radius = max(2.0, max(im.size) * blur_frac)
    hp = (np.asarray(grey).astype(np.float32) - np.asarray(grey.filter(ImageFilter.GaussianBlur(radius))).astype(np.float32)) / 255.0
    out = 0.5 + np.clip(hp * inside, -0.2, 0.2) * 1.5
    Image.fromarray((out * 255).clip(0, 255).astype(np.uint8), "L").save(dst)
    return dst


def _sharpen(job, src, dst):
    """The picture upscaled 4x with the framing unchanged, so a small part's box crop shows its real edges instead of a
    blurred enlargement (a pistol trigger is ~100 px in a 1200 px reference). None when the upscale fails or changes
    the framing: the part builder then uses the picture as it is."""
    if config.NO_SPEND:
        # no paid upscale: a 4x Lanczos enlargement with a light unsharp mask keeps the framing and softens the jaggies
        from PIL import ImageFilter
        im = Image.open(src).convert("RGB")
        im.resize((im.width * 4, im.height * 4), Image.LANCZOS).filter(
            ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2)).save(dst)
        job.log("  plan picture enlarged 4x on this PC (no-spend)")
        return dst
    try:
        out = job.fal.run(config.UPSCALE_MODEL, {"image_url": job.fal.upload(src), "scale": 4,
                                                 "model": "RealESRGAN_x4plus", "output_format": "png"})
        url = image_url(out)
        if not url:
            raise ValueError("no picture in the answer")
        job.fal.download(url, dst)
        a, b = Image.open(src).size, Image.open(dst).size
        if abs(a[0] / a[1] - b[0] / b[1]) > 0.01 or b[0] < a[0] * 1.5:
            raise ValueError("the upscale changed the framing (%s -> %s)" % (a, b))
        return dst
    except Exception as exc:
        job.log("  plan picture not sharpened (%s); parts use it as it is" % str(exc)[:120])
        return None


def _detail(job, plan):
    """Detail maps from the plan's side and front pictures, for the assembler (None when they cannot be made)."""
    try:
        d = {"side": detail_map(plan.get("side_hi") or plan["side"], os.path.join(job.work_dir, "detail_side.png")), "front": None,
             "dims": plan["dims_m"], "strength": 0.5}
        if plan.get("front"):
            d["front"] = detail_map(plan.get("front_hi") or plan["front"], os.path.join(job.work_dir, "detail_front.png"))
        return d
    except Exception as exc:                  # detail is a nicety: an odd picture must not stop the build
        job.log("  reference detail skipped: %s" % str(exc)[:120])
        return None


def _assemble(job, spec, plan, built, round_no, reference):
    parts = []
    for p in plan["parts"]:
        b = built.get(p["name"])
        if not b:
            continue
        entry = {"name": p["name"], "kind": "code" if b.get("code") else "vendor", "box_min": p["box_min"],
                 "box_max": p["box_max"], "material": p["material"], "centreline": bool(p.get("centreline")),
                 "zones": p.get("zones") or []}
        if b.get("code") and any(is_body(q, plan) and _mostly_inside(p, q) for q in plan["parts"]):
            entry["cover"] = 1.03      # over the vendor body's own soft copy of it (kept in the body's picture)
        if b.get("code"):
            entry.update({"blend": b["blend"]} if b.get("blend") and os.path.exists(b["blend"]) else {"glb": b["glb"]})
        else:
            # only the largest vendor part keeps the mesher's depth: a small part seeded from a three-quarter picture
            # comes out fat (the magazine, 2026-09-28) and its box, read off the picture, is the better width
            largest = max((q for q in plan["parts"] if q.get("method") == "vendor"),
                          key=lambda q: q["box_max"][0] - q["box_min"][0], default=None)
            is_largest = largest is not None and largest["name"] == p["name"]
            entry.update({"blend": b["blend"], "yaw": b.get("yaw", 0), "keep_depth": bool(b.get("keep_depth")) and is_largest,
                          "fill_box": not is_largest})
        parts.append(entry)
    out = os.path.join(job.dir, "delivery") if round_no == "final" else os.path.join(job.work_dir, "assembly_%s" % round_no)
    _blender(job, "assemble.py", {"name": spec.name, "out_dir": out, "tri_budget": spec.tri_budget, "engine": spec.engine,
                                  "atlas_size": 4096 if spec.tri_budget >= 100000 else 2048, "render_size": 768,
                                  "spec": spec.to_dict(), "reference": reference, "parts": parts,
                                  "detail": plan.get("detail")}, "assemble_%s" % round_no)
    report = json.load(open(os.path.join(out, "report.json")))
    return out, report


def _check(job, plan, out, report):
    work = os.path.join(job.work_dir, "check")
    os.makedirs(work, exist_ok=True)
    side_b = {p["name"]: p["side_box"] for p in plan["parts"]}
    front_b = {p["name"]: p["front_span"] + p["side_box"][2:] for p in plan["parts"]}   # each part at its own height
    views = [("left", plan["side"], side_b)] + ([("front", plan["front"], front_b)] if plan.get("front") else [])
    pics = []
    for view, src, boxes in views:
        pics.append(draw_grid(src, os.path.join(work, "ref_%s.png" % view), boxes=boxes))
    for view, _src, boxes in views:
        crop = os.path.join(work, "asm_%s.png" % view)
        crop_to_object(os.path.join(out, report["check_renders"][view]["file"]), crop)
        pics.append(draw_grid(crop, os.path.join(work, "asm_%s_grid.png" % view), boxes=boxes))
    top = (report.get("check_renders") or {}).get("top")
    if top:
        crop = os.path.join(work, "asm_top.png")
        crop_to_object(os.path.join(out, top["file"]), crop)
        pics.append(crop)
    listing = json.dumps([{"name": p["name"], "what": p["what"][:80], "side_box": p["side_box"], "front_span": p["front_span"]}
                          for p in plan["parts"]])
    prompt = CHECK_PROMPT.format(parts=listing)
    if not plan.get("front"):
        prompt = ("There is no front reference: picture 1 is the reference SIDE, picture 2 the assembly SIDE. Keep each "
                  "part's front_span unless it is clearly wrong.\n") + prompt
    text = job.llm.vision(prompt, pics, model=config.BUILDER_MODEL, max_tokens=4000, effort="medium")
    return extract_json(text) or {"ok": True, "notes": "no readable answer"}


def keep_part_pictures(plan, built, delivery):
    """Every part's pictures and its plan entry kept in delivery/parts/<Part>/ (side.png the picture the part is
    registered to, quarter.png the one the mesher got, seed.glb the mesh it returned) with a meta.json, so the same
    parts can be meshed again later with another vendor without drawing anything (owner, 2026-09-28:
    "keep the parts images so we can test them later with a better model"). scripts/reseed_parts.py does that."""
    import shutil
    root = os.path.join(delivery, "parts")
    index = []
    for p in plan["parts"]:
        b = built.get(p["name"]) or {}
        d = os.path.join(root, p["name"])
        os.makedirs(d, exist_ok=True)
        meta = {k: p.get(k) for k in ("name", "what", "method", "side_box", "front_span", "box_min", "box_max", "material",
                                      "zones", "centreline")}
        meta["kind"] = "code" if b.get("code") else "vendor"
        for key, src, dst in (("side_picture", b.get("side_picture") or (b.get("picture") if not b.get("side_picture") else None), "side.png"),
                              ("picture", b.get("picture") if b.get("side_picture") else None, "quarter.png"),
                              ("seed", b.get("seed"), "seed.glb")):
            if src and os.path.exists(src):
                shutil.copy2(src, os.path.join(d, dst))
                meta[key] = dst
        if b.get("code"):
            meta["code"] = b["code"]
        meta["registration"] = b.get("registration")
        meta["keep_depth"] = bool(b.get("keep_depth"))
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"), indent=1)
        index.append(p["name"])
    json.dump({"dims_m": plan.get("dims_m"), "parts": index}, open(os.path.join(root, "index.json"), "w"), indent=1)
    return index


def seed_images(plan, built, delivery):
    """Every part's seed images into the delivery (seed_<Part>_<kind>.png), so the page shows how each part was made
    next to the model: for a vendor part the picture the mesher got, the side picture it was registered to and the raw
    mesh it returned; for a code part its last reference-against-build comparison."""
    import shutil
    out = []
    for p in plan["parts"]:
        b = built.get(p["name"]) or {}
        for kind, src in (("picture", b.get("picture")), ("side", b.get("side_picture")), ("mesh", b.get("seed_render")),
                          ("compare", b.get("compare"))):
            if src and os.path.exists(src):
                name = "seed_%s_%s.png" % (p["name"], kind)
                shutil.copy2(src, os.path.join(delivery, name))
                out.append(name)
    return out


def build_assembly(job, spec, ref):
    """-> (report, delivery_dir, record) or None when the approved pictures have no side and front view."""
    views = pick_views(ref, spec.category)
    if not views:
        return None
    side_src, front_src, mirror = views
    job.stage("plan")
    plan = make_plan(job, spec, side_src, front_src, mirror)
    plan["side_hi"] = _sharpen(job, plan["side"], os.path.join(job.work_dir, "plan_side_hi.png"))
    if plan.get("front"):
        plan["front_hi"] = _sharpen(job, plan["front"], os.path.join(job.work_dir, "plan_front_hi.png"))
    plan["detail"] = _detail(job, plan)
    job.stage("parts")
    built = {}

    def one(part):
        if part["method"] == "code":
            b = build_code_part(job, spec, part, plan)
            if b:
                return part["name"], b
            job.log("  part %s: code could not build it; sending it to the vendor" % part["name"])
            part["method"] = "vendor"
        return part["name"], build_vendor_part(job, spec, part, plan)
    with ThreadPoolExecutor(max_workers=max(1, config.ASSEMBLY_WORKERS)) as pool:
        for name, b in pool.map(one, plan["parts"]):
            if b:
                built[name] = b
            else:
                job.log("  part %s: not built; the assembly goes on without it" % name)
    job.stage("assemble")
    record = {"plan": {k: v for k, v in plan.items() if k in ("parts", "dropped", "notes", "dims_m")}, "rounds": []}
    reference = ref["views"][0] if ref.get("views") else None
    for round_no in range(max(0, config.ASSEMBLY_CHECK_ROUNDS)):
        out, report = _assemble(job, spec, plan, built, round_no, reference)
        job.stage("check")
        verdict = _check(job, plan, out, report)
        record["rounds"].append(verdict)
        moves = {m.get("name"): m for m in verdict.get("moves") or [] if isinstance(m, dict)}
        rebuild = [r for r in verdict.get("rebuild") or [] if isinstance(r, dict)]
        job.log("  check %d: %s; %d move(s), %d rebuild(s)%s" % (round_no + 1, "ok" if verdict.get("ok") else "changes",
                len(moves), len(rebuild), (" - " + str(verdict.get("notes"))[:120]) if verdict.get("notes") else ""))
        if verdict.get("ok") or (not moves and not rebuild):
            break
        for p in plan["parts"]:
            m = moves.get(p["name"])
            if m and m.get("side_box") and m.get("front_span"):
                try:
                    p["box_min"], p["box_max"] = to_metres(m["side_box"], m["front_span"], plan["dims_m"])
                    p["side_box"], p["front_span"] = m["side_box"], m["front_span"]
                except (ValueError, TypeError):
                    pass
        job.stage("parts")
        for r in rebuild:
            p = next((q for q in plan["parts"] if q["name"] == r.get("name")), None)
            if not p:
                continue
            fixed = {**p, "what": "%s. Fix: %s" % (p["what"], str(r.get("why"))[:300])}
            if p["method"] == "code":
                b = build_code_part(job, spec, fixed, plan)
            elif not built.get(p["name"], {}).get("redone"):
                # a vendor part the check calls wrong is drawn and seeded once more, told what was wrong (the pistol's
                # grip was flagged twice and nothing happened, 2026-09-27); once only, a seed costs real money
                job.log("  part %s: the check calls its shape wrong; drawing and seeding it again" % p["name"])
                b = build_vendor_part(job, spec, fixed, plan)
                if b:
                    b["redone"] = True
            else:
                b = None
            old = built.get(p["name"]) or {}
            if b and b.get("fit") is not None and old.get("fit") is not None and b["fit"] < old["fit"] - 0.05:
                job.log("  part %s: the rebuild fits the picture worse (%.2f < %.2f); keeping the previous one"
                        % (p["name"], b["fit"], old["fit"]))
                b = None
            if b:
                built[p["name"]] = b
        job.stage("assemble")
    delivery, report = _assemble(job, spec, plan, built, "final", reference)
    report["seed_images"] = seed_images(plan, built, delivery)
    report["kept_parts"] = keep_part_pictures(plan, built, delivery)
    report["assembly"] = record
    report["build_mode"] = "assembly"
    with open(os.path.join(delivery, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    return report, delivery, record
