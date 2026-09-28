"""Assembly stage 1: the parts plan. The builder model looks at the approved side and front pictures, cropped to the
object's silhouette and drawn with a labelled percentage grid, and splits the object into parts: a box for each
(in percent of the silhouette), how to build it (code or vendor) and its material. The boxes become metres here:
the brief's size is the length, the side silhouette's aspect the height, the front silhouette's aspect the width."""
import json
import os
import re

from PIL import Image, ImageDraw, ImageFont

from .. import config
from ..llm import extract_json

PLAN_PROMPT = """You are the builder of a game-ready 3D asset. It will be modelled as SEPARATE PARTS and assembled, so that
every edge is crisp: nothing is sculpted as one blob.

The asset: {description}
Category: {category}. Real length {length_m:.3f} m (height {height_m:.3f} m, width {width_m:.3f} m).

Picture 1 is the SIDE view, cropped exactly to the object: the forward end (muzzle / nose) is on the RIGHT, up is up.
Picture 2 is the FRONT view (looking back at the forward end), cropped exactly to the object: the object's left is on
the right of the picture. Both carry a grid in percent: 0 at the left / top edge, 100 at the right / bottom edge.

Split the object the way a 3D modeller would: into every piece that is its own shape (between 4 and {max_parts}
parts) - receiver, stock, grip, handguard, magazine, each sight, rails, barrel, muzzle device, trigger group, levers,
pods, canopy frame, hull sections, wheels, mirrors, bumpers. Small parts are built well; one big part is not. No
decals, no text, no screws smaller than 1% of the length.

Keep together what is ONE moulding or casting in reality: a polymer pistol frame WITH its grip and trigger guard,
a bullpup stock that is moulded with its receiver, a vehicle's one-piece body shell. A cut through the middle of
one moulding becomes a visible seam or gap when the halves are built apart. Everything that is bolted, clipped or
slid onto that moulding is its own part.

For each part choose how it is built - by what the part IS, not by how big it is:
- "code": modelled in code from primitives with exact edges. Use it for every part that IS a primitive shape or a
  stack of them: turned parts (barrels, muzzle devices, suppressors, gas blocks, knobs, exhaust tips, wheel hubs, a
  wheel with its tyre - a lathe shape with a tread repeated round it), repeated machined parts (Picatinny / M-LOK
  rails, slotted covers, fin stacks, grilles, rocket-pod faces), flat or faceted panels and plates, boxes with cuts
  (a magazine, a butt pad, a mirror head, a bumper with tow hooks, a rocket pod), rods and tubes, and every control
  that sticks out sideways on ONE side only (a charging handle, a selector, a release button - the mesher would
  mirror it onto both sides).
- "vendor": an AI image-to-3D model of that ONE part, drawn alone. Use it for every part with sculpted, moulded or
  compound-curved surfaces that primitives cannot describe: a pistol grip with finger grooves, a moulded stock and
  receiver, a shaped handguard shell, a slide with its contours, a helicopter hull section, a canopy, a fender. The
  vendor is free on this PC, so use it for every such part - never lump several parts into one vendor part
  (four tyres drawn together came back as one black blob), and never send a primitive shape to it.
For a vendor part, "what" describes that part alone as it looks in the picture and names the neighbours it is drawn
WITHOUT. Give it the colour of its largest area; if it shows clearly different colours, set "keep_texture": true.
Windows, windshields and canopies are "glass" zones of the vendor part that carries them, or, when the glass is a
separate pane, a code part with "glass": true.
A typical rifle is 10-16 parts, a pistol 6-10, a truck 12-20 (body shell, hood, bed, doors if separate, each wheel,
bumpers, mirrors, lights), an aircraft 8-14 (hull, canopy, engines, pods, guns, skids or gear, tail).

Answer JSON only:
{{"parts": [{{"name": "PascalCase unique", "what": "one sentence: shape, features to model, colour and finish",
  "method": "code" | "vendor",
  "side_box": [x_left, x_right, z_top, z_bottom],    percent of picture 1, tight around the part as seen from the side
  "front_span": [y_left, y_right],                   percent of picture 2 across, tight around the part as seen from the front
  "material": {{"color": "#rrggbb as the camera sees it", "finish": "polymer" | "rubber" | "metal" | "painted" | "glass" | "wood" | "fabric",
               "metal": true/false, "roughness": 0.0-1.0, "glass": false, "keep_texture": false}},
  "zones": [{{"name": "...", "side_box": [...], "front_span": [...], "material": {{...as above}}}}]}}],   vendor parts only
 (zones: the areas of a vendor part that are a DIFFERENT material from the rest of it - a rubber grip or butt pad,
 bare steel (bolt, pins, sight blades, a bare-metal receiver), a coloured panel, a lens. Each has its own tight
 side_box and front_span (percent, like a part) and material. A real weapon is never one material: polymer furniture,
 rubber grips and pads, blued or parkerised steel, anodised aluminium, glass optics. Give every such area a zone.
 A lens or an optic's glass that should be see-through is a separate code part with "glass": true, not a zone.)
 (metal is true ONLY for bare metal - blued or parkerised steel, anodised aluminium, chrome. Anything painted, coated
 or plastic is metal false, even on a steel body: a painted truck panel is not metal.)
 "notes": "anything the assembly must respect"}}
Boxes of parts that touch MUST overlap by 1-2% where they join (a trigger guard into the frame, a grip into the
receiver): parts are modelled one at a time and a box that only meets its neighbour leaves a visible gap.
Together the boxes must cover the whole silhouette."""


NO_FRONT_FROM = """Picture 2 is the FRONT view (looking back at the forward end), cropped exactly to the object: the object's left is on
the right of the picture. Both carry a grid in percent: 0 at the left / top edge, 100 at the right / bottom edge."""
NO_FRONT_TO = """There is no front picture. The side picture carries a grid in percent: 0 at the left / top edge, 100 at the
right / bottom edge. Estimate the widths from the description and how such objects are built: front_span is percent
of the object's full width from its right side (0) to its left side (100). The width given above is only a guess;
give your own as overall_width_m."""


def foreground_box(path, threshold=0.08):
    """Pixel box (x0, y0, x1, y1) of whatever is not the backdrop (the median of the border)."""
    import numpy as np
    a = np.asarray(Image.open(path).convert("RGB")).astype(np.float32) / 255.0
    border = np.concatenate([a[:6].reshape(-1, 3), a[-6:].reshape(-1, 3), a[:, :6].reshape(-1, 3), a[:, -6:].reshape(-1, 3)])
    back = np.median(border, axis=0)
    fg = np.abs(a - back).max(axis=2) > threshold
    ys, xs = np.nonzero(fg)
    if len(xs) < 50:
        raise ValueError("no object found in %s" % os.path.basename(path))
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def crop_to_object(src, dst, max_side=1024):
    """The picture cropped to the object's silhouette (no margin), longest side `max_side`. -> (width, height) px."""
    x0, y0, x1, y1 = foreground_box(src)
    im = Image.open(src).convert("RGB").crop((x0, y0, x1, y1))
    s = max_side / float(max(im.size))
    im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    im.save(dst)
    return im.size


def draw_grid(src, dst, step=5, label_every=10, boxes=None):
    """A percent grid on the picture with a labelled margin, optionally with named boxes ({name: [x0, x1, y0, y1]} %)."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    pad = 34
    out = Image.new("RGB", (w + pad * 2, h + pad * 2), (255, 255, 255))
    out.paste(im, (pad, pad))
    d = ImageDraw.Draw(out, "RGBA")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    for p in range(0, 101, step):
        strong = p % label_every == 0
        col = (220, 30, 30, 150) if strong else (220, 30, 30, 60)
        x = pad + p / 100.0 * w
        y = pad + p / 100.0 * h
        d.line([(x, pad), (x, pad + h)], fill=col, width=1)
        d.line([(pad, y), (pad + w, y)], fill=col, width=1)
        if strong:
            d.text((x - 8, 4), str(p), fill=(200, 0, 0, 255), font=font)
            d.text((x - 8, pad + h + 8), str(p), fill=(200, 0, 0, 255), font=font)
            d.text((2, y - 7), str(p), fill=(200, 0, 0, 255), font=font)
            d.text((pad + w + 4, y - 7), str(p), fill=(200, 0, 0, 255), font=font)
    for name, b in (boxes or {}).items():
        x0, x1, y0, y1 = b
        d.rectangle([pad + x0 / 100 * w, pad + y0 / 100 * h, pad + x1 / 100 * w, pad + y1 / 100 * h],
                    outline=(0, 90, 255, 230), width=2)
        d.text((pad + x0 / 100 * w + 2, pad + y0 / 100 * h + 1), name, fill=(0, 60, 220, 255), font=font)
    out.save(dst)
    return dst


def object_dims(length_m, side_px, front_px):
    """(L, W, H) metres from the brief's length and the two silhouettes."""
    sw, sh = side_px
    fw, fh = front_px
    height = length_m * sh / float(sw)
    width = height * fw / float(fh)
    return float(length_m), float(width), float(height)


def pct(v):
    try:
        return min(100.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return None


def to_metres(side_box, front_span, dims):
    """Percent boxes -> (box_min, box_max) in the asset frame (+X forward, +Y left, +Z up, origin at the centre)."""
    L, W, H = dims
    x0, x1, zt, zb = (pct(v) for v in side_box)
    y0, y1 = (pct(v) for v in front_span)
    if None in (x0, x1, zt, zb, y0, y1):
        raise ValueError("a box has a missing number")
    x0, x1 = sorted((x0, x1))
    zt, zb = sorted((zt, zb))
    y0, y1 = sorted((y0, y1))
    lo = [-L / 2 + x0 / 100 * L, -W / 2 + y0 / 100 * W, H / 2 - zb / 100 * H]
    hi = [-L / 2 + x1 / 100 * L, -W / 2 + y1 / 100 * W, H / 2 - zt / 100 * H]
    floor = max(L, W, H) * 0.004            # a part is at least 0.4% of the object on every side
    for i in range(3):
        if hi[i] - lo[i] < floor:
            c = (hi[i] + lo[i]) / 2
            lo[i], hi[i] = c - floor / 2, c + floor / 2
    return [round(v, 5) for v in lo], [round(v, 5) for v in hi]


def clean_name(name, taken):
    base = re.sub(r"[^A-Za-z0-9]", "", str(name or "")) or "Part"
    base = base[0].upper() + base[1:]
    out, i = base[:40], 2
    while out in taken:
        out = "%s%d" % (base[:38], i)
        i += 1
    taken.add(out)
    return out


def sample_colours(plan, threshold=0.1):
    """Every part's (and zone's) colour read off the approved side picture inside its own box - the median of the
    object's pixels there - instead of the planner's guess: the planner called a medium-grey stock and magazine light
    grey and they came out near-white (2026-09-28). Glass keeps the planned colour. -> [(name, planned, sampled)]"""
    import numpy as np
    a = np.asarray(Image.open(plan["side"]).convert("RGB")).astype(np.float32) / 255.0
    h, w = a.shape[:2]
    k = max(2, min(h, w) // 40)
    back = np.median(np.concatenate([a[:k, :k].reshape(-1, 3), a[:k, -k:].reshape(-1, 3), a[-k:, :k].reshape(-1, 3),
                                     a[-k:, -k:].reshape(-1, 3)]), axis=0)
    fg = np.abs(a - back).max(axis=2) > threshold
    changed = []

    def sample(box, mat, name):
        if mat.get("glass") or mat.get("finish") == "glass":
            return
        x0, x1, zt, zb = box
        if (x1 - x0) * (zb - zt) < 150:
            return                     # a small part's box is mostly its neighbours (the trigger read the pale guard)
        c0, c1 = int(x0 / 100 * w), max(int(x0 / 100 * w) + 1, int(x1 / 100 * w))
        r0, r1 = int(zt / 100 * h), max(int(zt / 100 * h) + 1, int(zb / 100 * h))
        # the inner 60% of the box: the part's own surface, not its neighbours at the edges
        cx, cy = (c1 - c0) // 5, (r1 - r0) // 5
        crop, m = a[r0 + cy:r1 - cy, c0 + cx:c1 - cx], fg[r0 + cy:r1 - cy, c0 + cx:c1 - cx]
        px = crop[m]
        if len(px) < 40:
            return
        med = np.median(px, axis=0)
        hexc = "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in med)
        changed.append((name, mat.get("color"), hexc))
        mat["color_planned"] = mat.get("color")
        mat["color"] = hexc

    for p in plan["parts"]:
        sample(p["side_box"], p["material"], p["name"])
        for z in p.get("zones") or []:
            sample(z["side_box"], z["material"], "%s/%s" % (p["name"], z["name"]))
    return changed


def snap_to_silhouette(plan, threshold=0.1):
    """Thin code parts that stick out of everything else (a barrel, a muzzle device) get their height from the side
    picture's silhouette instead of the planner's eyeballed percentages: read off a 5% grid, the bullpup's barrel was
    planned 10.6 mm across where the picture shows 13.3 mm (2026-09-27). Only parts whose box lies mostly outside every
    other part's box, so the silhouette in their columns is theirs alone. -> [(name, old height %, new height %)]"""
    import numpy as np
    a = np.asarray(Image.open(plan["side"]).convert("RGB")).astype(np.float32) / 255.0
    h, w = a.shape[:2]
    k = max(2, min(h, w) // 40)
    back = np.median(np.concatenate([a[:k, :k].reshape(-1, 3), a[:k, -k:].reshape(-1, 3), a[-k:, :k].reshape(-1, 3),
                                     a[-k:, -k:].reshape(-1, 3)]), axis=0)
    fg = np.abs(a - back).max(axis=2) > threshold
    changed = []
    for p in plan["parts"]:
        if p.get("method") != "code":
            continue
        x0, x1, zt, zb = p["side_box"]
        area = max(1e-9, (x1 - x0) * (zb - zt))
        inside = 0.0
        for q in plan["parts"]:
            if q is p:
                continue
            qx0, qx1, qzt, qzb = q["side_box"]
            inside = max(inside, max(0.0, min(x1, qx1) - max(x0, qx0)) * max(0.0, min(zb, qzb) - max(zt, qzt)) / area)
        if inside > 0.5:
            continue
        # the part's own columns (clear of its ends, where neighbours meet it), rows near its planned band
        span = x1 - x0
        c0, c1 = int((x0 + 0.2 * span) / 100 * w), int((x1 - 0.2 * span) / 100 * w)
        band = zb - zt
        r0, r1 = int(max(0.0, zt - 0.6 * band) / 100 * h), int(min(100.0, zb + 0.6 * band) / 100 * h)
        tops, bots = [], []
        for c in range(max(0, c0), min(w, c1 + 1)):
            rows = np.nonzero(fg[r0:r1, c])[0]
            if len(rows):
                tops.append(r0 + rows.min())
                bots.append(r0 + rows.max() + 1)
        if len(tops) < 5:
            continue
        nzt, nzb = float(np.median(tops)) / h * 100, float(np.median(bots)) / h * 100
        if not (0.5 * band <= nzb - nzt <= 2.0 * band):
            continue
        p["side_box"] = [x0, x1, round(nzt, 2), round(nzb, 2)]
        p["box_min"], p["box_max"] = to_metres(p["side_box"], p["front_span"], plan["dims_m"])
        p["centreline"] = True                    # the assembler lines it up with the body's own axis
        # a part standing out on its own like this is a turned one (a barrel, a muzzle device): no narrower across than
        # it is tall, whatever the front picture's end-on reading said
        W = plan["dims_m"][1]
        tall = p["box_max"][2] - p["box_min"][2]
        # round: as wide as it is tall, the measured height deciding (a muzzle brake read 28.5 mm wide from the front
        # picture against 21.9 mm tall came out oval, 2026-09-28)
        if abs((p["box_max"][1] - p["box_min"][1]) - tall) > 0.05 * tall and W > 0:
            yc = (p["box_min"][1] + p["box_max"][1]) / 2
            p["front_span"] = [round(max(0.0, (yc - tall / 2 + W / 2) / W * 100), 2),
                               round(min(100.0, (yc + tall / 2 + W / 2) / W * 100), 2)]
            p["box_min"], p["box_max"] = to_metres(p["side_box"], p["front_span"], plan["dims_m"])
        changed.append((p["name"], band, nzb - nzt))
    return changed


FINISHES = ("polymer", "rubber", "metal", "painted", "glass", "wood", "fabric")


def clean_material(mat):
    """A plan material with every key present: colour, finish, metal, roughness, glass, keep_texture. The finish
    decides what the surface pass does (rubber: rough, stippled; metal: reflective, brushed; polymer: satin)."""
    mat = mat if isinstance(mat, dict) else {}
    colour = str(mat.get("color") or "#808080")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
        colour = "#808080"
    try:
        rough = min(1.0, max(0.05, float(mat.get("roughness", 0.6))))
    except (TypeError, ValueError):
        rough = 0.6
    finish = str(mat.get("finish") or "").lower()
    if finish not in FINISHES:
        finish = "metal" if mat.get("metal") else "glass" if mat.get("glass") else "polymer"
    return {"color": colour, "finish": finish, "metal": bool(mat.get("metal")) or finish == "metal",
            "roughness": round(rough, 3), "glass": bool(mat.get("glass")) or finish == "glass",
            "keep_texture": bool(mat.get("keep_texture"))}


def validate_plan(raw, dims, max_parts=None):
    """The builder's JSON -> a clean plan: unique names, a known method, a material, boxes in metres. Parts whose
    numbers make no sense are dropped with the reason; a plan with fewer than two parts is refused."""
    max_parts = max_parts or config.ASSEMBLY_MAX_PARTS
    parts, dropped, taken = [], [], set()
    for p in (raw or {}).get("parts") or []:
        if not isinstance(p, dict):
            continue
        name = clean_name(p.get("name"), taken)
        try:
            box_min, box_max = to_metres(p.get("side_box") or [], p.get("front_span") or [], dims)
        except (ValueError, TypeError) as exc:
            dropped.append({"name": name, "reason": str(exc)})
            continue
        method = str(p.get("method") or "code").lower()
        part = {"name": name, "what": str(p.get("what") or name)[:400],
                "method": method if method in ("code", "vendor") else "code",
                "side_box": [pct(v) for v in p["side_box"]], "front_span": [pct(v) for v in p["front_span"]],
                "box_min": box_min, "box_max": box_max, "material": clean_material(p.get("material"))}
        if part["method"] == "vendor":
            zones = []
            for z in (p.get("zones") or [])[:12]:
                if not isinstance(z, dict):
                    continue
                try:
                    zmin, zmax = to_metres(z.get("side_box") or [], z.get("front_span") or p["front_span"], dims)
                except (ValueError, TypeError):
                    continue
                zones.append({"name": clean_name(z.get("name"), set()), "box_min": zmin, "box_max": zmax,
                              "side_box": [pct(v) for v in z["side_box"]], "material": clean_material(z.get("material"))})
            part["zones"] = zones
        parts.append(part)
    if len(parts) > max_parts:
        dropped += [{"name": p["name"], "reason": "over the %d-part limit" % max_parts} for p in parts[max_parts:]]
        parts = parts[:max_parts]
    if len(parts) < 2:
        raise ValueError("the plan has %d usable part(s); an assembly needs at least two" % len(parts))
    return {"parts": parts, "dropped": dropped, "notes": str((raw or {}).get("notes") or "")[:800],
            "dims_m": [round(v, 4) for v in dims]}


def pick_views(ref, category):
    """(side picture, front picture) from the approved reference, or None when the set has no side and front view.
    Weapons: the primary is the side profile (muzzle to the right) and the second view looks down the barrel. Vehicles
    and aircraft: the orthographic set [front, left, back, right]; its left view is mirrored so the nose is on the right."""
    views = list(ref.get("views") or [])
    seed_views = list(ref.get("seed_views") or [])
    if category == "weapon" and len(views) >= 2:
        return views[0], views[1], False
    if category == "weapon" and views:
        # the muzzle view of a long gun often fails its check (the editor draws a side view into it; the shotgun of
        # 2026-09-27 twice): plan from the side alone, the builder estimates the widths
        return views[0], None, False
    if category in ("vehicle", "aircraft", "helicopter") and len(seed_views) >= 2:
        return seed_views[1], seed_views[0], True
    return None


FACING_PROMPT = """This picture shows an object from the side. Which end is its FRONT - the muzzle of a gun, the nose of a
vehicle or aircraft, the end that leads when it moves? Answer JSON only: {"front": "left" | "right", "confidence": 0-1}"""


def front_is_left(job, path):
    """True when the side picture has the object's front on the LEFT. The weapon skill once asked for a 'left-side
    profile, muzzle pointing right' - a contradiction - and the compact pistol came back muzzle-left (2026-09-27);
    planned as drawn, the assembly would have been built back to front."""
    from ..llm import extract_json
    j = extract_json(job.llm.vision(FACING_PROMPT, [path], max_tokens=300)) or {}
    return str(j.get("front", "right")).strip().lower() == "left"


def make_plan(job, spec, side_src, front_src, mirror_side=False):
    """-> plan dict (see validate_plan) with the gridded pictures it was made from."""
    work = os.path.join(job.work_dir, "plan")
    os.makedirs(work, exist_ok=True)
    side, front = os.path.join(work, "side.png"), os.path.join(work, "front.png")
    side_px = crop_to_object(side_src, side)
    if mirror_side:
        Image.open(side).transpose(Image.FLIP_LEFT_RIGHT).save(side)
    if front_is_left(job, side):
        Image.open(side).transpose(Image.FLIP_LEFT_RIGHT).save(side)
        job.log("  the side picture has the front on the left; mirrored so the front is on the right")
    side_g = draw_grid(side, os.path.join(work, "side_grid.png"))
    if front_src:
        front_px = crop_to_object(front_src, front)
        dims = object_dims(spec.size_m, side_px, front_px)
        front_g = draw_grid(front, os.path.join(work, "front_grid.png"))
        pictures = [side_g, front_g]
        prompt = PLAN_PROMPT
    else:
        front = front_g = None
        dims = (float(spec.size_m), float(spec.size_m) * side_px[1] / float(side_px[0]) * 0.3,
                float(spec.size_m) * side_px[1] / float(side_px[0]))            # provisional width until the builder says
        pictures = [side_g]
        prompt = PLAN_PROMPT.replace(NO_FRONT_FROM, NO_FRONT_TO).replace(
            '"front_span": [y_left, y_right],                   percent of picture 2 across, tight around the part as seen from the front',
            '"front_span": [y_left, y_right],                   your estimate, percent of the full width (a centred part is symmetric about 50)')
        prompt = prompt.replace(' "notes": "anything the assembly must respect"}}',
                                ' "overall_width_m": the object\'s full width in metres, "notes": "anything the assembly must respect"}}')
    prompt = prompt.format(description=spec.description, category=spec.category, length_m=dims[0], height_m=dims[2],
                           width_m=dims[1], max_parts=config.ASSEMBLY_MAX_PARTS)
    last = None
    for attempt in range(2):
        text = job.llm.vision(prompt + ("" if last is None else "\n\nThe previous answer was unusable: %s" % last),
                              pictures, model=config.BUILDER_MODEL, max_tokens=8000, effort="medium")
        try:
            raw = extract_json(text)
            if not front_src:
                try:
                    w = float((raw or {}).get("overall_width_m") or 0)
                except (TypeError, ValueError):
                    w = 0
                if 0.02 * dims[0] < w < 1.5 * dims[0]:
                    dims = (dims[0], w, dims[2])
            plan = validate_plan(raw, dims)
            break
        except (ValueError, TypeError) as exc:
            last = str(exc)
            job.log("  plan attempt %d unusable: %s" % (attempt + 1, last))
    else:
        raise RuntimeError("the builder could not plan the parts: %s" % last)
    plan.update({"side": side, "front": front, "side_grid": side_g, "front_grid": front_g})
    for name, before, after in snap_to_silhouette(plan):
        job.log("  %s: box snapped to the picture, height %.1f%% -> %.1f%%" % (name, before, after))
    sampled = sample_colours(plan)
    if sampled:
        job.log("  colours read off the picture: %s" % ", ".join("%s %s" % (n, c) for n, _p, c in sampled))
    with open(os.path.join(work, "plan.json"), "w") as f:
        json.dump(plan, f, indent=1)
    job.log("  plan: %d parts (%d code, %d vendor), %.3f x %.3f x %.3f m%s" % (
        len(plan["parts"]), sum(p["method"] == "code" for p in plan["parts"]), sum(p["method"] == "vendor" for p in plan["parts"]),
        dims[0], dims[1], dims[2], "; dropped %s" % ", ".join(d["name"] for d in plan["dropped"]) if plan["dropped"] else ""))
    for p in plan["parts"]:
        job.log("    %s (%s): %s" % (p["name"], p["method"], p["what"][:90]))
    return plan
