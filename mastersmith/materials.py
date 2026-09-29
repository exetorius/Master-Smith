"""CC0 PBR surface sets for the smart-material pass (issue #15): what a texture artist reaches for instead of noise.
One set per finish from ambientCG (CC0, 1K JPG, ~3.5 MB), fetched on first use into <local models>/pbr/<Set>/ and
kept. The set's colour only supplies light-and-dark variation (the planned colour stays), its roughness varies
ours, its displacement drives a fine bump: brushed steel brushes, polymer grains, rubber stipples."""
import io
import os
import zipfile

from . import config

# finish -> ambientCG set. "metal" splits by the planned colour: a dark metal is parkerised / powder-coated steel,
# a light one brushed steel; "aluminium" is the anodised look (a brushed alu set).
SETS = {
    "metal": "Metal009",           # brushed steel, fine scratches
    "metal_dark": "Metal027",      # black powder-coated / parkerised: matte, fine grain
    "aluminium": "Metal051A",      # brushed aluminium
    "painted": "Metal028",         # black powder-coated paint over metal
    "polymer": "Plastic012A",      # black moulded plastic, fine grain
    "rubber": "Rubber002",         # black rubber, stippled
}
# how the map files are named inside an ambientCG zip; the role our nodes use
ROLES = {"color": "_Color", "normal": "_NormalGL", "roughness": "_Roughness", "displacement": "_Displacement",
         "ao": "_AmbientOcclusion", "metalness": "_Metalness"}
# the tile's size on the part, metres: the grain of brushed metal is a millimetre, so a 1 m tile is shrunk
TILE_M = {"metal": 0.25, "metal_dark": 0.2, "aluminium": 0.25, "painted": 0.3, "polymer": 0.2, "rubber": 0.15}
# the look per finish: bump strength from the set's displacement, how much its roughness varies ours, cavity dirt
LOOK = {"metal": {"bump": 0.2, "rough_var": 0.5, "dirt": 0.35, "colour_var": 0.15},
        "metal_dark": {"bump": 0.25, "rough_var": 0.4, "dirt": 0.35, "colour_var": 0.12},
        "aluminium": {"bump": 0.15, "rough_var": 0.5, "dirt": 0.3, "colour_var": 0.15},
        "painted": {"bump": 0.2, "rough_var": 0.4, "dirt": 0.45, "colour_var": 0.2},
        "polymer": {"bump": 0.35, "rough_var": 0.4, "dirt": 0.3, "colour_var": 0.25},
        "rubber": {"bump": 0.6, "rough_var": 0.3, "dirt": 0.2, "colour_var": 0.2}}
DOWNLOAD = "https://ambientcg.com/get?file=%s_1K-JPG.zip"


def library_dir():
    return os.path.join(str(config.LOCAL_MODELS_DIR), "pbr")


def luminance_of(hex_colour):
    h = str(hex_colour or "").lstrip("#")
    if len(h) != 6:
        return 0.5
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def pick(material):
    """The set key for a plan material ({"finish", "color", "metal", ...}); None for glass and finishes without a set."""
    material = material or {}
    finish = str(material.get("finish") or ("metal" if material.get("metal") else "polymer")).lower()
    if finish == "glass" or material.get("glass"):
        return None
    if finish == "metal":
        what = str(material.get("what") or "").lower()
        if "alumin" in what or "anodi" in what:
            return "aluminium"
        return "metal_dark" if luminance_of(material.get("color")) < 0.22 else "metal"
    return finish if finish in SETS else None


def _fetch(url):
    import requests
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def ensure(key, fetch=_fetch, log=print):
    """The map files of a set, downloading the zip on first use. -> {"color": path, "normal": path, ...} or None
    when the set is unknown or the download fails (the pass is then skipped, never blocked)."""
    set_id = SETS.get(key)
    if not set_id:
        return None
    d = os.path.join(library_dir(), set_id)
    found = _maps_in(d)
    if found:
        return found
    try:
        data = fetch(DOWNLOAD % set_id)
        os.makedirs(d, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith((".jpg", ".png")) and "/" not in name.strip("/"):
                    with open(os.path.join(d, os.path.basename(name)), "wb") as f:
                        f.write(z.read(name))
        log("  materials: fetched %s (CC0, ambientCG) into %s" % (set_id, d))
    except Exception as exc:  # noqa: BLE001 - offline or a changed site: the pass is skipped, the build goes on
        log("  materials: %s not available (%s); the smart-material pass is skipped for it" % (set_id, str(exc)[:80]))
        return None
    return _maps_in(d)


def _maps_in(d):
    if not os.path.isdir(d):
        return None
    files = os.listdir(d)
    out = {}
    for role, suffix in ROLES.items():
        hit = next((f for f in files if suffix.lower() in f.lower() and f.lower().endswith((".jpg", ".png"))), None)
        if hit:
            out[role] = os.path.join(d, hit)
    return out if "color" in out and "roughness" in out else None


def library_for(plan_parts, fetch=_fetch, log=print):
    """Every set the plan's parts and zones need, fetched: {key: {"maps": {...}, "tile_m": .., **LOOK[key]}}; the
    parts and zones get a "pbr_set" key naming theirs."""
    lib = {}
    for p in plan_parts:
        items = [p] + list(p.get("zones") or [])
        for it in items:
            mat = dict(it.get("material") or {})
            mat.setdefault("what", p.get("what"))
            key = pick(mat)
            it["pbr_set"] = key
            if key and key not in lib:
                maps = ensure(key, fetch, log)
                if maps:
                    lib[key] = {"maps": maps, "tile_m": TILE_M[key], **LOOK[key]}
    return lib
