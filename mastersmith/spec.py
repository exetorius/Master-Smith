"""The brief the director fills in from the chat. Everything the pipeline needs, nothing it does not."""
from dataclasses import dataclass, field, asdict

CATEGORIES = ("weapon", "vehicle", "aircraft", "helicopter", "character", "prop", "environment")
ENGINES = ("unreal", "unity", "godot")
STYLES = ("realistic", "stylized")

# Scripted texture repairs the finish can apply on a re-finish of the same seed (no vendor, no spend). The director
# reaches for these before any repaint or new mesh when the complaint is about the texture, not the shape.
def weapon_has_glass(description):
    """True when the caption describes an optic, lens or light on the weapon, ignoring negated mentions ("no scope")."""
    import re
    text = re.sub(r"\b(no|without|never)(\s+an?|\s+any)?\s+(scope|optic|optics|sight|sights|lens|laser|light|flashlight)s?\b",
                  " ", (description or "").lower())
    return re.search(r"\b(scope|optic|optics|red[ -]dot|holograph\w*|reflex sight|lens|laser|flashlight|weapon light)\b", text) is not None


TEXTURE_FIXES = {
    "smooth_organic_normals": "opt-in shading-normal repair for a continuous organic mesh: smooth shared-vertex normals on source and LODs before baking; removes intentional hard edges too, so do not use on mechanical parts or mixed assemblies",
    "preserve_seed_maps": "preserve the original seed normal/AO and skip reference projection; repair finishing-induced artifacts without buying a mesh",
    "delight": "remove baked-in lighting and painted shadows/highlights from the base colour (strong de-light)",
    "clear_glass_highlights": "darken the reflections the vendor painted on the cockpit interior under a clear canopy",
    "dark_canopy": "make the canopy/windows an opaque dark tint instead of clear glass (hides a hollow interior)",
    "kill_highlights": "replace bright colourless speckles and streaks (painted specular on rails, receivers, barrels) with the surrounding colour",
}

# Where an added part goes, relative to its anchor (a phrase the segmenter finds on the body, or "glass" for the
# canopy/window faces, or "body" for the whole object).
PLACEMENTS = {
    "inside": "fitted inside the anchor's box (a cockpit interior under the canopy, a cargo load in a bed)",
    "on_top": "sitting on top of the anchor (a scope on the rail, a roof rack, a turret on the hull)",
    "in_front": "ahead of the anchor along +X (a suppressor at the muzzle, a plough on the nose)",
    "behind": "behind the anchor along -X (a stock, a tow hitch)",
    "below": "hanging under the anchor (an underbarrel launcher, a sensor pod, a drop tank)",
}

DEFAULT_TRIS = {"weapon": 60000, "vehicle": 120000, "aircraft": 120000, "helicopter": 120000, "character": 80000, "prop": 30000, "environment": 80000}
DEFAULT_SIZE_M = {"weapon": 1.0, "vehicle": 5.0, "aircraft": 15.0, "helicopter": 17.0, "character": 1.8, "prop": 1.0, "environment": 4.0}


@dataclass
class Spec:
    name: str                         # asset name, PascalCase, no spaces (SM_<name>)
    description: str                  # what it is, as a photo caption: materials, colours, era, distinctive parts
    category: str = "prop"
    style: str = "realistic"
    engine: str = "unreal"
    tri_budget: int = 0               # 0 -> category default
    size_m: float = 0.0               # longest dimension in metres; 0 -> category default
    reference_image: str = ""         # local path or URL the customer supplied (the first of them)
    reference_images: list = None     # every picture the customer supplied (paths or URLs)
    research: object = None           # None -> search the web for a photo when search_query names a real thing
    search_query: str = ""            # the real-world name to look up pictures for; empty for fictional/generic objects
    multiview: object = None          # None -> by category (weapons and vehicles yes); True/False to force
    premium: bool = False             # dearer picture model for hard briefs; pictures only (no part seeds, 2026-09-25)
    glass: object = None              # None -> by category (vehicles, weapons, environments yes); mark glass faces
    cockpit: object = None            # None -> aircraft and helicopters get a cockpit built under the canopy
    rig: object = None                # None -> by category (characters yes; weapons/vehicles when asked); rig the asset
    notes: str = ""                   # anything else the customer said that matters
    edit_instructions: str = ""       # a refine: what to change against the previous version; the build picture is then an
                                      # EDIT of the previous reference picture, so everything unmentioned stays as it was
    retexture: bool = False           # a refine that changes only colours/materials: repaint the existing mesh, keep its shape
    retexture_parts: list = None      # ...and only these named parts of it ("stock", "slide"); empty = the whole object
    protect_parts: list = None        # neighbouring parts a repaint must leave alone ("the translucent amber magazine")
    part_seeds: object = None         # None -> hero hard-surface builds (150k+ tris) seed small attached parts separately (issue #5)
    repaint: str = None               # with hybrid: "meshy" (retexture vendor) or "pictures" (renders repainted and baked); None -> config
    hybrid: object = None             # True -> Meshy v7 geometry + a retexture pass on our unwrap (clean albedo); None -> config default
    seed_vendor: str = None           # None -> Tripo H3.1; "meshy7mv" (Meshy v7 multi-image, ~$0.035, 3x slower),
                                      # "hitem3d3" (Hi3D v3, crisper textures, single view), "hitem3d3mv" (Hi3D v3 from every
                                      # approved angle, same price), "meshy7", "hitem3d"
    reference_job: str = None         # the directory of a finished reference job whose approved pictures this build
                                      # seeds from; the picture stage is skipped
    remove_parts: list = None         # a repair on the existing mesh: parts to delete in Blender, as descriptive phrases
                                      # ("the extra cylinder attached to the magazine"); re-applied on every re-finish
    picture_model: str = None         # OpenRouter image model for this build's pictures (concept, edits, views); None -> config
    texture_fixes: list = None        # scripted texture repairs applied on a re-finish (see TEXTURE_FIXES): free, deterministic
    add_parts: list = None            # parts to model separately and fit onto the existing mesh on a re-finish (see PLACEMENTS):
                                      # [{"name", "phrase", "anchor", "place", "size_m", "offset_m", "picture", "seed"}]; the
                                      # body is not reseeded; "seed" is a part mesh an earlier job bought, reused as it is;
                                      # offset_m = [forward, left, up] metres from where the placement would put it

    def __post_init__(self):
        # Asset name rule: letters, digits, underscores, hyphens, starting with a letter. Anything
        # else is squeezed into PascalCase words.
        import re
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", self.name or ""):
            pass
        else:
            words = "".join(ch if ch.isalnum() else " " for ch in self.name).split()
            self.name = "".join(w if w[:1].isupper() else w[:1].upper() + w[1:] for w in words) or "Asset"
        if self.category not in CATEGORIES:
            self.category = "prop"
        if self.style not in STYLES:
            self.style = "realistic"
        if self.engine not in ENGINES:
            self.engine = "unreal"
        if not self.tri_budget or self.tri_budget < 500:
            self.tri_budget = DEFAULT_TRIS[self.category]
        if self.size_m is None or self.size_m == 0:
            self.size_m = DEFAULT_SIZE_M[self.category]      # negative = keep the source size (convert jobs)
        if self.multiview is None:
            # weapons only: a profile + a muzzle view + its mirror are consistent 90-degree views. A vehicle's
            # three-quarter picture is not 90 degrees from its profile, and Tripo answered that mismatch with a
            # second tail on both helicopters of the 2026-09-17 batch.
            # vehicles, aircraft and helicopters get strictly orthographic front / left / back views edited from the
            # primary picture, each checked, and fall back to one picture when any check fails (issue #8, 2026-09-18)
            # every category: the customer approves the angles before the mesh is bought, and Tripo multiview measured
            # a win on the realism lab's rifle (2026-09-16); props/characters/environments get their skill's second view
            self.multiview = True
        self.multiview = bool(self.multiview)
        if self.glass is None:
            # a weapon has glass only when an optic or lens is described: "scope lens glass" masks found 1,000 faces on
            # a bullpup with no scope and the glass slot then blocked the detail bake (2026-09-25)
            self.glass = self.category in ("vehicle", "aircraft", "helicopter", "environment") or (
                self.category == "weapon" and weapon_has_glass(self.description))
        self.glass = bool(self.glass)
        if self.rig is None:
            self.rig = self.category == "character"
        self.rig = bool(self.rig)
        if self.cockpit is None:
            # off by default: the second "cockpit tub" model fitted under the canopy is a gamble (a Havoc gunship's
            # tub came out 2.7x scaled with a fifth of it through the airframe, 2026-09-23). The seed's own interior
            # and the glass slot ship; cockpit=true in the brief asks for the tub.
            self.cockpit = False
        self.cockpit = bool(self.cockpit) and self.category in ("aircraft", "helicopter") and bool(self.glass)
        refs = [r for r in (self.reference_images or []) if isinstance(r, str) and r.strip()]
        if self.reference_image:
            refs = [self.reference_image] + [r for r in refs if r != self.reference_image]   # the primary leads
        self.reference_images = refs[:4]
        self.reference_image = refs[0] if refs else ""
        self.search_query = " ".join(str(self.search_query or "").split())[:120]
        parts = []
        for p in self.remove_parts or []:
            phrase = (p.get("phrase") if isinstance(p, dict) else str(p or "")).strip()
            if phrase and phrase not in parts:
                parts.append(phrase)
        self.remove_parts = parts[:4]
        fixes = []
        for f in self.texture_fixes or []:
            key = str(f or "").strip().lower()
            if key in TEXTURE_FIXES and key not in fixes:
                fixes.append(key)
        self.texture_fixes = fixes
        added = []
        for p in self.add_parts or []:
            if not isinstance(p, dict):
                continue
            phrase = str(p.get("phrase") or "").strip()
            if not phrase:
                continue
            name = "".join(ch for ch in str(p.get("name") or phrase.split()[-1]) if ch.isalnum()) or "Part"
            place = str(p.get("place") or "inside").strip().lower()
            try:
                size = float(p.get("size_m") or 0)
            except (TypeError, ValueError):
                size = 0.0
            off = []
            for v in (p.get("offset_m") or [0, 0, 0])[:3]:
                try:
                    off.append(max(-50.0, min(50.0, float(v or 0))))
                except (TypeError, ValueError):
                    off.append(0.0)
            off = (off + [0.0, 0.0, 0.0])[:3]
            added.append({"name": name[:40], "phrase": phrase[:200], "anchor": str(p.get("anchor") or "body").strip()[:200],
                          "place": place if place in PLACEMENTS else "inside", "size_m": max(0.0, size), "offset_m": off,
                          "picture": str(p.get("picture") or "").strip() or None,
                          "seed": str(p.get("seed") or "").strip() or None})     # a mesh already bought for this part
            # A facing repair reuses the bought seed instead of asking vision to guess again.
            if p.get("yaw_degrees") in (-180, -90, 0, 90, 180) and not isinstance(p.get("yaw_degrees"), bool):
                added[-1]["yaw_degrees"] = int(p["yaw_degrees"])
            if isinstance(p.get("provides"), list):
                from .repair import COMPONENTS
                added[-1]["provides"] = list(dict.fromkeys(v for v in p["provides"] if isinstance(v, str) and v in COMPONENTS))
        self.add_parts = added[:4]

        if self.research is None:
            self.research = bool(self.search_query) and not refs
        self.research = bool(self.research)

    def portable(self):
        """The brief as it may be stored (in the .blend, a result, a chat state): pictures that are local
        paths of THIS job are dropped - the next job runs on another machine (previous_reference.png of
        job bb6abdc4, 2026-09-17). URLs stay."""
        d = self.to_dict()
        refs = [r for r in (d.get("reference_images") or []) if isinstance(r, str) and r.startswith(("http://", "https://"))]
        d["reference_images"] = refs
        d["reference_image"] = refs[0] if refs else ""
        return d

    def drop_local_pictures(self):
        refs = [r for r in (self.reference_images or []) if isinstance(r, str) and r.startswith(("http://", "https://"))]
        self.reference_images = refs
        self.reference_image = refs[0] if refs else ""
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        allowed = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        allowed.setdefault("name", "Asset")
        allowed.setdefault("description", "")
        return cls(**allowed)
