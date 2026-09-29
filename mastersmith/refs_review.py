"""`ms refs`: the reference pictures of several jobs on one local page, each with Approve / Redraw and a note, kept in
out/<Name>/ref/review.json. 2026-09-29: the owner approves a batch of references in one sitting from the browser; the
static server behind `ms preview` could show the pictures but not keep the choices.

Run as `python -m mastersmith.refs_review <out_dir> <port>`: serves out_dir (pictures, refs.html) on 127.0.0.1 and
takes POST /api/review {job, file, status, note}. Status "" clears a picture's entry."""
import json
import os
import re
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

PICTURE = (".png", ".jpg", ".jpeg", ".webp")
STATUSES = ("approved", "redraw")
JOB_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def ref_pictures(job_dir):
    """The pictures directly in ref/ (drafts moved to ref/unused/ are not up for review)."""
    d = os.path.join(job_dir, "ref")
    return sorted(f for f in os.listdir(d) if f.lower().endswith(PICTURE)) if os.path.isdir(d) else []


def is_photo(name):
    """The owner's own photos (customer_ref_*) are shown for comparison, never approved or redrawn."""
    return name.startswith("customer_")


def load_review(job_dir):
    path = os.path.join(job_dir, "ref", "review.json")
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}


def record_review(out_dir, job, file, status, note=""):
    """One picture's verdict into ref/review.json; returns the job's whole review. Refuses anything that is not a
    job folder's reference picture, so the page can only ever write that one file."""
    if not JOB_NAME.match(job or "") or not os.path.isfile(os.path.join(out_dir, job, "brief.json")):
        raise ValueError("no job %r" % job)
    job_dir = os.path.join(out_dir, job)
    if file not in ref_pictures(job_dir) or is_photo(file):
        raise ValueError("no reference picture %r in %s/ref" % (file, job))
    if status not in STATUSES + ("",):
        raise ValueError("status must be approved, redraw or empty")
    review = load_review(job_dir)
    if status:
        review[file] = {"status": status, "note": str(note or "")[:2000], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    else:
        review.pop(file, None)
    json.dump(review, open(os.path.join(job_dir, "ref", "review.json"), "w", encoding="utf-8"), indent=1)
    return review


def all_jobs(out_dir):
    """Every job folder with a brief and at least one reference picture."""
    return sorted(d for d in os.listdir(out_dir)
                  if os.path.isfile(os.path.join(out_dir, d, "brief.json")) and ref_pictures(os.path.join(out_dir, d)))


def page_data(out_dir, jobs):
    rows = []
    for job in jobs:
        job_dir = os.path.join(out_dir, job)
        brief = json.load(open(os.path.join(job_dir, "brief.json"), encoding="utf-8"))
        notes = os.path.join(job_dir, "ref", "notes.txt")
        pics = []
        for f in ref_pictures(job_dir):
            p = os.path.join(job_dir, "ref", f)
            with Image.open(p) as im:
                w, h = im.size
            pics.append({"file": f, "src": "%s/ref/%s?v=%d" % (job, f, int(os.path.getmtime(p))), "w": w, "h": h,
                         "photo": is_photo(f)})
        rows.append({"name": job, "category": brief.get("category", ""), "size_m": brief.get("size_m", 0),
                     "description": brief.get("description", ""),
                     "notes": open(notes, encoding="utf-8").read().strip() if os.path.exists(notes) else "",
                     "pictures": pics, "review": load_review(job_dir)})
    return {"jobs": rows}


def write_page(out_dir, jobs):
    """out_dir/refs.html with the jobs' pictures and their current review embedded."""
    path = os.path.join(out_dir, "refs.html")
    data = json.dumps(page_data(out_dir, jobs)).replace("</", "<\\/")
    open(path, "w", encoding="utf-8").write(REFS_HTML.replace("__DATA__", data))
    return path


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != "/api/review":
            return self._json(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            review = record_review(self.directory, body.get("job"), body.get("file"), body.get("status", ""), body.get("note", ""))
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"ok": True, "review": review})


def serve(out_dir, port):
    ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=os.path.abspath(out_dir))).serve_forever()


REFS_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>Reference Review</title>
<style>
:root{
  color-scheme:light;
  --ground:#EDEFF2; --surface:#FFFFFF; --ink:#1B2127; --muted:#5A6571; --line:#D4D9DF;
  --accent:#2E5E8C; --accent-ink:#FFFFFF; --ok:#2F7D4F; --warn:#A8690F; --warn-soft:#FBF0DC; --well:#FAFAFA;
  --shadow:0 1px 2px rgba(20,30,40,.06),0 4px 14px rgba(20,30,40,.06);
  --ui:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  --head:"Segoe UI Variable Display","Segoe UI Semibold","Segoe UI",system-ui,sans-serif;
  --mono:"Cascadia Mono",Consolas,ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){:root{
  color-scheme:dark;
  --ground:#13171B; --surface:#1C2228; --ink:#E6EAEE; --muted:#9AA6B2; --line:#2E363F;
  --accent:#7FA8D6; --accent-ink:#0E1620; --ok:#63BE87; --warn:#E3AA4F; --warn-soft:#3A2C14; --well:#F1F2F3;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 14px rgba(0,0,0,.25);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:400 15px/1.5 var(--ui);padding-inline:16px;padding-block:0 64px}
.wrap{max-width:1320px;margin:0 auto}
h1,h2{font-family:var(--head);font-weight:600;text-wrap:balance;margin:0}
h1{font-size:clamp(24px,3.6vw,32px)} h2{font-size:22px}
.intro{display:grid;gap:6px;padding-block:24px 8px} .intro p{margin:0;color:var(--muted);max-width:72ch}
.bar{position:sticky;top:0;z-index:5;background:var(--ground);border-bottom:1px solid var(--line);margin-inline:-16px;padding:10px 16px}
.bar-in{max-width:1320px;margin:0 auto;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
.tally{display:flex;gap:14px;font:13px var(--mono);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.t-ok{color:var(--ok)} .t-warn{color:var(--warn)} .t-pend{color:var(--muted)}
.spacer{flex:1} .save{font:12px var(--mono);color:var(--muted);min-width:10ch}
button{font:600 14px var(--ui);border-radius:6px;border:1px solid var(--line);background:var(--surface);color:var(--ink);padding:7px 12px;cursor:pointer}
button:focus-visible,textarea:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)} button:disabled{opacity:.5;cursor:default}
.jobs{display:flex;flex-wrap:wrap;gap:6px;padding-top:10px}
.chip{display:inline-flex;gap:8px;align-items:center;text-decoration:none;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font:500 13px var(--ui)}
.chip .dot{width:8px;height:8px;border-radius:50%;background:var(--line)}
.chip[data-s="ok"] .dot{background:var(--ok)} .chip[data-s="warn"] .dot{background:var(--warn)}
.err{margin-top:10px;padding:10px 14px;border-radius:6px;background:var(--warn-soft);border:1px solid var(--warn)}
section.job{padding-top:26px;border-top:1px solid var(--line);margin-top:24px;display:grid;gap:10px;scroll-margin-top:64px}
.job-head{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}
.meta{font:12px var(--mono);color:var(--muted);letter-spacing:.3px;text-transform:uppercase}
.job p{margin:0;max-width:80ch} .job p.desc{color:var(--muted);font-size:14px}
.job-actions{display:flex;gap:8px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);overflow:hidden;display:grid;align-content:start}
.card[data-s="approved"]{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok)}
.card[data-s="redraw"]{border-color:var(--warn);box-shadow:0 0 0 1px var(--warn)}
.thumb{display:block;background:var(--well);aspect-ratio:4/3;width:100%;max-width:100%;cursor:zoom-in}
.thumb img{width:100%;height:100%;object-fit:contain;display:block}
.body{padding:10px 12px 12px;display:grid;gap:8px}
.cap{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.cap strong{font:600 15px var(--head)} .cap code{font:11px var(--mono);color:var(--muted);overflow-wrap:anywhere}
.acts{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.acts button[aria-pressed="true"].a-ok{background:var(--ok);border-color:var(--ok);color:var(--surface)}
.acts button[aria-pressed="true"].a-warn{background:var(--warn);border-color:var(--warn);color:var(--surface)}
.fix{display:grid;gap:6px} .fix label{font:12px var(--mono);color:var(--muted)}
textarea{width:100%;min-height:64px;resize:vertical;font:14px/1.4 var(--ui);color:var(--ink);background:var(--ground);border:1px solid var(--line);border-radius:6px;padding:8px}
.fix-row{display:flex;justify-content:flex-end}
@media (prefers-reduced-motion:no-preference){.card{transition:border-color .15s,box-shadow .15s}}
@media (max-width:520px){.grid{grid-template-columns:1fr}}
</style></head><body>
<div class="bar"><div class="bar-in">
  <div class="tally" id="tally"></div><span class="spacer"></span>
  <span class="save" id="save" aria-live="polite"></span>
  <button id="approveAll" class="primary" type="button">Approve all pending</button>
</div></div>
<div class="wrap">
  <header class="intro">
    <h1>Reference review</h1>
    <p>Each asset is built from these pictures. Approve a picture, or mark it Redraw and say what to change. Every click
    is saved to that job's <code>ref/review.json</code>, which the agent reads before it builds. Click a picture to open it full size.</p>
    <div class="jobs" id="jobs"></div>
    <div class="err" id="err" hidden></div>
  </header>
  <main id="main"></main>
</div>
<script>
const DATA = __DATA__;
const LABEL = {ref_0:"Hero", ref_side:"Side, forward end right", ref_front:"Front", ref_back:"Back", ref_top:"Top", ref_quarter:"Three-quarter"};
const review = {}; DATA.jobs.forEach(j => review[j.name] = Object.assign({}, j.review));
const $ = s => document.querySelector(s);
const refsOf = j => j.pictures.filter(p => !p.photo);
function viewName(j, p) {
  if (p.photo) return "Your photo";
  const stem = p.file.replace(/\.[a-z]+$/i, "");
  if (stem === "ref_0" && j.category === "weapon") return "Hero, side profile";
  return LABEL[stem] || stem;
}
function card(j, p) {
  const c = document.createElement("article");
  c.className = "card"; c.dataset.job = j.name; c.dataset.file = p.file;
  c.innerHTML = '<a class="thumb" target="_blank" rel="noopener"><img loading="lazy" alt=""></a><div class="body"><div class="cap"><strong></strong><code></code></div></div>';
  c.querySelector("a").href = p.src; c.querySelector("img").src = p.src;
  c.querySelector("img").alt = j.name + ": " + viewName(j, p);
  c.querySelector("strong").textContent = viewName(j, p);
  c.querySelector("code").textContent = p.file + " · " + p.w + "×" + p.h;
  const body = c.querySelector(".body");
  if (p.photo) { body.insertAdjacentHTML("beforeend", '<span class="meta">For comparison, not reviewed</span>'); return c; }
  const acts = document.createElement("div"); acts.className = "acts";
  acts.innerHTML = '<button type="button" class="a-ok" aria-pressed="false">Approve</button><button type="button" class="a-warn" aria-pressed="false">Redraw</button>';
  const [bOk, bWarn] = acts.querySelectorAll("button");
  bOk.onclick = () => toggle(j.name, p.file, "approved");
  bWarn.onclick = () => toggle(j.name, p.file, "redraw");
  const fix = document.createElement("div"); fix.className = "fix"; fix.hidden = true;
  const id = "note-" + (j.name + "-" + p.file).replace(/[^A-Za-z0-9_-]/g, "_");
  fix.innerHTML = '<label for="' + id + '">What should change?</label><textarea id="' + id + '"></textarea><div class="fix-row"><button type="button">Save note</button></div>';
  const ta = fix.querySelector("textarea");
  fix.querySelector("button").onclick = () => send(j.name, p.file, "redraw", ta.value);
  ta.addEventListener("blur", () => { const r = review[j.name][p.file]; if (r && r.status === "redraw" && (r.note || "") !== ta.value) send(j.name, p.file, "redraw", ta.value); });
  body.append(acts, fix);
  return c;
}
function build() {
  for (const j of DATA.jobs) {
    const a = document.createElement("a"); a.className = "chip"; a.href = "#" + j.name; a.id = "chip-" + j.name;
    a.innerHTML = '<span class="dot"></span><span></span>'; a.lastChild.textContent = j.name; $("#jobs").append(a);
    const s = document.createElement("section"); s.className = "job"; s.id = j.name;
    s.innerHTML = '<div class="job-head"><h2></h2><span class="meta"></span></div><p class="notes"></p><p class="desc"></p><div class="job-actions"><button type="button" class="primary">Approve all for this asset</button></div><div class="grid"></div>';
    s.querySelector("h2").textContent = j.name;
    s.querySelector(".meta").textContent = j.category + " · " + j.size_m + " m";
    s.querySelector(".notes").textContent = j.notes; s.querySelector(".notes").hidden = !j.notes;
    s.querySelector(".desc").textContent = j.description;
    s.querySelector(".job-actions button").onclick = () => approveMany(refsOf(j).map(p => [j.name, p.file]));
    const g = s.querySelector(".grid"); j.pictures.forEach(p => g.append(card(j, p)));
    $("#main").append(s);
  }
}
function render() {
  let ok = 0, warn = 0, pend = 0;
  for (const j of DATA.jobs) {
    const states = [];
    for (const p of refsOf(j)) {
      const r = review[j.name][p.file], st = r ? r.status : "";
      states.push(st); if (st === "approved") ok++; else if (st === "redraw") warn++; else pend++;
      const c = document.querySelector('.card[data-job="' + j.name + '"][data-file="' + p.file + '"]');
      c.dataset.s = st;
      const [bOk, bWarn] = c.querySelectorAll(".acts button");
      bOk.setAttribute("aria-pressed", st === "approved"); bWarn.setAttribute("aria-pressed", st === "redraw");
      bOk.textContent = st === "approved" ? "Approved" : "Approve";
      const fix = c.querySelector(".fix"), ta = fix.querySelector("textarea");
      fix.hidden = st !== "redraw"; if (document.activeElement !== ta) ta.value = (r && r.note) || "";
    }
    $("#chip-" + j.name).dataset.s = states.includes("redraw") ? "warn" : states.every(x => x === "approved") ? "ok" : "";
  }
  $("#tally").innerHTML = '<span class="t-ok">' + ok + ' approved</span><span class="t-warn">' + warn + ' redraw</span><span class="t-pend">' + pend + ' pending</span>';
  $("#approveAll").disabled = pend === 0;
}
function flash(m) { $("#save").textContent = m; clearTimeout(flash.t); flash.t = setTimeout(() => $("#save").textContent = "", 2500); }
async function send(job, file, status, note) {
  try {
    const r = await fetch("/api/review", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({job, file, status, note: note || ""})});
    const b = await r.json();
    if (!r.ok) throw new Error(b.error || r.status);
    review[job] = b.review; $("#err").hidden = true; flash("Saved"); render();
  } catch (e) {
    $("#err").textContent = "Not saved: " + e.message + ". Is the ms refs server still running? Run it again and reload.";
    $("#err").hidden = false; flash("Not saved");
  }
}
function toggle(job, file, status) {
  const r = review[job][file];
  if (r && r.status === status) return send(job, file, "", "");
  if (status === "redraw") setTimeout(() => document.querySelector('.card[data-job="' + job + '"][data-file="' + file + '"] textarea').focus(), 0);
  return send(job, file, status, r ? r.note : "");
}
async function approveMany(list) { for (const [job, file] of list) if (!review[job][file]) await send(job, file, "approved", ""); }
$("#approveAll").onclick = () => approveMany(DATA.jobs.flatMap(j => refsOf(j).map(p => [j.name, p.file])));
build(); render();
</script></body></html>"""


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]))
