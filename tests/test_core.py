"""Pure tests: no network, no Blender. python -m pytest -q"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from mastersmith import config, pricing, skills  # noqa: E402
from mastersmith.spec import Spec  # noqa: E402


def test_spec_defaults_by_category():
    s = Spec(name="my sniper rifle", description="x", category="weapon")
    assert s.name == "MySniperRifle"
    assert s.tri_budget == 60000 and s.size_m == 1.0 and s.multiview
    c = Spec(name="Orc", description="x", category="character")
    assert c.multiview is True and c.size_m == 1.8      # every category draws its second view for approval
    assert Spec(name="RustyOilDrum", description="x").name == "RustyOilDrum"
    assert Spec(name="rusty oil-drum", description="x").name == "Rusty_oil-drum" or Spec(name="rusty oil-drum", description="x").name == "RustyOilDrum"
    assert Spec(name="QA_Smith_Crate", description="x").name == "QA_Smith_Crate"
    assert Spec(name="Crate", description="x", category="prop").multiview is True
    assert Spec(name="Crate", description="x", category="prop", multiview=False).multiview is False
    assert Spec(name="J", description="x", category="vehicle").glass is True and Spec(name="J", description="x", category="vehicle").rig is False
    assert Spec(name="J", description="x", category="vehicle").multiview is True   # issue #8: guarded orthographic views since 2026-09-18
    assert Spec(name="O", description="x", category="character").rig is True
    assert Spec(name="C", description="x", category="prop").glass is False


def test_estimate_prices_rig_and_glass():
    veh = pricing.estimate(Spec(build_mode="single", name="J", description="x", category="vehicle"))
    veh_rig = pricing.estimate(Spec(build_mode="single", name="J", description="x", category="vehicle", rig=True))
    assert veh_rig["usd"] - veh["usd"] >= pricing.price("fal-ai/hunyuan-3d/v3.1/part")
    chara = pricing.estimate(Spec(build_mode="single", name="O", description="x", category="character"))
    assert any("Meshy" in name for name, _ in chara["steps"])
    assert any("glass masks" in name for name, _ in veh["steps"])
    junk = Spec.from_dict({"name": "", "description": "", "category": "spaceship", "engine": "cryengine", "bogus": 1})
    assert junk.category == "prop" and junk.engine == "unreal" and junk.name == "Asset"


def test_tripo_prices_carry_the_surcharges():
    assert pricing.price("tripo3d/h3.1/image-to-3d") == 0.30
    assert pricing.price("tripo3d/h3.1/image-to-3d", {"geometry_quality": "detailed", "texture_quality": "detailed"}) == 0.60
    assert pricing.price("tripo3d/h3.1/multiview-to-3d", {"geometry_quality": "detailed", "texture_quality": "detailed", "quad": True}) == 0.65
    with pytest.raises(pricing.Unpriced):
        pricing.price("fal-ai/some/new/thing")


def test_estimate_is_the_worst_case_and_multiview_costs_more():
    single = pricing.estimate(Spec(name="A", description="x", category="prop", multiview=False))
    multi = pricing.estimate(Spec(name="A", description="x", category="weapon"))
    assert multi["usd"] > single["usd"] >= 0.60
    assert multi["credits"] == config.credits_for_usd(multi["usd"])


def test_weapon_glass_follows_the_caption():
    assert Spec(name="R", description="a carbine with a 4x ACOG scope on the rail", category="weapon").glass is True
    assert Spec(name="R", description="a bullpup carbine, no sling, no hands, no scope.", category="weapon").glass is False
    assert Spec(name="R", description="a plain pump shotgun with a wooden stock", category="weapon").glass is False
    assert Spec(name="R", description="a pistol with a red-dot sight and a weapon light", category="weapon").glass is True
    assert Spec(name="R", description="a plain rifle", category="weapon", glass=True).glass is True      # an explicit ask wins
    assert Spec(name="R", description="a sedan", category="vehicle").glass is True


def test_assembly_is_opt_in_unless_the_default_is_switched_on(monkeypatch):
    from mastersmith.spec import assembly_wanted
    plain = Spec(name="B", description="a bullpup carbine, no scope", category="weapon")
    assert not assembly_wanted(plain) and pricing.estimate(plain)["build_mode"] == "single"
    monkeypatch.setattr(config, "ASSEMBLY_DEFAULT", True)
    assert assembly_wanted(plain)
    monkeypatch.setattr(config, "ASSEMBLY_DEFAULT", False)
    w = Spec(name="B", description="a bullpup carbine, no scope", category="weapon", build_mode="assembly")
    assert assembly_wanted(w) and pricing.estimate(w)["build_mode"] == "assembly"
    names = [n for n, _ in pricing.estimate(w)["steps"]]
    assert any(n.startswith("parts plan") for n in names) and not any(n.startswith("3D seed") for n in names)
    assert not assembly_wanted(Spec(name="B", description="x", category="weapon", build_mode="single"))
    assert not assembly_wanted(Spec(name="C", description="a crate", category="prop"))
    assert assembly_wanted(Spec(name="C", description="a crate", category="prop", build_mode="assembly"))
    assert Spec(name="B", description="x", build_mode="nonsense").build_mode is None
    rw = pricing.estimate_rework(w)                         # a re-finish is one mesh: no plan, no parts
    assert not any(n.startswith(("parts plan", "code parts")) for n, _ in rw["steps"])


def test_rework_estimate_drops_the_seed_and_the_pictures():
    jet = Spec(build_mode="single", name="Jet", description="grey attack jet", category="aircraft", size_m=-1)
    full = pricing.estimate(jet)
    re = pricing.estimate_rework(jet, "refinish")
    names = [n for n, _ in re["steps"]]
    assert not any(n.startswith("3D seed") for n in names)
    assert not any(n.startswith(pricing.PICTURE_STEPS) for n in names)
    assert any(n.startswith("glass masks") for n in names) and any(n.startswith("review") for n in names)
    assert 0 < re["usd"] < full["usd"]


def test_old_specs_with_the_removed_repair_fields_still_load():
    """Jobs and chats stored before 2026-09-26 carry the post-op repair fields; they load, and the fields are gone."""
    old = {"name": "Havoc", "description": "police gunship", "category": "aircraft", "tri_budget": 90000,
           "texture_fixes": ["delight", "preserve_seed_maps"], "remove_parts": ["the extra cylinder"],
           "add_parts": [{"name": "Cabin", "phrase": "the cockpit interior", "anchor": "glass", "place": "inside",
                          "provides": ["seat"], "yaw_degrees": 180}],
           "retexture": True, "retexture_parts": [{"phrase": "the stock", "color": "#333333"}],
           "protect_parts": [{"phrase": "the magazine"}], "cockpit": True, "hybrid": True, "repaint": "pictures",
           "part_seeds": [{"phrase": "the magazine"}]}
    s = Spec.from_dict(old)
    assert s.name == "Havoc" and s.category == "aircraft" and s.tri_budget == 90000 and s.glass
    d = s.to_dict()
    for gone in ("texture_fixes", "remove_parts", "add_parts", "retexture", "retexture_parts", "protect_parts", "cockpit",
                 "hybrid", "repaint", "part_seeds"):
        assert gone not in d, gone
    assert pricing.estimate(s)["usd"] > 0 and pricing.estimate_rework(s)["usd"] > 0


def test_reference_lists_and_research_defaults():
    s = Spec(name="Jeep", description="x", category="vehicle", reference_image="a.png", reference_images=["b.png", "a.png"])
    assert s.reference_images == ["a.png", "b.png"] and s.reference_image == "a.png" and s.research is False
    r = Spec(name="Jeep", description="x", category="vehicle", search_query="  Willys   MB  jeep ")
    assert r.search_query == "Willys MB jeep" and r.research is True
    assert Spec(name="X", description="x", search_query="Willys MB", reference_images=["p.png"]).research is False


def test_skills_load_front_matter():
    for cat in ("weapon", "vehicle", "aircraft", "helicopter", "character", "prop", "environment"):
        s = skills.load(cat)
        assert s["meta"]["reference_view"] and s["meta"]["origin"] in ("bottom", "center")
        assert s["meta"]["forward_axis"] in ("long", "up") and len(s["body"]) > 200
    assert skills.load("nonsense")["meta"]["default_size_m"] == 1.0


def test_package_on_a_synthetic_delivery():
    from mastersmith.stages.package import write_package
    spec = Spec(name="Crate", description="a crate", category="prop", size_m=0.6, tri_budget=30000)
    with tempfile.TemporaryDirectory() as d:
        for fn in ("SM_Crate.fbx", "T_Crate_BC.png"):
            open(os.path.join(d, fn), "wb").write(b"x")
        report = {"lods": [{"lod": 0, "triangles": 29990}], "maps": [{"role": "BC"}, {"role": "N"}, {"role": "ORM"}],
                  "dimensions_m": [0.6, 0.4, 0.5], "collision": {"triangles": 72}, "files": ["SM_Crate.fbx", "T_Crate_BC.png"],
                  "roughness_mean": 0.55}
        pkg = write_package(spec, report, {"review": {"score": 7, "verdict": "ship", "issues": []}, "gate": {"ok": True}}, d)
        assert os.path.exists(os.path.join(d, pkg["zip"])) and os.path.exists(os.path.join(d, "README.txt"))
        assert "Unreal Engine import" in open(os.path.join(d, "README.txt")).read()


def test_portable_spec_drops_local_pictures():
    from mastersmith.spec import Spec
    sp = Spec(name="R", description="rifle", category="weapon", reference_images=["/job/abc/previous_reference.png", "https://cdn/x.png"])
    d = sp.portable()
    assert d["reference_images"] == ["https://cdn/x.png"] and d["reference_image"] == "https://cdn/x.png"
    sp2 = Spec.from_dict({**sp.to_dict(), "reference_images": ["/job/abc/previous_reference.png"]}).drop_local_pictures()
    assert sp2.reference_images == [] and sp2.reference_image == ""
    # an override that names a URL wins over a stale stored primary
    sp3 = Spec.from_dict({**sp.to_dict(), "reference_image": "https://cdn/v1.png", "reference_images": ["https://cdn/v1.png"]})
    assert sp3.reference_images[0] == "https://cdn/v1.png"


def test_image_client_and_cost_breakdown():
    import base64
    import io
    import json as _json

    from PIL import Image

    from mastersmith import images as im_mod
    from mastersmith.images import ImageRefused, Images
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, format="PNG")
    good = {"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode(), "media_type": "image/png"}], "usage": {"cost": 0.0123}}

    class Resp:
        def __init__(self, status, body, text=""):
            self.status_code, self._body, self.text = status, body, text or _json.dumps(body)

        def json(self):
            return self._body

    class Http:
        def __init__(self, answers):
            self.answers, self.sent = list(answers), []

        def post(self, url, headers=None, data=None, timeout=None):
            self.sent.append(_json.loads(data))
            return self.answers.pop(0)
    monkeypatch_sleep = im_mod.time.sleep
    im_mod.time.sleep = lambda s: None
    try:
        cl = Images(key="k", log=lambda m: None)
        with tempfile.TemporaryDirectory() as d:
            cl.http = Http([Resp(200, good)])
            cl.stage = "reference"
            out = cl.generate("a crate", os.path.join(d, "a.png"), model="google/gemini-3.1-flash-image", references=[])
            assert Image.open(out).size == (8, 8) and cl.spent() == 0.0123 and cl.calls[0]["stage"] == "reference"
            cl.http = Http([Resp(200, good)])
            cl.generate("edit", os.path.join(d, "b.png"), model="google/gemini-3.1-flash-image", references=[out, "https://x/y.png"])
            refs = cl.http.sent[-1]["input_references"]
            assert refs[0]["image_url"]["url"].startswith("data:image/png;base64,") and refs[1]["image_url"]["url"] == "https://x/y.png"
            cl.http = Http([Resp(400, {"error": {"message": "Gemini could not generate an image (STOP)"}}, "could not generate")])
            with pytest.raises(ImageRefused):
                cl.generate("gore", os.path.join(d, "c.png"), model="google/gemini-3.1-flash-image")
            with pytest.raises(pricing.Unpriced):
                cl.generate("x", os.path.join(d, "f.png"), model="nobody/unknown-image")
    finally:
        im_mod.time.sleep = monkeypatch_sleep


def test_reference_estimates_split_the_picture_stage():
    jet = Spec(build_mode="single", name="Jet", description="grey jet", category="aircraft")
    full, pics, rest = pricing.estimate(jet), pricing.estimate_reference(jet), pricing.estimate_after_reference(jet)
    assert 0 < pics["usd"] < full["usd"] and 0 < rest["usd"] < full["usd"]
    assert any(n.startswith("extra views") for n, _ in pics["steps"]) and not any(n.startswith("3D seed") for n, _ in pics["steps"])
    assert any(n.startswith("3D seed") for n, _ in rest["steps"]) and not any(n.startswith("concept picture") for n, _ in rest["steps"])
    assert abs(pics["usd"] + rest["usd"] - full["usd"] - 2 * pricing.LLM_CALL_ALLOWANCE_USD) < 1e-6   # overhead counted in both


def test_estimate_prices_the_chosen_mesh_vendor():
    cat = {v["key"]: v for v in pricing.vendor_catalogue()}
    assert cat["tripo"]["usd"] == 0.6 and cat["hitem3d3"]["usd"] == 2.1 and cat["hitem3d3mv"]["usd"] == 2.1 and cat["hitem3d3mv"]["multiview"]
    default = pricing.estimate(Spec(build_mode="single", name="R", description="rifle", category="weapon"))
    dear = pricing.estimate(Spec(build_mode="single", name="R", description="rifle", category="weapon", seed_vendor="hitem3d3"))
    assert any(n.startswith("3D seed (Tripo") for n, _ in default["steps"])
    assert any(n == "3D seed (Hitem3D v3 (2048))" for n, _ in dear["steps"]) and dear["usd"] > default["usd"]
    mv = pricing.estimate(Spec(build_mode="single", name="R", description="rifle", category="weapon", seed_vendor="hitem3d3mv"))
    assert any(n == "3D seed (Hitem3D v3 multi-view (2048))" for n, _ in mv["steps"]) and mv["usd"] == dear["usd"]
    assert pricing.seed_vendor(Spec(build_mode="single", name="R", description="r", seed_vendor="nonsense"))["key"] == "tripo"


def test_picture_model_choice_drives_the_estimate_and_the_catalogue():
    cat = pricing.picture_catalogue()
    assert cat[0]["id"] == config.CONCEPT_MODEL and cat[0]["default"] and "Nano Banana 2" in cat[0]["label"]
    assert not any(c["id"].endswith("-preview") for c in cat)
    crate = Spec(name="Crate", description="oak crate", category="prop")
    lite = Spec(name="Crate", description="oak crate", category="prop", picture_model="google/gemini-3.1-flash-lite-image")
    assert pricing.concept_model(lite) == "google/gemini-3.1-flash-lite-image" == pricing.edit_model(lite)
    assert pricing.edit_model(crate) == config.EDIT_MODEL
    assert pricing.estimate(lite)["usd"] < pricing.estimate(crate)["usd"]
    bogus = Spec(name="Crate", description="oak crate", category="prop", picture_model="nobody/unknown")
    assert pricing.concept_model(bogus) == pricing.concept_model(crate)     # an unknown id falls back to the config


def test_pictures_come_from_fal_by_default_and_edits_use_the_edit_endpoint():
    from mastersmith.images import fal_endpoint, fal_payload
    cat = pricing.picture_catalogue()
    assert cat[0]["id"] == "fal-ai/nano-banana-2" and cat[0]["default"] and cat[0]["provider"] == "fal.ai"
    assert not any(c["id"].endswith("/edit") for c in cat) and any(c["provider"] == "OpenRouter" for c in cat)
    assert fal_endpoint("fal-ai/nano-banana-2", True) == "fal-ai/nano-banana-2/edit"
    assert fal_endpoint("fal-ai/nano-banana-2", False) == "fal-ai/nano-banana-2"
    assert fal_endpoint("fal-ai/nano-banana-2/edit", True) == "fal-ai/nano-banana-2/edit"
    p = fal_payload("fal-ai/nano-banana-2/edit", "a crate", ["https://x/a.png"], "4:3", "1K")
    assert p == {"prompt": "a crate", "aspect_ratio": "4:3", "resolution": "1K", "num_images": 1, "output_format": "png", "image_urls": ["https://x/a.png"]}
    f = fal_payload("fal-ai/flux-2", "a crate", (), "1:1", "1K")
    assert f["image_size"] == "square_hd" and "image_urls" not in f
    assert pricing.image_price("fal-ai/nano-banana-2/edit") == 0.08 and pricing.price("fal-ai/nano-banana-pro") == 0.15
    crate = Spec(name="Crate", description="oak crate", category="prop")
    assert pricing.concept_model(crate) == "fal-ai/nano-banana-2" and pricing.edit_model(crate) == "fal-ai/nano-banana-2"


def test_six_view_gate_needs_every_view_ok():
    from mastersmith.stages.review import six_view_gate
    ok = {v: "ok" for v in ("left", "right", "front", "back", "top", "bottom")}
    assert six_view_gate({"score": 8, "verdict": "ship", "views": dict(ok)})["verdict"] == "ship"
    j = six_view_gate({"score": 8, "verdict": "ship", "views": {**ok, "front": "defect: barrel off the centreline"}})
    assert j["verdict"] == "rebuild" and j["score"] == 5 and "front view: barrel off the centreline" in j["issues"][-1]
    assert six_view_gate({"score": 8, "verdict": "ship", "views": {**ok, "bottom": "minor: soft grip"}})["verdict"] == "ship with notes"
    assert six_view_gate({"score": 8, "verdict": "ship", "views": {"left": "ok"}})["verdict"] == "rebuild"


def test_merged_review_keeps_agreed_defects_and_softens_lone_ones():
    from mastersmith.stages.review import merge_reviews
    ok = {v: "ok" for v in ("left", "right", "front", "back", "top", "bottom")}
    a = {"score": 5, "verdict": "rebuild", "issues": ["x"], "views": {**ok, "front": "defect: knob on both sides", "top": "defect: barrel angled"}}
    b = {"score": 7, "verdict": "ship with notes", "issues": ["y"], "views": {**ok, "front": "minor: busy front", "top": "ok"}}
    m = merge_reviews(a, b)
    assert m["views"]["front"].startswith("defect") and m["views"]["top"].startswith("minor")
    assert m["score"] == 5 and m["verdict"] == "rebuild"
