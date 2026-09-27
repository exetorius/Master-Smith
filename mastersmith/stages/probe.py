"""Between the two Blender passes: look at the probe renders and decide (a) which end is the front,
(b) where the glass is, (c) where the wheels are. Vision model for (a), SAM 3 text-prompted masks for (b, c)."""
import json
import os

from .. import config
from ..llm import extract_json

FRONT_PROMPT = """These {n} pictures show the same 3D model of: {brief}
They were taken from the four horizontal sides. Which picture looks straight at the FRONT of the object ({front_hint})?
Answer with JSON only: {{"front": "A" | "B" | "C" | "D", "confidence": 0-1, "reason": "few words"}}"""

LETTERS = "ABCD"
# yaw that brings each probe side round to +X (engine forward)
YAW_FOR = {"posx": 0, "negx": 180, "posy": -90, "negy": 90}


def decide_facing(job, skill, probe):
    """Returns (yaw_degrees, detail dict). Weapons/vehicles: which end of the long axis; characters: any side."""
    if skill["meta"]["forward_axis"] == "long":
        views = ["posx", "negx"]
    else:
        views = ["posx", "negx", "posy", "negy"]
    files = {v: os.path.join(job.work_dir, "probe_%s.png" % v) for v in views}
    prompt = FRONT_PROMPT.format(n=len(views), brief=job.spec.description,
                                 front_hint=skill["meta"].get("front_hint", "the end that leads when it moves"))
    text = job.llm.vision(prompt, [files[v] for v in views], max_tokens=800)
    j = extract_json(text) or {}
    letter = str(j.get("front", "A")).strip().upper()[:1]
    idx = LETTERS.index(letter) if letter in LETTERS[:len(views)] else 0
    front_view = views[idx]
    yaw = YAW_FOR[front_view]
    job.log("  facing: front is the %s side (%s) -> yaw %d" % (front_view, j.get("reason", ""), yaw))
    return yaw, {"front_view": front_view, "yaw": yaw, "confidence": j.get("confidence"), "reason": j.get("reason")}


def _sam(job, image_path, prompt, max_masks=6, min_score=0.4):
    """SAM 3 wants a descriptive noun phrase: 'the upper metal slide of the pistol' is found, 'slide' is not (2026-09-17)."""
    url = job.fal.upload(image_path)
    out = job.fal.run("fal-ai/sam-3/image", {"image_url": url, "prompt": prompt, "apply_mask": False,
                                             "return_multiple_masks": True, "max_masks": max_masks,
                                             "include_scores": True, "include_boxes": True, "output_format": "png"})
    masks = out.get("masks") or []
    scores = out.get("scores") or [1.0] * len(masks)
    kept = []
    for i, (m, s) in enumerate(zip(masks, scores)):
        if s is None or s >= min_score:
            path = os.path.join(job.work_dir, "mask_%s_%s_%d.png" % (
                os.path.basename(image_path)[6:-4], prompt.split()[0], i))
            job.fal.download(m["url"], path)
            kept.append({"file": os.path.basename(path), "score": s})
    return kept


def find_regions(job, skill, probe):
    """SAM masks per probe view for every region the category cares about (glass, wheels).
    Returns {"glass": {view: [masks]}, "wheel": {view: [masks]}}."""
    wanted = {}
    if job.spec.glass and skill["meta"].get("glass_prompt"):
        wanted["glass"] = (skill["meta"]["glass_prompt"], ["posx", "negx", "posy", "negy", "iso"])
    if job.spec.rig and skill["meta"].get("rig_parts_prompt"):
        wanted["wheel"] = (skill["meta"]["rig_parts_prompt"], ["posy", "negy", "iso"])
    regions = {}
    for key, (prompt, views) in wanted.items():
        regions[key] = {}
        for v in views:
            path = os.path.join(job.work_dir, "probe_%s.png" % v)
            kept = _sam(job, path, prompt)
            if kept:
                regions[key][v] = kept
        total = sum(len(v) for v in regions[key].values())
        job.log("  %s: %d mask(s) across %d view(s)" % (key, total, len(regions[key])))
    return regions


def run_probe(job, skill):
    probe = json.load(open(os.path.join(job.work_dir, "probe.json")))
    yaw, facing = decide_facing(job, skill, probe)
    regions = find_regions(job, skill, probe)
    decision = {"yaw": yaw, "facing": facing, "regions": regions}
    with open(os.path.join(job.work_dir, "decision.json"), "w") as f:
        json.dump(decision, f, indent=1)
    return decision
