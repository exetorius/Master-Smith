"""Review the delivered asset AND the requested repair, not just its distant silhouette."""
import json
import os

from ..llm import extract_json
from ..quality import requested_parts

PROMPT = """You are reviewing a finished game asset. Judge what the delivered renders actually show.
Brief and requested changes: {brief}
Images in order: {images}
The brief says where the reference came from. When it is the customer's own photograph, judge colours and materials
against the PHOTOGRAPH: the caption is the director's wording of it and can be wrong about a part's colour (a grey
magazine described as black is not a defect of the model).
The original reference may NOT contain newly requested parts. Judge those against their written requirements.
When a SOURCE SEED preview is supplied, compare it with the finished model. If the source looks clean but the finish
has triangle/faceted patterns, report a finishing regression, not a need for a new mesh vendor. Lighting/view may differ.
The CANOPY-HIDDEN diagnostic image is a cutaway for inspecting internal geometry only. It is NOT how the asset ships.
Use it to distinguish hidden controls from broken/missing controls, never to claim the delivered glass is clear.
Fitting metadata below proves only that geometry was joined, NOT that it looks right: {assembly}
Judge silhouette, proportions, missing/fused/duplicate parts, colours, materials and texture quality.
For every requested assembly part check orientation, scale, floor contact, clearance and clipping against its
neighbours and the hull. In a cockpit: a readable seat facing the panel/nose, an accessible stick ahead of the seat,
pedals forward of it, a coherent floor/walls/bulkhead, no duplicate controls, floating floors, intersections or
blocked seat/leg space. A bounding box fit does not establish any of these. Inspect the focused renders.
If glass, hull or another part obscures a requirement, mark it unverified, not pass. Do not infer detail from logs.
Do not suggest darkening the glass to hide a requested interior. Repair assembly geometry/placement separately
from texture defects. An attractive exterior does not compensate for a broken requested cabin.
Answer JSON only:
{{"score": 1-10, "silhouette_ok": true/false, "materials_ok": true/false,
 "issues": ["specific observed defects"], "verdict": "ship" | "ship with notes" | "rebuild",
 "assembly_ok": true/false/null,
 "finish_regression": true/false/null, "source_comparison": "visible before/after evidence, or unavailable",
 "assembly_checks": [{{"name": "exact requested part name", "status": "pass" | "fail" | "unverified",
                      "evidence": "specific visible evidence or what prevents inspection"}}],
 "rebuild_advice": "cheapest targeted remedy; do not default to buying another whole mesh"}}
Use assembly_ok=null and assembly_checks=[] when no assembly is requested. If any requested part fails or is
unverified, assembly_ok=false and verdict=rebuild. A low-confidence or unreadable review is not acceptance.
"""


def review(job, reference_path, renders, report=None):
    report = report or {}
    files, labels = [], []
    if reference_path and os.path.exists(reference_path):
        files.append(reference_path)
        labels.append("original reference")
    for path in renders[:2]:
        if os.path.exists(path):
            files.append(path)
            labels.append("delivered full-asset " + os.path.basename(path))
    for name in (report.get("review_renders") or [])[:2]:
        path = os.path.join(job.dir, "delivery", name)
        if os.path.exists(path) and path not in files:
            files.append(path)
            labels.append("delivered assembly close-up " + os.path.basename(path))
    for field, label in (("source_renders", "SOURCE SEED before finishing"),
                         ("inspection_renders", "CANOPY-HIDDEN diagnostic cutaway, NOT delivered appearance")):
        for name in (report.get(field) or [])[:1]:
            path = os.path.join(job.dir, "delivery", name)
            if os.path.exists(path) and path not in files:
                files.append(path)
                labels.append(label)
    requested = requested_parts(job.spec)
    brief = {"description": job.spec.description, "notes": job.spec.notes,
             "reference_source": "the customer's own photograph" if job.spec.reference_images else "a generated concept picture",
             "edit_instructions": job.spec.edit_instructions, "requested_parts": requested,
             "remove_parts": job.spec.remove_parts, "texture_fixes": job.spec.texture_fixes}
    if not any(label.startswith("delivered") for label in labels):
        j = {}
    else:
        text = job.llm.vision(PROMPT.format(
            brief=json.dumps(brief), images=json.dumps(labels),
            assembly=json.dumps({k: report.get(k) for k in ("added_parts", "cockpit", "cabin_lining")})),
            files, max_tokens=2400)
        j = extract_json(text)
    if not isinstance(j, dict):
        j = {}
    if not j:
        j = {"score": 0, "issues": ["visual review unavailable or returned no JSON"], "verdict": "rebuild"}
    checks = j.get("assembly_checks")
    j["assembly_checks"] = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    if not isinstance(j.get("issues"), list):
        j["issues"] = [str(j["issues"])] if j.get("issues") else []
    if requested:
        by_name = {c.get("name"): c for c in j["assembly_checks"]}
        proven = all(by_name.get(p["name"], {}).get("status") == "pass"
                     and str(by_name[p["name"]].get("evidence") or "").strip() for p in requested)
        if not proven or not any("close-up" in label for label in labels) or j.get("assembly_ok") is not True:
            j["assembly_ok"] = False
            j["verdict"] = "rebuild"
            j["issues"].append("requested assembly is failed or unverified; inspect the close-ups before accepting it")
    job.log("  review: %s/10, %s" % (j.get("score"), j.get("verdict")))
    return j
