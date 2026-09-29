"""Plan colours to base colours (mastersmith/blender/colour.py, shared by the code parts and the assembler)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mastersmith", "blender"))
import colour  # noqa: E402


def _saturation(rgb):
    return (max(rgb) - min(rgb)) / max(rgb)


def test_floor_and_malformed():
    assert colour.planned_linear("#000000") == colour.planned_linear("#2d2d2d")      # floored at sRGB 45
    assert colour.planned_linear("nope") is None and colour.planned_linear("#12345z") is None


def test_dark_metal_reaches_its_reflectance_without_a_stronger_tint():
    # 2026-09-29: the shotgun's blued receiver #283446 was scaled to a saturated blue and rendered royal blue
    plain = colour.planned_linear("#283446")
    lifted = colour.planned_linear("#283446", metal=True)
    assert abs(colour.luminance(lifted) - colour.METAL_MIN_REFLECTANCE) < 1e-9
    assert lifted[2] > lifted[1] > lifted[0]                                            # still a blue steel
    assert _saturation(lifted) < _saturation(plain)                                    # but a paler tint, not a stronger one


def test_light_metal_and_non_metal_are_left_alone():
    assert colour.planned_linear("#c8c8cc", metal=True) == colour.planned_linear("#c8c8cc")
    assert colour.planned_linear("#283446") == colour.planned_linear("#283446", metal=False)
