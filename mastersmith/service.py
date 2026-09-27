"""HTTP service: chat with the director, upload pictures and models, queue builds, poll jobs, fetch files, see the spend.
    python -m mastersmith serve --port 8080
Auth: one person's tool. With MASTERSMITH_API_KEY set, requests carry it (bearer or X-API-Key); with no key
configured, every request is the local user.
The worker thread runs inside this process unless MASTERSMITH_NO_WORKER=1 (then run `python -m mastersmith worker`)."""
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import uuid

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import config, pricing, providers
from .agent import Director
from .llm import LLMError
from .pipeline import MESH_EXTENSIONS, seed_of
from .spec import Spec
from .store import Store
from .wallet import Wallet
from .worker import Worker, new_job_id

app = FastAPI(title="Master Smith", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])
store = Store()
wallet = Wallet()
_lock = threading.Lock()
_sessions = {}          # session_id -> {"director": Director, "user": str, "touched": float}
SESSION_TTL = 6 * 3600
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".jfif", ".jpe", ".bmp", ".gif", ".tif", ".tiff", ".avif", ".heic")
NATIVE_IMAGE = (".png", ".jpg", ".jpeg")      # anything else is re-saved as PNG so every stage and vendor reads it
LOCAL_USER = {"user": "local"}


@app.on_event("startup")
def _start():
    if config.API_KEY == config.PLACEHOLDER_API_KEY:
        raise RuntimeError("MASTERSMITH_API_KEY is still the example value %r, which everyone knows. Put a key of your own in "
                           ".env (python -c \"import secrets; print(secrets.token_urlsafe(24))\") or leave it empty."
                           % config.PLACEHOLDER_API_KEY)
    os.makedirs(config.UPLOADS_DIR, exist_ok=True)
    if os.environ.get("MASTERSMITH_NO_WORKER") != "1":
        Worker(store, wallet).start()


# ------------------------------------------------------------------ auth
def auth(authorization: str = Header(default=""), x_api_key: str = Header(default="")):
    key = authorization[7:] if authorization.lower().startswith("bearer ") else (x_api_key or "")
    if not config.API_KEY:
        return dict(LOCAL_USER)             # no key configured: this is one person's machine
    if hmac.compare_digest(key.encode(), config.API_KEY.encode()):
        return {"user": config.API_USER}    # the fixed key from .env (scripts, agents, the web app)
    raise HTTPException(401, "wrong or missing API key (Authorization: Bearer <key> or X-API-Key)")


# ------------------------------------------------------------------ models
class Attachment(BaseModel):
    path: str
    name: str = ""
    kind: str = ""          # "image" | "mesh" (filled in by /v1/uploads)


class ChatIn(BaseModel):
    message: str
    session_id: str = "default"
    attachments: list[Attachment] = []
    settings: dict = {}     # per-session choices from the UI: {"seed_vendor": "tripo"|"meshy7mv"|..., "director_model": "<openrouter id>"}


class JobIn(BaseModel):
    spec: dict


class ImportIn(BaseModel):
    path: str               # a path returned by /v1/uploads
    spec: dict = {}         # name, category, size_m (omit to keep the file's size), tri_budget, glass, rig, engine


class RefinishIn(BaseModel):
    source_job: str
    overrides: dict = {}


# ------------------------------------------------------------------ helpers
def _inside(path, roots, want_dir=False):
    """`path` resolves (symlinks and .. included) to a file, or a directory with want_dir, under one of `roots`."""
    try:
        real = os.path.normcase(os.path.realpath(path))
    except (TypeError, ValueError):
        return False
    if not (os.path.isdir(real) if want_dir else os.path.isfile(real)):
        return False
    return any(real.startswith(os.path.normcase(os.path.realpath(r)) + os.sep) for r in roots)


def _upload_path_ok(path):
    """Only files under the uploads directory (or a finished job's output) may seed a job."""
    return _inside(path, (config.UPLOADS_DIR, config.OUT_DIR))


def _foreign_paths(spec_dict):
    """Local paths in a brief that point outside the uploads and job folders. The pipeline copies a brief's pictures
    into the job and uploads them to fal's CDN and hands its part meshes to Blender, so a brief from the API or the
    director may only name files this service stored (URLs stay allowed for pictures). The CLI passes local photos
    straight to the pipeline and is not checked here."""
    d = spec_dict or {}
    bad = []
    pictures = [d.get("reference_image")] + list(d.get("reference_images") or [])
    for p in d.get("add_parts") or []:
        if isinstance(p, dict):
            pictures.append(p.get("picture"))
            if p.get("seed") and not _upload_path_ok(p["seed"]):
                bad.append(str(p["seed"]))
    for pic in pictures:
        if not pic or (isinstance(pic, str) and pic.startswith(("http://", "https://"))):
            continue
        if not isinstance(pic, str) or not _upload_path_ok(pic):
            bad.append(str(pic))
    if d.get("reference_job") and not _inside(d["reference_job"], (config.OUT_DIR,), want_dir=True):
        bad.append(str(d["reference_job"]))
    return bad


def _refuse_paths(bad):
    return {"error": "the brief names files outside the uploads and job folders: %s; attach files through /v1/uploads"
                     % ", ".join(b[:120] for b in bad[:4]), "status": "rejected", "bad_paths": bad}


def last_seed(user, job_id):
    """The mesh and reference picture of a finished job, for a chat that goes on changing the same model."""
    if not job_id:
        return None
    row = store.job(job_id, user)
    if not row or row["status"] != "done":
        return None
    r = row.get("result") or {}
    glb = seed_of(r)
    if not glb:
        return None
    ref = ((r.get("reference") or {}).get("views") or [None])[0]
    return {"glb": glb, "ref": ref if ref and os.path.exists(ref) else None, "spec": r.get("spec") or {}}


def _enqueue(user, spec, kind, source=None):
    if kind in ("refinish", "rework"):     # the seed is reused; only the finishing calls are held
        mode = (source or {}).get("mode") if isinstance(source, dict) else "refinish"
        est = pricing.estimate_rework(spec, mode)
    else:
        est = pricing.estimate(spec)
    credits = est["credits"]
    bal = wallet.balance(user)
    try:
        providers.check_affordable(est["usd"])      # a known provider balance below the worst case stops it here
    except providers.ProviderBalanceLow as exc:
        return {"error": "provider balance too low: %s" % exc, "needed_usd": est["usd"], "providers": exc.data}
    job_id = new_job_id()
    store.enqueue(job_id, user, kind, spec.to_dict(), source_job=json.dumps(source) if isinstance(source, dict) else source)
    return {"job_id": job_id, "status": "queued", "kind": kind, "estimate_credits": credits, "balance": bal}


def run_removal_preview(user, source_dir, spec):
    """Red-on-render pictures of what remove_parts would delete, registered as a finished job so they are served."""
    from .pipeline import preview_removal_job
    from .providers import ProviderBalanceLow
    job_id = new_job_id()
    store.enqueue(job_id, user, "removal_preview", spec.to_dict(), source_job=source_dir)
    store.mark_running(job_id)
    try:
        r = preview_removal_job(source_dir, spec, user, wallet, log=lambda m: store.append_log(job_id, m), job_id=job_id)
    except ProviderBalanceLow as exc:
        store.finish(job_id, "refused", error=str(exc))
        return {"job_id": job_id, "status": "refused", "error": str(exc)}
    store.finish(job_id, r["status"], result=r, error=r.get("error"))
    pics = [{"label": p["label"], "url": "/v1/jobs/%s/files/%s" % (job_id, os.path.basename(p["path"])), "coverage": p.get("coverage")}
            for p in (r.get("pictures") or [])]
    return {"job_id": job_id, "status": "preview_removal" if r["status"] == "done" else r["status"], "error": r.get("error"),
            "pictures": pics, "usd_cost": (r.get("bill") or {}).get("usd_cost"),
            "next": ("The red areas are what will be deleted. Ask the customer to confirm, then call build with "
                     "confirm_removal=true; if the red covers the wrong thing, reword remove_parts (or drop it) and try again.")}


def _reuse_part_seeds(user, spec):
    """An added part whose mesh an earlier finished job of this user already bought (same name and phrase) keeps that
    mesh: a re-finish changes the fit, the budget or the body, not the $0.65 seed (the Havoc interior, 2026-09-24)."""
    open_parts = [p for p in (spec.add_parts or []) if not p.get("seed")]
    if not open_parts:
        return spec
    for row in store.jobs_for(user, limit=60):
        if row["status"] != "done":
            continue
        full = store.job(row["id"], user) or {}
        d = ((full.get("result") or {}).get("dir")) or ""
        prev = {(p.get("name"), p.get("phrase")): p for p in ((full.get("spec") or {}).get("add_parts") or [])}
        for p in open_parts:
            if p.get("seed") or (p["name"], p["phrase"]) not in prev:
                continue
            for ext in (".fbx", ".glb"):
                path = os.path.join(d, "part_%s_seed%s" % (p["name"], ext))
                if d and os.path.exists(path):
                    p["seed"] = path
                    pic = os.path.join(d, "part_%s_ref_0.png" % p["name"])
                    p["picture"] = p.get("picture") or (pic if os.path.exists(pic) else None)
                    break
        if all(p.get("seed") for p in open_parts):
            break
    return spec


def submit_build(user, spec_dict, seed=None, confirm_removal=False):
    """Queue a build. With `seed` (the session's current model): a repaint keeps the mesh, a change that keeps the
    shape re-finishes it, and a change of shape edits the previous picture rather than redrawing from the text.
    New remove_parts are previewed (red on the renders) and queued only once confirmed."""
    bad = _foreign_paths(spec_dict)
    if bad:
        return _refuse_paths(bad)
    spec = Spec.from_dict(spec_dict)
    from .spec import assembly_wanted
    if seed and assembly_wanted(spec) and not spec.remove_parts:
        # an assembly is built from the approved pictures, never by re-finishing the chat's previous mesh
        return _enqueue(user, spec, "build")
    if seed:
        same_shape = all(spec_dict.get(k) == seed["spec"].get(k) for k in ("description", "category", "style"))
        if (spec.seed_vendor or "tripo") != (seed["spec"].get("seed_vendor") or "tripo"):
            same_shape = False                      # another mesh vendor is another mesh: seed again from the same pictures
        new_removals = [p for p in spec.remove_parts if p not in (seed["spec"].get("remove_parts") or [])]
        if new_removals and not confirm_removal:
            source_dir = os.path.dirname(os.path.dirname(seed["glb"])) if os.path.basename(os.path.dirname(seed["glb"])) == "work" \
                else os.path.dirname(seed["glb"])
            return run_removal_preview(user, source_dir, spec)
        _reuse_part_seeds(user, spec)
        if spec.retexture:
            return _enqueue(user, spec, "rework", {"seed": seed["glb"], "ref": seed["ref"], "mode": "retexture"})
        if same_shape:
            return _enqueue(user, spec, "rework", {"seed": seed["glb"], "ref": seed["ref"], "mode": "refinish"})
        if seed["ref"] and not spec.reference_images:
            spec.reference_images, spec.reference_image = [seed["ref"]], seed["ref"]
            spec.research, spec.search_query = False, ""
            if not spec.edit_instructions:
                spec.edit_instructions = "make it match this description: " + spec.description[:400]
    return _enqueue(user, spec, "build")


def run_reference(user, spec_dict):
    """Stage 1 now, in this request: the reference picture(s) for approval. Registered as a finished job of kind
    "reference" so its pictures are served like any job's files. Tens of seconds; no Blender, no mesh."""
    from .pipeline import make_reference_only
    from .providers import ProviderBalanceLow
    bad = _foreign_paths({**spec_dict, "reference_job": None})
    if bad:
        return _refuse_paths(bad)
    spec = Spec.from_dict({**spec_dict, "reference_job": None})
    job_id = new_job_id()
    store.enqueue(job_id, user, "reference", spec.to_dict())
    store.mark_running(job_id)
    try:
        r = make_reference_only(spec, user, wallet, log=lambda m: store.append_log(job_id, m), job_id=job_id)
    except ProviderBalanceLow as exc:
        store.finish(job_id, "refused", error=str(exc))
        return {"job_id": job_id, "status": "refused", "error": str(exc)}
    store.finish(job_id, r["status"], result=r, error=r.get("error"))
    ref = r.get("reference") or {}
    views = ref.get("views") or []
    # every angle the build will use, labelled: the primary view, then the orthographic or second views
    labelled = ref.get("pictures") or [{"label": "reference", "path": v} for v in views]
    return {"job_id": job_id, "dir": r["dir"], "status": r["status"], "error": r.get("error"), "views": views,
            "pictures": [{"label": p["label"], "url": "/v1/jobs/%s/files/%s" % (job_id, os.path.basename(p["path"]))}
                         for p in labelled if p.get("path") and os.path.exists(p["path"])],
            "checks": (r.get("reference") or {}).get("checks"), "source": (r.get("reference") or {}).get("source"),
            "usd_cost": (r.get("bill") or {}).get("usd_cost")}


def submit_import(user, path, spec_dict):
    if not _upload_path_ok(path) or not path.lower().endswith(MESH_EXTENSIONS):
        return {"error": "not an uploaded model file: %s" % path}
    bad = _foreign_paths(spec_dict)
    if bad:
        return _refuse_paths(bad)
    d = dict(spec_dict or {})
    d.setdefault("name", re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(os.path.basename(path))[0]) or "Imported")
    d.setdefault("description", "imported model")
    d["size_m"] = float(d.get("size_m") or 0) or -1
    return _enqueue(user, Spec.from_dict(d), "rework", {"seed": path, "ref": None, "mode": "refinish"})


def job_view(row, user):
    from .quality import assess
    from .diagnose import diagnose
    r = row.get("result") or {}
    delivery = r.get("delivery") or {}
    quality = assess(Spec.from_dict(row["spec"]), delivery, r.get("review"))
    # Older jobs stored "usable" even when their reviewer said rebuild. Do not feed that stale conclusion
    # back to the director after an upgrade; preserve the historical files, correct the live status view.
    diagnosis = diagnose(r, os.path.join(r.get("dir") or "", "work"), Spec.from_dict(row["spec"])) if r else []
    if not quality["accepted"]:
        diagnosis = [d for d in diagnosis if not d.get("finding", "").startswith("reviewer ")]
        diagnosis.insert(0, {"finding": "not accepted: " + "; ".join(quality["issues"]),
                             "remedy": "inspect the requested repair; technical completion is not visual acceptance",
                             "fix": None})
    gate = dict(r.get("gate") or {})
    if gate:
        gate["technical_ok"] = gate.get("technical_ok", bool(gate.get("ok")))
        gate["quality"] = quality
        gate["warnings"] = list(dict.fromkeys((gate.get("warnings") or []) + quality["issues"]))
        gate["ok"] = bool(gate.get("ok")) and quality["accepted"]
    files = []
    if r.get("delivery_dir") and os.path.isdir(r["delivery_dir"]):
        files = sorted(os.listdir(r["delivery_dir"]))
    return {"id": row["id"], "status": row["status"], "kind": row["kind"], "spec": row["spec"],
            "created": row["created"], "started": row["started"], "finished": row["finished"], "error": row["error"],
            "log": row["log"].splitlines()[-40:],
            "summary": {"lods": delivery.get("lods"), "dimensions_m": delivery.get("dimensions_m"),
                        "glass": delivery.get("glass"), "materials": delivery.get("materials"),
                        "review": r.get("review"), "gate": gate or None, "package": r.get("package"),
                        "diagnosis": diagnosis, "quality": quality,
                        "added_parts": delivery.get("added_parts"), "cockpit": delivery.get("cockpit"),
                        "cabin_lining": delivery.get("cabin_lining"), "review_renders": delivery.get("review_renders"),
                        "source_renders": delivery.get("source_renders"), "inspection_renders": delivery.get("inspection_renders"),
                        "bake": delivery.get("bake"),
                        "rig": {k: v for k, v in (r.get("rig") or {}).items() if k != "notes"},
                        "bill": {k: v for k, v in (r.get("bill") or {}).items() if k not in ("fal_calls", "llm_calls", "image_calls")}},
            "files": ["/v1/jobs/%s/files/%s" % (row["id"], f) for f in files],
            "previews": ["/v1/jobs/%s/files/%s" % (row["id"], f) for f in files if f.startswith(("preview_", "ref_"))],
            "pictures": job_pictures(row),
            "glb": next(("/v1/jobs/%s/files/%s" % (row["id"], f) for f in files if f.lower().endswith(".glb") and f.startswith("SM_")), None)}


def _session(session_id, user):
    """The live session for a chat: in memory while it is warm, rebuilt from its folder on disk otherwise."""
    with _lock:
        now = time.time()
        for sid in [s for s, v in _sessions.items() if now - v["touched"] > SESSION_TTL]:
            _sessions.pop(sid, None)
        key = "%s:%s" % (user, session_id)
        if key not in _sessions:
            d = Director(user, wallet, log=lambda m: None)
            sess = {"director": d, "user": user, "touched": now, "settings": {}}
            state = load_chat(user, session_id)
            if state:
                # the transcript comes back behind a FRESH system prompt, so rule changes reach old chats too
                d.messages = d.messages[:1] + [m for m in (state.get("messages") or []) if m.get("role") != "system"]
                d.spec = Spec.from_dict(state["spec"]) if state.get("spec") else None
                d.reference = state.get("reference") or None
                d.last_job_id = state.get("last_job_id")
                d.model = state.get("model") or d.model
                sess["settings"] = dict(state.get("settings") or {})

            def with_settings(spec_dict):
                """The session's model choices ride on every spec the director sends, unless the brief names one."""
                sv = sess["settings"].get("seed_vendor")
                pm = sess["settings"].get("picture_model")
                return {**spec_dict, "seed_vendor": spec_dict.get("seed_vendor") or (sv if sv and sv != "tripo" else None),
                        "picture_model": spec_dict.get("picture_model") or (pm if pm and pm != config.CONCEPT_MODEL else None)}

            d.submit = lambda spec_dict, confirm_removal=False: submit_build(
                user, with_settings(spec_dict), seed=last_seed(user, d.last_job_id), confirm_removal=confirm_removal)
            d.import_model = lambda path, spec_dict: submit_import(user, path, spec_dict)
            d.make_reference = lambda spec_dict: run_reference(user, with_settings(spec_dict))
            d.job_status = lambda job_id: (lambda row: job_view(row, user) if row else {"error": "unknown job"})(store.job(job_id, user))
            _sessions[key] = sess
        _sessions[key]["touched"] = now
        return _sessions[key]


def _director(session_id, user):
    return _session(session_id, user)["director"]


def _session_settings(session_id, user):
    return _session(session_id, user)["settings"]


# ------------------------------------------------------------------ chats on disk
# Every chat is a folder: <data>/chats/<user>/<session>/chat.json holds the director's transcript, the brief, the
# approved reference, the settings, the jobs it made and the turns the web shows. Reloading a chat restores all of
# it, so a model built last week can be re-finished, repainted or repaired today.
CHATS_DIR = config.DATA_DIR / "chats"
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _chat_dir(user, session_id):
    return os.path.join(str(CHATS_DIR), _SAFE.sub("_", user)[:40], _SAFE.sub("_", session_id)[:80])


def load_chat(user, session_id):
    path = os.path.join(_chat_dir(user, session_id), "chat.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _job_ids_in(turn, director):
    ids = []
    if turn and (turn.get("turn") or {}).get("last_job"):
        ids.append(turn["turn"]["last_job"])
    for t in director.last_tools:
        m = re.search(r'"job_id":\s*"([0-9]{8}_[0-9]{6}_[0-9a-f]{6})"', t.get("result") or "")
        if m:
            ids.append(m.group(1))
    return ids


def _chat_title(state, director):
    if director.spec and director.spec.name:
        return director.spec.name
    for t in state.get("turns") or []:
        text = (t.get("user") or "").strip()
        if text:
            return text[:60]
    return "New chat"


def save_chat(user, session_id, sess, turn=None):
    d = sess["director"]
    state = load_chat(user, session_id) or {"id": session_id, "user": user, "created": time.time(), "turns": [], "jobs": []}
    if turn:
        state["turns"].append(turn)
    for jid in _job_ids_in(turn, d):
        if jid not in state["jobs"]:
            state["jobs"].append(jid)
    state.update({"updated": time.time(), "title": _chat_title(state, d), "messages": d.messages,
                  "spec": d.spec.to_dict() if d.spec else None, "reference": d.reference, "last_job_id": d.last_job_id,
                  "model": d.model, "settings": sess["settings"]})
    folder = _chat_dir(user, session_id)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "chat.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, default=str)
    os.replace(tmp, os.path.join(folder, "chat.json"))
    return state


def chat_summary(state):
    return {"id": state["id"], "title": state.get("title") or "New chat", "created": state.get("created"),
            "updated": state.get("updated"), "turns": len(state.get("turns") or []), "jobs": list(state.get("jobs") or []),
            "last_job_id": state.get("last_job_id"), "name": (state.get("spec") or {}).get("name")}


def list_chats(user, limit=100):
    root = os.path.join(str(CHATS_DIR), _SAFE.sub("_", user)[:40])
    out = []
    if os.path.isdir(root):
        for sid in os.listdir(root):
            st = load_chat(user, sid)
            if st:
                out.append(chat_summary(st))
    out.sort(key=lambda c: -(c.get("updated") or 0))
    return out[:limit]


def _director_entry(mid, prices, tag=None):
    p = prices.get(mid) or {}
    money = (" · $%.2f in / $%.2f out per 1M" % (p["in_per_m"], p["out_per_m"])) if p else ""
    return {"id": mid, "label": "%s%s%s" % (mid, (" (%s)" % tag) if tag else "", money),
            "in_per_m": p.get("in_per_m"), "out_per_m": p.get("out_per_m"), "tools": p.get("tools"), "vision": p.get("vision"),
            "context": p.get("context")}


def model_options(all_models=False):
    """What the UI may choose from and what is in force now. The director list is the configured set (the .env
    default first) with OpenRouter's prices; all_models adds every tool-and-vision-capable model OpenRouter serves."""
    prices = providers.openrouter_models()
    director, seen = [], set()
    for mid, tag in [(config.DIRECTOR_MODEL, "default")] + [(m, None) for m in config.DIRECTOR_MODELS] + [(config.PREMIUM_MODEL, "premium")]:
        if mid and mid not in seen:
            seen.add(mid)
            director.append(_director_entry(mid, prices, tag))
    if all_models:
        extra = [mid for mid, p in prices.items() if p["tools"] and p["vision"] and mid not in seen
                 and not mid.startswith("~") and ":" not in mid.split("/")[-1]]
        director += [_director_entry(mid, prices) for mid in sorted(extra)]
    return {"seed_vendors": pricing.vendor_catalogue(),
            "picture_models": pricing.picture_catalogue(),
            "director_models": director, "all_models": all_models,
            "pictures": {"concept": config.CONCEPT_MODEL, "concept_hard_surface": config.CONCEPT_MODEL_HARD,
                         "concept_premium": config.CONCEPT_MODEL_PREMIUM, "edit": config.EDIT_MODEL, "vision": config.VISION_MODEL},
            "defaults": {"seed_vendor": pricing.seed_vendor(Spec(name="X", description="x"))["key"], "director_model": config.DIRECTOR_MODEL,
                         "picture_model": config.CONCEPT_MODEL}}


def _attachment_note(attachments):
    lines = []
    for a in attachments:
        if not _upload_path_ok(a.path):
            raise HTTPException(400, "attachment is not an uploaded file: %s" % a.name)
        kind = a.kind or ("mesh" if a.path.lower().endswith(MESH_EXTENSIONS) else "image")
        lines.append("- %s %s at path %s" % ("3D model" if kind == "mesh" else "picture", a.name or os.path.basename(a.path), a.path))
    return ("\n\n[Attached files]\n" + "\n".join(lines)) if lines else ""


# ------------------------------------------------------------------ routes
@app.get("/", response_class=HTMLResponse)
def index():
    return ("<!doctype html><title>Master Smith</title><body style='font-family:system-ui;max-width:40em;margin:3em auto'>"
            "<h1>Master Smith API</h1><p>The chat interface is the Next.js app in <code>web/</code> "
            "(<code>npm run dev</code>, http://localhost:3000). This process answers <code>/v1/*</code> and "
            "<code>/healthz</code>; the OpenAPI schema is at <a href='/docs'>/docs</a>.</p></body>")


def local_mode():
    return not config.API_KEY


@app.get("/v1/me")
def me(who=Depends(auth)):
    return {"user": who["user"], "balance": wallet.balance(who["user"]),
            "local_mode": local_mode(), "providers": providers.balances(), "jobs": store.jobs_for(who["user"])}


@app.get("/v1/providers")
def provider_balances(refresh: bool = False, who=Depends(auth)):
    """What the fal and OpenRouter accounts behind this instance have left (cached a minute; ?refresh=1 re-reads)."""
    return providers.balances(force=refresh)


@app.post("/v1/uploads")
async def upload(file: UploadFile = File(...), who=Depends(auth)):
    """Store a picture or a 3D model for the chat to use; returns the path to put in the message's attachments."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(file.filename or "upload"))[:120] or "upload"
    ext = os.path.splitext(name)[1].lower()
    if ext not in MESH_EXTENSIONS + IMAGE_EXTENSIONS:
        raise HTTPException(415, "upload a picture (%s) or a model (%s)" % (", ".join(IMAGE_EXTENSIONS), ", ".join(MESH_EXTENSIONS)))
    folder = os.path.join(config.UPLOADS_DIR, who["user"], uuid.uuid4().hex[:10])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            f.write(chunk)
    kind = "mesh" if ext in MESH_EXTENSIONS else "image"
    if kind == "image" and ext not in NATIVE_IMAGE:
        # a .jfif is a JPEG, a .webp or .heic is not something every vendor reads: re-save as PNG
        from PIL import Image
        try:
            with Image.open(path) as im:
                png = os.path.splitext(path)[0] + ".png"
                im.convert("RGB").save(png)
            os.remove(path)
            path, name, size = png, os.path.basename(png), os.path.getsize(png)
        except Exception as exc:  # noqa: BLE001
            os.remove(path)
            raise HTTPException(415, "could not read that picture (%s)" % str(exc)[:120])
    return {"path": path, "name": name, "kind": kind, "bytes": size}


@app.get("/v1/models")
def get_models(all: bool = False, who=Depends(auth)):
    """Mesh vendors (with worst-case seed price), director models with OpenRouter's prices, and the picture models in
    force. ?all=1 lists every tool-and-vision-capable model OpenRouter serves."""
    return model_options(all_models=all)


@app.post("/v1/chat")
def chat(body: ChatIn, who=Depends(auth)):
    d = _director(body.session_id, who["user"])
    settings = _session_settings(body.session_id, who["user"])
    if body.settings:
        sv = str(body.settings.get("seed_vendor") or "").strip().lower()
        if sv in {v["key"] for v in pricing.SEED_VENDORS}:
            settings["seed_vendor"] = sv
        dm = str(body.settings.get("director_model") or "").strip()
        if dm:
            settings["director_model"] = dm
            d.model = dm
        pm = str(body.settings.get("picture_model") or "").strip()
        if pm in pricing.IMAGE_PRICES:
            settings["picture_model"] = pm
    text = body.message + _attachment_note(body.attachments)
    try:
        reply = d.turn(text)
    except LLMError as exc:
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise HTTPException(503, "the director needs OPENROUTER_API_KEY in .env (or drive it from Claude Code over MCP: "
                                     "docs/AGENT_MODE.md); restart the API after adding it")
        raise HTTPException(502, "director error: %s" % str(exc)[:300])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, "director error: %s" % str(exc)[:300])
    out = {"reply": reply, "brief": d.spec.to_dict() if d.spec else None, "balance": wallet.balance(who["user"]),
           "providers": providers.balances(), "last_job": d.last_job_id, "chat_cost_usd": round(d.chat_cost_usd(), 5),
           "pictures": list(d.last_pictures), "pictures_kind": d.last_pictures_kind, "question": d.last_question,
           "reference_job": (d.reference or {}).get("job_dir"),
           "settings": {"seed_vendor": settings.get("seed_vendor") or model_options()["defaults"]["seed_vendor"],
                        "director_model": d.model, "picture_model": settings.get("picture_model") or config.CONCEPT_MODEL},
           "tools": [{"name": t["name"], "args": t.get("args"), "result": t.get("result")} for t in d.last_tools]}
    try:
        turn = {"at": time.time(), "user": body.message, "attachments": [a.model_dump() for a in body.attachments], "reply": reply,
                "turn": {k: out[k] for k in ("brief", "last_job", "balance", "providers", "pictures", "pictures_kind", "question",
                                             "reference_job", "settings", "chat_cost_usd", "tools")}}
        out["chat"] = chat_summary(save_chat(who["user"], body.session_id, _session(body.session_id, who["user"]), turn))
    except Exception as exc:  # noqa: BLE001 - a chat that could not be saved still answers
        out["chat_save_error"] = str(exc)[:200]
    return out


@app.get("/v1/chats")
def get_chats(who=Depends(auth)):
    """Every chat this user had, newest first: title, when, the jobs it made."""
    return list_chats(who["user"])


@app.get("/v1/chats/{session_id}")
def get_chat(session_id: str, who=Depends(auth)):
    """One chat with its turns (what the web shows), brief, settings and the jobs it made with their files; loading it
    also warms the session so the next message continues where it left off."""
    state = load_chat(who["user"], session_id)
    if not state:
        raise HTTPException(404, "no such chat")
    _session(session_id, who["user"])
    jobs = []
    for jid in state.get("jobs") or []:
        row = store.job(jid, who["user"])
        if row:
            jobs.append(job_view(row, who["user"]))
    return {**chat_summary(state), "turns": state.get("turns") or [], "spec": state.get("spec"),
            "settings": state.get("settings") or {}, "reference": state.get("reference"), "job_views": jobs}


@app.delete("/v1/chats/{session_id}")
def delete_chat(session_id: str, who=Depends(auth)):
    """Forget a chat (its folder). The jobs it made and their files stay."""
    import shutil
    folder = _chat_dir(who["user"], session_id)
    gone = os.path.isdir(folder)
    if gone:
        shutil.rmtree(folder, ignore_errors=True)
    with _lock:
        _sessions.pop("%s:%s" % (who["user"], session_id), None)
    return {"deleted": gone}


@app.post("/v1/jobs")
def create_job(body: JobIn, dry_run: bool = False, who=Depends(auth)):
    """Queue a build from a spec. With ?dry_run=1 nothing is queued: the resolved brief, the worst-case estimate and
    the provider balances come back, so a script can check what a request would do before spending."""
    if dry_run:
        spec = Spec.from_dict(body.spec)
        est = pricing.estimate(spec)
        return {"dry_run": True, "brief": spec.to_dict(), "estimate_usd": est["usd"], "estimate_credits": est["credits"],
                "steps": [{"step": s, "usd": round(u, 4)} for s, u in est["steps"]], "providers": providers.balances()}
    out = submit_build(who["user"], body.spec)
    if "error" in out:
        raise HTTPException(400 if out.get("bad_paths") else 402, out)
    return out


@app.post("/v1/reference")
def make_reference_endpoint(body: JobIn, who=Depends(auth)):
    """Draw the reference picture(s) for a spec now and return them (tens of seconds, cents). Build from them with
    POST /v1/jobs and "reference_job": <dir> in the spec: the picture stage is then skipped."""
    out = run_reference(who["user"], body.spec)
    if out.get("status") != "done":
        raise HTTPException({"refused": 402, "rejected": 400}.get(out.get("status"), 500), out)
    return out


@app.post("/v1/jobs/import")
def import_job(body: ImportIn, who=Depends(auth)):
    out = submit_import(who["user"], body.path, body.spec)
    if "error" in out:
        raise HTTPException(400, out)
    return out


@app.post("/v1/jobs/refinish")
def refinish_job(body: RefinishIn, confirm_removal: bool = False, who=Depends(auth)):
    """Re-finish an earlier job's seed with a changed brief. New remove_parts answer a removal preview (red on the
    renders) until the call is repeated with ?confirm_removal=1."""
    src = store.job(body.source_job, who["user"])
    if not src or not (src.get("result") or {}).get("dir"):
        raise HTTPException(404, "source job not found or has no output")
    bad = _foreign_paths(body.overrides)       # the stored brief was checked when it was queued
    if bad:
        raise HTTPException(400, _refuse_paths(bad))
    spec = Spec.from_dict({**src["spec"], **body.overrides})
    new_removals = [p for p in spec.remove_parts if p not in (src["spec"].get("remove_parts") or [])]
    if new_removals and not confirm_removal:
        return run_removal_preview(who["user"], src["result"]["dir"], spec)
    _reuse_part_seeds(who["user"], spec)
    return _enqueue(who["user"], spec, "refinish", src["result"]["dir"])


@app.get("/v1/jobs")
def list_jobs(who=Depends(auth)):
    return store.jobs_for(who["user"], limit=50)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, who=Depends(auth)):
    row = store.job(job_id, who["user"])
    if not row:
        raise HTTPException(404, "unknown job")
    return job_view(row, who["user"])


@app.get("/v1/jobs/{job_id}/files/{name}")
def get_file(job_id: str, name: str, who=Depends(auth)):
    row = store.job(job_id, who["user"])
    r = (row or {}).get("result") or {}
    d = r.get("delivery_dir")
    if not d or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(404, "no such file")
    path = os.path.join(d, name)
    if not os.path.isfile(path) and r.get("dir") and name.startswith(PICTURE_PREFIXES) and name.lower().endswith(".png"):
        path = os.path.join(r["dir"], name)          # the pictures a build drew live beside the delivery, not in it
    if not os.path.isfile(path):
        # a build from approved pictures recorded views that live in the reference job's folder
        path = next((v for v in _recorded_views(r) if os.path.basename(v) == name), path)
    if not os.path.isfile(path):
        raise HTTPException(404, "no such file")
    return FileResponse(path, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream", filename=name)


PICTURE_PREFIXES = ("ref_", "cockpit_ref_", "remove_preview_", "part_", "customer_ref_")


def _recorded_views(result):
    return [v for v in ((result.get("reference") or {}).get("views") or []) if isinstance(v, str) and os.path.isfile(v)]


def job_pictures(row):
    """The pictures a job drew or was given (reference views, cockpit and part pictures, removal previews), as URLs.
    A build from approved pictures lists the views it recorded, which live in the reference job's folder."""
    r = row.get("result") or {}
    d = r.get("dir")
    names = []
    if d and os.path.isdir(d):
        names = sorted(f for f in os.listdir(d) if f.startswith(PICTURE_PREFIXES) and f.lower().endswith(".png"))
    for v in _recorded_views(r):
        if os.path.basename(v) not in names:
            names.append(os.path.basename(v))
    return ["/v1/jobs/%s/files/%s" % (row["id"], f) for f in names]


@app.get("/v1/wallet")
def get_wallet(who=Depends(auth)):
    return {"user": who["user"], "balance": wallet.balance(who["user"]), "spent_usd": round(-wallet.balance(who["user"]) / 100, 2),
            "history": [{"ts": t, "kind": k, "credits": c, "usd": u, "note": n} for t, k, c, u, n in wallet.history(who["user"])]}


@app.get("/v1/estimate")
def estimate(request: Request, who=Depends(auth)):
    spec = Spec.from_dict(dict(request.query_params))
    est = pricing.estimate(spec)
    return {"brief": spec.to_dict(), "credits": est["credits"], "usd": est["usd"], "steps": [s for s, _ in est["steps"]]}


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "blender": os.path.exists(config.BLENDER_BIN), "local_mode": local_mode(),
                         "worker": os.environ.get("MASTERSMITH_NO_WORKER") != "1"})


# ------------------------------------------------------------------ debugging and testing from scripts and agents
SAFE_CONFIG = ("DIRECTOR_MODEL", "VISION_MODEL", "PREMIUM_MODEL", "CONCEPT_MODEL", "CONCEPT_MODEL_PREMIUM", "CONCEPT_MODEL_HARD",
               "EDIT_MODEL", "IMAGE_RESOLUTION", "SEED_MODEL", "SEED_MULTIVIEW_MODEL", "RETEXTURE_MODEL", "REPAINT_DEFAULT",
               "HYBRID_SEED", "SEED_QUAD", "HARD_SURFACE_CATEGORIES", "BLENDER_BIN", "API_USER")


@app.get("/v1/config")
def get_config(who=Depends(auth)):
    """Effective configuration without secrets: models, vendors, paths, flags, and whether the keys are set."""
    return {**{k: getattr(config, k) for k in SAFE_CONFIG},
            "DATA_DIR": str(config.DATA_DIR), "OUT_DIR": str(config.OUT_DIR), "UPLOADS_DIR": str(config.UPLOADS_DIR),
            "blender_present": os.path.exists(config.BLENDER_BIN), "local_mode": local_mode(),
            "keys_set": {"FAL_KEY": bool(os.environ.get("FAL_KEY")), "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
                         "MASTERSMITH_API_KEY": bool(config.API_KEY)},
            "categories": list(__import__("mastersmith.spec", fromlist=["CATEGORIES"]).CATEGORIES)}


@app.get("/v1/jobs/{job_id}/log")
def job_log(job_id: str, tail: int = 200, who=Depends(auth)):
    """The whole build log (or its last `tail` lines) as plain text."""
    row = store.job(job_id, who["user"])
    if not row:
        raise HTTPException(404, "unknown job")
    lines = row["log"].splitlines()
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines[-tail:] if tail > 0 else lines))


@app.get("/v1/jobs/{job_id}/debug")
def job_debug(job_id: str, who=Depends(auth)):
    """Everything a debugger wants in one call: the full result (job.json), the work directory listing, the tails
    of the Blender logs, the bill by stage and the delivered files."""
    row = store.job(job_id, who["user"])
    if not row:
        raise HTTPException(404, "unknown job")
    r = row.get("result") or {}
    job_dir = r.get("dir")
    work = os.path.join(job_dir, "work") if job_dir else None
    blender_logs = {}
    work_files = []
    if work and os.path.isdir(work):
        work_files = sorted(os.listdir(work))
        for name in work_files:
            if name.endswith(".log"):
                try:
                    with open(os.path.join(work, name), encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    blender_logs[name] = text[-4000:]
                except OSError:
                    pass
    return {"id": row["id"], "status": row["status"], "kind": row["kind"], "error": row["error"], "spec": row["spec"],
            "created": row["created"], "started": row["started"], "finished": row["finished"],
            "result": {k: v for k, v in r.items() if k not in ("bill",)}, "bill": r.get("bill"),
            "job_dir": job_dir, "work_files": work_files, "blender_logs": blender_logs,
            "work_urls": ["/v1/jobs/%s/work/%s" % (row["id"], f) for f in work_files],
            "files": ["/v1/jobs/%s/files/%s" % (row["id"], f) for f in
                      (sorted(os.listdir(r["delivery_dir"])) if r.get("delivery_dir") and os.path.isdir(r["delivery_dir"]) else [])],
            "log_tail": row["log"].splitlines()[-60:]}


@app.get("/v1/jobs/{job_id}/work/{name}")
def get_work_file(job_id: str, name: str, who=Depends(auth)):
    """A file from the job's work directory: probe renders, Blender args and logs, masks, intermediate GLBs."""
    row = store.job(job_id, who["user"])
    d = ((row or {}).get("result") or {}).get("dir")
    if not d or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(404, "no such file")
    path = os.path.join(d, "work", name)
    if not os.path.isfile(path):
        raise HTTPException(404, "no such file")
    return FileResponse(path, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream", filename=name)


@app.get("/v1/sessions/{session_id}")
def get_session(session_id: str, who=Depends(auth)):
    """The director's transcript for a chat session (system prompt omitted), its brief and the last tool calls."""
    key = "%s:%s" % (who["user"], session_id)
    with _lock:
        s = _sessions.get(key)
    if not s:
        raise HTTPException(404, "no such session (sessions live in memory and expire after %d h)" % (SESSION_TTL // 3600))
    d = s["director"]
    return {"session_id": session_id, "user": who["user"], "brief": d.spec.to_dict() if d.spec else None,
            "last_job": d.last_job_id, "chat_cost_usd": round(d.chat_cost_usd(), 5),
            "messages": [m for m in d.messages if m.get("role") != "system"], "last_tools": d.last_tools}


# ------------------------------------------------------------------ another brain: Claude Code, Codex, any MCP client
# The director's tools and state are the session's; who calls them is up to you. These three routes let an outside
# model act as the director with the same system prompt, skills and tools (mastersmith/mcp_server.py wraps them for
# MCP), so a Claude Code or Codex subscription can drive builds instead of OpenRouter. The chat is still recorded.
class ToolIn(BaseModel):
    name: str
    args: dict = {}


class TurnIn(BaseModel):
    user: str
    reply: str
    attachments: list[Attachment] = []


@app.get("/v1/sessions/{session_id}/prompt")
def get_prompt(session_id: str, who=Depends(auth)):
    """The director's system prompt for this session, verbatim, plus the tool schemas."""
    from .agent import TOOLS
    d = _director(session_id, who["user"])
    return {"session_id": session_id, "system_prompt": d.messages[0]["content"], "tools": [t["function"] for t in TOOLS],
            "brief": d.spec.to_dict() if d.spec else None, "last_job": d.last_job_id}


@app.post("/v1/sessions/{session_id}/tool")
def run_tool(session_id: str, body: ToolIn, who=Depends(auth)):
    """Run one director tool in this session (set_brief, make_reference, build, ...) and answer what the model would see."""
    sess = _session(session_id, who["user"])
    d = sess["director"]
    fn = {"set_brief": d._set_brief, "build": d._build, "read_skill": d._read_skill, "balance": d._balance,
          "job_status": d._job_status, "import_model": d._import_model, "make_reference": d._make_reference,
          "ask_customer": d._ask, "plan_repair": d._plan_repair}.get(body.name)
    if not fn:
        raise HTTPException(404, "no such tool: %s" % body.name)
    try:
        out = fn(dict(body.args or {}))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "tool %s failed: %s" % (body.name, str(exc)[:300]))
    d.last_tools.append({"name": body.name, "args": body.args, "result": json.dumps(out, default=str)[:400]})
    # the chat on disk carries the brief and any job this call queued at once, so the web (which reads the chat and
    # polls its jobs) shows an outside director's build running instead of waiting for the turn to be recorded
    save_chat(who["user"], session_id, sess)
    if isinstance(out, dict) and out.get("pictures"):
        out = {**out, "pictures": [p if isinstance(p, dict) else {"label": "picture", "url": p} for p in out["pictures"]]}
    return out


@app.post("/v1/sessions/{session_id}/turn")
def record_turn(session_id: str, body: TurnIn, who=Depends(auth)):
    """Record one exchange (the customer's words and the outside model's reply) so the chat is saved and shows in
    the web like any other; the tool calls made since the last record ride along."""
    sess = _session(session_id, who["user"])
    d = sess["director"]
    settings = sess["settings"]
    d.messages.append({"role": "user", "content": body.user})
    d.messages.append({"role": "assistant", "content": body.reply})
    turn_data = {"brief": d.spec.to_dict() if d.spec else None, "last_job": d.last_job_id, "balance": wallet.balance(who["user"]),
                 "providers": providers.balances(), "pictures": list(d.last_pictures), "pictures_kind": d.last_pictures_kind,
                 "question": d.last_question, "reference_job": (d.reference or {}).get("job_dir"),
                 "settings": {"seed_vendor": settings.get("seed_vendor") or "tripo", "director_model": "external",
                              "picture_model": settings.get("picture_model") or config.CONCEPT_MODEL},
                 "chat_cost_usd": 0.0,
                 "tools": [{"name": t["name"], "args": t.get("args"), "result": t.get("result")} for t in d.last_tools]}
    turn = {"at": time.time(), "user": body.user, "attachments": [a.model_dump() for a in body.attachments], "reply": body.reply,
            "turn": turn_data}
    state = save_chat(who["user"], session_id, sess, turn)
    d.last_tools, d.last_pictures, d.last_pictures_kind, d.last_question = [], [], None, None
    return {"recorded": True, "chat": chat_summary(state)}


@app.delete("/v1/sessions/{session_id}")
def drop_session(session_id: str, who=Depends(auth)):
    with _lock:
        gone = _sessions.pop("%s:%s" % (who["user"], session_id), None) is not None
    return {"dropped": gone}
