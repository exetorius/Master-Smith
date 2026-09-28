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

Split the object into a vendor body and the few parts that code builds better (between 2 and {max_parts} parts). No
decals, no text, no screws smaller than 1% of the length.

Keep together what is ONE moulding or casting in reality: a polymer pistol frame WITH its grip and trigger guard,
a rifle stock with its pistol grip, a vehicle's one-piece body shell. A cut through the middle of one moulding
becomes a visible seam or gap when the halves are built apart.

For each part choose how it is built. The vendor models almost everything; code only what it makes perfectly:
- "code" ONLY for these, and only when they are clearly visible:
  * turned (lathe) parts, round in cross-section: barrels, muzzle brakes, flash hiders, suppressors, gas blocks,
    round knobs and bolt handles, exhaust tips, round lenses' housings, wheel hubs
  * repeated machined parts: Picatinny / M-LOK rails, rail covers with regular slots, cooling-fin stacks
  * small hardware: exposed pins, screw heads and rivets that matter at game distance
  * controls that stick out SIDEWAYS on one side only: a charging handle, a selector lever, a bolt or magazine release,
    a side knob. The vendor sees one side and mirrors them onto the other (the bullpup came out with its charging
    handle on both sides), so code builds them on the right side only
  Code parts are modelled in code from primitives; everything irregular, moulded, curved or busy with small features
  comes out better from the vendor.
- "vendor": an AI image-to-3D model. EVERYTHING else is ONE vendor part, the body: the housing or receiver, stock,
  grip, trigger and guard, handguard with its vents, sights, magazine, levers, cheek rest, slide, panels - drawn as the
  whole object WITHOUT the code parts. In its "what", describe it and list the parts it is drawn WITHOUT ("the
  bullpup body with its sights, magazine and handguard, without the barrel, muzzle brake and rails"). Give it the
  colour of its largest area; if it shows clearly different colours (an olive panel on a grey body), set
  "keep_texture": true in its material so the vendor's own colours are kept.
  A second vendor part only for a large separate piece of a different material that would confuse one picture
  (a vehicle's tyres, a sling).
A typical weapon: one vendor body plus a barrel, a muzzle device and a rail or two in code. A typical vehicle: one
vendor body plus code wheel hubs and exhaust tips.

Answer JSON only:
{{"parts": [{{"name": "PascalCase unique", "what": "one sentence: shape, features to model, colour and finish",
  "method": "code" | "vendor",
  "side_box": [x_left, x_right, z_top, z_bottom],    percent of picture 1, tight around the part as seen from the side
  "front_span": [y_left, y_right],                   percent of picture 2 across, tight around the part as seen from the front
  "material": {{"color": "#rrggbb as the camera sees it", "metal": true/false, "roughness": 0.0-1.0, "glass": false, "keep_texture": false}}}}],
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
        # a part standing out on its own like this is a turned one (a barrel, a muzzle device): no narrower across than
        # it is tall, whatever the front picture's end-on reading said
        W = plan["dims_m"][1]
        tall = p["box_max"][2] - p["box_min"][2]
        if p["box_max"][1] - p["box_min"][1] < 0.8 * tall and W > 0:
            yc = (p["box_min"][1] + p["box_max"][1]) / 2
            p["front_span"] = [round(max(0.0, (yc - tall / 2 + W / 2) / W * 100), 2),
                               round(min(100.0, (yc + tall / 2 + W / 2) / W * 100), 2)]
            p["box_min"], p["box_max"] = to_metres(p["side_box"], p["front_span"], plan["dims_m"])
        changed.append((p["name"], band, nzb - nzt))
    return changed


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
        mat = p.get("material") if isinstance(p.get("material"), dict) else {}
        colour = str(mat.get("color") or "#808080")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
            colour = "#808080"
        try:
            rough = min(1.0, max(0.05, float(mat.get("roughness", 0.6))))
        except (TypeError, ValueError):
            rough = 0.6
        parts.append({"name": name, "what": str(p.get("what") or name)[:400],
                      "method": method if method in ("code", "vendor") else "code",
                      "side_box": [pct(v) for v in p["side_box"]], "front_span": [pct(v) for v in p["front_span"]],
                      "box_min": box_min, "box_max": box_max,
                      "material": {"color": colour, "metal": bool(mat.get("metal")), "roughness": round(rough, 3),
                                   "glass": bool(mat.get("glass")), "keep_texture": bool(mat.get("keep_texture"))}})
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
    with open(os.path.join(work, "plan.json"), "w") as f:
        json.dump(plan, f, indent=1)
    job.log("  plan: %d parts (%d code, %d vendor), %.3f x %.3f x %.3f m%s" % (
        len(plan["parts"]), sum(p["method"] == "code" for p in plan["parts"]), sum(p["method"] == "vendor" for p in plan["parts"]),
        dims[0], dims[1], dims[2], "; dropped %s" % ", ".join(d["name"] for d in plan["dropped"]) if plan["dropped"] else ""))
    for p in plan["parts"]:
        job.log("    %s (%s): %s" % (p["name"], p["method"], p["what"][:90]))
    return plan
