"""Issue #5, first version: separately seeded parts fitted into the body. Image-to-3D melts small attached parts
(magazine, optic, muzzle device) when it builds the whole object at once; the same part alone is a simple shape
the vendor gets right. The picture editor cuts the part out of the reference on white, Tripo seeds it, and the
finish pass fits the seed into the bounding box of the part's faces on the body (the cockpit path), shrinking the
old faces underneath. Gated to premium / hero builds by the caller."""
import os
import re

from .. import config, pricing
from ..fal import first_url
from ..llm import extract_json

CHECK = """Is this a clear picture of ONE {part}, whole, isolated on a plain white background, with no other part of the
object around it? Answer JSON only: {{"ok": true/false, "score": 1-10, "fixes": "one sentence"}}"""

PRESENT = """Does this picture actually show {part}, as a part that is there on the object (not merely a place where one could
be mounted)? Answer JSON only: {{"present": true/false, "confidence": 0-1, "what": "few words on what is there"}}"""

_STOP = {"the", "mounted", "attached", "detachable", "gun", "rifle", "top", "front", "rear", "with", "that", "of", "on",
         "or", "and", "a", "an", "vehicle", "aircraft", "object", "small", "large"}


def part_denied_by_brief(spec, phrase):
    """The word of the brief that rules the part out ('no scope' -> 'scope'), or None. The picture editor draws whatever
    it is asked for: an EOTech appeared on a bullpup whose brief said 'no scope' (2026-09-25), so a part the brief
    denies is never drawn or seeded."""
    text = " ".join([getattr(spec, "description", "") or "", getattr(spec, "notes", "") or ""]).lower()
    for w in re.findall(r"[a-z]+", (phrase or "").lower()):
        if len(w) < 4 or w in _STOP:
            continue
        for stem in {w, w.rstrip("s")}:
            if re.search(r"\b(no|without|never)\s+(a\s+|an\s+|any\s+)?%s" % re.escape(stem), text):
                return w
    return None


def part_present(job, spec, part, reference_path):
    """False when the brief denies the part or the reference does not show it; one vision call at most."""
    denied = part_denied_by_brief(spec, part["phrase"])
    if denied:
        job.log("  part %s: the brief says no %s; not seeded" % (part.get("name"), denied))
        return False
    j = extract_json(job.llm.vision(PRESENT.format(part=part["phrase"]), [reference_path])) or {}
    if not j.get("present") or float(j.get("confidence") or 0) < 0.5:
        job.log("  part %s: not seen on the reference (%s); not seeded" % (part.get("name"), j.get("what") or "no answer"))
        return False
    return True


def make_part_seed(job, spec, part, reference_path):
    """part: {"phrase": "...", "name": "Magazine"} -> {"glb", "picture"} or None."""
    phrase = part["phrase"]
    if not part_present(job, spec, part, reference_path):
        return None
    fixes = ""
    picture = None
    for attempt in range(2):
        prompt = ("From this picture, show ONLY %s, whole and complete, exactly as it looks here (same colours, same "
                  "materials, same markings), isolated on a plain pure white background, nothing else in frame, sharp, "
                  "product photograph. %s" % (phrase, fixes)).strip()
        path = os.path.join(job.dir, "part_%s_ref_%d.png" % (part.get("name", "Part"), attempt))
        job.images.generate(prompt, path, model=pricing.edit_model(spec), references=[reference_path], aspect_ratio="4:3")
        j = extract_json(job.llm.vision(CHECK.format(part=phrase), [path])) or {}
        job.log("  part %s picture: score %s" % (part.get("name"), j.get("score")))
        if j.get("ok") and int(j.get("score", 0) or 0) >= 6:
            picture = path
            break
        fixes = str(j.get("fixes") or "")
        picture = picture or path
    url = job.fal.upload(picture)
    payload = {"image_url": url, "geometry_quality": "detailed", "texture_quality": "detailed", "pbr": True, "face_limit": 80000}
    if spec.category in config.HARD_SURFACE_CATEGORIES:
        payload["quad"] = True
    out = job.fal.run(config.SEED_MODEL, payload)
    mesh_url = first_url(out, (".glb",)) or first_url(out, (".fbx",))
    if not mesh_url:
        job.log("  part %s: the seed vendor returned no mesh" % part.get("name"))
        return None
    ext = ".fbx" if mesh_url.split("?")[0].lower().endswith(".fbx") else ".glb"
    glb = os.path.join(job.dir, "part_%s_seed%s" % (part.get("name", "Part"), ext))
    job.fal.download(mesh_url, glb)
    job.log("  part %s seed: %.1f MB" % (part.get("name"), os.path.getsize(glb) / 1e6))
    return {"glb": glb, "picture": picture, "name": part.get("name", "Part"), "phrase": phrase}


INTERIOR_WORDS = ("cockpit", "cabin", "interior", "seat", "console", "dashboard", "instrument")

ADD_CHECK = """Is this a clear picture of ONLY {part}, whole, isolated on a plain white background, with NO hull, fuselage, body,
engines, canopy or any other part of the vehicle or object it belongs to around it (a cockpit interior means the seat,
panel, consoles and floor pan alone, not the nose section they sit in)?
Answer JSON only: {{"ok": true/false, "score": 1-10, "fixes": "one sentence"}}"""

PART_FRONT_PROMPT = """These 4 pictures show the same 3D model of a part: {part}. It belongs on a {noun}.
They were taken from its four horizontal sides. Which picture looks straight at the part's FRONT, meaning the end that
points the same way as the {noun}'s nose or muzzle when the part is fitted ({hint})?
Answer with JSON only: {{"front": "A" | "B" | "C" | "D", "confidence": 0-1, "reason": "few words"}}"""

FRONT_HINTS = (("bulkhead", "the rear bulkhead wall is at the rear; the open end is the front"),
               ("shell", "the closed wall is at the rear; the open end is the front"),
               ("pedal", "the pedals lean toward the pilot: their treads face the rear"),
               ("stick", "the grip's trigger side faces the pilot at the rear; the stick leans forward"),
               ("cockpit", "the instrument panel the pilot looks at is the front, the seat back is at the rear"),
               ("seat", "the seat faces forward"), ("interior", "the dashboard is the front, the seat back the rear"),
               ("scope", "the large objective lens is the front, the eyepiece the rear"),
               ("suppressor", "the closed muzzle end is the front"), ("stock", "the butt pad is the rear"),
               ("pod", "the rounded nose is the front"), ("gun", "the muzzle is the front"),
               ("turret", "the barrel points forward"))


def orient_added_part(job, spec, part, glb):
    """Give the part the same treatment as the body's prepare pass (long axis -> X, scaled to its size_m, centred,
    four probe renders) and ask the vision model which side is its front: a Tripo seed faces any way it likes, and
    the Havoc's cockpit interior went in backwards when the long axis alone was aligned (2026-09-24).
    -> {"blend": oriented part, "yaw": degrees to turn it so its front points +X} or None when the pass fails."""
    from .finish import _blender
    from .probe import LETTERS, YAW_FOR
    name = part.get("name", "Part")
    part_dir = os.path.join(job.work_dir, "part_%s" % name)
    size = float(part.get("size_m") or 0) or -1.0        # -1: keep the seed's own size; the fit scales it later
    _blender(job, "prepare.py", {"name": name, "work_dir": part_dir, "glb": glb, "size_m": size, "forward_axis": "long",
                                 "origin": "center", "probe_size": 448, "keep_upright": True}, "part_%s_prepare" % name)
    if not os.path.exists(os.path.join(part_dir, "work.blend")):
        return None
    if part.get("yaw_degrees") is not None:
        yaw = int(part["yaw_degrees"])
        job.log("  add %s facing: explicit repair -> yaw %d (reusing the part geometry)" % (name, yaw))
        return {"blend": os.path.join(part_dir, "work.blend"), "yaw": yaw, "facing_source": "explicit"}
    views = ["posx", "negx", "posy", "negy"]
    files = [os.path.join(part_dir, "probe_%s.png" % v) for v in views]
    if not all(os.path.exists(f) for f in files):
        return None
    low = part["phrase"].lower()
    hint = next((h for w, h in FRONT_HINTS if w in low), "the end that leads when the whole object moves forward")
    noun = {"weapon": "gun", "vehicle": "vehicle", "aircraft": "aircraft", "helicopter": "helicopter",
            "character": "character", "prop": "object", "environment": "building"}.get(spec.category, "object")
    j = extract_json(job.llm.vision(PART_FRONT_PROMPT.format(part=part["phrase"], noun=noun, hint=hint), files,
                                    max_tokens=600)) or {}
    letter = str(j.get("front", "A")).strip().upper()[:1]
    front = views[LETTERS.index(letter)] if letter in LETTERS else "posx"
    yaw = YAW_FOR[front]
    job.log("  add %s facing: front is the %s side (%s) -> yaw %d" % (name, front, j.get("reason", ""), yaw))
    return {"blend": os.path.join(part_dir, "work.blend"), "yaw": yaw, "front": front, "confidence": j.get("confidence")}


def make_added_part(job, spec, part, reference_path):
    """A part to ADD to the built model: drawn alone on white (from the customer's picture when they gave one, edited out
    of the reference when the part is visible on it, or drawn from the words for an interior), checked, seeded as a
    small mesh. -> {"glb", "picture", "name", "phrase"} or None. The body is not touched here; the finish fits it."""
    phrase, name = part["phrase"], part.get("name", "Part")
    picture, fixes = None, ""
    if part.get("seed") and os.path.exists(part["seed"]):
        # bought by an earlier job of this chat: the fit, not the mesh, is what a re-finish changes
        job.log("  add %s: reusing the seed from %s" % (name, os.path.basename(os.path.dirname(part["seed"]))))
        pic = part.get("picture") if part.get("picture") and os.path.exists(part["picture"]) else None
        return {"glb": part["seed"], "picture": pic, "name": name, "phrase": phrase, "reused": True}
    if part.get("picture") and os.path.exists(part["picture"]):
        picture = part["picture"]
    else:
        low = phrase.lower()
        inside = part.get("place") == "inside"
        whole_interior = inside and any(w in low for w in INTERIOR_WORDS) \
            and not any(w in low for w in ("wall", "shell", "bulkhead", "stick", "pedal", "lever", "panel only"))
        for attempt in range(2):
            path = os.path.join(job.dir, "part_%s_ref_%d.png" % (name, attempt))
            if inside or not reference_path or not os.path.exists(reference_path):
                # a part that lives inside is not on the exterior reference: it is drawn from the words. The first Havoc
                # interior came as a whole nose module with engines round it, so the seat was toy-sized once the module
                # was scaled to the cockpit (2026-09-24): no bodywork, ever.
                what = phrase  # never append a floor/seat/controls that the requested module explicitly excludes
                prompt = ("ONLY %s, of a %s, as one object with nothing around it, NO fuselage, NO hull, NO engines, "
                          "NO canopy, NO exterior bodywork, seen from a three-quarter front angle slightly above, whole "
                          "and complete, isolated on a plain pure white background, nothing else in frame, "
                          "photorealistic, sharp. %s" % (what, (spec.search_query or spec.description[:140]), fixes)).strip()
                job.images.generate(prompt, path, model=pricing.concept_model(spec), aspect_ratio="4:3")
            else:
                prompt = ("Show ONLY %s that belongs on this exact object, whole and complete, matching its colours and "
                          "materials, isolated on a plain pure white background, nothing else in frame, sharp, product "
                          "photograph. %s" % (phrase, fixes)).strip()
                job.images.generate(prompt, path, model=pricing.edit_model(spec), references=[reference_path], aspect_ratio="4:3")
            j = extract_json(job.llm.vision(ADD_CHECK.format(part=phrase), [path])) or {}
            job.log("  add %s picture: score %s" % (name, j.get("score")))
            if j.get("ok") and int(j.get("score", 0) or 0) >= 6:
                picture = path
                break
            fixes = str(j.get("fixes") or "")
            picture = picture or path
    url = job.fal.upload(picture)
    payload = {"image_url": url, "geometry_quality": "detailed", "texture_quality": "detailed", "pbr": True, "face_limit": 80000}
    if spec.category in config.HARD_SURFACE_CATEGORIES:
        payload["quad"] = True
    out = job.fal.run(config.SEED_MODEL, payload)
    mesh_url = first_url(out, (".glb",)) or first_url(out, (".fbx",))
    if not mesh_url:
        job.log("  add %s: the seed vendor returned no mesh" % name)
        return None
    ext = ".fbx" if mesh_url.split("?")[0].lower().endswith(".fbx") else ".glb"
    glb = os.path.join(job.dir, "part_%s_seed%s" % (name, ext))
    job.fal.download(mesh_url, glb)
    job.log("  add %s seed: %.1f MB" % (name, os.path.getsize(glb) / 1e6))
    return {"glb": glb, "picture": picture, "name": name, "phrase": phrase}
