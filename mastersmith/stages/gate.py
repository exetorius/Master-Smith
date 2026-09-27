"""Stage 5: the delivery gate. Mechanical checks on what was produced, so a customer never receives a
folder that is missing a map or is ten times the wrong size without being told. Warnings, not refusals:
the asset still ships, the report says what to look at."""
import os
from ..quality import assess


def check(spec, report, review, delivery_dir):
    warnings = []
    lods = report.get("lods") or []
    if not lods:
        warnings.append("no LODs recorded")
    else:
        t0 = lods[0]["triangles"]
        if t0 > spec.tri_budget * 1.05:
            warnings.append("LOD0 has %d triangles, over the %d budget" % (t0, spec.tri_budget))
        if t0 < spec.tri_budget * 0.3 and t0 < 5000:
            warnings.append("LOD0 has only %d triangles; the seed was very light" % t0)
    roles = {m["role"] for m in report.get("maps", [])}
    if "BC" not in roles:
        warnings.append("no base colour map")
    if "N" not in roles:
        warnings.append("no normal map")
    if "ORM" not in roles:
        warnings.append("no roughness/metallic map")
    dims = report.get("dimensions_m") or []
    if dims:
        longest = max(dims) if spec.category != "character" else dims[2]
        if spec.size_m > 0 and abs(longest - spec.size_m) > 0.1 * spec.size_m:   # size_m <= 0 keeps the source size
            warnings.append("size is %.2f m, brief said %.2f m" % (longest, spec.size_m))
    hull = (report.get("collision") or {}).get("triangles", 0)
    if hull > 256:
        warnings.append("collision hull has %d triangles" % hull)
    for f in report.get("files", []):
        if not os.path.exists(os.path.join(delivery_dir, f)):
            warnings.append("missing file %s" % f)
    if spec.glass and not report.get("glass"):
        warnings.append("glass was requested but no glass region was found")
    rm = report.get("roughness_mean")
    if rm is not None and rm < 0.3:
        warnings.append("surface still very glossy (roughness %.2f)" % rm)
    technical_ok = not warnings
    quality = assess(spec, report, review)
    warnings.extend(quality["issues"])
    return {"ok": not warnings, "technical_ok": technical_ok, "quality": quality, "warnings": warnings}
