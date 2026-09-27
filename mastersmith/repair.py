"""Concrete, non-spending repair plans from a completed job's evidence."""
import re

COMPONENTS = {
    "seat": r"\bseats?\b", "panel": r"\b(?:instrument panel|dashboard|dash|coaming)\b",
    "consoles": r"\bconsoles?\b", "stick": r"\b(?:flight stick|control stick|joystick|cyclic)\b",
    "pedals": r"\bpedals?\b", "floor": r"\bfloor(?: pan)?\b(?![ -](?:pedestal|mounted|mounting)\b)", "walls": r"\bwalls?\b",
    "bulkhead": r"\bbulkheads?\b",
}


def part_components(part):
    if part.get("provides") is not None:
        return set(part["provides"]) & set(COMPONENTS)
    clauses = re.split(r"[,;:.]", str(part.get("phrase") or "").lower())
    positive = [re.split(r"\b(?:no|without|excluding)\b", c, maxsplit=1)[0] for c in clauses]
    phrase = " ".join(positive)
    return {name for name, pattern in COMPONENTS.items() if re.search(pattern, phrase)}


def assembly_conflicts(parts):
    """Flag a loose control already supplied by a compound module at the same interior anchor.

    Two individual seats aren't a conflict: tandem/side-by-side arrangements are intentional. This is a
    preflight warning about duplicated responsibilities, not a claim that AABBs prove mesh intersections.
    """
    conflicts = []
    for module in parts or []:
        owned = part_components(module)
        if module.get("place") != "inside" or len(owned) < 2:
            continue
        for loose in parts or []:
            needed = part_components(loose)
            if loose is module or loose.get("place") != "inside" or loose.get("anchor") != module.get("anchor"):
                continue
            if len(needed) == 1 and needed <= owned:
                conflicts.append({"module": module["name"], "part": loose["name"], "components": sorted(needed),
                                  "reason": "the compound module already supplies this component; omit the loose part "
                                            "or explicitly exclude it from the module before generating"})
    return conflicts


def plan_repair(job):
    spec, summary = job.get("spec") or {}, job.get("summary") or {}
    review = summary.get("review") or {}
    parts = spec.get("add_parts") or []
    actions = []
    issues = " ".join(str(v) for v in review.get("issues") or []).lower()
    bake = summary.get("bake") or {}
    if review.get("finish_regression") is True or (bake and bake.get("status") != "skipped" and
                                                  any(w in issues for w in ("facet", "baking artifact", "triangular"))):
        actions.append({"target": "finishing", "reason": "finishing may have introduced the surface artifacts; compare the "
                        "seed preview with the delivered render before blaming the mesh vendor",
                        "changes": {"texture_fixes": list(dict.fromkeys((spec.get("texture_fixes") or []) + ["preserve_seed_maps"]))},
                        "expected_evidence": "the same body's clean source appearance survives finishing; no triangular patches",
                        "cost_kind": "refinish existing seed; no mesh/image generation"})
    conflicts = assembly_conflicts(parts)
    omit = {c["part"] for c in conflicts}
    for check in review.get("assembly_checks") or []:
        evidence = str(check.get("evidence") or "").lower()
        if check.get("status") == "fail" and any(w in evidence for w in ("overlap", "redundant", "duplicate")):
            if any(p.get("name") == check.get("name") for p in parts):
                omit.add(check["name"])
    if omit and len(omit) < len(parts):
        actions.append({"target": "assembly", "reason": "remove redundant additions from the assembly plan, not faces "
                        "from the body; keep the purchased compound module",
                        "omit_additions": sorted(omit), "changes": {"add_parts": [p for p in parts if p.get("name") not in omit]},
                        "expected_evidence": "one coherent cabin, no duplicate shell/controls; confirm seat clearance and control reach",
                        "cost_kind": "refit existing seeds; no mesh/image generation"})
    uncertain = [c for c in review.get("assembly_checks") or [] if c.get("status") == "unverified"]
    if uncertain:
        actions.append({"target": "inspection", "reason": "occluded is not proof of missing or broken geometry",
                        "parts": [c.get("name") for c in uncertain], "changes": {},
                        "expected_evidence": "inspect the cabin with the canopy temporarily hidden before moving or buying controls",
                        "cost_kind": "local inspection only"})
    return {"job_id": job.get("id") or job.get("job_id"), "actions": actions, "conflicts": conflicts,
            "automatic_build": False, "requires_confirmation": True,
            "next": "Explain these targeted changes and get approval. Do not buy another whole mesh to fix a finishing regression."}
