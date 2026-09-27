"""Repairs should use evidence and existing seeds, not buy another copy of the same failure."""
import json
from pathlib import Path
from types import SimpleNamespace

from mastersmith.agent import Director, TOOLS
from mastersmith.blender.finish_policy import detail_bake_skip_reason
from mastersmith.diagnose import diagnose
from mastersmith.repair import assembly_conflicts, part_components, plan_repair
from mastersmith.spec import Spec


def parts():
    return [{"name": "CockpitInterior", "phrase": "a cockpit seat, instrument panel, consoles and flight stick",
             "place": "inside", "anchor": "glass", "seed": "/saved/cockpit.fbx"},
            {"name": "Joystick", "phrase": "a single flight control stick", "place": "inside", "anchor": "glass"},
            {"name": "CabinShell", "phrase": "floor pan, two side walls and rear bulkhead, no seat, no consoles",
             "place": "inside", "anchor": "glass"}]


def test_baker_refuses_to_mix_independent_material_atlases():
    assert detail_bake_skip_reason(["Body", "Cockpit"]) is not None
    assert detail_bake_skip_reason(["Body", "Body"]) is None
    assert detail_bake_skip_reason(["Body"], preserve_seed_maps=True) is not None


def test_exclusions_are_not_treated_as_owned_parts():
    assert part_components(parts()[2]) == {"floor", "walls", "bulkhead"}
    assert part_components({"phrase": "a seat without pedals or flight stick"}) == {"seat"}


def test_duplicate_control_is_caught_but_two_individual_seats_are_allowed():
    conflicts = assembly_conflicts(parts())
    assert len(conflicts) == 1 and conflicts[0]["part"] == "Joystick"
    seats = [{"name": n, "phrase": "a pilot seat", "place": "inside", "anchor": "glass"} for n in ("A", "B")]
    assert assembly_conflicts(seats) == []


def test_director_rejects_duplicate_components_before_any_provider_call():
    d = Director.__new__(Director)
    d.spec = Spec(name="Havoc", description="gunship", add_parts=parts())
    result = d._build({})
    assert "duplicates" in result["error"] and result["conflicts"][0]["part"] == "Joystick"


def test_havoc_floor_pedestal_is_not_a_second_cabin_component():
    additions = parts()
    additions[1]["phrase"] = (
        "a single sci-fi flight control stick on a short round floor pedestal: "
        "a tall vertical shaft with a moulded pistol-grip handle and trigger, dark grey composite"
    )
    assert part_components(additions[1]) == {"stick"}
    assert assembly_conflicts(additions)[0]["part"] == "Joystick"


def test_provides_inventory_round_trips_and_is_in_tool_schema():
    s = Spec(name="Havoc", description="gunship", add_parts=[{**parts()[0], "provides": ["seat", "panel", "junk"]}])
    assert s.add_parts[0]["provides"] == ["seat", "panel"]
    tool = next(t["function"] for t in TOOLS if t["function"]["name"] == "set_brief")
    assert "provides" in tool["parameters"]["properties"]["add_parts"]["items"]["properties"]
    assert any(t["function"]["name"] == "plan_repair" for t in TOOLS)


def test_havoc_plan_reuses_body_and_removes_redundant_additions():
    job = {"id": "havoc", "status": "done", "spec": {"add_parts": parts()}, "summary": {
        "bake": {"normal_detail_std": 0.05}, "review": {
            "issues": ["Heavy faceted baking artifact pattern"],
            "assembly_checks": [{"name": "CabinShell", "status": "fail", "evidence": "overlaps the cockpit shell"},
                                {"name": "Pedals", "status": "unverified", "evidence": "hidden by the dash"}]}}}
    plan = plan_repair(job)
    assert plan["automatic_build"] is False and plan["requires_confirmation"]
    finish = next(a for a in plan["actions"] if a["target"] == "finishing")
    assert finish["changes"]["texture_fixes"] == ["preserve_seed_maps"]
    assembly = next(a for a in plan["actions"] if a["target"] == "assembly")
    assert assembly["omit_additions"] == ["CabinShell", "Joystick"]
    assert assembly["changes"]["add_parts"][0]["seed"] == "/saved/cockpit.fbx"
    assert any(a["target"] == "inspection" for a in plan["actions"])
    assert len(job["spec"]["add_parts"]) == 3


def test_bake_regression_does_not_recommend_a_new_vendor(tmp_path):
    spec = Spec(name="Havoc", description="gunship", category="aircraft")
    result = {"delivery": {"bake": {"normal_detail_std": 0.05}},
              "review": {"score": 4, "verdict": "rebuild", "issues": ["low-poly faceted baking artifacts"]}}
    findings = diagnose(result, str(tmp_path), spec)
    assert any("preserve_seed_maps" in (f.get("fix") or {}).get("texture_fixes", []) for f in findings)
    assert not any("seed_vendor" in (f.get("fix") or {}) for f in findings)


def test_picture_prompt_respects_module_exclusions(tmp_path):
    from mastersmith.stages.parts import make_added_part
    seen = []
    def generate(prompt, path, **kwargs):
        seen.append(prompt)
        Path(path).write_bytes(b"mock picture")
    fal = SimpleNamespace(upload=lambda _: "https://example.invalid/ref.png",
                          run=lambda *a: {"model_glb": {"url": "https://example.invalid/model.glb"}},
                          download=lambda url, path: Path(path).write_bytes(b"mock seed"))
    job = SimpleNamespace(dir=str(tmp_path), log=lambda _: None, fal=fal, images=SimpleNamespace(generate=generate),
                          llm=SimpleNamespace(vision=lambda *a: json.dumps({"ok": True, "score": 9})))
    part = {"name": "Seat", "phrase": "the cockpit seat only, no floor, no controls, no panel", "place": "inside"}
    assert make_added_part(job, Spec(name="Havoc", description="gunship"), part, None)
    assert "no floor, no controls, no panel" in seen[0]
    assert "controls and floor pan as one open assembly" not in seen[0]
