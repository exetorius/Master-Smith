"""Review the delivered asset against its reference and brief, not just its distant silhouette."""
import json
import os

from ..llm import extract_json

PROMPT = """You are reviewing a finished game asset. Judge what the delivered renders actually show.
Brief: {brief}
Images in order: {images}
The brief says where the reference came from. When it is the customer's own photograph, judge colours and materials
against the PHOTOGRAPH: the caption is the director's wording of it and can be wrong about a part's colour (a grey
magazine described as black is not a defect of the model).
When a SOURCE SEED preview is supplied, compare it with the finished model. If the source looks clean but the finish
has triangle/faceted patterns, report a finishing regression. Lighting/view may differ.
Judge silhouette, proportions, missing/fused/duplicate parts, colours, materials and texture quality.
When a SIX VIEWS sheet is supplied, inspect EVERY one of its six views (left, front, top / right, back, bottom) before
judging. The asset must be right from all six: parts that belong on one line (a barrel, a muzzle, a rail and the
sights of a weapon; the wheels of a vehicle) line up in the front and top views; nothing is slanted, bent, twisted,
floating free, doubled, poking through another part or missing; left and right match where the object is symmetric;
the underside and the back are finished, not open or melted. An asset that looks right from one side and wrong from
another is NOT shippable: any such defect makes the verdict "rebuild". Name the view that shows each issue
("front view: the barrel sits left of the handguard").
Do not infer detail from logs. A low-confidence or unreadable review is not acceptance.
Answer JSON only:
{{"score": 1-10, "silhouette_ok": true/false, "materials_ok": true/false,
 "issues": ["specific observed defects"], "verdict": "ship" | "ship with notes" | "rebuild",
 "finish_regression": true/false/null, "source_comparison": "visible before/after evidence, or unavailable",
 "rebuild_advice": "what the next build should change in the brief or the pictures, or empty",
 "views": {{"left": "ok" | "minor: ..." | "defect: ...", "right": ..., "front": ..., "back": ..., "top": ..., "bottom": ...}}}}
In "views" (only when the SIX VIEWS sheet is supplied) judge each view on its own: "defect" for anything misassembled,
slanted, doubled, missing, floating or poking through; "minor" for softness or small blemishes; "ok" otherwise.
"""


def six_view_sheet(job, delivery_dir, size=512):
    """The delivered asset from all six sides on one labelled sheet (preview_views.png in the delivery, so the page
    shows it too): left, front, top on the first row, right, back, bottom on the second. None when it cannot be made."""
    import glob
    from PIL import Image, ImageDraw, ImageFont
    from .finish import _blender
    glbs = [g for g in glob.glob(os.path.join(delivery_dir, "SM_*.glb")) if "_LOD" not in os.path.basename(g)]
    if not glbs:
        return None
    work = os.path.abspath(os.path.join(job.work_dir if hasattr(job, "work_dir") else delivery_dir, "six_views"))
    delivery_dir = os.path.abspath(delivery_dir)            # Blender runs in its own working folder
    glbs = [os.path.abspath(g) for g in glbs]
    try:
        _blender(job, "six_views.py", {"glb": glbs[0], "out_dir": work, "size": size}, "six_views", timeout=1200)
        sheet = Image.new("RGB", (size * 3, size * 2), (255, 255, 255))
        d = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
        except OSError:
            try:
                font = ImageFont.truetype("arialbd.ttf", 22)
            except OSError:
                font = ImageFont.load_default()
        for k, view in enumerate(("left", "front", "top", "right", "back", "bottom")):
            tile = Image.open(os.path.join(work, "view_%s.png" % view)).convert("RGB").resize((size, size))
            x, y = (k % 3) * size, (k // 3) * size
            sheet.paste(tile, (x, y))
            d.rectangle((x, y, x + size - 1, y + size - 1), outline=(255, 255, 255), width=3)
            d.text((x + 10, y + 8), view.upper(), fill=(255, 220, 60), font=font)
        path = os.path.join(delivery_dir, "preview_views.png")
        sheet.save(path)
        return path
    except Exception as exc:  # noqa: BLE001 - the review goes on with what it has, and says so
        job.log("  six views not rendered: %s" % str(exc)[:160])
        return None


def six_view_gate(j):
    """An asset is good only when all six views are: a defect in any view makes it a rebuild (score at most 5), a
    minor flaw in any keeps it at "ship with notes", and a review that did not judge every view cannot pass it."""
    views = j.get("views") if isinstance(j.get("views"), dict) else {}
    marks = {v: str(views.get(v) or "").strip().lower() for v in ("left", "right", "front", "back", "top", "bottom")}
    defects = [v for v, m in marks.items() if m.startswith("defect")]
    unjudged = [v for v, m in marks.items() if not m]
    minor = [v for v, m in marks.items() if m.startswith("minor")]
    if defects or unjudged:
        j["verdict"] = "rebuild"
        j["score"] = min(int(j.get("score") or 0), 5)
        why = ["%s view: %s" % (v, str(views.get(v))[7:].strip() or "defect") for v in defects]
        why += ["%s view not judged" % v for v in unjudged]
        j["issues"] = list(j.get("issues") or []) + ["six-view gate: " + "; ".join(why)]
    elif minor and j.get("verdict") == "ship":
        j["verdict"] = "ship with notes"
    j["six_view_gate"] = {"defects": defects, "minor": minor, "unjudged": unjudged}
    return j


RANK = {"ship": 0, "ship with notes": 1, "rebuild": 2}


def merge_reviews(a, b):
    """Two independent passes -> the stricter reading: the lower score, the worse verdict, every issue, and per view the
    worse mark (a lenient pass must not wave through what the other one saw)."""
    if not a:
        return b
    if not b:
        return a
    out = dict(a)
    out["score"] = min(int(a.get("score") or 0), int(b.get("score") or 0))
    out["verdict"] = max((a.get("verdict"), b.get("verdict")), key=lambda v: RANK.get(v, 2))
    out["issues"] = list(a.get("issues") or []) + [i for i in (b.get("issues") or []) if i not in (a.get("issues") or [])]
    for k in ("silhouette_ok", "materials_ok"):
        out[k] = bool(a.get(k)) and bool(b.get(k))
    va = a.get("views") if isinstance(a.get("views"), dict) else {}
    vb = b.get("views") if isinstance(b.get("views"), dict) else {}
    def level(m):
        m = str(m or "").lower()
        return 2 if m.startswith("defect") else 1 if m.startswith("minor") else 0 if m else -1
    merged = {}
    for v in set(va) | set(vb):
        la, lb = level(va.get(v)), level(vb.get(v))
        hi_mark = va.get(v) if la >= lb else vb.get(v)
        if max(la, lb) == 2 and min(la, lb) == 0:
            # one pass saw a defect, the other saw nothing wrong in that view: a note, not a failure (a single pass
            # imagined the bullpup's straight barrel "angled off the centreline", 2026-09-27)
            merged[v] = "minor: " + str(hi_mark)[7:].strip()
        else:
            merged[v] = str(hi_mark or "")           # both saw a problem there (or both ok): the worse mark stands
    out["views"] = merged
    out["passes"] = 2
    return out


def review(job, reference_path, renders, report=None, extra_references=()):
    report = report or {}
    files, labels = [], []
    if reference_path and os.path.exists(reference_path):
        files.append(reference_path)
        labels.append("original reference")
    for r in extra_references or ():
        # the approved front (muzzle) view: the delivered front view is compared with it, a knob on both sides where
        # the reference has one shows (the bullpup's charging handle, 2026-09-27)
        if r and os.path.exists(r) and r not in files:
            files.append(r)
            labels.append("approved reference view " + os.path.basename(r) + " (compare the matching delivered view with it)")
    for path in renders[:2]:
        if os.path.exists(path):
            files.append(path)
            labels.append("delivered full-asset " + os.path.basename(path))
    sheet = six_view_sheet(job, os.path.dirname(renders[0])) if renders else None
    if sheet:
        files.append(sheet)
        labels.append("delivered SIX VIEWS sheet, orthographic: left, front, top (row 1) / right, back, bottom (row 2)")
        if isinstance(report.get("renders"), list) and "preview_views.png" not in report["renders"]:
            report["renders"].append("preview_views.png")
    for name in (report.get("source_renders") or [])[:1]:
        path = os.path.join(job.dir, "delivery", name)
        if os.path.exists(path) and path not in files:
            files.append(path)
            labels.append("SOURCE SEED before finishing")
    brief = {"description": job.spec.description, "notes": job.spec.notes,
             "reference_source": "the customer's own photograph" if job.spec.reference_images else "a generated concept picture",
             "edit_instructions": job.spec.edit_instructions}
    if not any(label.startswith("delivered") for label in labels):
        j = {}
    else:
        ask = PROMPT.format(brief=json.dumps(brief), images=json.dumps(labels))
        j = extract_json(job.llm.vision(ask, files, max_tokens=2400))
        if sheet:
            # a second independent pass: one reading of six views waved through a doubled charging handle the other
            # named (2026-09-27); the stricter of the two stands
            j = merge_reviews(j if isinstance(j, dict) else {}, extract_json(job.llm.vision(ask, files, max_tokens=2400)) or {})
    if not isinstance(j, dict):
        j = {}
    if not j:
        j = {"score": 0, "issues": ["visual review unavailable or returned no JSON"], "verdict": "rebuild"}
    if not isinstance(j.get("issues"), list):
        j["issues"] = [str(j["issues"])] if j.get("issues") else []
    if sheet:
        j = six_view_gate(j)
    job.log("  review: %s/10, %s" % (j.get("score"), j.get("verdict")))
    return j
