"""Evidence-based delivery status, shared by the reviewer, gate and director. No provider calls."""


def requested_parts(spec):
    parts = [{k: p.get(k) for k in ("name", "phrase", "anchor", "place", "size_m", "offset_m", "yaw_degrees")}
             for p in (spec.add_parts or [])]
    if spec.cockpit:
        parts.append({"name": "Cockpit", "phrase": "the requested cockpit interior", "anchor": "glass",
                      "place": "inside"})
    return parts


def assembly_findings(spec, report, review):
    """Fitted geometry is not proof of a usable assembly. Missing evidence is not a pass."""
    requested = requested_parts(spec)
    if not requested:
        return []
    findings = []
    actual = {p.get("name"): p for p in (report.get("added_parts") or [])}
    if spec.cockpit:
        actual["Cockpit"] = report.get("cockpit") or {}
    checks = {p.get("name"): p for p in ((review or {}).get("assembly_checks") or []) if isinstance(p, dict)}
    for part in requested:
        name = part["name"]
        fitted = actual.get(name) or {}
        if fitted.get("skipped") or not (fitted.get("faces_added") or fitted.get("faces")):
            findings.append("requested part %s was not fitted successfully" % name)
        check = checks.get(name) or {}
        if check.get("status") != "pass" or not str(check.get("evidence") or "").strip():
            findings.append("%s assembly %s: %s" % (name, "failed" if check.get("status") == "fail" else "unverified",
                            check.get("evidence") or "no close-up evidence of fit, orientation and clearance"))
    if not report.get("review_renders"):
        findings.append("requested assembly has no focused review renders")
    if (review or {}).get("assembly_ok") is not True:
        findings.append("requested assembly has not passed visual review")
    return findings


def assess(spec, report, review):
    review = review or {}
    issues = assembly_findings(spec, report or {}, review)
    verdict = review.get("verdict")
    score = review.get("score")
    if verdict == "rebuild":
        issues.insert(0, "reviewer verdict is rebuild; the asset is not accepted")
    elif verdict not in ("ship", "ship with notes"):
        issues.insert(0, "visual review is unavailable or inconclusive")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 10:
        issues.append("visual review has no valid score")
    elif score < 5:
        issues.append("reviewer scored %s/10" % score)
    return {"accepted": not issues, "status": "needs_attention" if issues else verdict,
            "issues": issues, "note": "Job completion and technical checks do not establish visual quality."}
