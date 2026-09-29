"""`ms`: Master Smith as a set of small tools for a coding-agent session in this folder. No server, no container, no
model calls of its own - the person (or the agent) looks at the pictures and decides; each command does one
deterministic thing and writes into a job folder under out/<Name>/:

    out/<Name>/brief.json                 what is being built
    out/<Name>/ref/                       reference pictures (ref_0.png the approved side or three-quarter view)
    out/<Name>/plan/                      side.png / front.png (silhouette-cropped), *_grid.png (percent grid), plan.json
    out/<Name>/parts/<Part>/              side.png, quarter.png, seed.glb, registered.blend, registration.json, seed_render.png
    out/<Name>/delivery/                  the assembled asset, previews, preview_views.png (six sides), zip

    python -m mastersmith.ms new BullpupCarbine --category weapon --size 0.68 --description "..."
    python -m mastersmith.ms picture out/BullpupCarbine --out ref/ref_0.png --prompt "..." [--ref photo.jpg] [--model nano|local]
    python -m mastersmith.ms view out/BullpupCarbine --which side|front|back|left --from ref/ref_0.png [--mirror]
    python -m mastersmith.ms grid out/BullpupCarbine --side ref/side.png [--front ref/front.png] [--mirror]
    python -m mastersmith.ms plan out/BullpupCarbine plan.json          (plan.json written by hand, see AGENTS.md)
    python -m mastersmith.ms part-pictures out/BullpupCarbine Handguard [--fixes "..."] [--erased] [--no-quarter]
    python -m mastersmith.ms build out/BullpupCarbine TopRail          (a "method": "code" part from parts/TopRail/build.py)
    python -m mastersmith.ms mesh out/BullpupCarbine Handguard [--vendor local|tripo|hitem3d3] [--from quarter|side]
    python -m mastersmith.ms register out/BullpupCarbine Magazine [--from quarter|side] [--yaw 180] [--pitch -30]
    python -m mastersmith.ms fit out/BullpupCarbine Magazine [--quarter]           (outline sculpted onto its pictures)
    python -m mastersmith.ms brush out/BullpupCarbine Grip --op inflate --at back+0,0,-0.02 --radius 12 --strength 2
    python -m mastersmith.ms sdf out/BullpupCarbine Barrel [sdf.py]                 (an exact part from a distance function)
    python -m mastersmith.ms assemble out/BullpupCarbine [--parts A,B] [--no-sharpen]
    python -m mastersmith.ms sheet path/to/any.glb [--out sheet.png]
    python -m mastersmith.ms preview out/BullpupCarbine [--no-open]   (delivery/preview.html served and opened)
    python -m mastersmith.ms package out/BullpupCarbine
    python -m mastersmith.ms status out/BullpupCarbine
"""
import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import webbrowser

from PIL import Image, ImageOps

from . import config, pricing
from .fal import Fal, first_url
from .images import Images
from .spec import Spec
from .stages import plan as planmod
from .stages.assembly import THREE_QUARTER_PROMPT, _register, detail_map, erased_body_picture, is_body
from .stages.finish import _blender
from .stages.package import write_package
from .stages.review import six_view_sheet

PICTURE_MODELS = {"nano": "fal-ai/nano-banana-2", "nano-pro": "fal-ai/nano-banana-pro", "local": config.LOCAL_PICTURE_MODEL}
VIEW_TEXT = {
    "side": "the direct LEFT-side profile, camera level with the object, exactly side-on, the forward end (muzzle, nose) "
            "pointing to the RIGHT of the picture, strictly orthographic with no perspective",
    "front": "the direct FRONT view: camera exactly ahead of the forward end, level with the object, strictly orthographic, "
             "looking straight back at it",
    "back": "the direct REAR view: camera exactly behind the object, level with it, strictly orthographic",
    "left": "the direct LEFT-side profile, camera level with the object, exactly side-on, strictly orthographic",
    "top": "the direct TOP view: camera exactly above the object looking straight down, strictly orthographic",
    "quarter": "a three-quarter view: the camera about 35 degrees round from the left side towards the forward end and "
               "about 20 degrees above, so the side, the forward end and the top all show; a long telephoto lens from far "
               "away, almost no perspective",
}


class Job:
    """What the stage functions need of a job: a folder, a fal client, a picture client, a log."""

    def __init__(self, folder):
        self.dir = os.path.abspath(folder)
        self.work_dir = self.dir
        self.fal = Fal(log=self.log)
        self.images = Images(log=self.log)
        self.spec = Spec.from_dict(json.load(open(os.path.join(self.dir, "brief.json"))))

    def log(self, msg):
        print(msg, flush=True)

    def path(self, *parts):
        return os.path.join(self.dir, *parts)


def _plan(job):
    p = json.load(open(job.path("plan", "plan.json")))
    p["side"] = job.path("plan", "side.png")
    p["front"] = job.path("plan", "front.png") if os.path.exists(job.path("plan", "front.png")) else None
    return p


def _part(plan, name):
    for p in plan["parts"]:
        if p["name"].lower() == name.lower():
            return p
    sys.exit("no part %s in the plan (have: %s)" % (name, ", ".join(p["name"] for p in plan["parts"])))


def keep_existing(path, redraw=False):
    """True when a picture is already there and must be kept. 2026-09-28: pictures cost money and were approved by the
    owner; the tools never draw over one. `--redraw` draws again, and only after the owner said so."""
    if redraw or not os.path.exists(path):
        return False
    print("kept: %s already exists (ask the owner; --redraw draws it again)" % path)
    return True


def existing_pictures(folder):
    """Every picture a job already has: the reference pictures and each part's side/quarter picture."""
    found = []
    ref = os.path.join(folder, "ref")
    if os.path.isdir(ref):
        found += [os.path.join("ref", f) for f in sorted(os.listdir(ref)) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    parts = os.path.join(folder, "parts")
    if os.path.isdir(parts):
        for part in sorted(os.listdir(parts)):
            for f in ("side.png", "quarter.png", "build.py"):
                if os.path.exists(os.path.join(parts, part, f)):
                    found.append(os.path.join("parts", part, f))
    return found


# ------------------------------------------------------------------ commands
def cmd_new(a):
    folder = os.path.join(str(config.OUT_DIR), a.name)
    have = existing_pictures(folder) if os.path.isdir(folder) else []
    if have:
        print("job folder exists with %d pictures/builders already - they are kept; ask the owner before drawing any again:" % len(have))
        for f in have:
            print("  " + f)
        if os.path.exists(os.path.join(folder, "brief.json")) and not a.rebrief:
            print("brief.json kept too (--rebrief rewrites it)")
            return
    os.makedirs(os.path.join(folder, "ref"), exist_ok=True)
    brief = {"name": a.name, "description": a.description, "category": a.category, "style": a.style, "engine": a.engine,
             "tri_budget": a.tris or 0, "size_m": a.size, "multiview": True, "build_mode": "assembly"}
    json.dump(Spec.from_dict(brief).to_dict(), open(os.path.join(folder, "brief.json"), "w"), indent=1)
    print("job folder:", folder)


def cmd_picture(a):
    job = Job(a.job)
    out = job.path(a.out)
    if keep_existing(out, a.redraw):
        return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    refs = [os.path.abspath(r) if os.path.exists(r) else job.path(r) for r in (a.ref or [])]
    model = PICTURE_MODELS.get(a.model, a.model)
    job.images.generate(a.prompt, out, model=model, references=refs, aspect_ratio=a.aspect)
    print("picture:", out, "($%.2f)" % job.images.spent() if hasattr(job.images, "spent") else "")


def cmd_view(a):
    """One standard view of the object in a picture, edited from it so it stays the same object."""
    job = Job(a.job)
    src = job.path(a.src)
    out = job.path(a.out or "ref/ref_%s.png" % a.which)
    if keep_existing(out, a.redraw):
        return
    prompt = ("Show this exact same object from %s. Same object, same design, same colours, markings and materials, same "
              "lighting, plain pure white background, sharp focus, nothing else in frame. %s" % (VIEW_TEXT[a.which], a.fixes or "")).strip()
    job.images.generate(prompt, out, model=PICTURE_MODELS.get(a.model, a.model), references=[src], aspect_ratio="1:1")
    if a.mirror:
        ImageOps.mirror(Image.open(out).convert("RGB")).save(out)
    print("view:", out)


def cmd_grid(a):
    job = Job(a.job)
    work = job.path("plan")
    os.makedirs(work, exist_ok=True)
    side_src = job.path(a.side)
    if a.mirror:
        m = job.path("plan", "side_mirrored_src.png")
        ImageOps.mirror(Image.open(side_src).convert("RGB")).save(m)
        side_src = m
    side = os.path.join(work, "side.png")
    side_px = planmod.crop_to_object(side_src, side)
    side_g = planmod.draw_grid(side, os.path.join(work, "side_grid.png"))
    if a.front:
        front = os.path.join(work, "front.png")
        front_px = planmod.crop_to_object(job.path(a.front), front)
        dims = planmod.object_dims(job.spec.size_m, side_px, front_px)
        planmod.draw_grid(front, os.path.join(work, "front_grid.png"))
    else:
        dims = (float(job.spec.size_m), float(a.width or job.spec.size_m * side_px[1] / side_px[0] * 0.3),
                float(job.spec.size_m) * side_px[1] / float(side_px[0]))
    json.dump({"dims_m": [round(v, 4) for v in dims]}, open(os.path.join(work, "dims.json"), "w"))
    print("gridded side view:", side_g)
    if a.front:
        print("gridded front view:", os.path.join(work, "front_grid.png"))
    print("dims L x W x H (m): %.3f x %.3f x %.3f" % dims)
    print("Now write plan.json (see CLAUDE.md) with side_box percents read off the grid, then: ms plan <job> plan.json")


def cmd_plan(a):
    job = Job(a.job)
    dims = json.load(open(job.path("plan", "dims.json")))["dims_m"]
    raw = json.load(open(a.plan if os.path.exists(a.plan) else job.path(a.plan)))
    plan = planmod.validate_plan(raw, dims)
    plan["side"] = job.path("plan", "side.png")
    plan["front"] = job.path("plan", "front.png") if os.path.exists(job.path("plan", "front.png")) else None
    for name, before, after in planmod.snap_to_silhouette(plan):
        print("  %s: thin part, box height read off the picture %.1f%% -> %.1f%%" % (name, before, after))
    for name, was, now in planmod.sample_colours(plan):
        print("  %s: colour read off the picture %s (was %s)" % (name, now, was))
    json.dump(plan, open(job.path("plan", "plan.json"), "w"), indent=1)
    print("plan: %d parts" % len(plan["parts"]))
    for p in plan["parts"]:
        print("  %-22s %-6s %s  %s mm  %s" % (p["name"], p["method"], p["material"]["finish"],
                                              [round((b - x) * 1000) for x, b in zip(p["box_min"], p["box_max"])],
                                              [z["name"] for z in p.get("zones") or []]))
    for d in plan.get("dropped") or []:
        print("  dropped %s: %s" % (d["name"], d["reason"]))


def cmd_build(a):
    """A code part: parts/<Part>/build.py holds `def build(kit, L, W, H)` (the hard-surface kit, see hskit.py); built in
    its box's own frame, exported as <Part>.blend beside its renders. 2026-09-28: the hybrid build."""
    job = Job(a.job)
    plan = _plan(job)
    part = _part(plan, a.part)
    d = job.path("parts", part["name"])
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "build.py")
    if not os.path.exists(src):
        sys.exit("no builder: write %s with `def build(kit, L, W, H)` (see mastersmith/blender/hskit.py)" % src)
    size = [part["box_max"][i] - part["box_min"][i] for i in range(3)]
    args = {"name": part["name"], "code": open(src, encoding="utf-8").read(), "size": size, "material": part["material"],
            "out_dir": d, "render_size": 512}
    _blender(job, "build_part.py", args, "build_%s" % part["name"])
    res = json.load(open(os.path.join(d, part["name"] + ".json")))
    if not res.get("ok"):
        sys.exit("build failed: %s" % res.get("error"))
    print("built: %s  box %s mm  fill %s  %s tris" % (res["blend"], [round(v * 1000) for v in size], res.get("fill"), res.get("triangles")))
    print("renders: " + ", ".join(os.path.join(d, r) for r in res.get("renders", {}).values()))
    print("Look at the renders next to side.png; edit build.py and build again if it is off.")


def cmd_part_pictures(a):
    job = Job(a.job)
    plan = _plan(job)
    part = _part(plan, a.part)
    d = job.path("parts", part["name"])
    os.makedirs(d, exist_ok=True)
    side = os.path.join(d, "side.png")
    model = PICTURE_MODELS.get(a.model, a.model)
    if a.no_side or keep_existing(side, a.redraw):
        print("side picture kept:", side)
    elif a.erased or (is_body(part, plan) and not a.drawn):
        _, erased = erased_body_picture(plan, part, side)
        print("side picture: the approved side view with %s erased -> %s" % (", ".join(erased) or "nothing", side))
    else:
        others = [q["name"] for q in plan["parts"] if q["name"] != part["name"]
                  and all(min(q["box_max"][i], part["box_max"][i]) - max(q["box_min"][i], part["box_min"][i]) > 0 for i in range(3))]
        leave = (" Leave out, they are separate parts: %s." % ", ".join(others)) if others else ""
        job.images.generate("Show ONLY %s from this exact object, whole and complete, exactly as it looks here (same shape, "
                            "colours and materials), seen from exactly the same side angle as this picture with the forward "
                            "end to the right, isolated on a plain pure white background, nothing else in frame, sharp product "
                            "photograph.%s %s" % (part["what"], leave, a.fixes or ""), side, model=model,
                            references=[plan["side"]], aspect_ratio="1:1")
        print("side picture:", side)
    quarter = os.path.join(d, "quarter.png")
    if not a.no_quarter and not keep_existing(quarter, a.redraw):
        # 2026-09-28: the whole-object front view makes the model draw the whole object round a part (8 of 13 carbine
        # parts came back as the whole rifle); --no-front drops it and --fixes reaches this prompt too.
        front = plan.get("front") if not a.no_front else None
        refs = [side] + ([front] if front else [])
        job.images.generate(THREE_QUARTER_PROMPT % ((" Picture 2 shows its front end." if front else "")
                                                    + (" " + a.fixes if a.fixes else "")), quarter,
                            model=model, references=refs, aspect_ratio="4:3")
        print("three-quarter picture:", quarter)
    print("Look at both (Read them). Redraw with --redraw --fixes '...' if the design drifted (ask the owner first).")


def _vendor_payload(vendor, url):
    if vendor.startswith("hitem3d3"):
        return "hitem3d/hi3d/v3.0/image-to-3d", {"image_url": url, "model": "hi3dv3.0", "resolution": "2048quality",
                                                "face_count": 200000, "enable_texture": True, "enable_pbr": True,
                                                "export_format": "glb", "enable_safety_checker": False}
    if vendor == "local":
        return config.LOCAL_SEED_MODEL, {"image_url": url}
    return config.SEED_MODEL, {"image_url": url, "geometry_quality": "detailed", "texture_quality": "detailed", "pbr": True,
                               "face_limit": 150000}


def cmd_mesh(a):
    job = Job(a.job)
    plan = _plan(job)
    part = _part(plan, a.part)
    d = job.path("parts", part["name"])
    pic = os.path.join(d, "quarter.png" if a.src == "quarter" else "side.png")
    if not os.path.exists(pic):
        pic = os.path.join(d, "side.png")
    if not os.path.exists(pic):
        sys.exit("no picture for %s: run part-pictures first" % part["name"])
    if a.vendor != "local":
        os.environ["MASTERSMITH_NO_SPEND"] = "0"
        config.NO_SPEND = False
    model, payload = _vendor_payload(a.vendor, job.fal.upload(pic))
    out = job.fal.run(model, payload)
    mesh_url = first_url(out, (".glb",))
    glb = os.path.join(d, "seed.glb")
    job.fal.download(mesh_url, glb)
    print("mesh: %s (%s, $%.2f)" % (glb, model, job.fal.spent()))
    _do_register(job, part, d, a.src == "quarter" and os.path.exists(os.path.join(d, "quarter.png")), 0, 0)


def _do_register(job, part, d, sweep, yaw, pitch):
    side = os.path.join(d, "side.png")
    reg = _register(job, part["name"], os.path.join(d, "seed.glb"), side, d, yaw_sweep=sweep, extra_yaw=yaw, extra_pitch=pitch)
    if not reg:
        print("registration failed (silhouette too different); try --from side or a manual --yaw/--pitch")
        return
    meta = {"keep_depth": bool(sweep)}                       # _register wrote registered.blend into the part folder
    json.dump(meta, open(os.path.join(d, "fit.json"), "w"))
    print("registered: mode %s, silhouette overlap %.2f (runner-up %.2f); render: %s" % (
        reg["mode"], reg["iou"], reg["runner_up_iou"], reg.get("render")))
    print("Look at seed_render.png next to side.png; re-run register with --yaw/--pitch if it sits wrong.")


def cmd_register(a):
    job = Job(a.job)
    part = _part(_plan(job), a.part)
    d = job.path("parts", part["name"])
    _do_register(job, part, d, a.src == "quarter", a.yaw, a.pitch)


# ---------------------------------------------------------------- sculpting: fit (#11), sdf (#12), brush (#13)
def _mask_npz(picture, out):
    """The picture's object mask (anything unlike the border), cropped tight, as a signed distance field in pixels
    (+ outside) with its gradient, for blender/fit_part.py. -> path or None when the picture is empty"""
    import numpy as np
    from scipy import ndimage
    a = np.asarray(Image.open(picture).convert("RGB")).astype(np.float32) / 255.0
    border = np.concatenate([a[:6].reshape(-1, 3), a[-6:].reshape(-1, 3), a[:, :6].reshape(-1, 3), a[:, -6:].reshape(-1, 3)])
    fg = np.abs(a - np.median(border, axis=0)).max(axis=2) > 0.1
    if fg.mean() < 0.01:
        return None
    fg = ndimage.binary_fill_holes(fg)
    ys, xs = np.nonzero(fg)
    fg = np.pad(fg[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 1, constant_values=False)   # the crop's edge is outside too
    sdf = (ndimage.distance_transform_edt(~fg) - ndimage.distance_transform_edt(fg))[1:-1, 1:-1]
    gy, gx = np.gradient(sdf)
    np.savez(out, sdf=sdf.astype(np.float32), gx=gx.astype(np.float32), gy=gy.astype(np.float32))
    return out


def cmd_fit(a):
    """Deform the registered seed until its outline lies on the part's side picture (and, with --quarter, its
    three-quarter picture at the assumed camera): the sculpting an artist does by eye, done by the picture."""
    job = Job(a.job)
    part = _part(_plan(job), a.part)
    d = job.path("parts", part["name"])
    blend = os.path.join(d, "registered.blend")
    if not os.path.exists(blend):
        sys.exit("no registered mesh for %s: mesh it first" % part["name"])
    views = []
    side = _mask_npz(os.path.join(d, "side.png"), os.path.join(d, "fit_side.npz"))
    if side:
        views.append({"name": "side", "npz": side, "yaw": 0.0, "pitch": 0.0, "weight": 1.0})
    if a.quarter and os.path.exists(os.path.join(d, "quarter.png")):
        q = _mask_npz(os.path.join(d, "quarter.png"), os.path.join(d, "fit_quarter.npz"))
        if q:
            views.append({"name": "quarter", "npz": q, "yaw": 35.0, "pitch": 20.0, "weight": 0.5})
    if not views:
        sys.exit("no picture with an object in it to fit to")
    if not a.no_backup and not os.path.exists(os.path.join(d, "registered_unfitted.blend")):
        shutil.copy2(blend, os.path.join(d, "registered_unfitted.blend"))
    out_json = os.path.join(d, "fit_report.json")
    box_size = [part["box_max"][i] - part["box_min"][i] for i in range(3)]
    _blender(job, "fit_part.py", {"blend": blend, "views": views, "iters": a.iters, "step": a.step, "out_blend": blend, "box_size": box_size,
                                  "out_render": os.path.join(d, "seed_render.png"), "out_json": out_json},
             "fit_%s" % part["name"], timeout=1800)
    rep = json.load(open(out_json))
    for name in rep["iou_before"]:
        print("  %s silhouette overlap %.3f -> %.3f" % (name, rep["iou_before"][name], rep["iou_after"][name]))
    print("fitted %s: moved up to %.1f mm (mean %.2f); render: %s" % (part["name"], rep["max_move_mm"], rep["mean_move_mm"], os.path.join(d, "seed_render.png")))
    print("Look at seed_render.png; registered_unfitted.blend is the mesh before (copy it back to undo).")


ANCHORS = {"centre": (0, 0, 0), "center": (0, 0, 0), "front": (0.5, 0, 0), "back": (-0.5, 0, 0), "top": (0, 0, 0.5),
           "bottom": (0, 0, -0.5), "left": (0, 0.5, 0), "right": (0, -0.5, 0)}


def _point(text, size):
    """x,y,z in metres in the part's frame, or an anchor name (front/back/top/bottom/left/right/centre, at the
    part's box faces), or anchor+dx,dy,dz."""
    base, delta = text, (0.0, 0.0, 0.0)
    if "+" in text:
        base, rest = text.split("+", 1)
        delta = tuple(float(v) for v in rest.split(","))
    if base in ANCHORS:
        p = [ANCHORS[base][i] * size[i] for i in range(3)]
    else:
        p = [float(v) for v in base.split(",")]
    return [p[i] + delta[i] for i in range(3)]


def cmd_brush(a):
    """One brush stroke on the registered mesh, headless: the fix the agent can name after looking at the views.
    Strokes are logged in brush_log.json and replay with --replay after a re-mesh."""
    job = Job(a.job)
    part = _part(_plan(job), a.part)
    d = job.path("parts", part["name"])
    blend = os.path.join(d, "registered.blend")
    if not os.path.exists(blend):
        sys.exit("no registered mesh for %s: mesh it first" % part["name"])
    log_path = os.path.join(d, "brush_log.json")
    log = json.load(open(log_path)) if os.path.exists(log_path) else []
    imported = json.load(open(os.path.join(d, "import.json"))) if os.path.exists(os.path.join(d, "import.json")) else None
    size = (imported or {}).get("size_m") or [part["box_max"][i] - part["box_min"][i] for i in range(3)]
    if a.replay:
        strokes = log
        if not strokes:
            sys.exit("nothing to replay")
    else:
        if not a.op:
            sys.exit("--op is required (one of inflate, move, smooth, flatten, crease)")
        op = {"op": a.op, "at": _point(a.at, size), "radius": a.radius / 1000.0, "strength": a.strength}
        if a.op == "inflate":
            op["strength"] = a.strength / 1000.0                  # mm at the centre
        if a.op == "move":
            op["delta"] = [float(v) / 1000.0 for v in (a.delta or "0,0,0").split(",")]
        if a.op == "crease":
            op["to"] = _point(a.to or a.at, size)
        if a.op == "flatten" and a.normal:
            op["normal"] = [float(v) for v in a.normal.split(",")]
        strokes = [op]
    if not os.path.exists(os.path.join(d, "registered_unbrushed.blend")):
        shutil.copy2(blend, os.path.join(d, "registered_unbrushed.blend"))
    res_path = os.path.join(d, "brush_report.json")
    box_size = [part["box_max"][i] - part["box_min"][i] for i in range(3)]
    _blender(job, "brush.py", {"blend": blend, "strokes": strokes, "out_blend": blend, "box_size": box_size,
                               "out_render": os.path.join(d, "seed_render.png"), "out_json": res_path}, "brush_%s" % part["name"])
    rep = json.load(open(res_path))
    if not a.replay:
        log.extend(strokes)
        json.dump(log, open(log_path, "w"), indent=1)
    print("brushed %s: %d stroke(s) moved %d of %d vertices, up to %.2f mm; render: %s" % (
        part["name"], rep["strokes"], rep["vertices_moved"], rep["vertices"], rep["max_move_mm"], os.path.join(d, "seed_render.png")))
    print("Log: %s (%d strokes). registered_unbrushed.blend is the mesh before the first stroke." % (log_path, len(log)))


def cmd_sdf(a):
    """An exact part from a signed distance function: parts/<Part>/sdf.py defines `part(kit, L, W, H)` (see
    mastersmith/sdfkit.py); meshed inside the part's box, imported as registered.blend with the planned material."""
    from . import sdfkit
    job = Job(a.job)
    plan = _plan(job)
    part = _part(plan, a.part)
    d = job.path("parts", part["name"])
    os.makedirs(d, exist_ok=True)
    src = a.script if a.script and os.path.exists(a.script) else os.path.join(d, "sdf.py")
    if not os.path.exists(src):
        sys.exit("no script: write %s with `def part(kit, L, W, H)` returning a kit shape (see mastersmith/sdfkit.py)" % src)
    L, W, H = [part["box_max"][i] - part["box_min"][i] for i in range(3)]
    shape = sdfkit.run_part_script(open(src, encoding="utf-8").read(), L, W, H)
    voxel = (a.voxel / 1000.0) if a.voxel else min(max(max(L, W, H) / 320.0, 0.00015), 0.001)
    half = [L / 2, W / 2, H / 2]
    verts, faces = sdfkit.mesh(shape, [-h for h in half], half, voxel)
    glb = os.path.join(d, "seed.glb")
    sdfkit.write_glb(verts, faces, glb)
    print("sdf mesh: %d tris at %.2f mm voxels -> %s" % (len(faces), voxel * 1000, glb))
    _blender(job, "import_part.py", {"glb": glb, "material": part["material"], "name": part["name"],
                                     "out_blend": os.path.join(d, "registered.blend"),
                                     "out_render": os.path.join(d, "seed_render.png"), "out_json": os.path.join(d, "import.json")},
             "import_%s" % part["name"])
    rep = json.load(open(os.path.join(d, "import.json")))
    json.dump({"keep_depth": False}, open(os.path.join(d, "fit.json"), "w"))
    if os.path.abspath(src) != os.path.abspath(os.path.join(d, "sdf.py")):
        shutil.copy2(src, os.path.join(d, "sdf.py"))
    for p in plan["parts"]:
        if p["name"] == part["name"]:
            p["method"] = "sdf"
    plan_out = {k: v for k, v in plan.items() if k not in ("side", "front")}
    json.dump(plan_out, open(job.path("plan", "plan.json"), "w"), indent=1)
    print("imported: %d tris, %s mm (box %s mm); render: %s" % (rep["triangles"], [round(v * 1000, 1) for v in rep["size_m"]],
                                                                [round(v * 1000) for v in (L, W, H)], os.path.join(d, "seed_render.png")))
    print("Look at seed_render.png next to side.png; edit sdf.py and run again if it is off.")


def cmd_assemble(a):
    job = Job(a.job)
    plan = _plan(job)
    want = [p.strip().lower() for p in a.parts.split(",")] if a.parts else None
    largest = max(plan["parts"], key=lambda q: q["box_max"][0] - q["box_min"][0])
    parts = []
    for p in plan["parts"]:
        if want and p["name"].lower() not in want:
            continue
        d = job.path("parts", p["name"])
        if p.get("method") == "code":
            blend = os.path.join(d, p["name"] + ".blend")
            if not os.path.exists(blend):
                print("  %s: not built yet (ms build), left out" % p["name"])
                continue
            parts.append({"name": p["name"], "kind": "code", "box_min": p["box_min"], "box_max": p["box_max"], "material": p["material"],
                          "centreline": bool(p.get("centreline")), "zones": [], "blend": blend, "yaw": 0,
                          "reference_detail": bool(p.get("reference_detail", True))})
            continue
        blend = os.path.join(d, "registered.blend")
        if not os.path.exists(blend):
            print("  %s: not meshed yet, left out" % p["name"])
            continue
        fit = json.load(open(os.path.join(d, "fit.json"))) if os.path.exists(os.path.join(d, "fit.json")) else {}
        is_largest = p["name"] == largest["name"]
        parts.append({"name": p["name"], "kind": "vendor", "box_min": p["box_min"], "box_max": p["box_max"], "material": p["material"],
                      "centreline": bool(p.get("centreline")), "zones": p.get("zones") or [], "blend": blend, "yaw": 0,
                      "keep_depth": bool(fit.get("keep_depth")) and is_largest, "fill_box": not is_largest})
    if not parts:
        sys.exit("nothing to assemble")
    delivery = job.path("delivery")
    os.makedirs(delivery, exist_ok=True)
    det = {"side": detail_map(plan["side"], job.path("plan", "detail_side.png")), "front": None, "dims": plan["dims_m"], "strength": 0.5}
    if plan.get("front"):
        det["front"] = detail_map(plan["front"], job.path("plan", "detail_front.png"))
    ref = job.path("ref", "ref_0.png")
    args = {"name": job.spec.name, "out_dir": delivery, "tri_budget": job.spec.tri_budget or 100000, "engine": job.spec.engine,
            "atlas_size": 4096 if (job.spec.tri_budget or 0) >= 100000 else 2048, "render_size": 768, "spec": job.spec.to_dict(),
            "reference": ref if os.path.exists(ref) else None, "parts": parts, "detail": det, "sharpen": not a.no_sharpen}
    _blender(job, "assemble.py", args, "assemble")
    rep = json.load(open(os.path.join(delivery, "report.json")))
    sheet = six_view_sheet(job, delivery)
    print("assembled %d parts -> %s" % (len(parts), delivery))
    print("  size %s m, LOD0 %s tris" % (rep.get("dimensions_m"), (rep.get("lods") or [{}])[0].get("triangles")))
    print("  previews: " + ", ".join(os.path.join(delivery, r) for r in rep["renders"]))
    print("  six views: %s" % sheet)
    print("  page: %s  (ms preview %s opens it)" % (write_preview(job), a.job))
    print("Now LOOK at the six views and the previews (Read them) before calling it good.")


PREVIEW_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>%(name)s</title>
<script type="module" src="https://cdn.jsdelivr.net/npm/@google/model-viewer@3.5.0/dist/model-viewer.min.js"></script>
<style>
 body{margin:0;font:14px/1.4 system-ui,sans-serif;background:#1c1d20;color:#ddd}
 h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:24px 0 8px;color:#9ab} small{color:#999}
 header,section{padding:16px 24px} header{background:#26272b;border-bottom:1px solid #333}
 model-viewer{width:100%%;height:70vh;background:#3a3b40;border-radius:6px}
 .row{display:flex;flex-wrap:wrap;gap:12px} .row img{max-width:360px;background:#fff;border-radius:4px}
 .sheet{width:100%%;max-width:1536px}
 table{border-collapse:collapse;width:100%%} td,th{padding:6px 8px;border-bottom:1px solid #333;vertical-align:top;text-align:left}
 td img{height:150px;background:#fff;border-radius:4px;margin-right:4px} .lod button{margin-right:6px}
</style></head><body>
<header><h1>%(name)s</h1><small>%(desc)s</small><br><small>%(dims)s m &middot; LOD0 %(tris)s tris &middot; %(nparts)d parts &middot; %(engine)s</small></header>
<section>
<model-viewer id="mv" src="%(glb)s" camera-controls camera-orbit="-35deg 78deg 110%%" exposure="1.1" shadow-intensity="0.6" environment-image="neutral" alt="%(name)s"></model-viewer>
<div class="lod" style="margin-top:8px">%(lods)s <button onclick="mv.autoRotate=!mv.autoRotate">rotate</button></div>
</section>
<section><h2>Six views</h2>%(sheet)s</section>
<section><h2>Previews</h2><div class="row">%(previews)s</div></section>
<section><h2>Reference and plan</h2><div class="row">%(refs)s</div></section>
<section><h2>Parts: picture the mesher got, side picture it was registered to, the seed as registered</h2>
<table><tr><th>part</th><th>pictures</th><th>seed</th><th>registration</th></tr>%(parts)s</table></section>
<script>const mv=document.getElementById('mv');function lod(f){mv.src=f}</script>
</body></html>"""


def write_preview(job):
    """delivery/preview.html: the GLB in a <model-viewer>, the six views, the previews, the reference pictures and
    every part's pictures beside its registered seed. Relative links, so the page needs the job folder served
    (`ms preview`): a browser will not fetch a GLB from file://."""
    delivery = job.path("delivery")
    rep = json.load(open(os.path.join(delivery, "report.json")))
    name = job.spec.name
    lods = [g for g in sorted(glob.glob(os.path.join(delivery, "SM_%s*.glb" % name)))]
    lod_buttons = "".join("<button onclick=\"lod('%s')\">%s</button>" % (os.path.basename(g), os.path.basename(g)[len("SM_%s" % name):-4].strip("_") or "LOD0")
                          for g in lods)
    rel = lambda path: os.path.relpath(path, delivery).replace(os.sep, "/")
    imgs = lambda paths: "".join('<a href="%s"><img src="%s" title="%s"></a>' % (rel(p), rel(p), os.path.basename(p)) for p in paths if os.path.exists(p))
    previews = [os.path.join(delivery, r) for r in (rep.get("renders") or []) + (rep.get("detail_renders") or [])]
    refs = sorted(glob.glob(job.path("ref", "*.png"))) + [job.path("plan", "side_grid.png"), job.path("plan", "front_grid.png")]
    rows = []
    plan = _plan(job) if os.path.exists(job.path("plan", "plan.json")) else {"parts": []}
    for p in plan["parts"]:
        d = job.path("parts", p["name"])
        reg = json.load(open(os.path.join(d, "registration.json"))) if os.path.exists(os.path.join(d, "registration.json")) else {}
        mm = [round((b - x) * 1000) for x, b in zip(p["box_min"], p["box_max"])]
        rows.append("<tr><td><b>%s</b><br><small>%s<br>%s mm, %s %s</small></td><td>%s</td><td>%s</td><td><small>%s</small></td></tr>" % (
            p["name"], p["what"][:160], mm, p["material"]["finish"], p["material"]["color"],
            imgs([os.path.join(d, "quarter.png"), os.path.join(d, "side.png")]), imgs([os.path.join(d, "seed_render.png")]),
            ("%s, IoU %.2f" % (reg.get("mode"), reg.get("iou", 0))) if reg else "not meshed"))
    sheet = os.path.join(delivery, "preview_views.png")
    html = PREVIEW_HTML % {
        "name": name, "desc": job.spec.description, "dims": " x ".join("%.3f" % v for v in rep.get("dimensions_m") or []),
        "tris": format((rep.get("lods") or [{}])[0].get("triangles", 0), ","), "nparts": len(plan["parts"]), "engine": job.spec.engine,
        "glb": os.path.basename(lods[0]) if lods else "", "lods": lod_buttons,
        "sheet": '<a href="preview_views.png"><img class="sheet" src="preview_views.png"></a>' if os.path.exists(sheet) else "<small>not rendered</small>",
        "previews": imgs(previews), "refs": imgs(refs), "parts": "".join(rows)}
    out = os.path.join(delivery, "preview.html")
    open(out, "w", encoding="utf-8").write(html)
    return out


def cmd_preview(a):
    job = Job(a.job)
    if not os.path.exists(job.path("delivery", "report.json")):
        sys.exit("nothing assembled yet: ms assemble %s first" % a.job)
    page = write_preview(job)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # a small static server on the job folder, left running in the background; the page links across ref/, plan/, parts/
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", job.dir],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    url = "http://127.0.0.1:%d/delivery/preview.html" % port
    print("preview: %s  (%s)" % (url, page))
    if not a.no_open:
        webbrowser.open(url)


def cmd_sheet(a):
    class J:
        pass
    j = J()
    j.work_dir = os.path.dirname(os.path.abspath(a.glb))
    j.log = print
    folder = j.work_dir
    tmp = os.path.join(folder, "_sheet_src")
    os.makedirs(tmp, exist_ok=True)
    shutil.copy2(a.glb, os.path.join(tmp, "SM_sheet.glb"))
    sheet = six_view_sheet(j, tmp)
    out = a.out or os.path.splitext(a.glb)[0] + "_views.png"
    shutil.move(os.path.join(tmp, "preview_views.png"), out)
    shutil.rmtree(tmp, ignore_errors=True)
    print("six views:", out)


def cmd_package(a):
    job = Job(a.job)
    rep = json.load(open(job.path("delivery", "report.json")))
    review = json.load(open(job.path("delivery", "review.json"))) if os.path.exists(job.path("delivery", "review.json")) else None
    print(write_package(job.spec, rep, {"review": review, "gate": {"ok": True}, "rig": {}}, job.path("delivery")))


def cmd_status(a):
    job = Job(a.job)
    print("brief:", job.spec.name, job.spec.category, "%.3f m" % job.spec.size_m)
    print("ref:", sorted(os.listdir(job.path("ref"))) if os.path.isdir(job.path("ref")) else "none")
    have = [f for f in existing_pictures(job.dir) if f.startswith("parts")]
    if have:
        print("part pictures/builders on disk (%d): reuse them; ask the owner before drawing any again" % len(have))
    if os.path.exists(job.path("plan", "plan.json")):
        plan = _plan(job)
        for p in plan["parts"]:
            d = job.path("parts", p["name"])
            have = [f for f in ("side.png", "quarter.png", "seed.glb", "registered.blend") if os.path.exists(os.path.join(d, f))]
            reg = json.load(open(os.path.join(d, "registration.json"))) if os.path.exists(os.path.join(d, "registration.json")) else {}
            print("  %-22s %s %s" % (p["name"], have, ("iou %.2f" % reg["iou"]) if reg else ""))
    else:
        print("plan: none yet (ms grid, then write plan.json, then ms plan)")
    print("delivery:", "yes" if os.path.exists(job.path("delivery", "report.json")) else "none")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ms", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new"); s.add_argument("name"); s.add_argument("--category", default="prop"); s.add_argument("--size", type=float, required=True)
    s.add_argument("--rebrief", action="store_true", help="rewrite brief.json of an existing job (its pictures are kept)")
    s.add_argument("--description", required=True); s.add_argument("--style", default="realistic"); s.add_argument("--engine", default="unreal")
    s.add_argument("--tris", type=int, default=0); s.set_defaults(fn=cmd_new)
    s = sub.add_parser("picture"); s.add_argument("job"); s.add_argument("--out", required=True); s.add_argument("--prompt", required=True)
    s.add_argument("--ref", action="append"); s.add_argument("--model", default="nano"); s.add_argument("--aspect", default="4:3")
    s.add_argument("--redraw", action="store_true", help="draw over an existing picture (ask the owner first)"); s.set_defaults(fn=cmd_picture)
    s = sub.add_parser("view"); s.add_argument("job"); s.add_argument("--which", choices=sorted(VIEW_TEXT), required=True)
    s.add_argument("--from", dest="src", required=True); s.add_argument("--out"); s.add_argument("--fixes"); s.add_argument("--mirror", action="store_true")
    s.add_argument("--model", default="nano"); s.add_argument("--redraw", action="store_true"); s.set_defaults(fn=cmd_view)
    s = sub.add_parser("grid"); s.add_argument("job"); s.add_argument("--side", required=True); s.add_argument("--front")
    s.add_argument("--mirror", action="store_true", help="the side picture has the forward end on the left"); s.add_argument("--width", type=float); s.set_defaults(fn=cmd_grid)
    s = sub.add_parser("plan"); s.add_argument("job"); s.add_argument("plan"); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("build"); s.add_argument("job"); s.add_argument("part"); s.set_defaults(fn=cmd_build)
    s = sub.add_parser("part-pictures"); s.add_argument("job"); s.add_argument("part"); s.add_argument("--fixes"); s.add_argument("--erased", action="store_true")
    s.add_argument("--drawn", action="store_true", help="draw the body alone instead of erasing the approved picture")
    s.add_argument("--no-quarter", action="store_true"); s.add_argument("--no-side", action="store_true", help="keep the side picture")
    s.add_argument("--no-front", action="store_true", help="three-quarter picture without the front-view reference")
    s.add_argument("--model", default="nano"); s.add_argument("--redraw", action="store_true", help="draw over existing pictures (ask the owner first)")
    s.set_defaults(fn=cmd_part_pictures)
    s = sub.add_parser("mesh"); s.add_argument("job"); s.add_argument("part"); s.add_argument("--vendor", default="local")
    s.add_argument("--from", dest="src", default="quarter", choices=("quarter", "side")); s.set_defaults(fn=cmd_mesh)
    s = sub.add_parser("register"); s.add_argument("job"); s.add_argument("part"); s.add_argument("--from", dest="src", default="quarter", choices=("quarter", "side"))
    s.add_argument("--yaw", type=float, default=0.0); s.add_argument("--pitch", type=float, default=0.0); s.set_defaults(fn=cmd_register)
    s = sub.add_parser("fit"); s.add_argument("job"); s.add_argument("part"); s.add_argument("--quarter", action="store_true")
    s.add_argument("--iters", type=int, default=8); s.add_argument("--step", type=float, default=0.6); s.add_argument("--no-backup", action="store_true"); s.set_defaults(fn=cmd_fit)
    s = sub.add_parser("brush"); s.add_argument("job"); s.add_argument("part"); s.add_argument("--op", choices=("inflate", "move", "smooth", "flatten", "crease"))
    s.add_argument("--at", default="centre", help="x,y,z metres in the part frame, or front/back/top/bottom/left/right/centre[+dx,dy,dz]")
    s.add_argument("--radius", type=float, default=10.0, help="mm"); s.add_argument("--strength", type=float, default=1.0, help="inflate: mm at the centre; others: 0-1")
    s.add_argument("--delta", help="move: dx,dy,dz in mm"); s.add_argument("--to", help="crease: the line's other end"); s.add_argument("--normal", help="flatten: nx,ny,nz")
    s.add_argument("--replay", action="store_true", help="re-apply brush_log.json to a fresh mesh"); s.set_defaults(fn=cmd_brush)
    s = sub.add_parser("sdf"); s.add_argument("job"); s.add_argument("part"); s.add_argument("script", nargs="?"); s.add_argument("--voxel", type=float, help="mm")
    s.set_defaults(fn=cmd_sdf)
    s = sub.add_parser("assemble"); s.add_argument("job"); s.add_argument("--parts"); s.add_argument("--no-sharpen", action="store_true"); s.set_defaults(fn=cmd_assemble)
    s = sub.add_parser("sheet"); s.add_argument("glb"); s.add_argument("--out"); s.set_defaults(fn=cmd_sheet)
    s = sub.add_parser("preview"); s.add_argument("job"); s.add_argument("--no-open", action="store_true"); s.set_defaults(fn=cmd_preview)
    s = sub.add_parser("package"); s.add_argument("job"); s.set_defaults(fn=cmd_package)
    s = sub.add_parser("status"); s.add_argument("job"); s.set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
