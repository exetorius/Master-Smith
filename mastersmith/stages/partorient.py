"""One part of an assembly, seeded on its own by a vendor: the check that its picture shows the part alone, and the
facing pass that turns the seed so its front points +X before the assembler places it (docs/ASSEMBLY.md)."""
import os

from ..llm import extract_json

PART_CHECK = """Is this a clear picture of ONLY {part}, whole, isolated on a plain white background, with nothing else of the
object it belongs to around it (no body, hull, frame or neighbouring part)?
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


def orient_part(job, spec, part, glb):
    """Give the part the same treatment as the body's prepare pass (long axis -> X, scaled to its size_m, centred,
    four probe renders) and ask the vision model which side is its front: a Tripo seed faces any way it likes, and a
    part went in backwards when the long axis alone was aligned (2026-09-24).
    part: {"name", "phrase", "size_m"} -> {"blend": oriented part, "yaw": degrees to turn it so its front points +X}
    or None when the pass fails."""
    from .finish import _blender
    from .probe import LETTERS, YAW_FOR
    name = part.get("name", "Part")
    part_dir = os.path.join(job.work_dir, "part_%s" % name)
    size = float(part.get("size_m") or 0) or -1.0        # -1: keep the seed's own size; the assembler scales it later
    _blender(job, "prepare.py", {"name": name, "work_dir": part_dir, "glb": glb, "size_m": size, "forward_axis": "long",
                                 "origin": "center", "probe_size": 448, "keep_upright": True}, "part_%s_prepare" % name)
    if not os.path.exists(os.path.join(part_dir, "work.blend")):
        return None
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
    job.log("  part %s facing: front is the %s side (%s) -> yaw %d" % (name, front, j.get("reason", ""), yaw))
    return {"blend": os.path.join(part_dir, "work.blend"), "yaw": yaw, "front": front, "confidence": j.get("confidence")}
