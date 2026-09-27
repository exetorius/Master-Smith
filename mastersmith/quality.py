"""Evidence-based delivery status, shared by the reviewer, gate and director. No provider calls."""


def assess(spec, report, review):
    review = review or {}
    issues = []
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
