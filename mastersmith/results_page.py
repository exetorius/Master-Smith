"""`ms results`: every delivered job on one local page - its six views, score, defects and cost, with links to its 3D
preview, GLB and zip. 2026-09-29: the owner reviews a batch of builds from one page instead of a preview URL per job.
Served by the same out/ server as `ms refs` (refs_review.py), so each job's preview.html opens from the same port.
The score and defects come from delivery/scorecard.json, which the agent writes after its own review:
{"score": 8, "spent": "$0.92", "tonetta": "...", "defects": ["..."]}."""
import html
import json
import os

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Build Results</title>
<style>
:root{color-scheme:light;--ground:#EDEFF2;--surface:#FFFFFF;--ink:#1B2127;--muted:#5A6571;--line:#D4D9DF;--accent:#2E5E8C;
 --good:#2F7D4F;--mid:#A8690F;--low:#B03A2E;--well:#9A9DA1;--shadow:0 1px 2px rgba(20,30,40,.06),0 4px 14px rgba(20,30,40,.06);
 --ui:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;--head:"Segoe UI Variable Display","Segoe UI Semibold","Segoe UI",system-ui,sans-serif;
 --mono:"Cascadia Mono",Consolas,ui-monospace,monospace}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;--ground:#13171B;--surface:#1C2228;--ink:#E6EAEE;--muted:#9AA6B2;--line:#2E363F;
 --accent:#7FA8D6;--good:#63BE87;--mid:#E3AA4F;--low:#E07A6E;--shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.25)}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:400 15px/1.5 var(--ui);padding-inline:16px;padding-block:24px 64px}
.wrap{max-width:1320px;margin:0 auto;display:grid;gap:18px}
h1,h2{font-family:var(--head);font-weight:600;margin:0;text-wrap:balance} h1{font-size:clamp(24px,3.6vw,32px)} h2{font-size:21px}
header{display:grid;gap:6px} header p{margin:0;color:var(--muted);max-width:75ch}
.totals{display:flex;flex-wrap:wrap;gap:8px 22px;font:13px var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.totals b{color:var(--ink);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);overflow:hidden;display:grid;grid-template-rows:auto 1fr}
.shot{display:block;background:var(--well)} .shot img{display:block;width:100%;height:auto;aspect-ratio:3/2;object-fit:contain}
.body{padding:12px 14px 14px;display:grid;gap:8px;align-content:start}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.score{font:600 15px var(--mono);font-variant-numeric:tabular-nums;padding:1px 9px;border-radius:999px;border:1px solid currentColor;white-space:nowrap}
.s-good{color:var(--good)} .s-mid{color:var(--mid)} .s-low{color:var(--low)} .s-none{color:var(--muted)}
.meta{font:12px var(--mono);color:var(--muted);overflow-wrap:anywhere}
ul{margin:0;padding-left:18px;display:grid;gap:3px;font-size:14px}
.links{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.links a{font:600 13px var(--ui);text-decoration:none;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 10px;background:var(--surface)}
.links a.primary{background:var(--accent);border-color:var(--accent);color:var(--surface)}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (max-width:420px){.grid{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<header><h1>Build results</h1><p>Every delivered build on one page. Click the six views or <b>3D preview</b> to open the model viewer with its previews,
reference pictures and parts. Scores and defects come from the agent's review of all six sides.</p><div class="totals">__TOTALS__</div></header>
<main class="grid">__CARDS__</main></div></body></html>"""


def _v(path):
    return "?v=%d" % int(os.path.getmtime(path)) if os.path.exists(path) else ""


def delivered_jobs(out_dir):
    return sorted(d for d in os.listdir(out_dir) if os.path.isfile(os.path.join(out_dir, d, "delivery", "report.json")))


def job_card(out_dir, job):
    d = os.path.join(out_dir, job, "delivery")
    rep = json.load(open(os.path.join(d, "report.json"), encoding="utf-8"))
    brief = json.load(open(os.path.join(out_dir, job, "brief.json"), encoding="utf-8"))
    sc_path = os.path.join(d, "scorecard.json")
    sc = json.load(open(sc_path, encoding="utf-8")) if os.path.exists(sc_path) else {}
    score = sc.get("score")
    cls = "s-none" if score is None else "s-good" if score >= 8 else "s-mid" if score >= 7 else "s-low"
    dims = " x ".join("%.2f" % v for v in rep.get("dimensions_m") or [])
    tris = (rep.get("lods") or [{}])[0].get("triangles", 0)
    glb = os.path.join(d, "SM_%s.glb" % job)
    zips = [f for f in sorted(os.listdir(d)) if f.endswith(".zip")]
    e = html.escape
    links = ['<a class="primary" href="%s/delivery/preview.html">3D preview</a>' % e(job),
             '<a href="%s/delivery/preview_views.png%s">Six views</a>' % (e(job), _v(os.path.join(d, "preview_views.png")))]
    if os.path.exists(glb):
        links.append('<a href="%s/delivery/%s%s">GLB</a>' % (e(job), e(os.path.basename(glb)), _v(glb)))
    if zips:
        links.append('<a href="%s/delivery/%s">Zip</a>' % (e(job), e(zips[0])))
    links.append('<a href="refs.html#%s">References</a>' % e(job))
    defects = "".join("<li>%s</li>" % e(x) for x in sc.get("defects") or [])
    return ('<article class="card"><a class="shot" href="%(job)s/delivery/preview.html"><img loading="lazy" alt="%(job)s from six sides" '
            'src="%(job)s/delivery/preview_views.png%(v)s"></a><div class="body"><div class="top"><h2>%(job)s</h2>'
            '<span class="score %(cls)s">%(score)s</span></div><div class="meta">%(meta)s</div>%(tonetta)s%(defects)s'
            '<div class="links">%(links)s</div></div></article>') % {
        "job": e(job), "v": _v(os.path.join(d, "preview_views.png")), "cls": cls,
        "score": ("%g / 10" % score) if score is not None else "not scored",
        "meta": e("%s · %s m · %s tris · %d parts%s" % (brief.get("category", ""), dims, format(tris, ","),
                                                       len(rep.get("parts") or []), (" · " + sc["spent"]) if sc.get("spent") else "")),
        "tonetta": ('<div class="meta">re-runs Tonetta %s</div>' % e(sc["tonetta"])) if sc.get("tonetta") else "",
        "defects": ("<ul>%s</ul>" % defects) if defects else "", "links": "".join(links)}, sc


def write_results(out_dir, jobs):
    """out_dir/results.html for the jobs (their order kept)."""
    cards, scores, spent = [], [], 0.0
    for job in jobs:
        card, sc = job_card(out_dir, job)
        cards.append(card)
        if sc.get("score") is not None:
            scores.append(float(sc["score"]))
        try:
            spent += float(str(sc.get("spent", "0")).lstrip("$"))
        except ValueError:
            pass
    totals = "<span><b>%d</b> builds</span>" % len(jobs)
    if scores:
        totals += "<span>mean score <b>%.1f</b> / 10</span>" % (sum(scores) / len(scores))
    if spent:
        totals += "<span>spent <b>$%.2f</b></span>" % spent
    path = os.path.join(out_dir, "results.html")
    open(path, "w", encoding="utf-8").write(PAGE.replace("__TOTALS__", totals).replace("__CARDS__", "".join(cards)))
    return path
