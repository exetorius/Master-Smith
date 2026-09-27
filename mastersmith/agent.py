"""The director: a cheap chat model that turns conversation into a Spec, quotes the price, and runs the
pipeline when the customer says go. It never writes Blender code and never sees images - the pipeline
does the work, the model does the talking. That is what keeps OpenRouter spend to a few cents a job."""
import json
import os

from . import config, pricing, providers, skills
from .llm import LLM
from .pipeline import build
from .spec import CATEGORIES, ENGINES, STYLES, TEXTURE_FIXES, Spec
from .quality import assess
from .repair import assembly_conflicts, plan_repair

SYSTEM = """You are Master Smith, a 3D asset director. A customer describes a game asset; you turn it into a build brief,
quote the credits, and run the build when they confirm. You are concise and concrete.

How a job goes:
1. From the customer's words call set_brief with your best complete brief. Fill sensible defaults yourself
   (category, real size in metres, triangle budget, engine) - ask at most ONE short question, and only when the
   answer would change the model itself (e.g. "which tank?", "realistic or stylized?"). Ask through ask_customer
   with 2-4 concrete options: the chat shows them as numbered buttons the customer can click, and they may also
   type anything. Do that whenever you need a decision, including "go ahead?" moments and which remedy to apply. Write `description`
   as a photo caption: what it is, its materials and colours, distinctive parts, era. Write colours as a camera
   sees them, never as trade terms ('blued steel' is dark blue-black oxidised steel, not blue). With a customer photo,
   read each part's colour off the photo (a magazine or grip the same grey as the body is grey, not 'black'): the
   reviewer holds the model to your caption, and a wrong colour word costs a repaint. Fixed-wing aircraft are
   category "aircraft" and rotorcraft "helicopter" (not "vehicle"). Name real machines by name
   and put that name in search_query so a real photograph is found; leave search_query empty for invented things.
   When the customer attached pictures, put them in reference_images (they are edited into the build picture).
   For a hard-surface build (weapon, vehicle, aircraft) from the customer's photo, seed_vendor "hitem3d3mv" is the
   quality pick: Hi3D v3's crisp geometry from every approved angle, same price as one angle. `premium` only changes
   the picture model (pin picture_model instead); it does not add parts. Part seeds run on hero budgets alone.
   When the customer wants an existing model changed, keep the whole description and put only what changes in
   edit_instructions; the previous picture is edited so the rest of the design stays as it was. When only colours,
   finishes or materials change ("the stock should be gunmetal, not cream"), set retexture=true and name the parts in
   retexture_parts: the mesh is repainted, nothing moves, and it costs about half a rebuild.
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
   If the reviewer said rebuild, say what you would change and ask before spending again.
4. For changes after a build, call set_brief again with ONLY the changed fields and then build when confirmed. The
   name and the description stay what they were: never replace them with the part being fixed (a request to fix the
   magazine is still the same rifle). Cheapest remedy first, and say which it will be:
   - a defect on the built model - an extra or wrong part the vendor grew (a cylinder on the magazine, a sling fused
     to the stock, a stand, a floating blob) - goes in remove_parts: Blender deletes it and re-finishes, no new mesh.
     build first shows the customer what would go, in red on the renders; ask them to confirm, and only then call
     build with confirm_removal=true. If the red covers more than the defect (the whole magazine instead of the
     cylinder on it), reword the phrase or use another remedy instead;
   - ADDING something to the built model (a cockpit interior, a scope, a suppressor, a launcher, a rack, a pod) is
     add_parts: say what it is, where it anchors, how it sits (inside / on_top / in_front / behind / below), its size
     in metres and, when several parts share an anchor, offset_m [forward, left, up] to spread them (pedals ahead of
     the seat, a stick between seat and panel); a re-finish models each alone, seeds it and fits it there. Parts that
     live inside are drawn from the words, so the phrase must say exactly what to include and what to leave out ("floor
     pan, two side walls and a rear bulkhead, no seat"). A part already bought is reused by name + phrase, so repeat
     them exactly to keep it. The body is NOT reseeded and the brief's
     name and description do not change. Ask for the size when it is not obvious (a cockpit interior of a 12 m gunship
     is about 2.2 m; a rifle scope 0.25 m);
   - size, triangle budget, glass, rig or engine changes re-finish the same mesh;
   - a TEXTURE complaint (baked-in lighting or painted shadows, reflections or white blobs on the glass, a milky or
     hollow-looking canopy, blurry highlights) is a job for a script, never for a new mesh: put the matching
     texture_fixes on the brief and build - a free re-finish applies them. Only if the scripts cannot fix it, a
     repaint (retexture=true, about $1.20). First use plan_repair to separate source texture defects from damage
     introduced by finishing; a clean seed damaged by the finish needs preserve_seed_maps, not automatic delight;
   - colour, finish or material changes go through retexture=true: the mesh is repainted, nothing moves;
   - only a change of shape or proportions buys a new mesh: keep the description, put the change in edit_instructions,
     call make_reference so the customer approves the edited picture, then build.
   When the reviewer's verdict is "rebuild", do not rebuild on your own: say which of these remedies fits each issue
   and ask. job_status carries a `diagnosis`: findings from the build's own logs (how many angles the seed had, which
   material families matched, whether a photo was projected on, what the reviewer's words mean) each with a remedy
   and, when there is one, the exact brief change (`fix`). Read it before proposing anything; quote the findings to
   the customer in plain words and offer the fixes as numbered options.
5. When the customer attaches a 3D model file (.glb, .gltf, .fbx, .obj or a delivered .blend), call import_model with
   its path, a PascalCase name, the category and whatever else they told you: the model is oriented, scaled, given
   glass, LODs, collision, maps and previews without buying a new mesh, and later changes work on it like on a build.
   Attached pictures go in reference_images of set_brief.

Skills you can read for guidance on a category: {categories}. Use read_skill when unsure how a category is handled.
Categories: {cats}. Styles: {styles}. Engines: {engines}.
Repair discipline (especially cabins and other assemblies):
- A complaint that a repair still looks wrong means the repair is unresolved. Call job_status before proposing another
  build. Read summary.quality, the actual review verdict, assembly checks and fitting records, not just the last log.
- "done" means files were produced; a technical gate pass means packaging checks passed. Neither means it looks right.
  A rebuild verdict ALWAYS wins over a numeric score. Never call 5/10 "usable" when the verdict is rebuild. Missing or
  obscured close-up evidence is UNVERIFIED, not fixed. Do not claim you saw an image: use the review's actual evidence.
- Plan an assembly, not a pile of separately generated objects. Inventory what each purchased module already contains
  before adding another seat, floor, console or flight stick. A cockpit module with a stick plus a separate joystick is
  a possible duplicate. Explicitly describe each module's exclusions; changing a phrase may purchase a new seed, so quote
  that cost. Do not blindly keep adding parts. Keep the original body and purchased seeds unless a defect requires replacement.
- Separate geometry/placement failures from texture problems. Check forward orientation, relative size, floor contact,
  panel/seat spacing, leg room, control reach and intersections. Never offer dark_canopy as a fix for a requested visible cabin.
- Before another rework, state the observed defect, the specific brief/placement change, and what the next close-ups must
  show to count as success. If the previous remedy failed, do not repeat it unchanged or invent better results from logs.
  If the available tools cannot resolve the geometry, say so and ask for a targeted modelling repair, not another blind spend.
- Call plan_repair on a failed job: it returns concrete set_brief changes, evidence needed and whether an action requires
  only a re-finish. Compare the seed preview with the final render. If the seed was clean and the finish became faceted,
  use preserve_seed_maps on the SAME seed; do not switch vendors or buy a new body. Do not apply delight to everything.
- Give each added module a truthful provides inventory matching its phrase/geometry. A full cockpit and a standalone
  stick cannot both own the same control by accident. To omit a redundant added part, remove its entry from add_parts;
  do not use remove_parts segmentation to delete it from the aircraft. Do not regenerate a purchased part just to change
  its placement. A hidden control needs an inspection view, not an invented offset or a replacement mesh.
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
            "premium": {"type": "boolean", "description": "Dearer picture model for hard briefs; it changes the pictures only "
                        "(no separately seeded parts). Pinning picture_model does the same without the flag."},
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
                                  "what changes in its appearance. The previous picture is edited, everything unmentioned stays."},
            "retexture": {"type": "boolean", "description": "True when ONLY colours, finishes or materials of the existing model change: "
                          "the mesh is repainted and its shape kept. False when parts, shape or proportions change."},
            "retexture_parts": {"type": "array", "items": {"type": "object", "properties": {
                                    "phrase": {"type": "string", "description": "the part as a descriptive phrase a segmenter can find on a render ('the upper metal slide of the pistol'), never a bare word"},
                                    "color": {"type": "string", "description": "#rrggbb flat colour wanted, or empty for a pattern/texture"},
                                    "current_hex": {"type": "string", "description": "#rrggbb of how the part looks now"},
                                    "metal": {"type": "boolean"}, "finish": {"type": "string", "enum": ["matte", "satin", "glossy"]}},
                                    "required": ["phrase"]},
                                "description": "With retexture: the parts that change; empty for the whole object"},
            "protect_parts": {"type": "array", "items": {"type": "object", "properties": {"phrase": {"type": "string"}, "hex": {"type": "string"}},
                              "required": ["phrase"]}, "description": "With retexture: neighbouring parts that must not change, each a "
                              "descriptive phrase with its colour ('the translucent amber magazine') and its #rrggbb"},
            "texture_fixes": {"type": "array", "items": {"type": "string", "enum": list(TEXTURE_FIXES)},
                              "description": "Scripted texture repairs on the built model, applied by a free re-finish of the same mesh: "
                                             + "; ".join("%s = %s" % (k, v) for k, v in TEXTURE_FIXES.items())
                                             + ". Use these FIRST for any texture complaint."},
            "add_parts": {"type": "array", "items": {"type": "object", "properties": {
                              "name": {"type": "string", "description": "PascalCase, e.g. Cockpit, Scope, Suppressor"},
                              "phrase": {"type": "string", "description": "what to model, as a photo caption ('the cockpit interior: pilot seat, "
                                                                          "instrument panel and side consoles', 'a 4x ACOG scope')"},
                              "anchor": {"type": "string", "description": "where on the body it goes: 'glass' (under the canopy/windows), 'body' (the "
                                                                          "whole object), or a phrase the segmenter finds ('the top rail of the rifle', "
                                                                          "'the muzzle of the barrel')"},
                              "place": {"type": "string", "enum": ["inside", "on_top", "in_front", "behind", "below"]},
                              "size_m": {"type": "number", "description": "the part's longest dimension in metres; 0 = fit the anchor"},
                              "offset_m": {"type": "array", "items": {"type": "number"}, "description": "[forward, left, up] metres to "
                                           "move it from where the placement puts it (a joystick 0.3 m ahead of the seat: [0.3, 0, 0])"},
                              "yaw_degrees": {"type": "integer", "enum": [-180, -90, 0, 90, 180],
                                              "description": "Optional absolute yaw of the prepared part, overriding automatic facing. "
                                                             "Use the previous job's yaw and observed direction to correct a backward "
                                                             "seat/panel/pedals without buying another seed; omit for automatic facing."},
                              "picture": {"type": "string", "description": "an attached picture of the part, if the customer gave one"},
                              "provides": {"type": "array", "items": {"type": "string", "enum": ["seat", "panel", "consoles", "stick", "pedals", "floor", "walls", "bulkhead"]},
                                           "description": "Components actually included by this module. Must match its phrase and geometry; prevents duplicate controls/shells."}},
                              "required": ["name", "phrase", "anchor", "place"]},
                          "description": "Model these parts separately and fit them onto the BUILT model on a re-finish (about $0.70 a part: "
                                         "one picture + one small seed). The body is not reseeded. A cockpit interior under the canopy, a scope "
                                         "on the rail, a suppressor at the muzzle, a roof rack, a drop tank. Keep earlier entries when adding."},
            "remove_parts": {"type": "array", "items": {"type": "string"},
                             "description": "A REPAIR of the built model: parts to delete in Blender, each a descriptive phrase a "
                                            "segmenter can find on a render ('the extra cylinder attached to the magazine', 'the "
                                            "sling fused to the stock', 'the display stand under the vehicle'). No new mesh is bought. "
                                            "Keep earlier entries when adding one; the list is re-applied on every re-finish."}},
            "required": ["name", "description", "category"]}}},
    {"type": "function", "function": {
        "name": "import_model",
        "description": "Bring in a 3D model file the customer attached and finish it for their engine (no new mesh is bought). "
                       "Becomes the current model: later set_brief + build calls re-finish or repaint it.",
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
                                        "they asked to skip the preview, approved the reference picture). With new remove_parts "
                                        "it first answers a preview (red on the renders); call again with confirm_removal=true "
                                        "once the customer has confirmed the red is right.",
        "parameters": {"type": "object", "properties": {
            "confirm_removal": {"type": "boolean", "description": "true only after the customer confirmed the red removal preview"},
            "skip_preview": {"type": "boolean", "description": "true ONLY when the customer explicitly said to skip the reference "
                                                               "preview; otherwise build needs approved pictures from make_reference"}}}}},
    {"type": "function", "function": {
        "name": "job_status", "description": "Status, log tail and results of a queued or finished build.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}},
    {"type": "function", "function": {
        "name": "plan_repair", "description": "Inspect the last failed job and return targeted brief changes without spending or building.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}}}},
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
        self.last_pictures_kind = None   # "reference" (approve to build) or "removal" (confirm to delete the red faces)
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

    def _build(self, a):
        if not self.spec:
            return {"error": "no brief yet; call set_brief first"}
        conflicts = assembly_conflicts(self.spec.add_parts)
        if conflicts:
            return {"error": "assembly plan duplicates components; call plan_repair and resolve ownership before spending",
                    "conflicts": conflicts}
        a = a or {}
        if self.last_job_id and self.job_status and (self.spec.add_parts or self.spec.cockpit):
            previous = self.job_status(self.last_job_id)
            if previous.get("status") in ("queued", "running"):
                return {"error": "the current job is still running; inspect job_status instead of queuing another repair",
                        "job_id": self.last_job_id}
            summary = previous.get("summary") or {}
            old_spec = previous.get("spec")
            quality = summary.get("quality")
            if old_spec and previous.get("status") == "done":
                if quality is None:
                    quality = assess(Spec.from_dict(old_spec), summary, summary.get("review"))
                current_spec = self.spec.to_dict()
                # The service fills provider defaults after submission; that bookkeeping is not a changed repair.
                for key in ("seed_vendor", "picture_model"):
                    if not current_spec.get(key):
                        current_spec[key] = old_spec.get(key)
                old_parts = {p.get("name"): p for p in old_spec.get("add_parts") or []}
                new_picture = any(p.get("picture") and p["picture"] != old_parts.get(p["name"], {}).get("picture")
                                  for p in self.spec.add_parts)
                if not quality.get("accepted") and not new_picture and self._repair_plan(old_spec) == self._repair_plan(current_spec):
                    return {"error": "the previous assembly is failed or unverified and the repair plan is unchanged; "
                                     "inspect job_status, identify a specific defect and change its fit/geometry before "
                                     "spending on another rework", "job_id": self.last_job_id, "quality": quality}
        approved = bool(self.reference and self.reference.get("for") == self._design())
        reworking = bool(self.last_job_id) and (self.spec.retexture or self.spec.remove_parts or self.spec.texture_fixes
                                                or self.spec.add_parts)
        if not approved and not reworking and not a.get("skip_preview") and not self.spec.reference_job:
            # the Havoc of 2026-09-24: two failed reference attempts, then a build with no approval. Never again.
            return {"error": "no approved reference pictures for this brief: call make_reference, show the pictures, and build "
                             "only when the customer approves (or when they explicitly asked to skip the preview)"}
        spec_dict = self.spec.to_dict()
        if self.reference and self.reference.get("for") == self._design():
            spec_dict["reference_job"] = self.reference["job_dir"]       # seed from the approved pictures
        if self.submit:
            out = self.submit(spec_dict, bool((a or {}).get("confirm_removal")))
            if out.get("status") == "preview_removal":
                self.last_pictures = list(out.get("pictures") or [])   # the red-on-render preview; the customer confirms
                self.last_pictures_kind = "removal"
                return out
            if out.get("job_id") and out.get("status") == "queued":
                self.last_job_id = out["job_id"]
            return out
        self.last_result = build(Spec.from_dict(spec_dict), self.user, self.wallet, log=self.log)
        return self._summary(self.last_result)

    @staticmethod
    def _repair_plan(spec):
        """Ignore bookkeeping/cache paths when detecting the Havoc-style unchanged rework loop."""
        d = Spec.from_dict(spec).to_dict()
        fields = ("description", "category", "style", "tri_budget", "size_m", "glass", "cockpit", "rig",
                  "edit_instructions", "retexture", "retexture_parts", "protect_parts", "remove_parts",
                  "texture_fixes", "seed_vendor", "picture_model")
        plan = {k: d.get(k) for k in fields}
        plan["seed_vendor"] = plan["seed_vendor"] or "tripo"
        plan["add_parts"] = [{k: v for k, v in p.items() if k not in ("seed", "picture")} for p in d["add_parts"]]
        return plan

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
        if r.get("diagnosis"):
            summary["diagnosis"] = r["diagnosis"]
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

    def _plan_repair(self, a):
        status = self._job_status(a or {})
        if status.get("error"):
            return status
        if status.get("status") != "done":
            return {"error": "wait for the job's completed report before planning a visual repair"}
        return plan_repair(status)

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
                      "make_reference": self._make_reference, "ask_customer": self._ask,
                      "plan_repair": self._plan_repair}.get(name)
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
