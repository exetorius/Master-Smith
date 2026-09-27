"""One build: reserve credits, run the four stages, settle the bill, write the job report.
The director calls this; nothing in here talks to the chat model except the two vision checks."""
import json
import os
import shutil
import subprocess
import time
import uuid

from . import config, pricing, providers, skills
from .fal import Fal, FalError
from .images import Images
from .llm import LLM
from .netsafe import download_public
from .diagnose import diagnose
from .stages.finish import run_finish
from .stages.gate import check as gate_check
from .stages.package import write_package
from .stages.reference import make_reference
from .stages.repaint import make_repaint
from .stages.review import review
from .stages.rig import rig_asset
from .stages.retexture import make_retexture
from .stages.seed import hybrid_wanted, make_seed
from .stages.assembly import build_assembly
from .spec import assembly_wanted


class Job:
    def __init__(self, spec, user, wallet, log=print, llm=None, fal=None, images=None, job_id=None):
        self.spec, self.user, self.wallet, self.log = spec, user, wallet, log
        self.id = job_id or (time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
        self.dir = str(config.OUT_DIR / ("%s_%s" % (spec.name, self.id)))
        self.work_dir = os.path.join(self.dir, "work")
        os.makedirs(self.work_dir, exist_ok=True)
        self.llm = llm or LLM(log=log)
        self.fal = fal or Fal(log=log)
        self.images = images or Images(log=log)
        self._customer_url = None
        self._customer = None

    def customer_image_urls(self):
        """The customer's own pictures on fal's CDN (uploaded once); the first is the primary."""
        if self._customer_url is not None:
            return self._customer_url
        urls = []
        for i, src in enumerate(self.spec.reference_images or ([self.spec.reference_image] if self.spec.reference_image else [])):
            ext = os.path.splitext(src.split("?")[0])[1][:5] or ".png"
            local = os.path.join(self.dir, "customer_ref_%d%s" % (i, ext))
            if src.startswith(("http://", "https://")):
                download_public(src, local)
            else:
                if not os.path.exists(src):
                    raise FileNotFoundError("reference image not found: %s" % src)
                shutil.copy(src, local)
            urls.append(self.fal.upload(local))
        self._customer_url = urls
        return urls

    def customer_image_url(self):
        urls = self.customer_image_urls()
        return urls[0] if urls else None

    def customer_pictures(self):
        """The customer's own pictures as local files (downloaded once); the first is the primary."""
        if self._customer is not None:
            return self._customer
        paths = []
        for i, src in enumerate(self.spec.reference_images or ([self.spec.reference_image] if self.spec.reference_image else [])):
            ext = os.path.splitext(src.split("?")[0])[1][:5] or ".png"
            local = os.path.join(self.dir, "customer_ref_%d%s" % (i, ext))
            if src.startswith(("http://", "https://")):
                download_public(src, local)
            else:
                if not os.path.exists(src):
                    raise FileNotFoundError("reference image not found: %s" % src)
                shutil.copy(src, local)
            paths.append(local)
        self._customer = paths
        return paths

    def stage(self, name):
        """Name the stage every following picture, fal and LLM call is billed to."""
        self.images.stage = name
        self.llm.stage = name
        self.fal.stage = name

    def spent_usd(self):
        return round(self.fal.spent() + self.images.spent() + self.llm.spent(), 6)

    def by_stage(self):
        """{stage: {"usd", "pictures", "fal_calls", "llm_calls"}} - where the money went."""
        out = {}
        for kind, calls in (("pictures", self.images.calls), ("fal_calls", self.fal.calls), ("llm_calls", self.llm.calls)):
            for c in calls:
                d = out.setdefault(c.get("stage") or "other", {"usd": 0.0, "pictures": 0, "fal_calls": 0, "llm_calls": 0})
                d["usd"] += float(c.get("usd") or 0)
                d[kind] += 1
        for d in out.values():
            d["usd"] = round(d["usd"], 4)
        return out

    def breakdown_text(self):
        bs = self.by_stage()
        total = sum(d["usd"] for d in bs.values()) or 1e-9
        return ", ".join("%s $%.2f (%.0f%%)" % (k, d["usd"], d["usd"] / total * 100) for k, d in sorted(bs.items(), key=lambda t: -t[1]["usd"]))

    def bill_calls(self):
        return {"by_stage": self.by_stage(), "fal_calls": self.fal.calls, "image_calls": self.images.calls, "llm_calls": self.llm.calls}


def repaint_seed(job, seed_glb, reference):
    """The hybrid repaint: Meshy retexture maps on the seed's UVs, or the picture repaint (a new textured GLB).
    -> (seed_glb to finish, retexture_maps or None, note)"""
    if pricing.repaint_mode(job.spec) == "pictures":
        out = make_repaint(job, seed_glb, reference)
        return out["glb"], None, "pictures"
    maps = make_retexture(job, seed_glb, style_image=reference)["maps"]
    return seed_glb, maps, "meshy"


def make_reference_only(spec, user, wallet, log=print, job_id=None):
    """Stage 1 alone: the reference picture(s) for the customer to approve before a mesh is bought. Writes
    reference.json and job.json in its own job directory; a build with spec.reference_job = that directory seeds
    from these pictures and skips the picture stage."""
    est = pricing.estimate_reference(spec)
    providers.check_affordable(est["usd"])
    hold = wallet.reserve(user, est["credits"], "reference %s" % spec.name)
    job = Job(spec, user, wallet, log, job_id=job_id)
    skill = skills.load(spec.category)
    result = {"job_id": job.id, "dir": job.dir, "spec": spec.to_dict(), "estimate": est, "status": "failed", "kind": "reference"}
    log("job %s: reference picture(s) for %s (%s) - reserved %d credits" % (job.id, spec.name, spec.category, est["credits"]))
    try:
        job.stage("reference")
        ref = make_reference(job, skill)
        result["reference"] = {"views": ref["views"], "checks": ref["checks"], "source": ref.get("source"),
                               "pictures": ref.get("pictures")}
        result["delivery_dir"] = job.dir          # the pictures are the delivery
        result["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:600])
        log("FAILED: %s" % result["error"])
    finally:
        usd = job.spent_usd()
        bill = wallet.settle(hold, usd, "reference %s %s" % (job.id, result["status"]))
        result["bill"] = {"usd_cost": usd, "credits_charged": bill["charged"], "credits_refunded": bill["refunded"],
                          "balance": bill["balance"], **job.bill_calls()}
        log("bill: $%.3f provider cost" % usd)
        with open(os.path.join(job.dir, "job.json"), "w") as f:
            json.dump(result, f, indent=1, default=str)
    return result


def load_reference(job_dir):
    """The reference.json of an earlier reference job, with its pictures checked to still exist."""
    path = os.path.join(job_dir, "reference.json")
    if not os.path.exists(path):
        raise FileNotFoundError("no reference.json in %s" % job_dir)
    ref = json.load(open(path))
    missing = [v for v in ref.get("views") or [] if not os.path.exists(v)]
    if missing or not ref.get("views"):
        raise FileNotFoundError("reference pictures missing: %s" % (missing or "none recorded"))
    return ref


def build(spec, user, wallet, log=print, job_id=None):
    """Run a full build for `user`. Raises ProviderBalanceLow before spending anything.
    With spec.reference_job set, the approved pictures of that job are used and the picture stage is skipped."""
    approved = load_reference(spec.reference_job) if spec.reference_job else None
    est = pricing.estimate_after_reference(spec) if approved else pricing.estimate(spec)
    providers.check_affordable(est["usd"])
    hold = wallet.reserve(user, est["credits"], "build %s" % spec.name)   # raises when the balance is short
    job = Job(spec, user, wallet, log, job_id=job_id)
    skill = skills.load(spec.category)
    result = {"job_id": job.id, "dir": job.dir, "spec": spec.to_dict(), "estimate": est, "status": "failed"}
    log("job %s: %s (%s, %s, %s tris, %.2f m) - reserved %d credits" % (
        job.id, spec.name, spec.category, spec.style, format(spec.tri_budget, ","), spec.size_m, est["credits"]))
    try:
        job.stage("reference")
        if approved:
            log("1/4 approved reference picture(s) from %s" % os.path.basename(spec.reference_job))
            ref = dict(approved)
            ref["urls"] = [job.fal.upload(p) for p in ref["views"]]                       # fresh CDN copies, free
            ref["seed_urls"] = [job.fal.upload(p) for p in ref["seed_views"]] if ref.get("seed_views") else None
            result["reference"] = {"views": ref["views"], "checks": ref.get("checks"), "from": spec.reference_job}
        else:
            log("1/4 reference picture")
            ref = make_reference(job, skill)
            result["reference"] = {"views": ref["views"], "checks": ref["checks"]}
        assembled = None
        if assembly_wanted(spec):
            log("2/4 assembly: plan the parts, build each one, assemble")
            assembled = build_assembly(job, spec, ref)
            if assembled is None and spec.build_mode == "assembly":
                raise RuntimeError("an assembly needs a side and a front picture and the front view was not accepted; "
                                   "draw the pictures again (make_reference) or build_mode single")
            if assembled is None:
                log("  the approved pictures have no side and front view; building from one seed instead")
        if assembled is not None:
            report, delivery_dir, record = assembled
            result["delivery"] = report
            result["delivery_dir"] = delivery_dir
            result["assembly"] = record
            job.stage("review")
            log("4/4 review")
            renders = [os.path.join(delivery_dir, r) for r in report["renders"]]
            result["review"] = review(job, ref["views"][0], renders, report)
            result["gate"] = gate_check(spec, report, result["review"], delivery_dir)
            if result["gate"]["warnings"]:
                log("gate: " + "; ".join(result["gate"]["warnings"]))
            result["diagnosis"] = []
            result["package"] = write_package(spec, report, result, delivery_dir)
            result["status"] = "done"
            return result          # the finally below settles the bill and writes job.json
        job.stage("seed")
        log("2/4 3D seed")
        seed = make_seed(job, ref.get("seed_urls") or ref["urls"])
        result["seed"] = {"model": seed["model"], "glb": seed["glb"]}
        retex_maps = None
        seed_glb = seed["glb"]
        if hybrid_wanted(spec):
            job.stage("repaint")
            log("2b/4 hybrid repaint of the seed (%s)" % pricing.repaint_mode(spec))
            seed_glb, retex_maps, note = repaint_seed(job, seed["glb"], ref["views"][0])
            result["seed"]["repaint"] = note
            result["seed"]["repainted_glb"] = seed_glb if note == "pictures" else None
        job.stage("finish")
        log("3/4 Blender finish")
        report = run_finish(job, skill, seed_glb, reference=ref["views"][0], retexture_maps=retex_maps,
                            reference_source=ref.get("source"))
        result["delivery"] = report
        result["delivery_dir"] = os.path.join(job.dir, "delivery")
        if spec.rig:
            job.stage("rig")
            log("3b/4 rig")
            result["rig"] = rig_asset(job, skill, report)
        job.stage("review")
        log("4/4 review")
        renders = [os.path.join(result["delivery_dir"], r) for r in report["renders"]]
        result["review"] = review(job, ref["views"][0], renders, report)
        result["gate"] = gate_check(spec, report, result["review"], result["delivery_dir"])
        if result["gate"]["warnings"]:
            log("gate: " + "; ".join(result["gate"]["warnings"]))
        result["diagnosis"] = diagnose(result, job.work_dir, spec, ref.get("source"))
        for f in result["diagnosis"]:
            log("diagnosis: %s -> %s" % (f["finding"], f["remedy"]))
        result["package"] = write_package(spec, report, result, result["delivery_dir"])
        result["status"] = "done"
    except Exception as exc:  # noqa: BLE001 - whatever failed, the hold must settle and the report be written
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:600])
        log("FAILED: %s" % result["error"])
    finally:
        usd = job.spent_usd()
        bill = wallet.settle(hold, usd, "job %s %s" % (job.id, result["status"]))
        result["bill"] = {"usd_cost": usd, "credits_charged": bill["charged"], "credits_refunded": bill["refunded"],
                          "balance": bill["balance"], **job.bill_calls()}
        log("bill: $%.3f provider cost, %d credits charged, balance %d" % (usd, bill["charged"], bill["balance"]))
        log("where the money went: %s" % job.breakdown_text())
        with open(os.path.join(job.dir, "job.json"), "w") as f:
            json.dump(result, f, indent=1, default=str)
    return result


MESH_EXTENSIONS = (".glb", ".gltf", ".fbx", ".obj", ".blend")


def blend_to_seed(blend_path, work_dir, log=print):
    """A delivered .blend -> its LOD0 as a GLB plus the brief and reference picture stored in the file."""
    glb = os.path.join(work_dir, "blend_seed.glb")
    info = os.path.join(work_dir, "blend_seed.json")
    script = str(config.ROOT / "mastersmith" / "blender" / "blend_to_seed.py")
    proc = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", script, "--", blend_path, glb, info],
                          capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0 or not os.path.exists(glb):
        raise RuntimeError("blend_to_seed failed: %s" % ((proc.stdout or "")[-600:] + (proc.stderr or "")[-300:]))
    meta = json.load(open(info)) if os.path.exists(info) else {}
    log("  opened %s: %s" % (os.path.basename(blend_path), "brief found" if meta.get("spec") else "no stored brief"))
    return glb, meta


def rework(seed_path, spec, user, wallet, ref_view=None, mode="refinish", log=print, job_id=None):
    """Finish an EXISTING mesh under a brief without buying a new seed: a model the customer imported (GLB, glTF,
    FBX, OBJ or a delivered .blend) or the seed of an earlier job.
      mode "refinish":  orient, scale, glass, LODs, maps, collision, rig, review and package again;
      mode "retexture": the mesh is repainted first - recoloured under part masks when every named part has a flat
                        colour, else by the retexture vendor guided by a picture edited from `ref_view` (or by the
                        text alone when there is no picture) - then finished.
    Pays for the probe, repaint, rig and review calls only."""
    est = pricing.estimate_rework(spec, mode)
    providers.check_affordable(est["usd"])
    credits = est["credits"]
    hold = wallet.reserve(user, credits, "%s %s" % (mode, spec.name))
    job = Job(spec, user, wallet, log, job_id=job_id)
    skill = skills.load(spec.category)
    result = {"job_id": job.id, "dir": job.dir, "spec": spec.to_dict(), "reworked_from": seed_path, "mode": mode,
              "status": "failed", "seed": {"glb": seed_path, "source": seed_path}}
    log("job %s: %s %s from %s - reserved %d credits" % (job.id, mode, spec.name, os.path.basename(seed_path), credits))
    try:
        if not os.path.exists(seed_path):
            raise FileNotFoundError("mesh not found: %s" % seed_path)
        seed_glb = seed_path
        if seed_path.lower().endswith(".blend"):
            seed_glb, meta = blend_to_seed(seed_path, job.work_dir, log=log)
            stored_ref = meta.get("reference")
            if not ref_view and stored_ref and os.path.exists(stored_ref):
                ref_view = stored_ref
            result["seed"]["glb"] = seed_glb
        retex_maps, recolor = None, None
        if mode == "retexture":
            parts = spec.retexture_parts or []
            if parts and all(isinstance(p, dict) and p.get("color") for p in parts):
                recolor = parts                       # flat colours: recolour the existing maps under the part masks
                log("1/4 recolouring %s on the existing mesh" % ", ".join(p["phrase"] for p in parts))
            else:
                guide = ref_view
                if ref_view and os.path.exists(ref_view):
                    spec.reference_images, spec.reference_image = [ref_view], ref_view
                    spec.research, spec.search_query = False, ""
                    job.stage("reference")
                    log("1/4 guide picture of the new look")
                    try:
                        guide = make_reference(job, skill)["views"][0]
                    except Exception as exc:  # noqa: BLE001 - the text prompt alone still repaints
                        log("  guide picture failed (%s); repainting from the text alone" % str(exc)[:160])
                job.stage("repaint")
                log("2/4 repainting the mesh (retexture vendor, original UVs)")
                retex_maps = make_retexture(job, seed_glb, style_image=guide)["maps"]
                ref_view = guide or ref_view
        result["reference"] = {"views": [ref_view] if ref_view else []}
        job.stage("finish")
        log("3/4 Blender finish")
        report = run_finish(job, skill, seed_glb, reference=ref_view, retexture_maps=retex_maps, recolor=recolor)
        result["delivery"] = report
        result["delivery_dir"] = os.path.join(job.dir, "delivery")
        if spec.rig:
            log("3b/4 rig")
            result["rig"] = rig_asset(job, skill, report)
        log("4/4 review")
        renders = [os.path.join(result["delivery_dir"], r) for r in report["renders"]]
        result["review"] = review(job, ref_view, renders, report)
        result["gate"] = gate_check(spec, report, result.get("review"), result["delivery_dir"])
        if result["gate"]["warnings"]:
            log("gate: " + "; ".join(result["gate"]["warnings"]))
        result["diagnosis"] = diagnose(result, job.work_dir, spec)
        for f in result["diagnosis"]:
            log("diagnosis: %s -> %s" % (f["finding"], f["remedy"]))
        result["package"] = write_package(spec, report, result, result["delivery_dir"])
        result["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:600])
        log("FAILED: %s" % result["error"])
    finally:
        usd = job.spent_usd()
        bill = wallet.settle(hold, usd, "%s %s %s" % (mode, job.id, result["status"]))
        result["bill"] = {"usd_cost": usd, "credits_charged": bill["charged"], "credits_refunded": bill["refunded"],
                          "balance": bill["balance"], **job.bill_calls()}
        log("bill: $%.3f provider cost, %d credits charged, balance %d" % (usd, bill["charged"], bill["balance"]))
        log("where the money went: %s" % job.breakdown_text())
        with open(os.path.join(job.dir, "job.json"), "w") as f:
            json.dump(result, f, indent=1, default=str)
    return result


def refinish(job_dir, user, wallet, overrides=None, log=print, job_id=None):
    """Re-run finishing on an earlier job's seed with a changed brief (`python -m mastersmith rerun out/<job>`)."""
    from .spec import Spec
    prev = json.load(open(os.path.join(job_dir, "job.json")))
    spec = Spec.from_dict({**prev["spec"], **(overrides or {})})
    seed_glb = seed_of(prev) or os.path.join(job_dir, "seed.glb")
    ref_view = ((prev.get("reference") or {}).get("views") or [None])[0]
    return rework(seed_glb, spec, user, wallet, ref_view=ref_view if ref_view and os.path.exists(ref_view) else None,
                  mode="refinish", log=log, job_id=job_id)


def seed_of(result):
    """The mesh a finished job's finish ran on (the repainted GLB when the hybrid picture repaint made one)."""
    seed = (result or {}).get("seed") or {}
    for key in ("repainted_glb", "glb"):
        p = seed.get(key)
        if p and os.path.exists(p):
            return p
    return None


def preview_removal_job(source_dir, spec, user, wallet, log=print, job_id=None):
    """What a remove_parts repair would delete, drawn in red on the source job's probe renders, for the customer to
    confirm before any face goes. A few SAM calls; no Blender, no mesh."""
    from .stages.removal import preview_removal
    phrases = list(spec.remove_parts or [])
    est_usd = 0.005 * 3 * len(phrases) * 2 + 0.01                    # masks (two wordings a view at worst) + a box fallback
    providers.check_affordable(est_usd)
    hold = wallet.reserve(user, config.credits_for_usd(est_usd), "removal preview %s" % spec.name)
    job = Job(spec, user, wallet, log, job_id=job_id)
    result = {"job_id": job.id, "dir": job.dir, "spec": spec.to_dict(), "status": "failed", "kind": "removal_preview",
              "source": source_dir}
    log("job %s: preview of removing %s from %s" % (job.id, ", ".join(phrases), os.path.basename(source_dir)))
    try:
        job.stage("probe")
        pics = preview_removal(job, source_dir, phrases)
        result["pictures"] = pics
        result["delivery_dir"] = job.dir
        result["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:600])
        log("FAILED: %s" % result["error"])
    finally:
        usd = job.spent_usd()
        bill = wallet.settle(hold, usd, "removal preview %s %s" % (job.id, result["status"]))
        result["bill"] = {"usd_cost": usd, "credits_charged": bill["charged"], "credits_refunded": bill["refunded"],
                          "balance": bill["balance"], **job.bill_calls()}
        with open(os.path.join(job.dir, "job.json"), "w") as f:
            json.dump(result, f, indent=1, default=str)
    return result


__all__ = ["build", "make_reference_only", "load_reference", "preview_removal_job", "refinish", "rework", "seed_of", "Job",
           "MESH_EXTENSIONS"]
