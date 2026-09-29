"""Plan colours to linear base colours, shared by build_part.py and assemble.py (pure Python, no bpy, so it is tested)."""

ALBEDO_FLOOR = 45 / 255.0          # the darkest real paint or polymer; a black read off a shadowed photo is darker than any albedo (the pistol, 2026-09-27)
METAL_MIN_REFLECTANCE = 0.07       # blued or black steel: clearly metal, still dark (0.12 read as silver on a pistol slide)


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def planned_linear(h, metal=False):
    """The plan's colour (#rrggbb) in linear RGB, floored at the darkest real albedo; None for a malformed colour.
    A metal's base colour is how much it reflects, so a dark metal is lifted to METAL_MIN_REFLECTANCE by adding the
    same amount to every channel: it keeps the colour's tint without multiplying it. Scaling the channels (until
    2026-09-29) tripled the blue of a blued-steel receiver (#283446) and the flat faces rendered royal blue."""
    h = str(h or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        out = [srgb_to_linear(max(int(h[i:i + 2], 16) / 255.0, ALBEDO_FLOOR)) for i in (0, 2, 4)]
    except ValueError:
        return None
    if metal:
        lum = luminance(out)
        if lum < METAL_MIN_REFLECTANCE:
            out = [min(1.0, v + METAL_MIN_REFLECTANCE - lum) for v in out]
    return tuple(out)
