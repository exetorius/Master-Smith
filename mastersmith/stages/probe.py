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


BOX_PROMPT = """Find "{part}" in this render of a game asset. Answer JSON only:
{{"found": true/false, "box": [x_min, y_min, x_max, y_max]}} with the box as fractions 0-1 of the image width and height,
tight around that part only. found=false when the part is not visible from this side."""


OBJECT_NOUN = {"weapon": "gun", "vehicle": "vehicle", "aircraft": "aircraft", "helicopter": "helicopter",
               "character": "character", "prop": "object", "environment": "building"}


def part_variants(spec, part):
    """SAM 3 finds 'the white rear shoulder stock of the bullpup rifle' (0.59) and not 'the rifle stock' (nothing,
    2026-09-17): the wordings tried, most specific first."""
    phrase = (part.get("phrase") if isinstance(part, dict) else str(part)).strip()
    current = (part.get("current") if isinstance(part, dict) else "") or ""
    noun = OBJECT_NOUN.get(spec.category, "object")
    core = phrase[4:] if phrase.lower().startswith("the ") else phrase
    out = []
    if current:
        out.append("the %s %s of the %s" % (current, core, noun))
    out.append("%s of the %s" % (phrase, noun) if " of the " not in phrase else phrase)
    out.append(phrase)
    seen, uniq = set(), []
    for v in out:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


def _sam_by_box(job, image_path, phrase):
    """SAM 3 with a box the vision model draws, for parts its text prompt cannot name ('the rifle stock' on a
    bullpup, 2026-09-17)."""
    from PIL import Image
    j = {}
    for _attempt in range(2):
        try:
            j = extract_json(job.llm.vision(BOX_PROMPT.format(part=phrase), [image_path], max_tokens=700)) or {}
        except Exception as exc:  # noqa: BLE001
            job.log("  box for %s: vision call failed (%s)" % (phrase, str(exc)[:80]))
            j = {}
        if isinstance(j.get("box"), list) and len(j["box"]) == 4:
            break
    if not j.get("found") or not (isinstance(j.get("box"), list) and len(j["box"]) == 4):
        job.log("  box for %s: not found on %s" % (phrase, os.path.basename(image_path)))
        return []
    w, h = Image.open(image_path).size
    vals = []
    for i, v in enumerate(j["box"]):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return []
        if v > 1.0:                       # the model wrote pixels (or dropped a "0."): scale by the image size
            v = v / (w if i % 2 == 0 else h)
        vals.append(v)
    x0, y0, x1, y1 = [max(0.0, min(1.0, v)) for v in vals]
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        return []
    url = job.fal.upload(image_path)
    out = job.fal.run("fal-ai/sam-3/image", {"image_url": url, "apply_mask": False, "return_multiple_masks": False,
                                             "include_scores": True, "output_format": "png",
                                             "box_prompts": [{"x_min": int(x0 * w), "y_min": int(y0 * h),
                                                              "x_max": int(x1 * w), "y_max": int(y1 * h)}]})
    masks = out.get("masks") or []
    if not masks:
        return []
    path = os.path.join(job.work_dir, "mask_%s_%s_box.png" % (os.path.basename(image_path)[6:-4], phrase.split()[1] if len(phrase.split()) > 1 else "part"))
    job.fal.download(masks[0]["url"], path)
    job.log("  %s: box from the vision model on %s" % (phrase, os.path.basename(image_path)))
    return [{"file": os.path.basename(path), "score": (out.get("scores") or [0.5])[0]}]


def find_regions(job, skill, probe):
    """SAM masks per probe view for every region the category cares about (glass, wheels).
    Returns {"glass": {view: [masks]}, "wheel": {view: [masks]}}."""
    wanted = {}
    if job.spec.glass and skill["meta"].get("glass_prompt"):
        wanted["glass"] = (skill["meta"]["glass_prompt"], ["posx", "negx", "posy", "negy", "iso"])
    if job.spec.rig and skill["meta"].get("rig_parts_prompt"):
        wanted["wheel"] = (skill["meta"]["rig_parts_prompt"], ["posy", "negy", "iso"])
    seeds = skill["meta"].get("part_seeds") if isinstance(skill["meta"].get("part_seeds"), list) else []
    # hero budgets or an explicit ask only: `premium` is the picture model and nothing else (it silently seeded an
    # optic onto a bullpup whose brief said "no scope", 2026-09-25)
    want_parts = job.spec.part_seeds if job.spec.part_seeds is not None else job.spec.tri_budget >= 150000
    if want_parts and getattr(job.spec, "style", "realistic") == "realistic" and not getattr(job.spec, "retexture", False):
        for i, sd in enumerate(seeds[:3]):
            wanted["seed%d" % i] = (part_variants(job.spec, sd), ["posy", "negy", "iso", "posx", "negx"])
    for i, phrase in enumerate((getattr(job.spec, "remove_parts", None) or [])[:4]):
        wanted["remove%d" % i] = (part_variants(job.spec, phrase), ["posx", "negx", "posy", "negy", "iso"])   # faces to delete
    for i, part in enumerate((getattr(job.spec, "add_parts", None) or [])[:4]):
        if part.get("anchor") not in ("glass", "body"):
            wanted["anchor%d" % i] = (part_variants(job.spec, part["anchor"]), ["posx", "negx", "posy", "negy", "iso"])   # where it goes
    cyls = skill["meta"].get("repair_cylinders") if isinstance(skill["meta"].get("repair_cylinders"), list) else []
    if getattr(job.spec, "style", "realistic") == "realistic" and not getattr(job.spec, "retexture", False):
        for i, cyl in enumerate(cyls[:2]):
            wanted["cyl%d" % i] = (part_variants(job.spec, cyl), ["posy", "negy", "iso", "posx"])
    fams = skill["meta"].get("material_families") if isinstance(skill["meta"].get("material_families"), list) else []
    if getattr(job.spec, "style", "realistic") == "realistic" and not getattr(job.spec, "retexture", False):
        for i, fam in enumerate(fams[:4]):
            wanted["family%d" % i] = (part_variants(job.spec, fam), ["posx", "negx", "posy", "negy", "iso"])
    for i, part in enumerate((job.spec.retexture_parts or [])[:4] if job.spec.retexture else []):
        phrase = part.get("phrase") if isinstance(part, dict) else str(part)
        wanted["part%d" % i] = (part_variants(job.spec, part), ["posx", "negx", "posy", "negy", "iso"])   # the faces a repaint may touch
    for i, prot in enumerate((job.spec.protect_parts or [])[:4] if job.spec.retexture and job.spec.retexture_parts else []):
        phrase = prot.get("phrase") if isinstance(prot, dict) else str(prot)
        wanted["protect%d" % i] = ([phrase, "%s of the %s" % (phrase, OBJECT_NOUN.get(job.spec.category, "object"))],
                                   ["posx", "negx", "posy", "negy", "iso"])
    regions = {}
    for key, (prompt, views) in wanted.items():
        regions[key] = {}
        for v in views:
            path = os.path.join(job.work_dir, "probe_%s.png" % v)
            if isinstance(prompt, list) and key.startswith("seed"):
                kept = []
                for variant in prompt:
                    kept = _sam(job, path, variant, min_score=0.4)
                    if kept:
                        break
            elif isinstance(prompt, list) and key.startswith("cyl"):
                kept = []
                for variant in prompt:
                    kept = _sam(job, path, variant, min_score=0.35)
                    if kept:
                        break
            elif isinstance(prompt, list) and key.startswith("family"):
                kept = []
                for variant in prompt:
                    kept = _sam(job, path, variant, min_score=0.35)
                    if kept:
                        break
            elif isinstance(prompt, list) and key.startswith("protect"):
                kept = []
                for variant in prompt:
                    kept = _sam(job, path, variant, min_score=0.3)
                    if kept:
                        break
            elif isinstance(prompt, list):        # a part: try each wording, then a vision-drawn box
                kept = []
                for variant in prompt:
                    kept = _sam(job, path, variant, min_score=0.3)
                    if kept:
                        break
                if not kept:
                    kept = _sam_by_box(job, path, prompt[0])
            else:
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
