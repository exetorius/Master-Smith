"""Pure tests: no network, no Blender. python -m pytest -q"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from mastersmith import config, pricing, skills  # noqa: E402
from mastersmith.spec import Spec  # noqa: E402
from mastersmith.stages.seed import seed_payload  # noqa: E402
from mastersmith.wallet import Wallet  # noqa: E402


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


def test_seed_payload_switches_to_multiview_with_two_views():
    s = Spec(name="A", description="x", category="weapon")
    model, p = seed_payload(s, ["u1"])
    assert model == config.SEED_MODEL and p["image_url"] == "u1" and p["face_limit"] == 360000
    m = Spec(name="A", description="x", category="weapon", seed_vendor="meshy7mv")
    assert seed_payload(m, ["u1", "u2"])[0] == "fal-ai/meshy/v7/multi-image-to-3d"
    model, p = seed_payload(s, ["u1", "u2", "u3"])
    assert model == config.SEED_MULTIVIEW_MODEL and p["image_urls"] == ["u1", "u2", "u3"]
    tiny = Spec(name="A", description="x", category="prop", tri_budget=2000)
    assert seed_payload(tiny, ["u"])[1]["face_limit"] == 150000


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


def test_hi3d_multiview_fills_its_named_slots_from_our_view_list():
    w = Spec(name="A", description="x", category="weapon", seed_vendor="hitem3d3mv")
    model, p = seed_payload(w, ["left", "front", "mirror"])          # the weapon skill: profile, muzzle view, mirrored profile
    assert model == "hitem3d/hi3d/v3.0/multi-view-to-3d"
    assert (p["front_image_url"], p["left_image_url"], p["right_image_url"]) == ("front", "left", "mirror") and "back_image_url" not in p
    assert p["resolution"] == "2048quality" and p["enable_pbr"] and p["export_format"] == "glb" and p["face_count"] == 360000
    v = Spec(name="A", description="x", category="vehicle", seed_vendor="hitem3d3mv")
    p = seed_payload(v, ["f", "l", "b", "r"])[1]                        # the orthographic set
    assert (p["front_image_url"], p["left_image_url"], p["back_image_url"], p["right_image_url"]) == ("f", "l", "b", "r")
    p = seed_payload(v, ["tq", "l"])[1]                                 # no orthographic set: the three-quarter shot stands in for the front
    assert (p["front_image_url"], p["left_image_url"]) == ("tq", "l")
    p = seed_payload(Spec(name="A", description="x", category="prop", seed_vendor="hitem3d3mv"), ["tq", "front"])[1]
    assert p["front_image_url"] == "front" and [k for k in p if k.endswith("_image_url")] == ["front_image_url"]
    assert seed_payload(w, ["only"])[0] == "hitem3d/hi3d/v3.0/image-to-3d"   # one picture: the single-image sibling


def test_wallet_keeps_score_of_spend_and_never_refuses():
    with tempfile.TemporaryDirectory() as d:
        w = Wallet(os.path.join(d, "w.db"))
        assert w.balance("kev") == 0
        hold = w.reserve("kev", 200, "job")          # a hold for the worst case; nothing topped up, nothing refused
        assert w.balance("kev") == -200
        bill = w.settle(hold, 0.5, "done")
        assert bill["charged"] == 50 and bill["refunded"] == 150 and w.balance("kev") == -50
        hold2 = w.reserve("kev", 12, "import")
        bill2 = w.settle(hold2, 0.76, "an imported jet that bought a cockpit")   # past the hold: the true cost is kept
        assert bill2["charged"] == 76 and bill2["refunded"] == -64 and w.balance("kev") == -126
        with pytest.raises(ValueError):
            w.settle(hold2, 0.1)
        hold3 = w.reserve("kev", 50, "cancelled")
        assert w.release(hold3)["refunded"] == 50 and w.balance("kev") == -126
        assert config.credits_for_usd(0.70) == 70
        w.close()      # Windows cannot remove the temp dir while sqlite holds the file


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
    from mastersmith.pipeline import seed_of
    assert seed_of({"seed": {"repainted_glb": __file__, "glb": None}}) is None      # only the seed the finish ran on


def test_reference_lists_and_research_defaults():
    s = Spec(name="Jeep", description="x", category="vehicle", reference_image="a.png", reference_images=["b.png", "a.png"])
    assert s.reference_images == ["a.png", "b.png"] and s.reference_image == "a.png" and s.research is False
    r = Spec(name="Jeep", description="x", category="vehicle", search_query="  Willys   MB  jeep ")
    assert r.search_query == "Willys MB jeep" and r.research is True
    assert Spec(name="X", description="x", search_query="Willys MB", reference_images=["p.png"]).research is False
    from mastersmith.stages.research import query_words, relevant_to
    assert query_words("G-Police Havoc gunship side view") == {"police", "havoc", "gunship"}
    assert relevant_to("Willys MB jeep", {"title": "1943 Willys MB", "image": "https://x/y.jpg"})
    assert not relevant_to("Willys MB jeep", {"title": "letter W wallpaper", "image": "https://x/w.jpg"})


def test_skills_load_front_matter():
    for cat in ("weapon", "vehicle", "aircraft", "helicopter", "character", "prop", "environment"):
        s = skills.load(cat)
        assert s["meta"]["reference_view"] and s["meta"]["origin"] in ("bottom", "center")
        assert s["meta"]["forward_axis"] in ("long", "up") and len(s["body"]) > 200
    assert skills.load("nonsense")["meta"]["default_size_m"] == 1.0


def test_gate_and_package_on_a_synthetic_delivery():
    from mastersmith.stages.gate import check
    from mastersmith.stages.package import write_package
    spec = Spec(name="Crate", description="a crate", category="prop", size_m=0.6, tri_budget=30000)
    with tempfile.TemporaryDirectory() as d:
        for fn in ("SM_Crate.fbx", "T_Crate_BC.png"):
            open(os.path.join(d, fn), "wb").write(b"x")
        report = {"lods": [{"lod": 0, "triangles": 29990}], "maps": [{"role": "BC"}, {"role": "N"}, {"role": "ORM"}],
                  "dimensions_m": [0.6, 0.4, 0.5], "collision": {"triangles": 72}, "files": ["SM_Crate.fbx", "T_Crate_BC.png"],
                  "roughness_mean": 0.55}
        g = check(spec, report, {"score": 7, "verdict": "ship", "issues": []}, d)
        assert g["ok"], g
        bad = check(spec, {**report, "maps": [{"role": "BC"}], "dimensions_m": [1.2, 0.4, 0.5]}, {"score": 3, "issues": ["melted"]}, d)
        assert not bad["ok"] and any("normal map" in w for w in bad["warnings"]) and any("size" in w for w in bad["warnings"])
        pkg = write_package(spec, report, {"review": {"score": 7, "verdict": "ship", "issues": []}, "gate": g}, d)
        assert os.path.exists(os.path.join(d, pkg["zip"])) and os.path.exists(os.path.join(d, "README.txt"))
        assert "Unreal Engine import" in open(os.path.join(d, "README.txt")).read()


def test_brief_fix_category():
    from mastersmith.brief import fix_category
    assert fix_category("vehicle", "F-16C Fighting Falcon fighter jet, grey") == "aircraft"
    assert fix_category("vehicle", "AH-64 Apache attack helicopter") == "helicopter"
    assert fix_category("vehicle", "M1A2 Abrams main battle tank") == "vehicle"
    assert fix_category("weapon", "jet black rifle") == "weapon"


def test_edit_prompt_keeps_unmentioned_design():
    from mastersmith import skills
    from mastersmith.spec import Spec
    from mastersmith.stages.reference import edit_prompt
    spec = Spec(name="Raven", description="bullpup rifle, amber magazine", category="weapon",
                edit_instructions="seat the magazine like an SA80 magazine")
    p = edit_prompt(spec, skills.load("weapon"))
    assert "Change ONLY this: seat the magazine like an SA80 magazine" in p
    assert "Keep every other detail" in p
    plain = edit_prompt(Spec(name="Raven", description="bullpup rifle", category="weapon"), skills.load("weapon"))
    assert "Recreate this exact object" in plain


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
    from mastersmith.pipeline import Job
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

    class Calls:
        def __init__(self):
            self.calls, self.stage = [], ""

        def spent(self):
            return sum(c["usd"] for c in self.calls)
    with tempfile.TemporaryDirectory() as d:
        old = config.OUT_DIR
        config.OUT_DIR = __import__("pathlib").Path(d)
        try:
            job = Job(Spec(name="X", description="x"), "u", None, log=lambda m: None, llm=Calls(), fal=Calls(), images=Calls(), job_id="t")
        finally:
            config.OUT_DIR = old
        job.stage("seed")
        job.fal.calls.append({"usd": 0.6, "stage": job.fal.stage})
        job.stage("reference")
        job.images.calls.append({"usd": 0.07, "stage": job.images.stage})
        job.llm.calls.append({"usd": 0.01, "stage": job.llm.stage})
        bs = job.by_stage()
        assert bs["seed"]["usd"] == 0.6 and bs["seed"]["fal_calls"] == 1 and bs["reference"] == {"usd": 0.08, "pictures": 1, "fal_calls": 0, "llm_calls": 1}
        assert job.breakdown_text().startswith("seed $0.60 (88%)") and abs(job.spent_usd() - 0.68) < 1e-9


def test_provider_balances_and_affordability(monkeypatch):
    from mastersmith import providers

    class R:
        def __init__(self, text=None, js=None):
            self.text, self._js = text, js

        def raise_for_status(self):
            pass

        def json(self):
            return self._js

    def fake_get(url, headers=None, timeout=None):
        if url == providers.FAL_BALANCE:
            return R(text="135.5861")
        if url == providers.OPENROUTER_CREDITS:
            return R(js={"data": {"total_credits": 870, "total_usage": 712.26}})
        return R(js={"data": {"usage_daily": 0.16, "usage_weekly": 11.75, "usage_monthly": 568.56, "limit": None}})

    monkeypatch.setattr(providers.requests, "get", fake_get)
    monkeypatch.setenv("FAL_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    d = providers.balances(force=True)
    assert d["fal"]["usd"] == 135.5861 and d["openrouter"]["usd"] == 157.74 and d["openrouter"]["key_usage_month_usd"] == 568.56
    assert providers.short(d) == "fal $135.59, OpenRouter $157.74"
    providers.check_affordable(100.0, d)
    with pytest.raises(providers.ProviderBalanceLow):
        providers.check_affordable(140.0, d)
    unknown = {"fal": None, "openrouter": None, "errors": {"fal": "down"}, "checked": 0}
    providers.check_affordable(1e6, unknown)       # an outage never blocks a build


def test_reference_estimates_split_the_picture_stage():
    jet = Spec(build_mode="single", name="Jet", description="grey jet", category="aircraft")
    full, pics, rest = pricing.estimate(jet), pricing.estimate_reference(jet), pricing.estimate_after_reference(jet)
    assert 0 < pics["usd"] < full["usd"] and 0 < rest["usd"] < full["usd"]
    assert any(n.startswith("extra views") for n, _ in pics["steps"]) and not any(n.startswith("3D seed") for n, _ in pics["steps"])
    assert any(n.startswith("3D seed") for n, _ in rest["steps"]) and not any(n.startswith("concept picture") for n, _ in rest["steps"])
    assert abs(pics["usd"] + rest["usd"] - full["usd"] - 2 * pricing.LLM_CALL_ALLOWANCE_USD) < 1e-6   # overhead counted in both


def test_build_reuses_approved_reference_or_fails_loudly(tmp_path):
    from mastersmith.pipeline import load_reference
    with pytest.raises(FileNotFoundError):
        load_reference(str(tmp_path))
    pic = tmp_path / "ref_0.png"
    pic.write_bytes(b"x")
    (tmp_path / "reference.json").write_text(json.dumps({"views": [str(pic)], "urls": ["u"], "checks": []}))
    assert load_reference(str(tmp_path))["views"] == [str(pic)]
    pic.unlink()
    with pytest.raises(FileNotFoundError):
        load_reference(str(tmp_path))


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


def test_director_ask_records_the_question_and_options():
    from mastersmith.agent import Director
    from mastersmith.wallet import Wallet
    with tempfile.TemporaryDirectory() as d:
        w = Wallet(os.path.join(d, "w.db"))
        director = Director("kev", w, log=lambda m: None)
        assert director.last_question is None
        out = director._ask({"question": "Realistic or stylized?", "options": ["Realistic", " Stylized, low-poly ", ""]})
        assert out["status"] == "asked" and director.last_question == {"question": "Realistic or stylized?", "options": ["Realistic", "Stylized, low-poly"]}
        assert "error" in director._ask({"question": "?", "options": ["only one"]})
        w.close()


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


def test_build_refuses_without_approved_reference_pictures():
    from mastersmith.agent import Director
    from mastersmith.wallet import Wallet
    with tempfile.TemporaryDirectory() as d:
        w = Wallet(os.path.join(d, "w.db"))
        director = Director("kev", w, log=lambda m: None)
        director.submit = lambda spec_dict: {"job_id": "j1", "status": "queued"}
        director._set_brief({"name": "Havoc", "description": "police gunship", "category": "aircraft"})
        out = director._build({})
        assert "no approved reference" in out.get("error", "")
        assert director._build({"skip_preview": True}).get("job_id") == "j1"       # the customer said to skip
        director.reference = {"job_dir": "/x", "views": ["/x/ref_0.png"], "for": director._design()}
        assert director._build({}).get("job_id") == "j1"                            # approved pictures for THIS design
        director._set_brief({"description": "a different gunship"})
        assert "no approved reference" in director._build({}).get("error", "")     # the design changed: approve again
        w.close()


def test_a_refinish_of_the_last_job_needs_no_new_pictures():
    """Size, budget, engine, rig and glass re-finish the same mesh (an imported model has no pictures at all); any change
    to the design still needs approved pictures."""
    from mastersmith.agent import Director
    from mastersmith.wallet import Wallet
    with tempfile.TemporaryDirectory() as d:
        w = Wallet(os.path.join(d, "w.db"))
        director = Director("kev", w, log=lambda m: None)
        sent = []
        director.submit = lambda spec_dict: sent.append(spec_dict) or {"job_id": "j2", "status": "queued"}
        director.import_model = lambda path, spec_dict: {"job_id": "j1", "status": "queued"}
        director._import_model({"path": "/uploads/rifle.glb", "name": "Rifle", "category": "weapon", "description": "a rifle"})
        assert director.last_job_id == "j1"
        director.job_status = lambda jid: {"status": "done", "spec": {"name": "Rifle", "description": "a rifle", "category": "weapon",
                                                                      "style": "realistic", "edit_instructions": ""}}
        director._set_brief({"rig": True, "tri_budget": 40000})
        assert director._build({}).get("job_id") == "j2" and sent[-1]["rig"] is True
        director._set_brief({"edit_instructions": "make the stock gunmetal"})
        assert "no approved reference" in director._build({}).get("error", "")
        w.close()


def test_single_picture_counts_only_when_the_customer_asked():
    from mastersmith.agent import Director
    from mastersmith.wallet import Wallet
    with tempfile.TemporaryDirectory() as d:
        w = Wallet(os.path.join(d, "w.db"))
        director = Director("kev", w, log=lambda m: None)
        director.messages.append({"role": "user", "content": "an Apache helicopter for Unreal"})
        director._set_brief({"name": "Apache", "description": "AH-64D", "category": "helicopter", "single_picture": True})
        assert director.spec.multiview is True                      # the model asked for one view; the customer did not
        director.messages.append({"role": "user", "content": "just one picture is fine, keep it cheap"})
        director._set_brief({"single_picture": True})
        assert director.spec.multiview is False
        w.close()


def test_outside_links_must_resolve_to_public_addresses(monkeypatch, tmp_path):
    import socket
    from mastersmith import netsafe
    table = {"good.example": "93.184.216.34", "lan.example": "192.168.1.20", "meta.example": "169.254.169.254",
             "loop6.example": "::1", "mapped.example": "::ffff:127.0.0.1"}

    def fake_dns(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror("unknown")
        fam = socket.AF_INET6 if ":" in table[host] else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (table[host], port))]
    monkeypatch.setattr(netsafe.socket, "getaddrinfo", fake_dns)
    netsafe.check_public("https://good.example/a.png")
    for bad in ("http://lan.example/x", "http://meta.example/latest/meta-data", "http://loop6.example/", "http://mapped.example/",
                "file:///etc/passwd", "ftp://good.example/a.png", "http://nowhere.example/"):
        with pytest.raises(netsafe.UnsafeURL):
            netsafe.check_public(bad)

    class Resp:
        def __init__(self, status, location=None, body=b""):
            self.status_code, self.headers, self.body = status, {"location": location} if location else {}, body
            self.is_redirect = location is not None
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def raise_for_status(self):
            pass
        def iter_content(self, n):
            yield self.body
    hops = {"https://good.example/a.png": Resp(302, "http://lan.example/secret"),
            "https://good.example/b.png": Resp(200, body=b"x" * 2048)}
    monkeypatch.setattr(netsafe.requests, "get", lambda url, **k: hops[url])
    with pytest.raises(netsafe.UnsafeURL):                      # a public link that redirects inward is refused
        netsafe.download_public("https://good.example/a.png", str(tmp_path / "a.png"))
    assert os.path.getsize(netsafe.download_public("https://good.example/b.png", str(tmp_path / "b.png"))) == 2048
    with pytest.raises(netsafe.UnsafeURL):
        netsafe.download_public("https://good.example/b.png", str(tmp_path / "c.png"), max_bytes=1024)
    assert not os.path.exists(tmp_path / "c.png")
    from mastersmith.stages import research
    assert research.fetch_image("https://good.example/a.png") == (None, None)


def test_the_job_store_survives_the_worker_and_requests_at_once(tmp_path):
    """The service shares one Store between request threads and the worker: a build logs while the chat polls it."""
    import threading
    from mastersmith.store import Store
    st = Store(str(tmp_path / "jobs.db"))
    st.enqueue("job", "u", "build", {"name": "X"})
    errors = []

    def run(fn, n=300):
        try:
            for i in range(n):
                fn(i)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    work = [lambda i: st.append_log("job", "line %d" % i),
            lambda i: st.job("job", "u"),
            lambda i: st.enqueue("q%d_%d" % (threading.get_ident(), i), "u", "build", {"name": "Y"}),
            lambda i: st.jobs_for("u"),
            lambda i: st.claim_next(),
            lambda i: st.mark_running("job")]
    threads = [threading.Thread(target=run, args=(w,)) for w in work * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(st.job("job")["log"].splitlines()) == 600
