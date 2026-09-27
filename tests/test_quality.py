"""Havoc regression tests: no live models, images, provider calls or money."""
import json
from types import SimpleNamespace

import pytest

from mastersmith.agent import Director
from mastersmith.diagnose import diagnose
from mastersmith.quality import assess
from mastersmith.spec import Spec
from mastersmith.stages.gate import check
from mastersmith.stages.review import review


def cabin_spec():
    return Spec(name="Havoc", description="a sci-fi gunship", category="aircraft", glass=True,
                add_parts=[{"name": "Cabin", "phrase": "a cabin with a seat and console, no flight stick",
                            "anchor": "glass", "place": "inside", "size_m": 2.2}])


def assembly_report():
    return {"added_parts": [{"name": "Cabin", "faces_added": 5000}],
            "review_renders": ["preview_assembly_iso.png", "preview_assembly_top.png"]}


def passed_review():
    return {"score": 8, "verdict": "ship with notes", "issues": [], "assembly_ok": True,
            "assembly_checks": [{"name": "Cabin", "status": "pass",
                                 "evidence": "Seat rests on the floor, facing the panel with clear leg space."}]}


@pytest.mark.parametrize("score", [5, 8, 10])
def test_rebuild_verdict_never_becomes_usable(score, tmp_path):
    spec = Spec(name="Crate", description="crate", category="prop")
    r = {"review": {"score": score, "verdict": "rebuild", "issues": ["bad geometry"]}}
    assert not assess(spec, {}, r["review"])["accepted"]
    finding = diagnose(r, str(tmp_path), spec)[0]["finding"]
    assert "not accepted" in finding and "rebuild" in finding


def test_gate_separates_technical_completion_from_visual_acceptance(tmp_path):
    spec = Spec(name="Crate", description="crate", category="prop")
    report = {"lods": [{"triangles": spec.tri_budget}], "maps": [{"role": r} for r in ("BC", "N", "ORM")]}
    gate = check(spec, report, {"score": 5, "verdict": "rebuild"}, str(tmp_path))
    assert gate["technical_ok"] is True
    assert gate["ok"] is False and not gate["quality"]["accepted"]


@pytest.mark.parametrize("defect", ["missing", "skipped", "unverified", "wrong_name", "no_evidence", "no_closeup"])
def test_each_requested_part_needs_fit_and_visual_evidence(defect):
    report, result = assembly_report(), passed_review()
    if defect == "missing":
        report["added_parts"] = []
    elif defect == "skipped":
        report["added_parts"][0]["skipped"] = "missing anchor"
    elif defect == "unverified":
        result["assembly_checks"][0]["status"] = "unverified"
    elif defect == "wrong_name":
        result["assembly_checks"][0]["name"] = "OtherPart"
    elif defect == "no_evidence":
        result["assembly_checks"][0]["evidence"] = ""
    else:
        report["review_renders"] = []
    assert not assess(cabin_spec(), report, result)["accepted"]


def test_complete_assembly_can_pass():
    assert assess(cabin_spec(), assembly_report(), passed_review())["accepted"]


def test_diagnosis_does_not_hide_requested_interior(tmp_path):
    r = {"review": {"score": 5, "verdict": "rebuild", "issues": ["milky canopy hides the hollow cabin"]},
         "delivery": assembly_report()}
    findings = diagnose(r, str(tmp_path), cabin_spec())
    assert not any("dark_canopy" in (f.get("fix") or {}).get("texture_fixes", []) for f in findings)
    assert any("not readable" in f["finding"] for f in findings)


def review_job(tmp_path, answer):
    seen = {}
    def vision(prompt, images, **kwargs):
        seen.update(prompt=prompt, images=images)
        return answer
    job = SimpleNamespace(dir=str(tmp_path), spec=cabin_spec(), llm=SimpleNamespace(vision=vision), log=lambda _: None)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    for name in ["preview_iso.png", "preview_side.png", *assembly_report()["review_renders"]]:
        (delivery / name).write_bytes(b"mock image; no provider sees this")
    return job, seen, [str(delivery / n) for n in ("preview_iso.png", "preview_side.png")]


def test_review_receives_parts_and_closeups_without_a_reference(tmp_path):
    job, seen, renders = review_job(tmp_path, json.dumps(passed_review()))
    result = review(job, None, renders, assembly_report())
    assert result["verdict"] == "ship with notes"
    assert len(seen["images"]) == 4
    assert "no flight stick" in seen["prompt"] and "close-up" in seen["prompt"]


@pytest.mark.parametrize("answer", ["not JSON", "[]", '{"score": 9, "verdict": "ship"}'])
def test_unreadable_or_incomplete_review_cannot_pass(tmp_path, answer):
    job, _, renders = review_job(tmp_path, answer)
    result = review(job, None, renders, assembly_report())
    assert result["verdict"] == "rebuild" and result["assembly_ok"] is False


def test_missing_closeups_cannot_be_invented_by_reviewer(tmp_path):
    job, _, renders = review_job(tmp_path, json.dumps(passed_review()))
    for name in assembly_report()["review_renders"]:
        (tmp_path / "delivery" / name).unlink()
    assert review(job, None, renders, assembly_report())["verdict"] == "rebuild"


def test_reviewer_gets_source_and_explicitly_labeled_cutaway(tmp_path):
    job, seen, renders = review_job(tmp_path, json.dumps(passed_review()))
    report = {**assembly_report(), "source_renders": ["preview_seed_iso.png"],
              "inspection_renders": ["preview_inspection_canopy_hidden.png"]}
    for name in report["source_renders"] + report["inspection_renders"]:
        (tmp_path / "delivery" / name).write_bytes(b"mock image")
    review(job, None, renders, report)
    assert len(seen["images"]) == 6
    assert "SOURCE SEED before finishing" in seen["prompt"]
    assert "CANOPY-HIDDEN diagnostic cutaway, NOT delivered appearance" in seen["prompt"]


def test_director_blocks_unchanged_failed_assembly_but_allows_targeted_change():
    # Construct without an LLM or wallet: this guard must run before either can spend anything.
    director = Director.__new__(Director)
    director.spec = cabin_spec()
    director.last_job_id = "old"
    previous_spec = director.spec.to_dict()
    previous_spec["picture_model"] = "service/default-image-model"
    previous_spec["add_parts"][0]["seed"] = "/cached/seed.fbx"
    director.job_status = lambda _: {"status": "done", "spec": previous_spec,
                                   "summary": {"review": {"score": 5, "verdict": "rebuild"}}}
    calls = []
    director.submit = lambda *a: calls.append(a) or {"job_id": "new", "status": "queued"}
    director.reference = None
    assert "unchanged" in director._build({})["error"]
    assert calls == []
    director.spec.add_parts[0]["offset_m"][0] = 0.25
    assert director._build({})["job_id"] == "new"
    assert len(calls) == 1


@pytest.mark.parametrize("yaw", [-180, -90, 0, 90, 180])
def test_explicit_part_facing_is_preserved_and_changes_repair_plan(yaw):
    spec = cabin_spec()
    original = Director._repair_plan(spec.to_dict())
    spec.add_parts[0]["yaw_degrees"] = yaw
    spec = Spec.from_dict(spec.to_dict())
    assert spec.add_parts[0]["yaw_degrees"] == yaw
    assert Director._repair_plan(spec.to_dict()) != original


def test_explicit_facing_does_not_call_vision(tmp_path, monkeypatch):
    from mastersmith.stages.parts import orient_added_part
    from mastersmith.stages import finish
    part_dir = tmp_path / "part_Cabin"
    part_dir.mkdir()
    (part_dir / "work.blend").write_bytes(b"prepared mesh placeholder")
    monkeypatch.setattr(finish, "_blender", lambda *a: None)
    job = SimpleNamespace(work_dir=str(tmp_path), log=lambda _: None)
    result = orient_added_part(job, cabin_spec(), {"name": "Cabin", "size_m": 2.2, "yaw_degrees": 180}, "seed.fbx")
    assert result["yaw"] == 180 and result["facing_source"] == "explicit"
