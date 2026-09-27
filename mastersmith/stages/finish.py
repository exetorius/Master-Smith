"""Stage 3: headless Blender in two passes around a Python decision step.
    prepare.py  -> work.blend + probe renders (known cameras)
    probe.run   -> which end is the front (vision), glass / wheel masks (SAM 3)
    finish.py   -> yaw, glass slot, maps, LODs, collision, renders, FBX/GLB
Deterministic where it can be; the only judgement calls are the two vision answers."""
import json
import os
import subprocess
import shutil

from .. import config
from .tiles import make_tiles
from .probe import run_probe

BLENDER_DIR = config.ROOT / "mastersmith" / "blender"


def _blender(job, script, args, tag, timeout=2400):
    if not os.path.exists(config.BLENDER_BIN):
        raise RuntimeError("Blender not found at %s (set BLENDER_BIN)" % config.BLENDER_BIN)
    args_path = os.path.join(job.work_dir, "%s_args.json" % tag)
    with open(args_path, "w") as f:
        json.dump(args, f, indent=1)
    cmd = [config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(BLENDER_DIR / script), "--", args_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Blender %s ran past %d s and was stopped" % (script, timeout))
    log_path = os.path.join(job.work_dir, "%s.log" % tag)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout or "")
        f.write("\n--- stderr ---\n")
        f.write(proc.stderr or "")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-12:])
        err = "\n".join((proc.stderr or "").splitlines()[-6:])
        raise RuntimeError("Blender %s failed (exit %s); see %s\n%s\n%s" % (script, proc.returncode, log_path, tail, err))


def run_finish(job, skill, seed_glb, reference=None):
    """Package one seed mesh: orient, scale, glass slot, maps, LODs, collision, renders, exports. Nothing here repairs
    the mesh; a wrong shape, part or colour is a new build (2026-09-26)."""
    spec = job.spec
    common = {"name": spec.name, "work_dir": job.work_dir, "out_dir": os.path.join(job.dir, "delivery"),
              "tri_budget": spec.tri_budget, "size_m": spec.size_m, "engine": spec.engine,
              "forward_axis": skill["meta"].get("forward_axis", "long"), "origin": skill["meta"].get("origin", "bottom"),
              "spec": spec.portable(), "reference": reference}
    job.log("  Blender pass 1: orient, scale to %.2f m, probe renders" % spec.size_m)
    _blender(job, "prepare.py", {**common, "glb": seed_glb, "probe_size": 896}, "prepare")
    job.log("  deciding: facing%s%s" % (", glass" if spec.glass and skill["meta"].get("glass_prompt") else "",
                                        ", wheels" if spec.rig and skill["meta"].get("rig_parts_prompt") else ""))
    decision = run_probe(job, skill)
    job.log("  Blender pass 2: glass slot, maps, LODs to %s tris, collision, export" % format(spec.tri_budget, ","))
    _blender(job, "finish.py", {**common, "render_size": 768, "bake_detail": True}, "finish")
    report_path = os.path.join(common["out_dir"], "report.json")
    if not os.path.exists(report_path):
        log_path = os.path.join(job.work_dir, "finish.log")
        tail = ""
        if os.path.exists(log_path):
            lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
            tail = "\n".join(l for l in lines[-25:] if l.strip())
        raise RuntimeError("Blender finish wrote no report (exit 0). Log tail:\n%s" % tail)
    with open(report_path) as f:
        report = json.load(f)
    report["decision"] = decision
    source_preview = os.path.join(job.work_dir, "probe_iso.png")
    if os.path.exists(source_preview):
        shutil.copy2(source_preview, os.path.join(common["out_dir"], "preview_seed_iso.png"))
        report["source_renders"] = ["preview_seed_iso.png"]
        with open(report_path, "w") as f:
            json.dump(report, f, indent=1)
    if spec.category == "environment" and spec.style == "realistic" and reference and os.path.exists(reference):
        try:
            job.log("  tiling PBR material from the reference (Patina)")
            tiles = make_tiles(job, reference, common["out_dir"])
            if tiles:
                report["tiles"] = [os.path.basename(t) for t in tiles]
        except Exception as exc:  # noqa: BLE001 - the asset ships without tiles
            job.log("  tiles skipped: %s" % str(exc)[:160])
    for note in report.get("notes", []):
        if any(w in note for w in ("dropped", "WARNING", "canopy", "glass:", "baked", "bake ")):
            job.log("  blender: %s" % note[:220])
    job.log("  finished: %s tris LOD0, %d maps, %d files%s" % (
        format(report["lods"][0]["triangles"], ","), len(report["maps"]), len(report["files"]),
        (", glass %d faces" % report["glass"]["faces"]) if report.get("glass") else ""))
    return report
