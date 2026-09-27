"""The director: a cheap chat model that turns conversation into a Spec, quotes the price, and runs the
pipeline when the customer says go. It never writes Blender code and never sees images - the pipeline
does the work, the model does the talking. That is what keeps OpenRouter spend to a few cents a job."""
import json
import os

from . import config, pricing, providers, skills
from .llm import LLM
from .pipeline import build
from .spec import CATEGORIES, ENGINES, STYLES, Spec
from .quality import assess

SYSTEM = """You are Master Smith, a 3D asset director. A customer describes a game asset; you turn it into a build brief,
quote the credits, and run the build when they confirm. You are concise and concrete.

How a job goes:
1. From the customer's words call set_brief with your best complete brief. Fill sensible defaults yourself
   (category, real size in metres, triangle budget, engine) - ask at most ONE short question, and only when the
   answer would change the model itself (e.g. "which tank?", "realistic or stylized?"). Ask through ask_customer
   with 2-4 concrete options: the chat shows them as numbered buttons the customer can click, and they may also
   type anything. Do that whenever you need a decision, including "go ahead?" moments and which change to make. Write `description`
   as a photo caption: what it is, its materials and colours, distinctive parts, era. Never name a camera angle in it
   ("seen in side profile"): the same caption is checked against every view, and a front view then fails. Write colours as a camera
   sees them, never as trade terms ('blued steel' is dark blue-black oxidised steel, not blue). With a customer photo,
   read each part's colour off the photo (a magazine or grip the same grey as the body is grey, not 'black'): the
   reviewer holds the model to your caption, and a wrong colour word costs a rebuild. Fixed-wing aircraft are
   category "aircraft" and rotorcraft "helicopter" (not "vehicle"). Name real machines by name
   and put that name in search_query so a real photograph is found; leave search_query empty for invented things.
   When the customer attached pictures, put them in reference_images (they are edited into the build picture).
   One seed is the default for every category. build_mode "assembly" (on request, or when the customer wants the
   experimental parts path) builds a hard surface as parts: after the pictures are approved, the builder model splits
   the object, the vendor models the sculpted main body in one piece, the builder models the mechanical parts in code
   (barrels, rails, sights, pins, wheels: exact edges), and they are assembled and checked against the pictures. Quote
   its estimate; it is dearer than one seed. seed_vendor then picks the vendor for the moulded parts.
   For a one-seed build (build_mode "single") of a hard surface from the customer's photo, seed_vendor "hitem3d3mv" is the
   quality pick: Hi3D v3's crisp geometry from every approved angle, same price as one angle. `premium` only changes
   the picture model (pin picture_model instead).
   When the customer wants an existing model changed, keep the whole description and put only what changes in
   edit_instructions; the previous picture is edited so the rest of the design stays as it was.
2. set_brief returns the worst-case estimate. Tell the customer the plan in two or three lines and what it will
   cost, then wait for them to say go (or change something).
3. When they confirm, call make_reference FIRST (unless they say to skip the preview). It draws the reference
   picture(s) the mesh will be built from, for cents, and shows them to the customer in the chat. Tell them to look at
   the pictures and say go, or say what to change. Never paste picture paths, URLs or markdown images into your reply:
   the chat displays the pictures itself, with each angle labelled. If they want changes, call set_brief with the changed description
   (or edit_instructions for a small change to the same design) and make_reference again. When they approve, call
   build: it seeds from the approved picture and draws nothing new. If build returns a queued job_id, tell the customer
   the job is building and that the page shows progress; when they ask how it is going call job_status. When build
   returns a finished result, report it: files, triangle counts, glass, rig, the reviewer's score and issues, cost.
   "done" means files were produced, not that the model looks right: read summary.quality, and a "rebuild" verdict
   always wins over the score. If the reviewer said rebuild, say what you would change and ask before spending again.
4. For changes after a build, call set_brief again with ONLY the changed fields and then build when confirmed. Nothing
   is repaired on a finished mesh: the model is built right from its pictures, and a change is one of two things:
   - size, triangle budget, engine, rig or glass: the same mesh is re-finished (a one-seed build or an imported
     model). No pictures, no new mesh;
   - anything the customer would see - shape, proportions, a wrong, missing or extra part, colours, finishes,
     materials, a melted detail, a bad texture: a new build. Keep the name and the description (change the
     description only when the object itself changes), put the change in edit_instructions, call make_reference so
     the customer approves the edited pictures, then build. An assembly plans its parts again from the new pictures.
   When the reviewer's verdict is "rebuild", do not rebuild on your own: say what the pictures or the brief should
   change (the review's rebuild_advice helps) and ask.
5. When the customer attaches a 3D model file (.glb, .gltf, .fbx, .obj or a delivered .blend), call import_model with
   its path, a PascalCase name, the category and whatever else they told you: the model is oriented, scaled, given
   glass, LODs, collision, maps and previews without buying a new mesh; size, budget, engine, rig and glass changes
   re-finish it like a build.
   Attached pictures go in reference_images of set_brief.

Skills you can read for guidance on a category: {categories}. Use read_skill when unsure how a category is handled.
Categories: {cats}. Styles: {styles}. Engines: {engines}.
Never invent file paths or results; only report what tools return. {pricing}"""

TOOLS = [
    {"type": "function", "function": {
        "name": "set_brief",
        "description": "Set or update the build brief. Returns the brief as understood, the credit estimate and the balance.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Asset name in PascalCase, e.g. SniperRifle"},
            "description": {"type": "string", "description": "Photo-caption description: object, materials, colours, parts, era"},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "style": {"type": "string", "enum": list(STYLES)},
            "engine": {"type": "string", "enum": list(ENGINES)},
            "tri_budget": {"type": "integer", "description": "LOD0 triangle budget; 0 for the category default"},
            "size_m": {"type": "number", "description": "Real longest dimension in metres (height for characters)"},
            "reference_image": {"type": "string", "description": "Path or URL of the customer's main picture, if any"},
            "reference_images": {"type": "array", "items": {"type": "string"}, "description": "Every picture the customer supplied (up to 4)"},
            "search_query": {"type": "string", "description": "For a REAL, named thing (an M1 Abrams, a Willys MB, a Glock 17, a Ford F-150): its exact name, so a photograph is looked up on the web and the mesh is built from it. Empty for fictional or generic objects."},
            "research": {"type": "boolean", "description": "Force web research on or off (default: on when search_query is set and no pictures were supplied)"},
            "single_picture": {"type": "boolean", "description": "ONLY when the customer explicitly asks for a single picture / "
                               "one view. Leave it out otherwise: every build draws and seeds from several angles by default."},
            "premium": {"type": "boolean", "description": "Dearer picture model for hard briefs; it changes the pictures only. "
                        "Pinning picture_model does the same without the flag."},
            "build_mode": {"type": "string", "enum": ["assembly", "single"],
                           "description": "Leave unset for one vendor mesh (the default). 'assembly' builds a hard surface as parts: "
                                          "the vendor models the sculpted main body, the builder models the mechanical parts in code with crisp "
                                          "edges, and they are assembled and checked against the pictures (experimental, dearer)."},
            "seed_vendor": {"type": "string", "enum": ["tripo", "hitem3d3mv", "hitem3d3", "meshy7mv", "meshy7", "hitem3d"],
                            "description": "The mesh vendor. Leave unset for the default (Tripo). hitem3d3 = Hitem3D v3 at 2048 voxels, the "
                                           "crispest hard-surface geometry (about $2.10 a seed): the answer to melted rails, fused trigger "
                                           "guards and blobby detail; hitem3d3mv = the same model fed every approved angle (front, side, mirrored "
                                           "side, back when drawn) for the same price, the better pick whenever more than one view was "
                                           "approved. A change of vendor seeds again from the approved pictures."},
            "glass": {"type": "boolean", "description": "Give windows/lenses a glass material slot (default for vehicles and buildings; a weapon gets one only when its caption describes a scope, optic, lens or light)"},
            "rig": {"type": "boolean", "description": "Rig it: characters get a UE5-named humanoid skeleton with walk/run clips; vehicles get wheel bones; weapons get Muzzle/Grip/Sight socket bones"},
            "notes": {"type": "string"},
            "edit_instructions": {"type": "string", "description": "When changing an EXISTING model: one or two sentences naming only "
                                  "what changes in its appearance. The previous picture is edited, everything unmentioned stays."}},
            "required": ["name", "description", "category"]}}},
    {"type": "function", "function": {
        "name": "import_model",
        "description": "Bring in a 3D model file the customer attached and finish it for their engine (no new mesh is bought). "
                       "Becomes the current model: later set_brief + build calls with size, budget, engine, rig or glass changes re-finish it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "The attached file's path exactly as given"},
            "name": {"type": "string", "description": "Asset name in PascalCase"},
            "description": {"type": "string", "description": "What the model is, as a photo caption (materials, colours, parts)"},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "engine": {"type": "string", "enum": list(ENGINES)},
            "size_m": {"type": "number", "description": "Real longest dimension in metres to scale it to; omit to keep the file's own size"},
            "tri_budget": {"type": "integer", "description": "LOD0 triangle budget; 0 for the category default"},
            "glass": {"type": "boolean"}, "rig": {"type": "boolean"}, "notes": {"type": "string"}},
            "required": ["path", "name", "category"]}}},
    {"type": "function", "function": {
        "name": "ask_customer",
        "description": "Ask the customer to choose. The chat shows the options as numbered buttons (they can also type). "
                       "Call it, then end your reply with the question; do nothing else this turn.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "One short question"},
            "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 5,
                        "description": "2-5 concrete answers, each a few words ('Realistic', 'Stylized, low-poly')"}},
            "required": ["question", "options"]}}},
    {"type": "function", "function": {
        "name": "make_reference",
        "description": "Draw the reference picture(s) for the current brief and show them to the customer, without buying a "
                       "mesh. Call it when the customer confirms the brief; call build once they approve the picture.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "build", "description": "Run the build for the current brief. Only after the customer confirmed (and, unless "
                                        "they asked to skip the preview or only size, budget, engine, rig or glass changed, "
                                        "approved the reference picture).",
        "parameters": {"type": "object", "properties": {
            "skip_preview": {"type": "boolean", "description": "true ONLY when the customer explicitly said to skip the reference "
                                                               "preview; otherwise build needs approved pictures from make_reference"}}}}},
    {"type": "function", "function": {
        "name": "job_status", "description": "Status, log tail and results of a queued or finished build.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}},
    {"type": "function", "function": {
        "name": "read_skill", "description": "Read the guidance for an asset category.",
        "parameters": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "balance", "description": "What the customer has spent so far (or, when credits are enforced, their balance).",
        "parameters": {"type": "object", "properties": {}}}},
]

MAX_TOOL_ROUNDS = 8

PRICING_NOTE_OWN_KEYS = ("Money: the customer runs this on their own fal.ai and OpenRouter keys. Quote the worst case as US dollars "
                         "(estimate_usd, e.g. 'about $1.20 worst case') next to what the two accounts have left "
                         "(provider_balances_usd, e.g. 'fal has $135, OpenRouter $157'). There is no local balance, hold or top-up. "
                         "Only when provider_too_low_for_this_build names an account say the build will be refused until that "
                         "account is topped up at the provider.")


class Director:
    def __init__(self, user, wallet, log=print, model=None, pricing_note=None):
        if pricing_note is None:
            pricing_note = PRICING_NOTE_OWN_KEYS
        self.user, self.wallet, self.log = user, wallet, log
        self.last_tools = []
        self.model = model or config.DIRECTOR_MODEL
        self._llm = None            # made on the first chat turn: an outside director (MCP) never needs OpenRouter
        self.spec = None
        self.last_result = None
        self.last_job_id = None
        self.submit = None          # service hook: spec dict -> {"job_id", ...}; when set, build is queued not run
        self.job_status = None      # service hook: job_id -> status dict
        self.import_model = None    # service hook: (path, spec dict) -> {"job_id", ...}
        self.make_reference = None  # service hook: spec dict -> {"job_id", "dir", "views", "pictures" (URLs), ...}
        self.reference = None       # the approved pictures: {"job_dir", "views", "for": design snapshot}
        self.last_pictures = []     # pictures produced this turn, for the chat to show: [{"label", "url"}]
        self.last_pictures_kind = None   # "reference" when this turn drew pictures for the customer to approve
        self.last_question = None   # {"question", "options"} when the director asked the customer to choose this turn
        self.messages = [{"role": "system", "content": SYSTEM.format(
            categories=", ".join(skills.all_categories()), cats=", ".join(CATEGORIES),
            styles=", ".join(STYLES), engines=", ".join(ENGINES), pricing=pricing_note)}]

    # ------------------------------------------------------------ tools
    def _set_brief(self, a):
        base = self.spec.to_dict() if self.spec else {}
        a = dict(a)
        if "single_picture" in a:                       # the tool's narrow switch; multiview itself is never exposed
            wants_single = bool(a.pop("single_picture"))
            # the model reaches for this on its own (the Apache of 2026-09-24 was seeded from one picture); it counts
            # only when the customer's own last words asked for one picture
            last_user = next((m.get("content") or "" for m in reversed(self.messages) if m.get("role") == "user"), "")
            asked = any(w in str(last_user).lower() for w in ("single picture", "one picture", "one view", "single view", "just one"))
            base["multiview"] = not (wants_single and asked)
        a.pop("multiview", None)
        base.update({k: v for k, v in a.items() if v not in (None, "")})
        # the words in the brief decide the category (a helicopter filed as a prop loses its canopy and cockpit)
        from .brief import fix_category
        base["category"] = fix_category(base.get("category"), base.get("description"), base.get("name"), base.get("search_query"))
        self.spec = Spec.from_dict(base)
        est = pricing.estimate(self.spec)
        out = {"brief": self.spec.to_dict(), "estimate_credits": est["credits"], "estimate_usd": est["usd"],
               "steps": [s for s, _ in est["steps"]]}
        return {**out, **self._money(est["credits"])}

    def _money(self, needed=0):
        """What the director may say about money: the provider accounts, and what this instance has spent so far."""
        bal = self.wallet.balance(self.user)
        accounts = providers.balances()
        money = {"provider_balances_usd": {k: (v or {}).get("usd") for k, v in accounts.items() if k in ("fal", "openrouter")}}
        needed_usd = needed * config.CREDIT_USD
        low = [k for k, v in money["provider_balances_usd"].items() if v is not None and needed and v < needed_usd]
        if low:
            money["provider_too_low_for_this_build"] = low
        return {**money, "spent_so_far_usd": round(-bal / 100, 2)}

    DESIGN_FIELDS = ("description", "category", "style", "edit_instructions", "reference_images", "search_query", "multiview")

    def _design(self):
        d = self.spec.to_dict()
        return {k: d.get(k) for k in self.DESIGN_FIELDS}

    def _make_reference(self, _a):
        if not self.spec:
            return {"error": "no brief yet; call set_brief first"}
        spec_dict = self.spec.to_dict()
        if self.make_reference:
            out = self.make_reference(spec_dict)
        else:
            from .pipeline import make_reference_only
            r = make_reference_only(self.spec, self.user, self.wallet, log=self.log)
            ref = r.get("reference") or {}
            out = {"job_id": r["job_id"], "dir": r["dir"], "status": r["status"], "error": r.get("error"),
                   "views": ref.get("views") or [],
                   "pictures": [{"label": p["label"], "url": p["path"]} for p in (ref.get("pictures") or [])],
                   "checks": ref.get("checks"), "usd_cost": (r.get("bill") or {}).get("usd_cost")}
        if out.get("status") == "done" and out.get("dir"):
            self.reference = {"job_dir": out["dir"], "views": out.get("views") or [], "for": self._design()}
            self.last_pictures = list(out.get("pictures") or [])
            self.last_pictures_kind = "reference"
            return {**out, "next": "The pictures are shown to the customer. Ask them to approve (then call build) or say what to change."}
        return out

    def _refinishes_last(self):
        """True when the brief keeps the last job's design (description, category, style, edit_instructions): the
        service then re-finishes that mesh (size, budget, engine, rig, glass) and no new pictures are needed."""
        if not (self.last_job_id and self.job_status):
            return False
        prev = (self.job_status(self.last_job_id) or {}).get("spec") or {}
        if not prev:
            return False
        cur = self.spec.to_dict()
        return all(cur.get(k) == prev.get(k) for k in ("description", "category", "style", "edit_instructions"))

    def _build(self, a):
        if not self.spec:
            return {"error": "no brief yet; call set_brief first"}
        a = a or {}
        approved = bool(self.reference and self.reference.get("for") == self._design())
        if not approved and not a.get("skip_preview") and not self.spec.reference_job and not self._refinishes_last():
            # the Havoc of 2026-09-24: two failed reference attempts, then a build with no approval. Never again.
            return {"error": "no approved reference pictures for this brief: call make_reference, show the pictures, and build "
                             "only when the customer approves (or when they explicitly asked to skip the preview)"}
        spec_dict = self.spec.to_dict()
        if approved:
            spec_dict["reference_job"] = self.reference["job_dir"]       # seed from the approved pictures
        if self.submit:
            out = self.submit(spec_dict)
            if out.get("job_id") and out.get("status") == "queued":
                self.last_job_id = out["job_id"]
            return out
        self.last_result = build(Spec.from_dict(spec_dict), self.user, self.wallet, log=self.log)
        return self._summary(self.last_result)

    @staticmethod
    def _summary(r):
        summary = {"status": r["status"], "job_dir": r["dir"], "bill": {k: r["bill"][k] for k in
                   ("usd_cost", "credits_charged", "balance")}}
        if r.get("error"):
            summary["error"] = r["error"]
        if r.get("delivery"):
            d = r["delivery"]
            summary["delivery"] = {"dir": r["delivery_dir"], "files": d["files"], "lods": d["lods"],
                                   "dimensions_m": d.get("dimensions_m"), "maps": [m["role"] for m in d["maps"]],
                                   "roughness_mean": d.get("roughness_mean"), "notes": d["notes"][-4:]}
        if r.get("review"):
            summary["review"] = r["review"]
        if r.get("gate"):
            summary["gate"] = r["gate"]
        if r.get("spec"):
            summary["quality"] = assess(Spec.from_dict(r["spec"]), r.get("delivery") or {}, r.get("review"))
        if r.get("package"):
            summary["package"] = r["package"]
        return summary

    def _import_model(self, a):
        path = (a.get("path") or "").strip()
        if not path:
            return {"error": "no path given"}
        d = {k: v for k, v in a.items() if k != "path" and v not in (None, "")}
        d.setdefault("description", "imported model")
        d["size_m"] = float(d.get("size_m") or 0) or -1            # negative keeps the file's own size
        self.spec = Spec.from_dict(d)
        if not self.import_model:
            from .pipeline import rework
            self.last_result = rework(path, self.spec, self.user, self.wallet, log=self.log)
            return self._summary(self.last_result)
        out = self.import_model(path, self.spec.to_dict())
        if out.get("job_id"):
            self.last_job_id = out["job_id"]
        return out

    def _ask(self, a):
        q = str((a or {}).get("question") or "").strip()
        opts = [str(o).strip() for o in ((a or {}).get("options") or []) if str(o).strip()][:5]
        if not q or len(opts) < 2:
            return {"error": "ask_customer needs a question and 2-5 options"}
        self.last_question = {"question": q, "options": opts}
        return {"status": "asked", "next": "End your reply with the question and the options numbered 1..n. Do not call "
                                           "other tools this turn; the customer's next message is their answer."}

    def _job_status(self, a):
        jid = a.get("job_id") or self.last_job_id
        if not self.job_status and self.last_result:
            r = self.last_result
            return {"id": r.get("job_id"), "status": r.get("status"), "spec": r.get("spec"),
                    "summary": {**(r.get("delivery") or {}), "review": r.get("review"), "gate": r.get("gate")}}
        if not jid:
            return {"error": "no job yet"}
        if self.job_status:
            st = self.job_status(jid)
            st = dict(st)
            st.pop("files", None)
            st["log"] = (st.get("log") or [])[-12:]
            return st
        return {"job_id": jid, "status": "unknown"}

    def _read_skill(self, a):
        s = skills.load(a.get("category", "prop"))
        return {"category": s["category"], "guidance": s["body"][:2500]}

    def _balance(self, _a):
        return self._money()

    # ------------------------------------------------------------ chat
    def turn(self, text):
        """One customer message in, the assistant's reply out (tools run in between)."""
        self.messages.append({"role": "user", "content": text})
        self.last_tools = []
        self.last_pictures = []
        self.last_pictures_kind = None
        self.last_question = None
        for _ in range(MAX_TOOL_ROUNDS):
            msg = self.llm.chat(self.messages, model=self.model, tools=TOOLS)
            self.messages.append({"role": "assistant", "content": msg.get("content") or "",
                                  **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
            if not msg.get("tool_calls"):
                return msg.get("content") or ""
            for call in msg["tool_calls"]:
                name = call["function"]["name"]
                try:
                    a = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    a = {}
                fn = {"set_brief": self._set_brief, "build": self._build, "read_skill": self._read_skill,
                      "balance": self._balance, "job_status": self._job_status, "import_model": self._import_model,
                      "make_reference": self._make_reference, "ask_customer": self._ask}.get(name)
                out = fn(a) if fn else {"error": "unknown tool %s" % name}
                self.last_tools.append({"name": name, "args": a, "result": json.dumps(out, default=str)[:400]})
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                                      "content": json.dumps(out, default=str)})
        done = ", ".join(t["name"] for t in self.last_tools) or "nothing"
        return ("I used all my tool calls this turn (%s). Look at what came back above and tell me how to go on." % done)

    @property
    def llm(self):
        if self._llm is None:
            self._llm = LLM(log=self.log)
        return self._llm

    def chat_cost_usd(self):
        return self._llm.spent() if self._llm else 0.0
