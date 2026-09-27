"""Review the delivered asset against its reference and brief, not just its distant silhouette."""
import json
import os

from ..llm import extract_json

PROMPT = """You are reviewing a finished game asset. Judge what the delivered renders actually show.
Brief: {brief}
Images in order: {images}
The brief says where the reference came from. When it is the customer's own photograph, judge colours and materials
against the PHOTOGRAPH: the caption is the director's wording of it and can be wrong about a part's colour (a grey
magazine described as black is not a defect of the model).
When a SOURCE SEED preview is supplied, compare it with the finished model. If the source looks clean but the finish
has triangle/faceted patterns, report a finishing regression. Lighting/view may differ.
Judge silhouette, proportions, missing/fused/duplicate parts, colours, materials and texture quality.
Do not infer detail from logs. A low-confidence or unreadable review is not acceptance.
Answer JSON only:
{{"score": 1-10, "silhouette_ok": true/false, "materials_ok": true/false,
 "issues": ["specific observed defects"], "verdict": "ship" | "ship with notes" | "rebuild",
 "finish_regression": true/false/null, "source_comparison": "visible before/after evidence, or unavailable",
 "rebuild_advice": "what the next build should change in the brief or the pictures, or empty"}}
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
    for name in (report.get("source_renders") or [])[:1]:
        path = os.path.join(job.dir, "delivery", name)
        if os.path.exists(path) and path not in files:
            files.append(path)
            labels.append("SOURCE SEED before finishing")
    brief = {"description": job.spec.description, "notes": job.spec.notes,
             "reference_source": "the customer's own photograph" if job.spec.reference_images else "a generated concept picture",
             "edit_instructions": job.spec.edit_instructions}
    if not any(label.startswith("delivered") for label in labels):
        j = {}
    else:
        text = job.llm.vision(PROMPT.format(brief=json.dumps(brief), images=json.dumps(labels)), files, max_tokens=2400)
        j = extract_json(text)
    if not isinstance(j, dict):
        j = {}
    if not j:
        j = {"score": 0, "issues": ["visual review unavailable or returned no JSON"], "verdict": "rebuild"}
    if not isinstance(j.get("issues"), list):
        j["issues"] = [str(j["issues"])] if j.get("issues") else []
    job.log("  review: %s/10, %s" % (j.get("score"), j.get("verdict")))
    return j
