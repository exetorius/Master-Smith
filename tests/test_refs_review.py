"""`ms refs` keeps the owner's verdict per reference picture in ref/review.json and writes nothing else (2026-09-29)."""
import json

import pytest
from PIL import Image

from mastersmith import refs_review


def _job(out, name="Rifle"):
    job = out / name
    (job / "ref" / "unused").mkdir(parents=True)
    (job / "brief.json").write_text(json.dumps({"name": name, "category": "weapon", "size_m": 0.8, "description": "a rifle"}))
    for f in ("ref_0.png", "ref_front.png", "customer_ref_0.png", "unused/ref_front.png"):
        Image.new("RGB", (40, 20), "white").save(job / "ref" / f)
    return job


def test_record_review_writes_and_clears(tmp_path):
    job = _job(tmp_path)
    refs_review.record_review(str(tmp_path), "Rifle", "ref_0.png", "approved")
    review = refs_review.record_review(str(tmp_path), "Rifle", "ref_front.png", "redraw", "look straight down the barrel")
    assert review["ref_0.png"]["status"] == "approved"
    assert review["ref_front.png"] == json.loads((job / "ref" / "review.json").read_text())["ref_front.png"]
    assert review["ref_front.png"]["note"] == "look straight down the barrel"
    assert "ref_0.png" not in refs_review.record_review(str(tmp_path), "Rifle", "ref_0.png", "")


@pytest.mark.parametrize("job,file,status", [
    ("Rifle", "ref_0.png", "maybe"),                  # not a verdict
    ("Rifle", "customer_ref_0.png", "approved"),      # the owner's photo is for comparison only
    ("Rifle", "unused/ref_front.png", "approved"),    # a moved-aside draft
    ("Rifle", "../brief.json", "approved"),
    ("..", "ref_0.png", "approved"),
    ("Nope", "ref_0.png", "approved"),
])
def test_record_review_refuses_anything_but_a_ref_picture(tmp_path, job, file, status):
    _job(tmp_path)
    with pytest.raises(ValueError):
        refs_review.record_review(str(tmp_path), job, file, status)


def test_page_lists_top_level_pictures_with_photos_marked(tmp_path):
    _job(tmp_path)
    (tmp_path / "Empty").mkdir()
    (tmp_path / "Empty" / "brief.json").write_text("{}")
    assert refs_review.all_jobs(str(tmp_path)) == ["Rifle"]
    data = refs_review.page_data(str(tmp_path), ["Rifle"])["jobs"][0]
    assert [(p["file"], p["photo"]) for p in data["pictures"]] == [
        ("customer_ref_0.png", True), ("ref_0.png", False), ("ref_front.png", False)]
    page = open(refs_review.write_page(str(tmp_path), ["Rifle"]), encoding="utf-8").read()
    assert "__DATA__" not in page and "Rifle/ref/ref_0.png?v=" in page
