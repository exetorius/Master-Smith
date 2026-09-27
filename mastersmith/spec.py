"""The brief the director fills in from the chat. Everything the pipeline needs, nothing it does not."""
from dataclasses import dataclass, field, asdict

CATEGORIES = ("weapon", "vehicle", "aircraft", "helicopter", "character", "prop", "environment")
ENGINES = ("unreal", "unity", "godot")
STYLES = ("realistic", "stylized")


def assembly_wanted(spec):
    """Hard surfaces are built from planned parts unless the brief asks for one seed; the pipeline still falls back to
    one seed when the approved pictures have no side and front view."""
    if spec.build_mode == "single":
        return False
    if spec.build_mode == "assembly":
        return True
    return spec.category in ("weapon", "vehicle", "aircraft", "helicopter") and bool(spec.multiview)


def weapon_has_glass(description):
    """True when the caption describes an optic, lens or light on the weapon, ignoring negated mentions ("no scope")."""
    import re
    text = re.sub(r"\b(no|without|never)(\s+an?|\s+any)?\s+(scope|optic|optics|sight|sights|lens|laser|light|flashlight)s?\b",
                  " ", (description or "").lower())
    return re.search(r"\b(scope|optic|optics|red[ -]dot|holograph\w*|reflex sight|lens|laser|flashlight|weapon light)\b", text) is not None


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
    premium: bool = False             # dearer picture model for hard briefs; pictures only
    glass: object = None              # None -> by category (vehicles, weapons, environments yes); mark glass faces
    rig: object = None                # None -> by category (characters yes; weapons/vehicles when asked); rig the asset
    notes: str = ""                   # anything else the customer said that matters
    edit_instructions: str = ""       # a refine: what to change against the previous version; the build picture is then an
                                      # EDIT of the previous reference picture, so everything unmentioned stays as it was
    seed_vendor: str = None           # None -> Tripo H3.1; "meshy7mv" (Meshy v7 multi-image, ~$0.035, 3x slower),
                                      # "hitem3d3" (Hi3D v3, crisper textures, single view), "hitem3d3mv" (Hi3D v3 from every
                                      # approved angle, same price), "meshy7", "hitem3d"
    reference_job: str = None         # the directory of a finished reference job whose approved pictures this build
                                      # seeds from; the picture stage is skipped
    build_mode: str = None            # None -> assembly for hard surfaces with a side + front view, else one seed;
                                      # "assembly" | "single" force a path (docs/ASSEMBLY.md)
    picture_model: str = None         # OpenRouter image model for this build's pictures (concept, edits, views); None -> config

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
        if self.build_mode not in (None, "assembly", "single"):
            self.build_mode = None
        if self.rig is None:
            self.rig = self.category == "character"
        self.rig = bool(self.rig)
        refs = [r for r in (self.reference_images or []) if isinstance(r, str) and r.strip()]
        if self.reference_image:
            refs = [self.reference_image] + [r for r in refs if r != self.reference_image]   # the primary leads
        self.reference_images = refs[:4]
        self.reference_image = refs[0] if refs else ""
        self.search_query = " ".join(str(self.search_query or "").split())[:120]
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
        # unknown keys are dropped: specs stored in the database and chats before 2026-09-26 still carry the post-op
        # repair fields (texture_fixes, add_parts, remove_parts, retexture, cockpit, hybrid, ...) that no longer exist
        allowed = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        allowed.setdefault("name", "Asset")
        allowed.setdefault("description", "")
        return cls(**allowed)
