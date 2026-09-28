"""Stage 1: the picture the mesh will be built from. Quality starts here - a foreshortened photo with a
busy background gives the modeller a foreshortened object with the background in it. So: either generate a
clean product shot from the brief, or edit the customer's picture into one, then have a vision model check it.
Pictures come from OpenRouter's image API; the same model draws from text and edits from a reference."""
import json
import os

from PIL import Image, ImageOps

from .. import config, pricing
from ..fal import image_url
from ..images import ImageRefused
from ..llm import extract_json
from ..spec import Spec
from .research import find_reference_photo

SAFE_REWRITE = """Rewrite this game-asset caption so an image generator's content checker accepts it, keeping the design,
silhouette, colours and materials but replacing graphic or violent terms (gore, blood, rotting, wounds, corpse, kill)
with neutral art-direction words (weathered, pale, tattered, distressed, undead costume). Answer with the caption only.
Caption: {caption}"""

CLEAN = ("clean studio product photograph, plain pure white background, soft even lighting, sharp focus everywhere, "
         "no people, no text, no watermark, no ground shadow, the entire object in frame with a margin around it")


def _caption(spec, skill):
    return "%s. %s" % (spec.description.rstrip("."), spec.notes) if spec.notes else spec.description


def concept_prompt(spec, skill, view=None, fixes=""):
    view = view or skill["meta"]["reference_view"]
    look = "Photorealistic." if spec.style == "realistic" else "Stylized game art, clean shapes."
    return ("%s, %s view. %s. %s %s" % (_caption(spec, skill), view, CLEAN, look, fixes)).strip()


def edit_prompt(spec, skill, view=None, fixes=""):
    view = view or skill["meta"]["reference_view"]
    if spec.edit_instructions:
        return ("This is the previous version of the object. Change ONLY this: %s. Keep every other detail, colour, "
                "material, part and proportion exactly as in the picture. Show it as a %s: %s view; remove everything "
                "that is not the object. %s %s" % (spec.edit_instructions.rstrip("."), CLEAN, view, _caption(spec, skill), fixes)).strip()
    return ("Recreate this exact object as a %s: %s view. Keep every detail, colour, material and proportion exactly as "
            "in the photo; remove everything that is not the object. %s %s" % (CLEAN, view, _caption(spec, skill), fixes)).strip()


CHECK_PROMPT = """You are checking a reference picture that a 3D modeller will build a mesh from.
The brief: {brief}
Wanted view: {view}
Judge the camera angle ONLY against the wanted view. The brief describes the object; any viewpoint words in it
("side profile", "three-quarter") describe another picture and are not a reason to fail this one.
Answer with JSON only:
{{"single_object": true/false, "plain_background": true/false, "whole_object_visible": true/false,
 "view_matches": true/false, "matches_brief": true/false, "score": 1-10, "fixes": "one sentence of prompt changes if score < 7, else empty"}}"""


def _check(job, path, view):
    text = job.llm.vision(CHECK_PROMPT.format(brief=job.spec.description, view=view), [path])
    j = extract_json(text) or {}
    ok = all(j.get(k) for k in ("single_object", "plain_background", "whole_object_visible", "matches_brief")) \
        and int(j.get("score", 0) or 0) >= 6
    job.log("  picture check: score %s, %s" % (j.get("score"), "ok" if ok else "retry: " + str(j.get("fixes", ""))[:120]))
    return ok, j


def _source_pictures(job):
    """The local pictures the clean product shot is edited from: the customer's, else a web photograph of a
    named real thing, else nothing (the shot is then generated from the brief)."""
    if getattr(job, "_sources", None) is not None:
        return job._sources
    spec = job.spec
    paths = []
    if spec.reference_images:
        paths = job.customer_pictures()
        job.log("  %d customer picture(s) as the source" % len(paths))
    elif spec.research and spec.search_query:
        job.log("  researching pictures of: %s" % spec.search_query)
        photo = find_reference_photo(job, spec)
        if photo:
            paths = [photo]
            job.research_photo = photo
    job._sources = paths
    return paths


def _cutout(job, source, path):
    """A source photograph the editor refused: cut it out on white and seed from it as it is (fal birefnet, or the
    BiRefNet that ships with TRELLIS on this PC when nothing may be spent)."""
    if config.NO_SPEND:
        from .. import local
        local.cutout(source, path)
        return path
    src_url = source if source.startswith(("http://", "https://")) else job.fal.upload(source)
    cut = job.fal.run(config.CUTOUT_MODEL, {"image_url": src_url, "operating_resolution": "2048x2048",
                                            "output_format": "png", "refine_foreground": True})
    job.fal.download(image_url(cut), path)
    im = Image.open(path).convert("RGBA")
    flat = Image.new("RGB", im.size, (255, 255, 255))
    flat.paste(im, mask=im.split()[3])
    flat.save(path)
    return path


def _make_view(job, skill, view, fixes, index):
    """One picture of the wanted view: edit the customer's (or researched) picture when there is one, else generate.
    An edit the picture model refuses falls back to generating the shot from the brief; a refusal must not sink the build."""
    spec, images = job.spec, job.images
    path = os.path.join(job.dir, "ref_%d.png" % index)
    sources = _source_pictures(job)
    made = False
    if sources:
        try:
            images.generate(edit_prompt(spec, skill, view, fixes), path, model=pricing.edit_model(spec), references=sources[:4], aspect_ratio="4:3")
            made = True
            job.edit_refused = False        # a refusal is per call: one 422 on an Apache photo must not cost the extra angles
        except ImageRefused as exc:
            job.edit_refused = True
            job.log("  the picture editor refused this subject (%s); cutting the photograph out instead" % str(exc)[:100])
            try:
                return _cutout(job, sources[0], path)
            except Exception as exc2:  # noqa: BLE001 - the concept from the brief is the last resort
                job.log("  cut-out failed (%s); generating the shot from the brief instead" % str(exc2)[:100])
    if not made:
        model = pricing.concept_model(spec)
        prompt = concept_prompt(spec, skill, view, fixes)
        try:
            images.generate(prompt, path, model=model, aspect_ratio="4:3")
        except ImageRefused:
            # The picture model's content checker refused the wording (a zombie's "rotting skin", 2026-09-17).
            # Ask the director model for a tamer caption that keeps the design, and try once more.
            job.log("  the picture model refused the wording; rewording the caption once")
            text = job.llm.chat([{"role": "user", "content": SAFE_REWRITE.format(caption=_caption(spec, skill))}],
                                max_tokens=300, temperature=0.2).get("content") or ""
            safe = text.strip().strip('"')
            if not safe:
                raise
            prompt = concept_prompt(Spec.from_dict({**spec.to_dict(), "description": safe, "notes": ""}), skill, view, fixes)
            try:
                images.generate(prompt, path, model=model, aspect_ratio="4:3")
            except ImageRefused as exc2:
                raise RuntimeError("The picture model refused this subject twice (content policy). Reword the brief "
                                   "without graphic terms (gore, wounds, real weapons brand names) and try again.") from exc2
    return path


ORTHO_VIEWS = (("front", "the direct FRONT view: camera exactly ahead of the nose, level with the object, strictly "
                          "orthographic with no perspective, the object centred"),
               ("left", "the direct LEFT-side profile: camera exactly on the left side, level with the object, strictly "
                        "orthographic with no perspective, the whole length visible"),
               ("back", "the direct REAR view: camera exactly behind the tail, level with the object, strictly orthographic "
                        "with no perspective, the object centred"))


def _foreground_aspect(path):
    """Width / height of the object's silhouette (border-median backdrop)."""
    import numpy as np
    a = np.asarray(Image.open(path).convert("RGB")).astype(np.float32) / 255.0
    back = np.median(np.concatenate([a[:6].reshape(-1, 3), a[-6:].reshape(-1, 3), a[:, :6].reshape(-1, 3), a[:, -6:].reshape(-1, 3)]), axis=0)
    fg = np.abs(a - back).max(axis=2) > 0.08
    ys, xs = np.nonzero(fg)
    if len(xs) < 100:
        return None
    return float(np.ptp(xs) + 1) / float(np.ptp(ys) + 1)


def _orthographic_views(job, skill, primary):
    """Issue #8: front / left / back views of the SAME vehicle, edited from the primary picture so the object stays the
    object, each checked by vision (view_matches included) and by silhouette: the front and back must be narrower
    than the side. All three pass -> [front, left, back, mirrored left] for the modeller; anything fails -> None
    and the build works from the one picture."""
    got = {}
    for key, text in ORTHO_VIEWS:
        fixes, path, ok = "", None, False
        for attempt in range(2):        # one retry with the checker's fix (the editor often flips left/right)
            path = os.path.join(job.dir, "ref_ortho_%s_%d.png" % (key, attempt))
            try:
                job.images.generate(("Show this exact same vehicle from %s. Same vehicle, same colours, markings and materials, same "
                                     "lighting, plain pure white background, sharp focus, nothing else in frame. %s" % (text, fixes)).strip(),
                                    path, model=pricing.edit_model(job.spec), references=[primary], aspect_ratio="1:1")
            except ImageRefused:
                job.log("  the picture editor refused the %s view; seeding from one picture" % key)
                return None
            ok, j = _check(job, path, text)
            ok = bool(ok and j.get("view_matches"))
            if ok:
                break
            fixes = str(j.get("fixes") or "")
            # the editor drew the other side (the F-150 'left' came back as the right side): a vehicle's side is
            # the mirror of the other, so mirror and re-check before paying for another edit
            if key in ("left",) and any(w in fixes.lower() for w in ("flip", "mirror", "right side", "other side", "wrong side")):
                mp = os.path.join(job.dir, "ref_ortho_%s_%d_mirror.png" % (key, attempt))
                ImageOps.mirror(Image.open(path).convert("RGB")).save(mp)
                ok_m, j_m = _check(job, mp, text)
                if ok_m and j_m.get("view_matches"):
                    job.log("  %s view accepted after mirroring" % key)
                    path, ok = mp, True
                    break
        if not ok:
            job.log("  orthographic %s view rejected twice (%s); seeding from one picture" % (key, fixes[:90]))
            return None
        got[key] = path
    asp = {k: _foreground_aspect(p) for k, p in got.items()}
    if None in asp.values() or not (asp["front"] < 0.85 * asp["left"] and asp["back"] < 0.85 * asp["left"]):
        job.log("  orthographic views inconsistent (aspect front %.2f left %.2f back %.2f); seeding from one picture" % (
            asp.get("front") or 0, asp.get("left") or 0, asp.get("back") or 0))
        return None
    right = os.path.join(job.dir, "ref_ortho_right_mirror.png")
    ImageOps.mirror(Image.open(got["left"]).convert("RGB")).save(right)
    job.log("  orthographic front / left / back views accepted; Tripo multiview with four pictures")
    return [got["front"], got["left"], got["back"], right]


def make_reference(job, skill):
    """Returns {"views": [local paths], "pictures": [{"label", "path"}] for the modeller, "checks": [...]}
    with the primary view first."""
    spec = job.spec
    view = skill["meta"]["reference_view"]
    checks, primary, fixes = [], None, ""
    for attempt in range(2):
        path = _make_view(job, skill, view, fixes, attempt)
        ok, j = _check(job, path, view)
        checks.append(j)
        if ok:
            primary = path
            break
        fixes = str(j.get("fixes") or "")
        primary = primary or path
    views = [primary]
    pictures = [{"label": view, "path": primary}]
    seed_views = None
    from ..spec import assembly_wanted
    if spec.multiview and spec.category == "helicopter" and not assembly_wanted(spec):
        # the skill's rule: one three-quarter picture. A helicopter's front view is its rotor disc edge-on, which the
        # checker rejects for "cut-off blade tips" every time (the Apache, 2026-09-24: two edits paid, none accepted)
        # and which would fail the narrower-than-the-side test anyway; a profile grew a second tail rotor (2026-09-17).
        # An assembly cannot be planned without a side and a front view, so it tries them anyway (a rotorless
        # gunship passes; a real helicopter's failed front view leaves seed_views empty and the build says so).
        job.log("  helicopter: seeding from the one three-quarter picture (no orthographic views, per the skill)")
    elif spec.multiview and spec.category in ("vehicle", "aircraft", "helicopter") and not getattr(job, "edit_refused", False):
        seed_views = _orthographic_views(job, skill, primary)
        if seed_views:
            pictures += [{"label": "orthographic front view", "path": seed_views[0]}, {"label": "orthographic left side", "path": seed_views[1]},
                         {"label": "orthographic rear view", "path": seed_views[2]}]
    elif spec.multiview and skill["meta"].get("second_view"):
        # A second consistent view: edit the primary picture rather than generating from text so the
        # object stays the same object. It is checked like the first; a failed one is simply not used.
        job.log("  extra view: %s" % skill["meta"]["second_view"])
        ok2, second = False, os.path.join(job.dir, "ref_view2.png")
        fixes2 = ""
        for attempt2 in range(2):        # an assembly cannot be planned without it: one retry with the checker's fixes
            if getattr(job, "edit_refused", False):
                break
            try:
                job.images.generate(("Show this exact same object %s. Same object, same colours and materials, same lighting, plain "
                                     "pure white background, sharp focus. %s" % (skill["meta"]["second_view"], fixes2)).strip(),
                                    second, model=pricing.edit_model(spec), references=[primary], aspect_ratio="1:1")
                ok2, j2 = _check(job, second, skill["meta"]["second_view"])
                checks.append(j2)
            except ImageRefused:
                job.log("  the picture editor refused the extra view; seeding from one picture")
                break
            if ok2:
                break
            fixes2 = str(j2.get("fixes") or "")
        if ok2:
            views.append(second)
            pictures.append({"label": skill["meta"]["second_view"], "path": second})
            if skill["meta"].get("mirror_as_third_view"):
                third = os.path.join(job.dir, "ref_view3_mirror.png")
                ImageOps.mirror(Image.open(primary).convert("RGB")).save(third)
                views.append(third)
    urls = [job.fal.upload(p) for p in views]
    seed_urls = [job.fal.upload(p) for p in seed_views] if seed_views else None
    result = {"views": views, "urls": urls, "seed_urls": seed_urls, "seed_views": seed_views, "pictures": pictures, "checks": checks,
              "source": "customer" if spec.reference_images else ("research" if getattr(job, "research_photo", None) else "concept"),
              "research_photo": getattr(job, "research_photo", None)}
    with open(os.path.join(job.dir, "reference.json"), "w") as f:
        json.dump(result, f, indent=1)
    return result
