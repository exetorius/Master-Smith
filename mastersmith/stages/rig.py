"""Stage 3b: rigging.
  characters -> Meshy auto-rig (humanoid skeleton, walk/run clips) on the finished LOD0 GLB
  vehicles   -> Hunyuan part split of the LOD2 FBX, then wheel bones in Blender (rig_vehicle.py)
  everything else -> reported as not applicable"""
import json
import os
import subprocess

from .. import config
from ..fal import first_url

BLENDER_DIR = config.ROOT / "mastersmith" / "blender"


def _download_named(job, url, name):
    path = os.path.join(job.dir, "delivery", name)
    job.fal.download(url, path)
    return os.path.basename(path)


def rig_character(job, report):
    delivery = os.path.join(job.dir, "delivery")
    glb = os.path.join(delivery, "SM_%s.glb" % job.spec.name)
    url = job.fal.upload(glb)
    job.log("  Meshy auto-rig, %.2f m tall" % job.spec.size_m)
    out = job.fal.run("fal-ai/meshy/rigging", {"model_url": url, "height_meters": float(job.spec.size_m),
                                                "enable_animation": False})
    files = []
    for key, name in (("rigged_character_fbx", "SK_%s.fbx"), ("rigged_character_glb", "SK_%s.glb")):
        u = (out.get(key) or {}).get("url") if isinstance(out.get(key), dict) else out.get(key)
        if u:
            files.append(_download_named(job, u, name % job.spec.name))
    anims = out.get("basic_animations") or {}
    if isinstance(anims, dict):
        for k, v in anims.items():
            u = v.get("url") if isinstance(v, dict) else v
            if isinstance(u, str):
                ext = os.path.splitext(u.split("?")[0])[1] or ".fbx"
                files.append(_download_named(job, u, "A_%s_%s%s" % (job.spec.name, k.replace(" ", "_"), ext)))
    elif isinstance(anims, list):
        for i, v in enumerate(anims):
            u = v.get("url") if isinstance(v, dict) else v
            if isinstance(u, str):
                ext = os.path.splitext(u.split("?")[0])[1] or ".fbx"
                files.append(_download_named(job, u, "A_%s_%d%s" % (job.spec.name, i, ext)))
    cleaned = None
    bones, clips = [], []
    if "SK_%s.glb" % job.spec.name in files:
        # Make the vendor rig ours: drop scraps, metres, root bone, UE5 Mannequin names for Unreal, and the
        # walk/run clips re-exported through the same renaming so they match the skeleton.
        anim_glbs = [os.path.join(delivery, f) for f in files if f.startswith("A_%s_" % job.spec.name) and f.endswith(".glb")
                     and "armature" not in f]
        args_path = os.path.join(job.work_dir, "clean_rig_args.json")
        json.dump({"glb": os.path.join(delivery, "SK_%s.glb" % job.spec.name), "name": job.spec.name, "out_dir": delivery,
                   "skeleton": "ue5" if job.spec.engine == "unreal" else "vendor", "animations": anim_glbs}, open(args_path, "w"))
        proc = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(BLENDER_DIR / "clean_rig.py"), "--", args_path],
                              capture_output=True, text=True, timeout=1200)
        with open(os.path.join(job.work_dir, "clean_rig.log"), "w", encoding="utf-8") as f:
            f.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
        rig_json = os.path.join(delivery, "rig.json")
        if proc.returncode == 0 and os.path.exists(rig_json):
            info = json.load(open(rig_json))
            bones, clips = info.get("bones", []), info.get("clips", [])
            cleaned = "%d bones (%s), %d clip(s)" % (len(bones), info.get("skeleton"), len(clips))
            # the vendor's own clip files are superseded by our re-exports
            for f in list(files):
                if f.startswith("A_%s_" % job.spec.name) and (f.endswith(".glb") or f.endswith("_fbx.fbx")):
                    try:
                        os.remove(os.path.join(delivery, f))
                    except OSError:
                        pass
                    files.remove(f)
            files = [f for f in files if not f.startswith("A_")] + [c["file"] for c in clips]
        else:
            cleaned = "failed (kept the vendor files)"
        job.log("  rig cleanup: %s" % cleaned)
    job.log("  rigged: %s" % ", ".join(files))
    return {"status": "rigged" if files else "vendor returned no files", "vendor": "fal-ai/meshy/rigging",
            "files": files, "rig_task_id": out.get("rig_task_id"), "cleanup": cleaned, "bones": bones, "clips": clips}


def rig_vehicle(job, report):
    delivery = os.path.join(job.dir, "delivery")
    lod2 = os.path.join(delivery, "SM_%s_LOD2.fbx" % job.spec.name)
    tris = report["lods"][2]["triangles"]
    if tris > 30000:
        return {"status": "skipped", "reason": "LOD2 has %d triangles; the splitter takes 30k at most" % tris}
    url = job.fal.upload(lod2, mime="application/octet-stream")
    job.log("  part split (Hunyuan) on LOD2, %s tris" % format(tris, ","))
    out = job.fal.run("fal-ai/hunyuan-3d/v3.1/part", {"input_file_url": url})
    parts_dir = os.path.join(job.work_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    n = 0
    for i, f in enumerate(out.get("result_files") or []):
        u = f.get("url") if isinstance(f, dict) else f
        if u:
            job.fal.download(u, os.path.join(parts_dir, "part_%d.glb" % i))
            n += 1
    job.log("  %d parts returned" % n)
    if n < 2:
        return {"status": "skipped", "reason": "splitter returned %d part(s)" % n}
    args = {"name": job.spec.name, "out_dir": delivery, "parts_dir": parts_dir}
    args_path = os.path.join(job.work_dir, "rig_args.json")
    json.dump(args, open(args_path, "w"))
    proc = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(BLENDER_DIR / "rig_vehicle.py"), "--", args_path],
                          capture_output=True, text=True, timeout=1800)
    with open(os.path.join(job.work_dir, "rig.log"), "w", encoding="utf-8") as f:
        f.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    rig_path = os.path.join(delivery, "rig.json")
    if proc.returncode != 0 or not os.path.exists(rig_path):
        return {"status": "failed", "reason": "rig_vehicle.py exit %s: %s" % (proc.returncode, (proc.stderr or "")[-400:])}
    rig = json.load(open(rig_path))
    job.log("  rig: %s, bones %s" % (rig.get("status"), rig.get("bones")))
    return rig


def rig_asset(job, skill, report):
    cat = job.spec.category
    try:
        if cat == "character":
            return rig_character(job, report)
        if cat == "vehicle":
            return rig_vehicle(job, report)
        if cat == "weapon" and report.get("skeleton"):
            return {"status": "rigged", "vendor": "mastersmith", "bones": report["skeleton"]["bones"],
                    "files": ["SK_%s.fbx" % job.spec.name], "sockets": report.get("sockets", [])}
        return {"status": "not_applicable", "reason": "%s assets ship static; animate in the engine" % cat}
    except Exception as exc:  # noqa: BLE001 - a failed rig must not lose the static delivery
        job.log("  rig failed: %s" % str(exc)[:200])
        reason = str(exc)[:400]
        if "Could not rig" in reason or "Pose estimation" in reason:
            reason = ("The auto-rigger needs an upright humanoid figure in an A- or T-pose and could not read this one as "
                      "such (animals, crouched or hunched poses are not supported yet). The static mesh is delivered unrigged; "
                      "ask for the character standing upright to get a skeleton.")
        return {"status": "failed", "reason": reason}
