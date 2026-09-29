"""A code part can opt out of the reference detail projection (2026-09-28: the projected hood interior of the
drawn sight printed a pale patch on the coded sight)."""
from mastersmith.stages.plan import validate_plan


def test_reference_detail_flag_is_kept_and_defaults_true():
    raw = {"parts": [
        {"name": "FrontSight", "method": "code", "side_box": [70, 80, 0, 20], "front_span": [40, 60], "reference_detail": False},
        {"name": "Barrel", "method": "code", "side_box": [80, 95, 25, 33], "front_span": [45, 55]}]}
    plan = validate_plan(raw, [0.68, 0.078, 0.26])
    by = {p["name"]: p for p in plan["parts"]}
    assert by["FrontSight"]["reference_detail"] is False
    assert by["Barrel"]["reference_detail"] is True


def test_color_lock_keeps_the_planned_colour():
    from mastersmith.stages.plan import clean_material
    m = clean_material({"color": "#1e2024", "finish": "metal", "color_lock": True})
    assert m["color_lock"] is True and m["color"] == "#1e2024"
    assert clean_material({"color": "#1e2024"})["color_lock"] is False
