"""Stage 2b: repaint an EXISTING mesh instead of rebuilding it. A colour or material complaint ("the stock
should be gunmetal, not cream") must not change the approved shape, so the previous version's mesh goes to
Meshy's retexture with its original UVs kept; the maps come back on our atlas and the finish pass wires them
in place of the old ones. A picture of the wanted look (the previous reference picture, edited) guides it;
the text prompt carries the materials. About $0.30 and a few minutes."""
import os
import subprocess

from .. import config

STRIP = config.ROOT / "mastersmith" / "blender" / "strip_textures.py"


def geometry_only(job, glb_path):
    """The mesh and its UVs without the packed maps: a 51 MB delivered GLB becomes a few MB (2026-09-17)."""
    out = os.path.join(job.work_dir, "retex_geometry.glb")
    proc = subprocess.run([config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(STRIP), "--", glb_path, out],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("could not strip the mesh for the retexture vendor: %s" % (proc.stderr or proc.stdout)[-400:])
    return out

MAP_KEYS = {"base_color": "BC", "metallic": "M", "roughness": "R", "normal": "N"}


def _texture_urls(result):
    found = {}
    tex = result.get("texture_urls")
    entries = tex if isinstance(tex, list) else ([tex] if isinstance(tex, dict) else [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for vendor_key, label in MAP_KEYS.items():
            v = entry.get(vendor_key)
            url = v.get("url") if isinstance(v, dict) else v
            if isinstance(url, str) and url.startswith("http"):
                found[label] = url
    return found


def retexture_prompt(spec):
    """Materials first, then the change: Meshy reads at most 600 characters."""
    parts = []
    if spec.edit_instructions:
        parts.append(spec.edit_instructions.rstrip(".") + ".")
    parts.append(spec.description)
    parts.append("Photorealistic PBR materials, correct metal and polymer, no baked lighting, no text or logos added.")
    return " ".join(parts)[:600]


def make_retexture(job, glb_path, style_image=None):
    """-> {"maps": {"BC": path, "R": path, "M": path, "N": path}, "usd": float} (BC always present)."""
    spec = job.spec
    glb_path = geometry_only(job, glb_path)
    size = os.path.getsize(glb_path)
    if size > 40e6:
        raise RuntimeError("the mesh is %.0f MB even without textures; the retexture vendor takes 40 MB at most" % (size / 1e6))
    job.log("  retexture: uploading %.1f MB mesh to %s" % (size / 1e6, config.RETEXTURE_MODEL.split("/")[1]))
    payload = {"model_url": job.fal.upload(glb_path, "model/gltf-binary"), "text_style_prompt": retexture_prompt(spec),
               "enable_original_uv": True, "enable_pbr": True}
    if style_image and os.path.exists(style_image):
        payload["image_style_url"] = job.fal.upload(style_image)
    out = job.fal.run(config.RETEXTURE_MODEL, payload, timeout=1500)
    urls = _texture_urls(out)
    if "BC" not in urls:
        raise RuntimeError("the retexture vendor returned no base colour map (%s)" % ", ".join(sorted(urls)) or "nothing")
    maps = {}
    for label, url in urls.items():
        path = os.path.join(job.dir, "retex_%s.png" % label)
        job.fal.download(url, path)
        maps[label] = path
    job.log("  retexture: %s" % ", ".join(sorted(maps)))
    return {"maps": maps}
