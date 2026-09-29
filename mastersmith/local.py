"""The free tier: pictures and meshes from models on this PC instead of fal.

Pictures: FLUX.2 [klein] 4B through ComfyUI (text -> picture, and edits from up to 4 reference pictures), ~11 s each on
an RTX 4080 Laptop. Meshes: TRELLIS.2 through trellis.cpp (image -> textured PBR GLB), 1.5-6.5 min at res 1024.
Both live in config.LOCAL_MODELS_DIR (see its README); both are Apache-2.0 / MIT.

One GPU job at a time: a 12 GB card holds one of these models, not two, and an assembly seeds its parts on several
workers. Before a mesh the picture model is unloaded from ComfyUI so TRELLIS has the whole card."""
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config

GPU = threading.Lock()

# FLUX.2 wants sides that are multiples of 16; about one megapixel, the size klein was tuned at.
KLEIN_SIZES = {"1:1": (1024, 1024), "4:3": (1184, 896), "3:4": (896, 1184), "16:9": (1344, 768), "9:16": (768, 1344),
               "3:2": (1248, 832), "2:3": (832, 1248)}


class LocalError(Exception):
    pass


# ---------------------------------------------------------------- ComfyUI

def _comfy(path, body=None, timeout=60):
    url = config.LOCAL_COMFY_URL + path
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}) if body is not None else url
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise LocalError("ComfyUI %s: HTTP %d %s" % (path, e.code, e.read().decode(errors="replace")[:1500]))


def comfy_up():
    try:
        _comfy("/system_stats", timeout=3)
        return True
    except (OSError, LocalError):
        return False


def ensure_comfy(log=print, wait=180):
    """ComfyUI answering on LOCAL_COMFY_URL; started from LOCAL_MODELS_DIR when it is not."""
    if comfy_up():
        return
    root = config.LOCAL_MODELS_DIR / "ComfyUI"
    py = root / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        raise LocalError("the local picture model is not installed: no %s (see %s\\README.md)" % (py, config.LOCAL_MODELS_DIR))
    port = urllib.parse.urlparse(config.LOCAL_COMFY_URL).port or 8188
    log("  starting ComfyUI for the local picture model (first picture waits for it)")
    logf = open(config.LOCAL_MODELS_DIR / "logs" / "comfyui.log", "ab")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(py), "main.py", "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch"],
                     cwd=str(root), stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
    t0 = time.time()
    while time.time() - t0 < wait:
        if comfy_up():
            return
        time.sleep(2)
    raise LocalError("ComfyUI did not come up on %s within %ds (log: %s)" % (
        config.LOCAL_COMFY_URL, wait, config.LOCAL_MODELS_DIR / "logs" / "comfyui.log"))


def free_comfy():
    """Unload ComfyUI's models so the card is free for TRELLIS. Harmless when ComfyUI is not running."""
    if comfy_up():
        try:
            _comfy("/free", {"unload_models": True, "free_memory": True})
        except LocalError:
            pass


def _upload(path):
    boundary = uuid.uuid4().hex
    name = "ms_%s_%s" % (uuid.uuid4().hex[:8], os.path.basename(path))
    with open(path, "rb") as f:
        data = f.read()
    body = (("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode() + data +
            ("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(config.LOCAL_COMFY_URL + "/upload/image", body,
                                 {"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["name"]


def _as_file(ref, folder):
    """A reference as a local file: paths pass through; http(s) and data: URLs are saved beside the output."""
    ref = str(ref)
    if ref.startswith(("http://", "https://")):
        dest = os.path.join(folder, "ref_%s.png" % uuid.uuid4().hex[:8])
        req = urllib.request.Request(ref, headers={"User-Agent": "Mozilla/5.0 Master-Smith"})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            f.write(r.read())
        return dest
    if ref.startswith("data:"):
        import base64
        dest = os.path.join(folder, "ref_%s.png" % uuid.uuid4().hex[:8])
        with open(dest, "wb") as f:
            f.write(base64.b64decode(ref.split(",", 1)[1]))
        return dest
    return ref


def klein_graph(prompt, ref_names, width, height, seed, steps=4):
    """ComfyUI API graph for FLUX.2 klein 4B (distilled: 4 steps, cfg 1). Each reference is VAE-encoded and attached
    to both conditionings, as in ComfyUI's own klein edit template."""
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux-2-klein-4b.safetensors", "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "pos0": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg0": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos0", 0]}},
    }
    pos, neg = ["pos0", 0], ["neg0", 0]
    for i, name in enumerate(ref_names):
        g["img%d" % i] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["scale%d" % i] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": ["img%d" % i, 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 1}}
        g["enc%d" % i] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale%d" % i, 0], "vae": ["vae", 0]}}
        g["rp%d" % i] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": ["enc%d" % i, 0]}}
        g["rn%d" % i] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": neg, "latent": ["enc%d" % i, 0]}}
        pos, neg = ["rp%d" % i, 0], ["rn%d" % i, 0]
    g.update({
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sched": {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": width, "height": height}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["unet", 0], "positive": pos, "negative": neg, "cfg": 1.0}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["noise", 0], "guider": ["guider", 0],
                   "sampler": ["sampler", 0], "sigmas": ["sched", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "mastersmith"}},
    })
    return g


def picture(prompt, path, references=(), aspect_ratio="4:3", log=print, timeout=900):
    """One PNG at `path` from FLUX.2 klein; with references it is an edit of them. Returns seconds taken."""
    refs = [_as_file(r, os.path.dirname(os.path.abspath(path))) for r in references if r][:4]
    width, height = KLEIN_SIZES.get(aspect_ratio, KLEIN_SIZES["4:3"])
    with GPU:
        ensure_comfy(log)
        t0 = time.time()
        graph = klein_graph(prompt, [_upload(r) for r in refs], width, height, random.randrange(2**31))
        pid = json.loads(_comfy("/prompt", {"prompt": graph, "client_id": "mastersmith"}))["prompt_id"]
        while True:
            if time.time() - t0 > timeout:
                raise LocalError("the local picture took longer than %ds" % timeout)
            h = json.loads(_comfy("/history/" + pid)).get(pid)
            if h:
                st = h.get("status", {})
                if st.get("status_str") == "error":
                    err = [m[1] for m in st.get("messages", []) if m[0] == "execution_error"]
                    raise LocalError("ComfyUI failed: %s" % (err[-1].get("exception_message", "")[:500] if err else st))
                imgs = [i for o in h.get("outputs", {}).values() for i in o.get("images", [])]
                if imgs:
                    q = urllib.parse.urlencode({k: imgs[0][k] for k in ("filename", "subfolder", "type")})
                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                    with open(path, "wb") as f:
                        f.write(_comfy("/view?" + q))
                    return round(time.time() - t0, 1)
                if st.get("completed"):
                    raise LocalError("ComfyUI finished without a picture")
            time.sleep(0.5)


# ---------------------------------------------------------------- TRELLIS.2

def trellis(image, glb_path, res=None, seed=42, log=print, timeout=2400):
    """Textured GLB at `glb_path` from one picture with trellis.cpp. Returns seconds taken."""
    root = config.LOCAL_MODELS_DIR
    exe = root / "trellis" / "trellis-cli.exe"
    models = root / "models" / "trellis2" / config.LOCAL_TRELLIS_QUANT if config.LOCAL_TRELLIS_QUANT else root / "models" / "trellis2"
    if not exe.exists() or not models.exists():
        raise LocalError("the local mesh model is not installed: need %s and %s (see %s\\README.md)" % (exe, models, root))
    res = res or config.LOCAL_TRELLIS_RES
    work = tempfile.mkdtemp(prefix="trellis_", dir=os.path.dirname(os.path.abspath(glb_path)))
    out = os.path.join(work, "seed.glb")
    cmd = [str(exe), str(image), out, "--models", str(models), "--res", str(res), "--seed", str(seed), "--require-gpu"]
    if config.LOCAL_TRELLIS_TEX_RES:
        cmd += ["--tex-res", str(config.LOCAL_TRELLIS_TEX_RES), "--atlas", "4096"]
    with GPU:
        free_comfy()
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired:
            raise LocalError("TRELLIS took longer than %ds" % timeout)
        secs = round(time.time() - t0, 1)
    with open(os.path.join(work, "trellis.log"), "w", encoding="utf-8") as f:
        f.write(p.stdout + p.stderr)
    if p.returncode != 0 or not os.path.exists(out):
        tail = (p.stdout + p.stderr).strip().splitlines()[-6:]
        raise LocalError("TRELLIS failed (exit %s): %s" % (p.returncode, " | ".join(tail)[:600]))
    shutil.move(out, glb_path)
    shutil.rmtree(work, ignore_errors=True)
    return secs


def cutout(image, dst, timeout=600):
    """The object cut out on white with the BiRefNet that ships with TRELLIS (trellis-cli --bg-only), free."""
    from PIL import Image
    root = config.LOCAL_MODELS_DIR
    exe = root / "trellis" / "trellis-cli.exe"
    models = root / "models" / "trellis2" / config.LOCAL_TRELLIS_QUANT if config.LOCAL_TRELLIS_QUANT else root / "models" / "trellis2"
    work = tempfile.mkdtemp(prefix="ms_cut_")
    try:
        out = os.path.join(work, "cut.glb")
        with GPU:
            p = subprocess.run([str(exe), str(image), out, "--models", str(models), "--bg-only", "--birefnet"],
                               capture_output=True, text=True, timeout=timeout, errors="replace")
        cut = next((os.path.join(work, f) for f in os.listdir(work) if f.lower().endswith(".png")), None)
        if p.returncode != 0 or not cut:
            raise LocalError("background removal failed: %s" % (p.stderr or p.stdout)[-400:])
        im = Image.open(cut).convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))
        flat.paste(im, mask=im.split()[3])
        flat.save(dst)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


def seed_output(glb_path):
    """A fal-shaped answer for a local mesh, so make_seed and the assembly read it like any vendor's."""
    return {"model_mesh": {"url": "file:///" + os.path.abspath(glb_path).replace("\\", "/")}}


def file_path(url):
    """file:///E:/x/y.glb -> E:/x/y.glb; anything else -> None."""
    if not str(url).startswith("file://"):
        return None
    p = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    return p[1:] if len(p) > 2 and p[0] == "/" and p[2] == ":" else p
