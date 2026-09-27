"""Stage 2: the mesh. Tripo H3.1 with detailed geometry and HD textures; multiview when the reference
stage produced consistent extra views. The vendor mesh IS the asset - nothing downstream sculpts it."""
import os

from .. import config
from ..fal import first_url

# Tripo builds enormous meshes when left to "adaptively determine" the count (2M triangles on a rifle).
# Asking for a few times the delivery budget keeps the detail the normal map needs and the download sane.
SEED_FACE_MULTIPLIER = 6
SEED_FACE_MIN, SEED_FACE_MAX = 150_000, 600_000
MESHY_MAX_POLYCOUNT = 300_000          # fal answers 422 "less than or equal to 300000" above this (wave 17, 2026-09-18)


def hybrid_wanted(spec):
    h = getattr(spec, "hybrid", None)
    if h is None:
        h = config.HYBRID_DEFAULT
    if h is None:
        h = spec.category in config.HYBRID_CATEGORIES
    return bool(h)


def quad_wanted(spec):
    return config.SEED_QUAD if config.SEED_QUAD is not None else spec.category in config.HARD_SURFACE_CATEGORIES


# Which of Hi3D's named slots each of our views fills, by category, for the skill's [primary, second view, mirrored
# primary] list (an orthographic set is always [front, left, back, right]). None = a three-quarter primary with no
# slot of its own; it fills the front only when nothing else does.
HI3D_ROLES = {"weapon": ("left", "front", "right"), "character": ("front", "back"),
              "prop": (None, "front"), "environment": (None, "front"),
              "vehicle": (None, "left"), "aircraft": (None, "left"), "helicopter": (None, "left")}


def hi3d_views(spec, urls):
    roles = ("front", "left", "back", "right") if len(urls) >= 4 else HI3D_ROLES.get(spec.category, (None, "front"))
    slots = {}
    for url, role in zip(urls, roles):
        if role and role not in slots:
            slots[role] = url
    if "front" not in slots:
        slots["front"] = urls[0]
    return {"%s_image_url" % k: v for k, v in slots.items()}


def seed_payload(spec, urls):
    face_limit = max(SEED_FACE_MIN, min(SEED_FACE_MAX, spec.tri_budget * SEED_FACE_MULTIPLIER))
    alt = (getattr(spec, "seed_vendor", None) or os.environ.get("MASTERSMITH_SEED_MODEL", "")).strip().lower()
    if not alt and hybrid_wanted(spec) and config.HYBRID_SEED != "tripo":
        alt = config.HYBRID_SEED          # "meshy7mv" as before; "tripo" keeps Tripo's geometry under the repaint
    if alt == "hitem3d3mv":
        if len(urls) >= 2:
            return config.SEED_HI3D_MULTIVIEW, {**hi3d_views(spec, urls), "model": "hi3dv3.0", "resolution": "2048quality",
                                                "face_count": face_limit, "enable_texture": True, "enable_pbr": True,
                                                "export_format": "glb", "enable_safety_checker": False}
        alt = "hitem3d3"                # one picture: its single-image sibling
    if alt in config.SEED_MULTIVIEW_ALTERNATIVES:
        # a multiview alternative takes the whole view set; with one view it would waste the extra angles, so it
        # falls back to its single-image sibling only when the caller has just one picture
        model = config.SEED_MULTIVIEW_ALTERNATIVES[alt]
        if len(urls) >= 2:
            return model, {"image_urls": urls[:4], "topology": "quad" if quad_wanted(spec) else "triangle",
                           "target_polycount": min(face_limit, MESHY_MAX_POLYCOUNT), "symmetry_mode": "auto", "should_remesh": True,
                           "should_texture": True, "enable_pbr": True}
        alt = "meshy7"
    if alt in config.SEED_MODEL_ALTERNATIVES:
        urls = urls[:1]                 # the alternative vendors take one picture; the primary view is first
        # issue #6: alternative vendors for the hard-surface comparison; not the default until measured
        model = config.SEED_MODEL_ALTERNATIVES[alt]
        if alt == "hitem3d":
            return model, {"image_url": urls[0], "model": "hitem3dv2.1", "resolution": "1536pro", "face_count": face_limit,
                           "enable_texture": True, "enable_pbr": True, "export_format": "glb"}
        if alt == "hitem3d3":
            return model, {"image_url": urls[0], "model": "hi3dv3.0", "resolution": "2048quality", "face_count": face_limit,
                           "enable_texture": True, "enable_pbr": True, "export_format": "glb", "enable_safety_checker": False}
        if alt == "meshy7":
            return model, {"image_url": urls[0], "model_type": "standard", "topology": "quad" if quad_wanted(spec) else "triangle",
                           "target_polycount": min(face_limit, MESHY_MAX_POLYCOUNT), "enable_pbr": True}
    payload = {"geometry_quality": "detailed", "texture_quality": "detailed", "pbr": True, "face_limit": face_limit}
    quad = quad_wanted(spec)
    if quad:
        payload["quad"] = True                      # +$0.05: quad loops decimate and bake cleaner on hard surfaces
    if len(urls) >= 2:
        # Tripo's order is [front, left, back, right] around the object; our list is
        # [primary, second view, mirrored primary], which is exactly front / left / back.
        return config.SEED_MULTIVIEW_MODEL, {**payload, "image_urls": urls[:4]}
    return config.SEED_MODEL, {**payload, "image_url": urls[0]}


def make_seed(job, urls):
    model, payload = seed_payload(job.spec, urls)
    n_views = len(payload.get("image_urls") or [k for k in payload if k.endswith("_image_url")] or [payload.get("image_url")])
    job.log("  seeding with %s (%d view%s)" % (model.split("/")[-3] if model.count("/") >= 2 else model.split("/")[0],
                                             n_views, "" if n_views == 1 else "s"))
    out = job.fal.run(model, payload)
    glb_url = first_url(out, (".glb",)) or first_url(out, (".fbx",))   # Tripo answers quad requests with an FBX only
    if not glb_url:
        raise RuntimeError("the seed vendor returned no mesh")
    ext = ".fbx" if glb_url.split("?")[0].lower().endswith(".fbx") else ".glb"
    path = os.path.join(job.dir, "seed" + ext)
    job.fal.download(glb_url, path)
    job.log("  seed: %.1f MB" % (os.path.getsize(path) / 1e6))
    return {"model": model, "glb": path, "preview": first_url(out, (".png", ".webp", ".jpg"))}
