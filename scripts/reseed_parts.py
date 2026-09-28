"""Mesh a finished assembly's parts again with another vendor, from the pictures kept in its delivery, and assemble.

    python scripts/reseed_parts.py out/<job> --vendor hitem3d3 [--parts Handguard,GripTriggerGuard] [--out delivery_hitem3d3]

The parts' pictures were drawn once (delivery/parts/<Part>/quarter.png and side.png); nothing is redrawn. Each named
part (default: every vendor part) is meshed by the vendor from its quarter picture (side.png when there is none),
registered to its side picture like the original, and the assembly is rebuilt with the new meshes into --out. Paid
vendors spend real money: the no-spend guard is lifted for this run only when --vendor is not local."""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ap = argparse.ArgumentParser()
ap.add_argument("job")
ap.add_argument("--vendor", default="hitem3d3", help="local, tripo, hitem3d3, hitem3d3mv")
ap.add_argument("--parts", default="", help="comma-separated part names; default every vendor part")
ap.add_argument("--out", default="")
a = ap.parse_args()
if a.vendor != "local":
    os.environ["MASTERSMITH_NO_SPEND"] = "0"
    os.environ["MASTERSMITH_PAID_PICTURES"] = "0"

from mastersmith import config  # noqa: E402
from mastersmith.fal import Fal, first_url  # noqa: E402
from mastersmith.stages import assembly  # noqa: E402
from mastersmith.stages.finish import _blender  # noqa: E402

job_dir = os.path.abspath(a.job)
delivery = os.path.join(job_dir, "delivery")
index = json.load(open(os.path.join(delivery, "parts", "index.json")))
want = [p.strip() for p in a.parts.split(",") if p.strip()] or index["parts"]
out_dir = os.path.abspath(a.out or os.path.join(job_dir, "delivery_%s" % a.vendor))
work = os.path.join(job_dir, "work", "reseed_%s" % a.vendor)
os.makedirs(work, exist_ok=True)


class J:                      # what _blender and _register need of a job
    def __init__(self):
        self.dir, self.work_dir = job_dir, work
        self.fal = Fal(log=print)
        self.log = print


job = J()
args = json.load(open(os.path.join(job_dir, "work", "assemble_final_args.json")))
by_name = {p["name"]: p for p in args["parts"]}
spent = 0.0
for name in want:
    d = os.path.join(delivery, "parts", name)
    meta = json.load(open(os.path.join(d, "meta.json")))
    if meta.get("kind") != "vendor" and name not in a.parts:
        continue
    pic = os.path.join(d, meta.get("picture") or meta.get("side_picture") or "")
    side = os.path.join(d, meta.get("side_picture") or "")
    if not os.path.exists(pic):
        print("  %s: no kept picture, skipped" % name)
        continue
    url = job.fal.upload(pic)
    if a.vendor.startswith("hitem3d3"):
        model, payload = "hitem3d/hi3d/v3.0/image-to-3d", {"image_url": url, "model": "hi3dv3.0", "resolution": "2048quality",
                                                          "face_count": 200000, "enable_texture": True, "enable_pbr": True,
                                                          "export_format": "glb", "enable_safety_checker": False}
    elif a.vendor == "local":
        model, payload = config.LOCAL_SEED_MODEL, {"image_url": url}
    else:
        model, payload = config.SEED_MODEL, {"image_url": url, "geometry_quality": "detailed", "texture_quality": "detailed",
                                             "pbr": True, "face_limit": 150000}
    out = job.fal.run(model, payload)
    mesh_url = first_url(out, (".glb",))
    part_work = os.path.join(work, name)
    os.makedirs(part_work, exist_ok=True)
    glb = os.path.join(part_work, "seed.glb")
    job.fal.download(mesh_url, glb)
    reg = assembly._register(job, name, glb, side if os.path.exists(side) else pic, part_work, yaw_sweep=bool(meta.get("picture")))
    if not reg:
        print("  %s: registration failed, keeping the original mesh" % name)
        continue
    by_name[name]["blend"] = reg["blend"]
    by_name[name]["keep_depth"] = bool(meta.get("picture"))
    print("  %s: meshed by %s, registered IoU %.2f" % (name, model.split("/")[0], reg["iou"]))
spent = job.fal.spent()
args["out_dir"] = out_dir
os.makedirs(out_dir, exist_ok=True)
args_path = os.path.join(work, "assemble_args.json")
json.dump(args, open(args_path, "w"))
_blender(job, "assemble.py", args, "assemble_reseed")
shutil.copytree(os.path.join(delivery, "parts"), os.path.join(out_dir, "parts"), dirs_exist_ok=True)
print("assembled into %s; vendor spend $%.2f" % (out_dir, spent))
