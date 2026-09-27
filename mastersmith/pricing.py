"""What each paid call costs (USD, per fal's published prices as of 2026-09-16) and the job estimate
shown to the user before anything is spent. THIS TABLE IS THE BILL: an endpoint missing here is refused,
not guessed."""
import os

from . import config

FAL_PRICES = {
    "fal-ai/patina": 0.12,                       # $0.01 + $0.01 per megapixel per map; 5 maps at 2K ~ $0.21 (issue #7)
    "hitem3d/hi3d/v3.0/image-to-3d": 2.10,       # fal: $0.02/credit, 2048quality geometry + texture 10 + pbr 5 = $2.10 (master $9.10)
    "hitem3d/hi3d/v3.0/multi-view-to-3d": 2.10,  # fal: same credits as the single-image endpoint (2026-09-25)
    "fal-ai/hitem3d/image-to-3d": 0.50,          # CALIBRATE: fal shows no fixed price; Hitem3D 1536pro list price (issue #6)
    "fal-ai/meshy/v7/image-to-3d": 0.05,         # inferred from wave 17's measured spend (2026-09-18): the whole wave
                                                 # came in $2.63 UNDER the table with five of these rows at 0.40; the
                                                 # multi-image sibling measured $0.035, so hold both at 0.05
    "fal-ai/meshy/v7/multi-image-to-3d": 0.05,   # MEASURED 2026-09-18 via fal's balance endpoint: $0.034 and $0.037
                                                 # on two 4-view 300k-quad PBR runs. Held at 0.05 as the reservation.
    "fal-ai/birefnet/v2": 0.003,
    # pictures on fal (fal's published per-image prices, 2026-09-24; 2K/4K outputs cost more)
    "fal-ai/nano-banana-2": 0.08, "fal-ai/nano-banana-2/edit": 0.08,
    "fal-ai/nano-banana": 0.04, "fal-ai/nano-banana/edit": 0.04,
    "fal-ai/nano-banana-pro": 0.15, "fal-ai/nano-banana-pro/edit": 0.15,
    "fal-ai/flux-2": 0.02, "fal-ai/flux-2/edit": 0.04, "fal-ai/flux-2-pro": 0.05,
    "tripo3d/h3.1/image-to-3d": 0.30,         # standard; +0.10 HD textures, +0.20 detailed geometry, +0.05 quad
    "tripo3d/h3.1/multiview-to-3d": 0.30,
    "fal-ai/hyper3d/rodin/v2": 0.40,
    "fal-ai/meshy/v5/remesh": 0.20,
    "fal-ai/sam-3/image": 0.005,                 # text-prompted segmentation masks (glass, wheels)
    "fal-ai/hunyuan-3d/v3.1/part": 0.45,         # split a fused mesh (FBX, <=30k faces) into parts
    "fal-ai/meshy/rigging": 0.20,                # humanoid auto-rig from a GLB; +0.12 with enable_animation
    "fal-ai/meshy/rigging/multi-animation": 0.56,
    "tripo3d/tripo/segment": 0.20,               # semantic part split of a GLB (CALIBRATE against the fal dashboard)
}


# Worst-case USD for ONE picture. fal ids (the default: the same account as the meshes) are fal's fixed per-image
# prices at 1K; OpenRouter ids are per-image output token prices as of 2026-09-18, and the reported usage.cost is what
# gets billed there.
IMAGE_PRICES = {
    "fal-ai/nano-banana-2": 0.08, "fal-ai/nano-banana-2/edit": 0.08,
    "fal-ai/nano-banana": 0.04, "fal-ai/nano-banana/edit": 0.04,
    "fal-ai/nano-banana-pro": 0.15, "fal-ai/nano-banana-pro/edit": 0.15,
    "fal-ai/flux-2": 0.02, "fal-ai/flux-2/edit": 0.04, "fal-ai/flux-2-pro": 0.05,
    "google/gemini-3.1-flash-image": 0.08,
    "google/gemini-3.1-flash-image-preview": 0.08,
    "google/gemini-3.1-flash-lite-image": 0.04,
    "google/gemini-2.5-flash-image": 0.04,
    "google/gemini-3-pro-image": 0.16,
    "google/gemini-3-pro-image-preview": 0.16,
    "openai/gpt-5.4-image-2": 0.10,
    "openai/gpt-5-image": 0.12,
    "openai/gpt-5-image-mini": 0.03,
}


class Unpriced(Exception):
    pass


def image_price(model):
    if model not in IMAGE_PRICES:
        raise Unpriced("no price for picture model %s; add it to pricing.IMAGE_PRICES before calling it" % model)
    return IMAGE_PRICES[model]


PICTURE_NAMES = {           # what the providers call them; the ids are what the APIs take
    "fal-ai/nano-banana-2": "Nano Banana 2", "fal-ai/nano-banana": "Nano Banana", "fal-ai/nano-banana-pro": "Nano Banana Pro",
    "fal-ai/flux-2": "FLUX 2", "fal-ai/flux-2-pro": "FLUX 2 Pro",
    "google/gemini-3.1-flash-image": "Nano Banana 2", "google/gemini-3.1-flash-image-preview": "Nano Banana 2 (preview)",
    "google/gemini-3.1-flash-lite-image": "Nano Banana 2 Lite", "google/gemini-2.5-flash-image": "Nano Banana",
    "google/gemini-3-pro-image": "Nano Banana Pro", "google/gemini-3-pro-image-preview": "Nano Banana Pro (preview)",
    "openai/gpt-5.4-image-2": "GPT-5.4 Image 2", "openai/gpt-5-image": "GPT-5 Image", "openai/gpt-5-image-mini": "GPT-5 Image Mini",
}


def picture_catalogue():
    """The picture models a build may pick (Spec.picture_model), with the worst-case price of one picture."""
    out = []
    for mid, usd in IMAGE_PRICES.items():
        if mid.endswith("-preview") or mid.endswith("/edit"):
            continue                                   # preview ids alias the released ones; /edit is derived from the base id
        provider = "fal.ai" if mid.startswith("fal-ai/") else "OpenRouter"
        out.append({"id": mid, "label": "%s · %s (%s) · $%.2f a picture" % (PICTURE_NAMES.get(mid, mid), provider, mid, usd),
                    "name": PICTURE_NAMES.get(mid, mid), "provider": provider, "usd": usd, "default": mid == config.CONCEPT_MODEL})
    return sorted(out, key=lambda r: (not r["default"], r["provider"] != "fal.ai", r["usd"]))


def edit_model(spec=None):
    """The picture model for edits and extra views: the build's choice, else the configured editor."""
    chosen = getattr(spec, "picture_model", None) if spec is not None else None
    return chosen if chosen in IMAGE_PRICES else config.EDIT_MODEL


def concept_model(spec):
    chosen = getattr(spec, "picture_model", None)
    if chosen in IMAGE_PRICES:
        return chosen                                   # the customer's pick wins over the category rules
    model = config.CONCEPT_MODEL_PREMIUM if spec.premium else config.CONCEPT_MODEL
    if spec.category in config.HARD_SURFACE_CATEGORIES and config.CONCEPT_MODEL_HARD:
        model = config.CONCEPT_MODEL_HARD
    return model


def price(model, payload=None):
    if model not in FAL_PRICES:
        raise Unpriced("no price for %s; add it to mastersmith/pricing.py before calling it" % model)
    usd = FAL_PRICES[model]
    payload = payload or {}
    if model.startswith("tripo3d/"):
        if payload.get("texture_quality") == "detailed":
            usd += 0.10
        if payload.get("geometry_quality") == "detailed":
            usd += 0.20
        if payload.get("quad"):
            usd += 0.05                             # quad topology (default for hard-surface categories)
    return round(usd, 4)


# A generous per-call allowance for the director/vision LLM; settled to the real usage.cost after.
LLM_CALL_ALLOWANCE_USD = 0.03


# The mesh vendors a build can pick (Spec.seed_vendor; empty = the default). Shown in the chat's model selector.
SEED_VENDORS = [
    {"key": "tripo", "label": "Tripo H3.1", "model": "tripo3d/h3.1/image-to-3d", "multiview": True,
     "note": "default: full PBR, thin parts survive, seeds from every approved angle"},
    {"key": "meshy7mv", "label": "Meshy v7 multi-image", "model": "fal-ai/meshy/v7/multi-image-to-3d", "multiview": True,
     "note": "sharper geometry from several angles, speckled albedo, about 3x slower"},
    {"key": "meshy7", "label": "Meshy v7", "model": "fal-ai/meshy/v7/image-to-3d", "multiview": False,
     "note": "sharper geometry from one picture, speckled albedo"},
    {"key": "hitem3d", "label": "Hitem3D v2.1", "model": "fal-ai/hitem3d/image-to-3d", "multiview": False,
     "note": "1536-voxel geometry from one picture"},
    {"key": "hitem3d3", "label": "Hitem3D v3 (2048)", "model": "hitem3d/hi3d/v3.0/image-to-3d", "multiview": False,
     "note": "crispest geometry, the best high-poly source for baking, one picture, dear"},
    {"key": "hitem3d3mv", "label": "Hitem3D v3 multi-view (2048)", "model": config.SEED_HI3D_MULTIVIEW, "multiview": True,
     "note": "the same crisp geometry seeded from every approved angle (front, sides, back), same price"},
]


def seed_vendor(spec):
    """The catalogue row a spec's seed will come from."""
    key = (getattr(spec, "seed_vendor", None) or os.environ.get("MASTERSMITH_SEED_MODEL", "") or "tripo").strip().lower()
    return next((v for v in SEED_VENDORS if v["key"] == key), SEED_VENDORS[0])


def vendor_catalogue():
    out = []
    for v in SEED_VENDORS:
        try:
            usd = price(v["model"], {"texture_quality": "detailed", "geometry_quality": "detailed"})
        except Unpriced:
            usd = None
        out.append({**v, "usd": usd})
    return out


# Assembly builds: what the builder model's calls and the vendor parts cost at most. Settled to actual usage.cost.
BUILDER_CALL_USD = 0.30             # one builder call with pictures (plan, big part code, checks)
BUILDER_SMALL_CALL_USD = 0.08       # one call of the cheaper builder for a small part
ASSEMBLY_CODE_PARTS = 10            # the worst case reserves this many code parts at 2.5 builder calls each
ASSEMBLY_BIG_PARTS = 3              # of which this many are big enough for the main builder
ASSEMBLY_VENDOR_PARTS = 3


def estimate_assembly(spec):
    """Worst case of the parts path after the pictures: plan, code parts, vendor parts, assembly and its checks."""
    vendor = seed_vendor(spec)
    part_seed = FAL_PRICES["hitem3d/hi3d/v3.0/image-to-3d"] if vendor["key"].startswith("hitem3d3") else \
        price(config.SEED_MODEL, {"geometry_quality": "detailed", "texture_quality": "detailed"})
    return [
        ("parts plan from the approved pictures (builder)", 2 * BUILDER_CALL_USD),
        ("code parts: modelled, built and self-checked (builder, up to %d parts)" % ASSEMBLY_CODE_PARTS,
         2.5 * (ASSEMBLY_BIG_PARTS * BUILDER_CALL_USD + (ASSEMBLY_CODE_PARTS - ASSEMBLY_BIG_PARTS) * BUILDER_SMALL_CALL_USD)),
        ("vendor parts: drawn alone and seeded (up to %d, %s)" % (ASSEMBLY_VENDOR_PARTS, vendor["label"] if vendor["key"].startswith("hitem3d3") else "Tripo H3.1"),
         ASSEMBLY_VENDOR_PARTS * (image_price(edit_model(spec)) + 2 * LLM_CALL_ALLOWANCE_USD + part_seed)),
        ("assembly checks against the pictures (builder)", config.ASSEMBLY_CHECK_ROUNDS * BUILDER_CALL_USD),
        ("Blender assembly: place, bake one atlas, LODs, collision, FBX/GLB", 0.0),
    ]


def estimate(spec):
    """Worst-case USD for one build of `spec`, step by step. Reserved up front, settled to actual."""
    from .spec import assembly_wanted
    if assembly_wanted(spec):
        steps = [s for s in _estimate_steps(spec) if s[0].startswith(REFERENCE_STEPS)]
        steps += estimate_assembly(spec)
        steps.append(("review the result against the picture (vision)", LLM_CALL_ALLOWANCE_USD))
        steps.append(("director chat overhead", 2 * LLM_CALL_ALLOWANCE_USD))
        total = round(sum(u for _, u in steps), 4)
        return {"steps": steps, "usd": total, "credits": config.credits_for_usd(total), "build_mode": "assembly"}
    steps = _estimate_steps(spec)
    total = round(sum(u for _, u in steps), 4)
    return {"steps": steps, "usd": total, "credits": config.credits_for_usd(total), "build_mode": "single"}


def _estimate_steps(spec):
    steps = []
    seed_payload = {"texture_quality": "detailed", "geometry_quality": "detailed"}
    quad = config.SEED_QUAD if config.SEED_QUAD is not None else spec.category in config.HARD_SURFACE_CATEGORIES
    if quad:
        seed_payload["quad"] = True
    if spec.reference_images:
        steps.append(("clean up your reference picture(s)", image_price(edit_model(spec))))
    elif spec.research and spec.search_query:
        steps.append(("find a photo of %s on the web and clean it up" % spec.search_query[:40], image_price(edit_model(spec)) + LLM_CALL_ALLOWANCE_USD))
    else:
        steps.append(("concept picture", image_price(concept_model(spec))))
    steps.append(("check the picture (vision)", LLM_CALL_ALLOWANCE_USD))
    steps.append(("second picture attempt if the first fails", image_price(edit_model(spec)) + LLM_CALL_ALLOWANCE_USD))
    vendor = seed_vendor(spec)
    if spec.multiview:
        steps.append(("extra views for multiview seeding", 3 * (image_price(edit_model(spec)) + LLM_CALL_ALLOWANCE_USD)))
    if vendor["key"] == "tripo":
        steps.append(("3D seed (Tripo H3.1%s, detailed)" % (" multiview" if spec.multiview else ""),
                      price(config.SEED_MULTIVIEW_MODEL if spec.multiview else config.SEED_MODEL, seed_payload)))
    else:
        steps.append(("3D seed (%s)" % vendor["label"], price(vendor["model"], seed_payload)))
    steps.append(("which end is the front (vision)", LLM_CALL_ALLOWANCE_USD))
    if spec.category == "environment" and spec.style == "realistic":
        steps.append(("tiling PBR material set from the reference (Patina)", price("fal-ai/patina")))
    if spec.glass:
        steps.append(("glass masks, 5 views (SAM 3)", 5 * price("fal-ai/sam-3/image")))
    if spec.rig and spec.category == "vehicle":
        steps.append(("wheel masks, 3 views (SAM 3)", 3 * price("fal-ai/sam-3/image")))
    if spec.rig and spec.category == "vehicle":
        steps.append(("part split for wheel bones (Hunyuan)", price("fal-ai/hunyuan-3d/v3.1/part")))
    if spec.rig and spec.category == "character":
        steps.append(("humanoid auto-rig with walk/run (Meshy)", price("fal-ai/meshy/rigging")))
    steps.append(("Blender finish: decimate, orient, scale, LODs, collision, maps, FBX/GLB", 0.0))
    steps.append(("review the result against the picture (vision)", LLM_CALL_ALLOWANCE_USD))
    steps.append(("director chat overhead", 2 * LLM_CALL_ALLOWANCE_USD))
    return steps


SEED_STEPS = ("3D seed", "extra views for multiview seeding")
PICTURE_STEPS = ("concept picture", "clean up your reference picture", "find a photo of", "check the picture",
                 "second picture attempt")


REFERENCE_STEPS = PICTURE_STEPS + ("extra views for multiview seeding",)


def estimate_reference(spec):
    """Worst case of the picture stage alone: the reference picture(s) the customer approves before a mesh is bought."""
    est = estimate(spec)
    steps = [(n, u) for n, u in est["steps"] if n.startswith(REFERENCE_STEPS) or n == "director chat overhead"]
    usd = sum(u for _, u in steps)
    return {"steps": steps, "usd": round(usd, 4), "credits": config.credits_for_usd(usd)}


def estimate_after_reference(spec):
    """Worst case of a build that reuses approved reference pictures: everything but the picture stage."""
    est = estimate(spec)
    steps = [(n, u) for n, u in est["steps"] if not n.startswith(REFERENCE_STEPS)]
    usd = sum(u for _, u in steps)
    return {"steps": steps, "usd": round(usd, 4), "credits": config.credits_for_usd(usd)}


def estimate_rework(spec, mode="refinish"):
    """Worst case of finishing an EXISTING mesh: no seed and no reference pictures, only the probe, masks, rig and
    review calls of the finish."""
    steps = [(name, usd) for name, usd in _estimate_steps(spec)       # a rework finishes one mesh, never an assembly
             if not name.startswith(SEED_STEPS) and not name.startswith(PICTURE_STEPS)]
    usd = sum(u for _, u in steps)
    return {"steps": steps, "usd": round(usd, 4), "credits": config.credits_for_usd(usd)}
