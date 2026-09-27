"""Assembly builds (docs/ASSEMBLY.md): plan the parts, build each one right, assemble, check against the pictures.
Nothing here repairs a finished mesh: a part that is wrong is rebuilt, a part in the wrong place is moved."""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from .. import config, pricing
from ..fal import first_url
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
Be critical. Answer OK only if every major shape, opening, recess and step is there and the outline matches the
reference within a few percent of the box. A missing section, a wrong outline, a part that does not fill its box
like the reference does, or a plain slab where the reference has recesses and facets is NOT OK.
If it is not OK, first list in one or two lines what is wrong, then give the corrected complete function in one
```python block. Your previous code:
```python
{code}
```"""

CHECK_PROMPT = """You are checking an assembled game asset against its reference. Pictures 1 and 2: the reference side
and front views. Pictures 3 and 4: the assembly, same framing, same grid. Blue rectangles are the planned part boxes
(percent of the pictures), labelled by name.
The parts: {parts}
Find parts that are in the wrong place or the wrong size by more than 2% of the picture, and parts whose shape is
clearly wrong. Answer JSON only:
{{"ok": true/false, "moves": [{{"name": "...", "side_box": [x_left, x_right, z_top, z_bottom], "front_span": [y_left, y_right]}}],
  "rebuild": [{{"name": "...", "why": "what is wrong with its shape"}}], "notes": "..."}}
Coordinates in percent of the REFERENCE pictures, like the plan. Empty lists when nothing needs changing."""


def _code_from(text):
    m = re.search(r"```(?:python)?\s*(.*?)```", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()


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


def _size(part):
    return [round(part["box_max"][i] - part["box_min"][i], 5) for i in range(3)]


def build_code_part(job, spec, part, plan):
    """The builder writes the part, Blender builds it, errors go back to the builder (two retries), then the builder
    compares its render with the reference crops once and may correct it. -> {"glb", "code", ...} or None."""
    name = part["name"]
    out_dir = os.path.join(job.work_dir, "parts", name)
    os.makedirs(out_dir, exist_ok=True)
    side_crop = _crop_part(plan["side"], part["side_box"][:2] + part["side_box"][2:], os.path.join(out_dir, "ref_side.png"))
    L, W, H = _size(part)
    zt, zb = part["side_box"][2], part["side_box"][3]
    side_sq = _box_square(plan["side"], part["side_box"], os.path.join(out_dir, "box_side.png"))
    side_mm = _mm_scale(side_sq, os.path.join(out_dir, "box_side_mm.png"), max(L, H), ("x", "z"))
    front_sq = front_mm = None
    if plan.get("front"):
        front_sq = _box_square(plan["front"], part["front_span"] + [zt, zb], os.path.join(out_dir, "box_front.png"))
        front_mm = _mm_scale(front_sq, os.path.join(out_dir, "box_front_mm.png"), max(W, H), ("y", "z"))
    dims = plan.get("dims_m") or [L, W, H]
    big = L > 0.25 * dims[0] or H > 0.35 * dims[2]           # a housing or stock carries the look: think harder
    effort = "high" if big else "medium"
    prompt = CODE_PROMPT.format(category=spec.category, description=spec.description[:600], name=name, what=part["what"],
                                L_mm=L * 1000, W_mm=W * 1000, H_mm=H * 1000, kit=codecheck.KIT_DOC)
    if front_mm is None:
        prompt = prompt.replace("Picture 2: the same from the FRONT (the part's left on the right of the picture): y across, z up, millimetres.\nPicture 3:",
                                "There is no front picture: shape the cross-section (y) from the description and how such a part is made.\nPicture 2:")
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}] + _images([p for p in (side_mm, front_mm, side_crop) if p])}]
    best, code = None, None
    for attempt in range(3):
        reply = job.llm.chat(messages, model=config.BUILDER_MODEL, max_tokens=12000 if big else 8000, temperature=0.2,
                             effort=effort)
        code = _code_from(reply.get("content"))
        messages.append({"role": "assistant", "content": reply.get("content") or ""})
        res = _run_part(job, part, code, out_dir, attempt)
        if res.get("ok"):
            best = {**res, "code": code}
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
        refine = REFINE_PROMPT.format(name=name, what=part["what"], code=best["code"])
        if not front_sq:
            refine = "(There is no front picture: the comparison has only the SIDE row.)\n" + refine
        text = job.llm.vision(refine, [cmp],
                              model=config.BUILDER_MODEL, max_tokens=12000 if big else 8000, effort=effort, json_only=False)
        if text.strip().upper().startswith("OK") or "```" not in text:
            job.log("  part %s: accepted by its own check after %d correction(s) (%d tris)" % (name, round_no, best.get("triangles", 0)))
            return best
        why = text.split("```")[0].strip().replace("\n", " ")[:160]
        res = _run_part(job, part, _code_from(text), out_dir, "refined%d" % round_no)
        if not res.get("ok"):
            job.log("  part %s: correction %d failed (%s); keeping the previous build" % (name, round_no + 1, str(res.get("error"))[:120]))
            return best
        best = {**res, "code": _code_from(text), "refined": round_no + 1}
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
    run_dir = os.path.join(out_dir, "run_%s" % tag)
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
        res["renders"] = {k: os.path.join("run_%s" % tag, v) for k, v in res["renders"].items()}
        res["box_renders"] = {k: os.path.join("run_%s" % tag, v) for k, v in (res.get("box_renders") or {}).items()}
    return res


def build_vendor_part(job, spec, part, plan):
    """The part drawn alone from the side reference, checked, seeded on its own and oriented. -> {"blend", "yaw"} or None."""
    name = part["name"]
    out_dir = os.path.join(job.work_dir, "parts", name)
    os.makedirs(out_dir, exist_ok=True)
    ref = plan["side"]
    picture, fixes = None, ""
    for attempt in range(2):
        path = os.path.join(out_dir, "picture_%d.png" % attempt)
        job.images.generate("Show ONLY %s from this exact object, whole and complete, exactly as it looks here (same shape, "
                            "colours and materials), seen from a three-quarter view, isolated on a plain pure white "
                            "background, nothing else in frame, sharp product photograph. %s" % (part["what"], fixes),
                            path, model=pricing.edit_model(spec), references=[ref], aspect_ratio="1:1")
        j = extract_json(job.llm.vision(PART_CHECK.format(part=part["what"]), [path])) or {}
        if j.get("ok") and int(j.get("score", 0) or 0) >= 6:
            picture = path
            break
        fixes, picture = str(j.get("fixes") or ""), picture or path
    url = job.fal.upload(picture)
    vendor = pricing.seed_vendor(spec)["key"]
    if vendor.startswith("hitem3d3"):
        model, payload = "hitem3d/hi3d/v3.0/image-to-3d", {"image_url": url, "model": "hi3dv3.0", "resolution": "2048quality",
                                                          "face_count": 200000, "enable_texture": True, "enable_pbr": True,
                                                          "export_format": "glb", "enable_safety_checker": False}
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
    oriented = orient_part(job, spec, {"name": name, "phrase": part["what"], "size_m": max(_size(part))}, glb)
    if not oriented:
        return None
    job.log("  part %s: seeded by %s, facing yaw %s" % (name, model.split("/")[0], oriented.get("yaw")))
    return {"blend": oriented["blend"], "yaw": oriented.get("yaw", 0), "picture": picture, "seed": glb}


def _assemble(job, spec, plan, built, round_no, reference):
    parts = []
    for p in plan["parts"]:
        b = built.get(p["name"])
        if not b:
            continue
        entry = {"name": p["name"], "kind": "code" if b.get("code") else "vendor", "box_min": p["box_min"],
                 "box_max": p["box_max"], "material": p["material"]}
        if b.get("code"):
            entry.update({"blend": b["blend"]} if b.get("blend") and os.path.exists(b["blend"]) else {"glb": b["glb"]})
        else:
            entry.update({"blend": b["blend"], "yaw": b.get("yaw", 0)})
        parts.append(entry)
    out = os.path.join(job.dir, "delivery") if round_no == "final" else os.path.join(job.work_dir, "assembly_%s" % round_no)
    _blender(job, "assemble.py", {"name": spec.name, "out_dir": out, "tri_budget": spec.tri_budget, "engine": spec.engine,
                                  "atlas_size": 4096 if spec.tri_budget >= 100000 else 2048, "render_size": 768,
                                  "spec": spec.to_dict(), "reference": reference, "parts": parts}, "assemble_%s" % round_no)
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
    listing = json.dumps([{"name": p["name"], "what": p["what"][:80], "side_box": p["side_box"], "front_span": p["front_span"]}
                          for p in plan["parts"]])
    prompt = CHECK_PROMPT.format(parts=listing)
    if not plan.get("front"):
        prompt = ("There is no front reference: picture 1 is the reference SIDE, picture 2 the assembly SIDE. Keep each "
                  "part's front_span unless it is clearly wrong.\n") + prompt
    text = job.llm.vision(prompt, pics, model=config.BUILDER_MODEL, max_tokens=4000, effort="medium")
    return extract_json(text) or {"ok": True, "notes": "no readable answer"}


def build_assembly(job, spec, ref):
    """-> (report, delivery_dir, record) or None when the approved pictures have no side and front view."""
    views = pick_views(ref, spec.category)
    if not views:
        return None
    side_src, front_src, mirror = views
    job.stage("plan")
    plan = make_plan(job, spec, side_src, front_src, mirror)
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
            if p and p["method"] == "code":
                p = {**p, "what": "%s. Fix: %s" % (p["what"], str(r.get("why"))[:300])}
                b = build_code_part(job, spec, p, plan)
                if b:
                    built[p["name"]] = b
        job.stage("assemble")
    delivery, report = _assemble(job, spec, plan, built, "final", reference)
    report["assembly"] = record
    report["build_mode"] = "assembly"
    with open(os.path.join(delivery, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    return report, delivery, record
