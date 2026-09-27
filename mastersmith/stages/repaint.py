"""Stage 2b (the "pictures" hybrid repaint): the seed's own orthographic renders are repainted photoreal by the
picture model in the reference's look and projected back onto the mesh from the exact same cameras, then the base
colour is baked into the seed's own atlas. Ported from the openrouter-only branch (2026-09-19), where it carried the
stencils, latches and panel lines of a reference onto the faces that face each side. The seed's roughness, metallic
and normal maps are kept; only the colour changes. About five pictures (7 cents each) and half a minute of Blender.

Guards learned on that branch: a repaint whose silhouette strays from the render (the picture model drawing its own
copy of the object) is not projected; faces take a picture only within ~40 degrees of its camera; the projection never
fully replaces the atlas underneath, so a picture that does not quite fit smears less."""
import json
import os
import subprocess

import numpy as np
from PIL import Image

from .. import config, pricing
from ..images import ImageError
from ..llm import extract_json

BLENDER_DIR = config.ROOT / "mastersmith" / "blender"
VIEWS = ("front", "left", "back", "right", "top")
WORDS = {"front": "from the front", "left": "from the left side", "back": "from the back", "right": "from the right side",
         "top": "from straight above"}
PROMPT = ("Picture 1 is a photograph of the object. Picture 2 is a plain grey render of a 3D model of that same object, seen "
          "{view}, orthographic. Repaint picture 2 as a photograph of the surfaces it shows: keep every shape, edge, part and "
          "proportion exactly where it is in picture 2 and keep the same framing; do NOT add, move, redraw or correct any part "
          "even where the model is crude, and do NOT draw a second copy of the object; give the existing surfaces the "
          "materials, colours, markings, stencils, wear and hardware of picture 1 as they appear on that side{extra}. Perfectly "
          "even flat lighting, no shadows, no highlights, plain white background, sharp, nothing else in frame.")
MIN_OVERLAP = 0.55
MARKINGS = """List the printed markings (stencils, labels, decals, badges) visible on this object: {brief}
Answer JSON only: {{"markings": [{{"text": "exact text", "side": "front|left|back|right|top", "u": 0-1 left to right on that side,
 "v": 0-1 bottom to top, "height": fraction of that side's height, "color": "#rrggbb"}}, ...]}} (empty list when there are none)."""


def _fg(im):
    border = np.concatenate([im[:6].reshape(-1, 3), im[-6:].reshape(-1, 3), im[:, :6].reshape(-1, 3), im[:, -6:].reshape(-1, 3)])
    back = np.median(border, axis=0)
    return np.abs(im - back).max(axis=2)


def fit_to_render(picture, render, out_path):
    """The picture as drawn or warped so its silhouette box lands on the render's, whichever overlaps the render's
    silhouette best; the backdrop goes into the alpha channel. -> (path, box, overlap) or (None, None, 0)."""
    pic = np.asarray(Image.open(picture).convert("RGB")).astype(np.float32)
    ren = np.asarray(Image.open(render).convert("RGB")).astype(np.float32)
    d_pic, d_ren = _fg(pic), _fg(ren)
    yp, xp = np.nonzero(d_pic > 22.0)
    yr, xr = np.nonzero(d_ren > 18.0)
    if len(xp) < 200 or len(xr) < 200:
        return None, None, 0.0
    bp = (int(xp.min()), int(yp.min()), int(xp.max()) + 1, int(yp.max()) + 1)
    br = (int(xr.min()), int(yr.min()), int(xr.max()) + 1, int(yr.max()) + 1)
    alpha = np.clip((d_pic - 12.0) / 30.0, 0, 1)
    full = Image.fromarray(np.clip(np.dstack([pic, alpha * 255.0]), 0, 255).astype(np.uint8), "RGBA")
    ren_mask = d_ren > 18.0
    warped = full.crop(bp).resize((max(1, br[2] - br[0]), max(1, br[3] - br[1])), Image.LANCZOS)
    canvas = Image.new("RGBA", (ren.shape[1], ren.shape[0]), (255, 255, 255, 0))
    canvas.paste(warped, (br[0], br[1]))
    raw = full.resize((ren.shape[1], ren.shape[0]), Image.LANCZOS) if full.size != (ren.shape[1], ren.shape[0]) else full

    def overlap(im):
        m = np.asarray(im)[:, :, 3] > 100
        inter, union = float((m & ren_mask).sum()), float((m | ren_mask).sum())
        return inter / union if union else 0.0
    o_raw, o_warp = overlap(raw), overlap(canvas)
    best = canvas if o_warp >= o_raw else raw
    best.save(out_path)
    return out_path, (br if o_warp >= o_raw else (0, 0, ren.shape[1], ren.shape[0])), max(o_raw, o_warp)


def _hex(c, default=(17, 17, 17)):
    c = str(c or "").strip().lstrip("#")
    if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    return default


def stamp_markings(path, box, markings):
    """Exact lettering drawn onto the fitted side picture (u left to right, v bottom to top over the object's box)."""
    from PIL import ImageDraw, ImageFont
    if not markings:
        return 0
    im = Image.open(path).convert("RGBA")
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    n = 0
    for m in markings:
        px = int(round(max(4, (y1 - y0) * float(m["height"]))))
        try:
            font = ImageFont.load_default(size=px)
        except TypeError:
            font = ImageFont.load_default()
        text = str(m["text"])
        tw, th = draw.textbbox((0, 0), text, font=font)[2:]
        cx, cy = x0 + float(m["u"]) * (x1 - x0), y1 - float(m["v"]) * (y1 - y0)
        r, g, b = _hex(m.get("color"))
        draw.text((cx - tw / 2.0, cy - th / 2.0), text, font=font, fill=(r, g, b, 225))
        n += 1
    a = np.asarray(im).astype(np.float32)
    l = np.asarray(layer).astype(np.float32)
    la = l[:, :, 3:4] / 255.0 * (a[:, :, 3:4] / 255.0)
    out = a.copy()
    out[:, :, :3] = a[:, :, :3] * (1 - la) + l[:, :, :3] * la
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA").save(path)
    return n


def read_markings(job, reference):
    try:
        j = extract_json(job.llm.vision(MARKINGS.format(brief=job.spec.description), [reference], max_tokens=800)) or {}
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for m in (j.get("markings") or [])[:12]:
        if not isinstance(m, dict) or not str(m.get("text") or "").strip():
            continue
        side = str(m.get("side") or "").lower()
        if side not in VIEWS:
            continue

        def num(v, d):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d
        out.setdefault(side, []).append({"text": str(m["text"]).strip()[:60], "u": max(0.0, min(1.0, num(m.get("u"), 0.5))),
                                         "v": max(0.0, min(1.0, num(m.get("v"), 0.5))), "height": max(0.02, min(0.5, num(m.get("height"), 0.08))),
                                         "color": str(m.get("color") or "#111111")[:9]})
    return out


def make_repaint(job, seed_glb, reference):
    """-> {"glb": repainted textured GLB, "elevations": [...], "skipped": [...]}; raises when Blender fails."""
    work = os.path.join(job.work_dir, "repaint")
    os.makedirs(work, exist_ok=True)
    odir = os.path.join(work, "ortho")
    args = {"glb": seed_glb, "out_dir": odir, "size": 1024, "views": list(VIEWS)}
    args_path = os.path.join(work, "ortho_args.json")
    json.dump(args, open(args_path, "w"))
    proc = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(BLENDER_DIR / "ortho_views.py"), "--", args_path],
                          capture_output=True, text=True, timeout=900)
    views_path = os.path.join(odir, "views.json")
    if proc.returncode != 0 or not os.path.exists(views_path):
        raise RuntimeError("the orthographic renders failed: %s" % (proc.stderr or proc.stdout or "")[-300:])
    views = json.load(open(views_path))
    marks = read_markings(job, reference) if config.STAMP_MARKINGS else {}
    elevations, skipped = {}, []
    for key in VIEWS:
        rec = views.get(key)
        if not rec:
            continue
        extra = ""
        if marks.get(key):
            extra = ". Leave out all lettering, stencils and text: the markings are added separately"
        pic = os.path.join(work, "repaint_%s.png" % key)
        try:
            job.images.generate(PROMPT.format(view=WORDS[key], extra=extra), pic, model=pricing.edit_model(job.spec),
                                references=[reference, rec["file"]], aspect_ratio="1:1")
        except ImageError as exc:
            job.log("  repaint %s not made (%s)" % (key, str(exc)[:80]))
            skipped.append(key)
            continue
        fitted, box, ov = fit_to_render(pic, rec["file"], os.path.join(work, "repaint_%s_fit.png" % key))
        if not fitted or ov < MIN_OVERLAP:
            job.log("  repaint %s not used: the picture strayed from the render (overlap %.0f%%)" % (key, ov * 100))
            skipped.append(key)
            continue
        if marks.get(key):
            job.log("  %d marking(s) stamped on the %s side" % (stamp_markings(fitted, box, marks[key]), key))
        elevations[key] = {"file": fitted, "R": rec["R"], "U": rec["U"], "S": rec["S"], "C": rec["C"]}
    if not elevations:
        raise RuntimeError("no side picture could be used for the repaint")
    out_glb = os.path.join(job.dir, "seed_repainted.glb")
    bargs = {"glb": seed_glb, "out_glb": out_glb, "work_dir": work, "name": job.spec.name, "elevations": elevations,
             "bake_size": int(os.environ.get("MASTERSMITH_BAKE_SIZE", "2048") or 2048)}
    bpath = os.path.join(work, "repaint_args.json")
    json.dump(bargs, open(bpath, "w"), indent=1)
    job.log("  repaint: %d side picture(s) projected and baked" % len(elevations))
    proc = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(BLENDER_DIR / "repaint.py"), "--", bpath],
                          capture_output=True, text=True, timeout=1800)
    with open(os.path.join(work, "blender.log"), "w", encoding="utf-8") as f:
        f.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    rep_path = os.path.join(work, "repaint.json")
    if proc.returncode != 0 or not os.path.exists(rep_path):
        tail = "\n".join([l for l in (proc.stdout or "").splitlines() if l.strip()][-10:])
        raise RuntimeError("the repaint bake failed (exit %s):\n%s\n%s" % (proc.returncode, tail, (proc.stderr or "")[-400:]))
    rep = json.load(open(rep_path))
    if not rep.get("ok"):
        raise RuntimeError("the repaint bake failed: %s" % str(rep.get("error"))[-400:])
    return {"glb": out_glb, "elevations": sorted(elevations), "skipped": skipped, "materials": rep.get("materials")}
