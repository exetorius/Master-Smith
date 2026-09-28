"""The free tier routes local/ ids to this PC's models and bills them at $0. No GPU or network needed here: the two
runners (local.picture, local.trellis) are replaced by fakes that write a file."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mastersmith import config, local, pricing  # noqa: E402
from mastersmith.fal import Fal  # noqa: E402
from mastersmith.images import Images  # noqa: E402
from mastersmith.spec import Spec  # noqa: E402


def free_spec(**kw):
    kw = {"category": "prop", **kw}
    return Spec(name="Lantern", description="a brass lantern",
                seed_vendor="local", picture_model=config.LOCAL_PICTURE_MODEL, **kw)


def test_a_free_build_estimates_nothing_for_pictures_and_mesh(monkeypatch):
    monkeypatch.setattr(pricing, "LLM_CALL_ALLOWANCE_USD", 0.0)     # the vision checks still run on OpenRouter
    est = pricing.estimate(free_spec())
    assert est["usd"] == 0, est["steps"]
    assert pricing.concept_model(free_spec()) == config.LOCAL_PICTURE_MODEL
    assert pricing.edit_model(free_spec()) == config.LOCAL_PICTURE_MODEL


def test_a_free_assembly_prices_its_vendor_parts_at_zero():
    steps = dict(pricing.estimate_assembly(free_spec(category="weapon")))
    part = next(usd for name, usd in steps.items() if name.startswith("vendor parts"))
    assert part == 3 * 2 * pricing.LLM_CALL_ALLOWANCE_USD          # only the part-picture checks remain


def test_the_selectors_offer_the_free_tier():
    local_vendor = next(v for v in pricing.vendor_catalogue() if v["key"] == "local")
    assert local_vendor["usd"] == 0.0 and local_vendor["model"] == config.LOCAL_SEED_MODEL
    pic = next(p for p in pricing.picture_catalogue() if p["id"] == config.LOCAL_PICTURE_MODEL)
    assert pic["usd"] == 0.0 and pic["provider"] == "this PC"


def test_fal_runs_local_ids_on_this_pc_and_hands_back_the_mesh(tmp_path, monkeypatch):
    image = tmp_path / "view.png"
    image.write_bytes(b"png")
    seen = {}

    def fake_trellis(img, glb, **kw):
        seen["image"] = img
        with open(glb, "wb") as f:
            f.write(b"glTF")
        return 12.5

    monkeypatch.setattr(local, "trellis", fake_trellis)
    fal = Fal(key="test", log=lambda m: None)
    fal.uploads["https://cdn/view.png"] = str(image)           # what upload() records
    out = fal.run(config.LOCAL_SEED_MODEL, {"image_url": "https://cdn/view.png"})
    assert seen["image"] == str(image)                          # the local file, not the CDN copy
    dest = tmp_path / "job" / "seed.glb"
    fal.download(out["model_mesh"]["url"], str(dest))
    assert dest.read_bytes() == b"glTF"
    assert not os.path.exists(local.file_path(out["model_mesh"]["url"]))   # the scratch folder is cleaned up
    assert fal.calls == [{"model": config.LOCAL_SEED_MODEL, "seconds": 12.5, "usd": 0.0, "stage": ""}]


def test_local_pictures_are_free_and_keep_their_references(tmp_path, monkeypatch):
    got = {}

    def fake_picture(prompt, path, refs, aspect_ratio, log=print):
        got.update(prompt=prompt, refs=list(refs), aspect=aspect_ratio)
        with open(path, "wb") as f:
            f.write(b"png")
        return 11.0

    monkeypatch.setattr(local, "picture", fake_picture)
    im = Images(log=lambda m: None)
    out = im.generate("a lantern", str(tmp_path / "a.png"), model=config.LOCAL_PICTURE_MODEL,
                      references=["ref.png"], aspect_ratio="1:1")
    assert os.path.exists(out) and got == {"prompt": "a lantern", "refs": ["ref.png"], "aspect": "1:1"}
    assert im.spent() == 0 and im.calls[0]["model"] == config.LOCAL_PICTURE_MODEL


def test_klein_graph_attaches_every_reference_to_both_conditionings():
    g = local.klein_graph("x", ["a.png", "b.png"], 1024, 1024, seed=1)
    assert g["guider"]["inputs"]["positive"] == ["rp1", 0] and g["guider"]["inputs"]["negative"] == ["rn1", 0]
    assert g["rp1"]["inputs"]["conditioning"] == ["rp0", 0] and g["rn0"]["inputs"]["conditioning"] == ["neg0", 0]
    assert g["img1"]["inputs"]["image"] == "b.png"
    plain = local.klein_graph("x", [], 1344, 768, seed=1)
    assert "img0" not in plain and plain["latent"]["inputs"]["width"] == 1344


def test_klein_sizes_are_multiples_of_16():
    assert all(w % 16 == 0 and h % 16 == 0 for w, h in local.KLEIN_SIZES.values())


def test_file_urls_become_windows_paths():
    assert local.file_path("file:///E:/x/seed.glb") == "E:/x/seed.glb"
    assert local.file_path("https://cdn/seed.glb") is None
