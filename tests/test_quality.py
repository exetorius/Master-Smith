"""Acceptance and review tests: no live models, images, provider calls or money."""
import json
from types import SimpleNamespace

import pytest

from mastersmith.quality import assess
from mastersmith.spec import Spec
from mastersmith.stages.gate import check
from mastersmith.stages.review import review


def crate():
    return Spec(name="Crate", description="a weathered oak crate", category="prop")


@pytest.mark.parametrize("score", [5, 8, 10])
def test_rebuild_verdict_never_becomes_usable(score):
    q = assess(crate(), {}, {"score": score, "verdict": "rebuild", "issues": ["bad geometry"]})
    assert not q["accepted"] and q["status"] == "needs_attention"
    assert "rebuild" in q["issues"][0]


@pytest.mark.parametrize("review_answer, accepted", [
    ({"score": 8, "verdict": "ship with notes", "issues": []}, True),
    ({"score": 4, "verdict": "ship", "issues": []}, False),              # a low score is not acceptance
    ({"score": 8, "verdict": "maybe"}, False),                           # an unknown verdict is inconclusive
    ({"verdict": "ship"}, False),                                        # no score
    (None, False),
])
def test_acceptance_needs_a_shipping_verdict_and_a_fair_score(review_answer, accepted):
    assert assess(crate(), {}, review_answer)["accepted"] is accepted


def test_gate_separates_technical_completion_from_visual_acceptance(tmp_path):
    spec = crate()
    report = {"lods": [{"triangles": spec.tri_budget}], "maps": [{"role": r} for r in ("BC", "N", "ORM")]}
    gate = check(spec, report, {"score": 5, "verdict": "rebuild"}, str(tmp_path))
    assert gate["technical_ok"] is True
    assert gate["ok"] is False and not gate["quality"]["accepted"]


def review_job(tmp_path, answer, spec=None):
    seen = {}

    def vision(prompt, images, **kwargs):
        seen.update(prompt=prompt, images=images)
        return answer
    job = SimpleNamespace(dir=str(tmp_path), spec=spec or crate(), llm=SimpleNamespace(vision=vision), log=lambda _: None)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    for name in ("preview_iso.png", "preview_side.png", "preview_seed_iso.png"):
        (delivery / name).write_bytes(b"mock image; no provider sees this")
    return job, seen, [str(delivery / n) for n in ("preview_iso.png", "preview_side.png")]


def test_reviewer_gets_the_reference_the_renders_and_the_source_seed(tmp_path):
    ref = tmp_path / "ref_0.png"
    ref.write_bytes(b"mock reference")
    job, seen, renders = review_job(tmp_path, json.dumps({"score": 8, "verdict": "ship", "issues": []}))
    result = review(job, str(ref), renders, {"source_renders": ["preview_seed_iso.png"]})
    assert result["verdict"] == "ship"
    assert len(seen["images"]) == 4 and seen["images"][0] == str(ref)
    assert "SOURCE SEED before finishing" in seen["prompt"] and "a weathered oak crate" in seen["prompt"]
    for gone in ("assembly_checks", "requested_parts", "remove_parts", "texture_fixes", "CANOPY-HIDDEN"):
        assert gone not in seen["prompt"], gone


@pytest.mark.parametrize("answer", ["not JSON", "[]", ""])
def test_unreadable_review_cannot_pass(tmp_path, answer):
    job, _, renders = review_job(tmp_path, answer)
    result = review(job, None, renders, {})
    assert result["verdict"] == "rebuild" and result["issues"]
    assert not assess(job.spec, {}, result)["accepted"]


def test_no_delivered_render_means_no_review_call(tmp_path):
    job, seen, _ = review_job(tmp_path, json.dumps({"score": 9, "verdict": "ship"}))
    result = review(job, None, [str(tmp_path / "missing.png")], {})
    assert seen == {} and result["verdict"] == "rebuild"
