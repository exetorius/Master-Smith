"""Environment, paths and model routing. Keys come from <repo>/.env (FAL_KEY, OPENROUTER_API_KEY)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "mastersmith" / "skills"
BLENDER_SCRIPT = ROOT / "mastersmith" / "blender" / "finish.py"


def load_env(path=None):
    """KEY=VALUE lines into os.environ; variables already set win. Never prints values."""
    path = Path(path or ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# Everything the service writes lives under one directory: builds (out/), uploads and the SQLite ledger. The Docker
# image points MASTERSMITH_DATA at a volume; locally it is the repository.
DATA_DIR = Path(os.environ.get("MASTERSMITH_DATA") or ROOT).resolve()
OUT_DIR = DATA_DIR / "out"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "mastersmith.db"

BLENDER_BIN = os.environ.get("BLENDER_BIN", r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")
# Every Blender run: headless, and never the scripts a .blend carries (-Y), whatever the user's "Auto Run Python
# Scripts" preference says - customers hand us .blend files.
BLENDER_FLAGS = ["-b", "-Y"]

# A fixed API key for scripts and agents, straight from .env: any string you make up. Requests then carry it as a
# bearer token or X-API-Key. With no key configured, every request is the local user: this is one person's tool.
API_KEY = os.environ.get("MASTERSMITH_API_KEY", "").strip()
API_USER = os.environ.get("MASTERSMITH_API_USER", "agent").strip() or "agent"
# The key older copies of .env.example shipped: known to everyone, so the service refuses to start with it.
PLACEHOLDER_API_KEY = "ms_dev_change_me"
# Browser origins that may call the API directly. The chat's own requests go through its Next.js server, so this only
# matters for a page you build yourself; any other website open in your browser is refused.
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "MASTERSMITH_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()]

# --- LLMs (OpenRouter ids). The director runs the chat and decides; it is cheap on purpose.
DIRECTOR_MODEL = os.environ.get("MASTERSMITH_DIRECTOR_MODEL", "google/gemini-3.8-flash")
VISION_MODEL = os.environ.get("MASTERSMITH_VISION_MODEL", "google/gemini-3.8-flash")
PREMIUM_MODEL = os.environ.get("MASTERSMITH_PREMIUM_MODEL", "anthropic/claude-sonnet-5")
# Assembly builds (docs/ASSEMBLY.md): the builder plans the parts, writes the code parts and checks the assembly.
BUILDER_MODEL = os.environ.get("MASTERSMITH_BUILDER_MODEL", "anthropic/claude-opus-5.5")
# small parts (pins, levers, sights) are written by a cheaper model: the builder calls were 97% of an assembly's cost
BUILDER_MODEL_SMALL = os.environ.get("MASTERSMITH_BUILDER_MODEL_SMALL", "anthropic/claude-sonnet-5")
# Assembly is opt-in until it beats one seed on the same object (owner, 2026-09-27): "1" makes it the hard-surface default
ASSEMBLY_DEFAULT = os.environ.get("MASTERSMITH_ASSEMBLY_DEFAULT", "0") == "1"
ASSEMBLY_MAX_PARTS = int(os.environ.get("MASTERSMITH_ASSEMBLY_MAX_PARTS", "24"))
ASSEMBLY_WORKERS = int(os.environ.get("MASTERSMITH_ASSEMBLY_WORKERS", "4"))
ASSEMBLY_CHECK_ROUNDS = int(os.environ.get("MASTERSMITH_ASSEMBLY_CHECK_ROUNDS", "2"))
# The director models the chat offers (OpenRouter ids; every one must support tools). DIRECTOR_MODEL is the default
# and always listed first; the UI can also show every tool-and-vision-capable model OpenRouter serves.
DIRECTOR_MODELS = [m.strip() for m in os.environ.get(
    "MASTERSMITH_DIRECTOR_MODELS",
    "deepseek/deepseek-v4.1-flash,openai/gpt-6-luna,z-ai/glm-5.3-flash,openai/gpt-6-sol,meta/muse-spark-1.3-contributor,meta/muse-spark-1.3,anthropic/claude-sonnet-5,anthropic/claude-opus-5.5"
).split(",") if m.strip()]

# --- fal endpoints by role. One vendor per role; the 2026-09-16 bake-off picked these.
# --- pictures come from fal too (fal-ai/nano-banana-2 and friends; a fal id with reference pictures runs the /edit
# endpoint), so one account covers pictures and meshes. OpenRouter image ids (google/gemini-3.1-flash-image, ...) still
# work anywhere a picture model is named. Ids and prices in pricing.IMAGE_PRICES; every one has a MASTERSMITH_* override.
CONCEPT_MODEL = os.environ.get("MASTERSMITH_CONCEPT_MODEL", "fal-ai/nano-banana-2")        # text -> clean product shot
CONCEPT_MODEL_PREMIUM = os.environ.get("MASTERSMITH_CONCEPT_PREMIUM", "fal-ai/nano-banana-pro")
# Hard-surface categories get a stronger picture model: the seed reproduces every 2D error as geometry, and
# the cheap model hallucinates extra fine parts on weapons and vehicles (issue #8). Set MASTERSMITH_CONCEPT_HARD to override.
CONCEPT_MODEL_HARD = os.environ.get("MASTERSMITH_CONCEPT_HARD", "fal-ai/nano-banana-pro")
IMAGE_RESOLUTION = os.environ.get("MASTERSMITH_IMAGE_RESOLUTION", "1K")
HARD_SURFACE_CATEGORIES = ("weapon", "vehicle", "aircraft", "helicopter")
# Alternative seed vendors for the hard-surface comparison of issue #6; MASTERSMITH_SEED_MODEL overrides the default.
SEED_MODEL_ALTERNATIVES = {"hitem3d": "fal-ai/hitem3d/image-to-3d", "meshy7": "fal-ai/meshy/v7/image-to-3d",
                           "hitem3d3": "hitem3d/hi3d/v3.0/image-to-3d"}   # issue #6: the 2048-voxel model, on fal since 2026-09
# Multiview alternatives: these take the SAME checked view set as Tripo multiview. Meshy v7 multi-image asks only for
# "1 to 4 images of the same object from different angles" - no fixed [front, left, back, right] order (issue #6).
SEED_MULTIVIEW_ALTERNATIVES = {"meshy7mv": "fal-ai/meshy/v7/multi-image-to-3d"}
# Hi3D v3 multi-view (2026-09-25): the same 2048-voxel model fed named front / back / left / right pictures instead of one.
# Same price as its single-image sibling; every slot is optional, so our [primary, second view, mirror] set fits it.
SEED_HI3D_MULTIVIEW = "hitem3d/hi3d/v3.0/multi-view-to-3d"
# Tripo quad topology (+$0.05, answers FBX): on the Glock photo it kept the slide text legible and scored the best edge
# energy of four vendors (issue #6, 2026-09-17). Default for hard-surface categories; MASTERSMITH_SEED_QUAD=0/1 forces it.
_quad_env = os.environ.get("MASTERSMITH_SEED_QUAD", "")
SEED_QUAD = _quad_env == "1" if _quad_env in ("0", "1") else None   # None -> by category
EDIT_MODEL = os.environ.get("MASTERSMITH_EDIT_MODEL", "fal-ai/nano-banana-2")   # photo -> clean profile / other view (its /edit endpoint)
CUTOUT_MODEL = "fal-ai/birefnet/v2"             # background removal, $0.003
# 4x upscale of the plan pictures, framing unchanged: a trigger is ~100 px in a 1200 px reference (assemblies)
UPSCALE_MODEL = os.environ.get("MASTERSMITH_UPSCALE_MODEL", "fal-ai/esrgan")
SEED_MODEL = "tripo3d/h3.1/image-to-3d"         # full PBR, thin parts survive
SEED_MULTIVIEW_MODEL = "tripo3d/h3.1/multiview-to-3d"
SEED_ALT_MODEL = "fal-ai/hyper3d/rodin/v2"      # several references at once; refuses military subjects

# --- the free tier: models on this PC (mastersmith/local.py). Pick picture_model "local/flux2-klein-4b" and seed_vendor
# "local" for a build; the ids below route there and cost $0. The folder holds ComfyUI (FLUX.2 klein 4B) and trellis.cpp
# (TRELLIS.2 GGUF): see its README. ComfyUI is started on first use when it is not already running.
LOCAL_MODELS_DIR = Path(os.environ.get("MASTERSMITH_LOCAL_MODELS_DIR", r"E:\local-models"))
LOCAL_COMFY_URL = os.environ.get("MASTERSMITH_LOCAL_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
LOCAL_PICTURE_MODEL = "local/flux2-klein-4b"
LOCAL_SEED_MODEL = "local/trellis2"
LOCAL_TRELLIS_RES = int(os.environ.get("MASTERSMITH_LOCAL_TRELLIS_RES", "1024"))       # 512 is ~2x faster and softer
LOCAL_TRELLIS_QUANT = os.environ.get("MASTERSMITH_LOCAL_TRELLIS_QUANT", "q8")           # q8 fits 12 GB; "" = f16, "q4"
# texture volume: "" lets trellis.cpp pick (512 at res 1024), "1024" is sharper (a stippled grip survives) and ~3x
# slower (485 s against 146 s on the pistol, 2026-09-27); the atlas then goes to 4096
LOCAL_TRELLIS_TEX_RES = os.environ.get("MASTERSMITH_LOCAL_TRELLIS_TEX_RES", "")

# --- spend. You run this against your own fal and OpenRouter keys; the ledger only keeps score of what they spent,
# in cents. Nothing is charged, held or refused here.
CREDIT_USD = 0.01                                # one credit is one cent


def credits_for_usd(usd):
    """Provider cost -> cents, rounded up."""
    import math
    return int(math.ceil(usd / CREDIT_USD))
