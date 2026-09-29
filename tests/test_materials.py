"""The CC0 surface library: which set a finish gets, and the download-and-cache with a fake fetcher."""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mastersmith import config, materials  # noqa: E402


def test_pick_by_finish_and_colour():
    assert materials.pick({"finish": "metal", "color": "#2a2a2a"}) == "metal_dark"
    assert materials.pick({"finish": "metal", "color": "#9a9a9a"}) == "metal"
    assert materials.pick({"finish": "metal", "color": "#9a9a9a", "what": "anodised aluminium handguard"}) == "aluminium"
    assert materials.pick({"metal": True, "color": "#111111"}) == "metal_dark"
    assert materials.pick({"finish": "rubber"}) == "rubber" and materials.pick({"finish": "polymer"}) == "polymer"
    assert materials.pick({"finish": "painted", "color": "#556b2f"}) == "painted"
    assert materials.pick({"finish": "glass"}) is None and materials.pick({"finish": "wood"}) is None
    assert materials.pick(None) == "polymer"


def fake_zip(set_id):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for suffix in ("_Color", "_NormalGL", "_Roughness", "_Displacement", "_AmbientOcclusion"):
            z.writestr("%s_1K-JPG%s.jpg" % (set_id, suffix), b"\xff\xd8\xff")
        z.writestr("%s_1K-JPG.usdc" % set_id, b"x")
    return buf.getvalue()


def test_ensure_downloads_once_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_MODELS_DIR", tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        assert url.endswith("Rubber002_1K-JPG.zip")
        return fake_zip("Rubber002")
    maps = materials.ensure("rubber", fetch=fetch, log=lambda m: None)
    assert set(maps) == {"color", "normal", "roughness", "displacement", "ao"}
    assert os.path.exists(maps["color"]) and maps["color"].startswith(str(tmp_path))
    again = materials.ensure("rubber", fetch=fetch, log=lambda m: None)
    assert again == maps and len(calls) == 1                           # cached: no second download
    assert materials.ensure("nonsense", fetch=fetch) is None
    def broken(url):
        raise OSError("offline")
    assert materials.ensure("metal", fetch=broken, log=lambda m: None) is None   # skipped, never blocks


def test_library_for_names_every_part_and_zone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_MODELS_DIR", tmp_path)
    parts = [{"name": "Barrel", "what": "steel barrel", "material": {"finish": "metal", "color": "#222222"}},
             {"name": "Stock", "what": "polymer stock", "material": {"finish": "polymer", "color": "#555555"},
              "zones": [{"name": "Pad", "material": {"finish": "rubber", "color": "#111111"}},
                        {"name": "Lens", "material": {"finish": "glass"}}]}]
    lib = materials.library_for(parts, fetch=lambda url: fake_zip(url.split("file=")[1].split("_")[0]), log=lambda m: None)
    assert set(lib) == {"metal_dark", "polymer", "rubber"}
    assert parts[0]["pbr_set"] == "metal_dark" and parts[1]["pbr_set"] == "polymer"
    assert parts[1]["zones"][0]["pbr_set"] == "rubber" and parts[1]["zones"][1]["pbr_set"] is None
    assert lib["rubber"]["tile_m"] == 0.15 and lib["rubber"]["bump"] == 0.6
