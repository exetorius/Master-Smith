"""Words in the brief that decide the category. The category picks which passes run (glass, canopy,
sockets, rig), so the customer's words win over the chat model's filing: a helicopter is a helicopter whatever
it was called (an Apache filed as a PROP got a 1 m, 30k-tri build with no canopy)."""

AIRCRAFT_WORDS = ("fighter jet", "jet ", "airplane", "aeroplane", "aircraft", "bomber", "warplane", "fixed-wing", "biplane",
                  "airliner", "glider", "seaplane")
HELICOPTER_WORDS = ("helicopter", "chopper", "rotorcraft", "gunship", "helo ")
WEAPON_WORDS = (" rifle", " pistol", " carbine", " shotgun", " revolver", " handgun", " smg", " submachine", " sniper",
                " assault rifle", " machine gun", " launcher", " crossbow", " sword", " axe", " dagger", " katana")
VEHICLE_WORDS = (" truck", " pickup", " car ", " sedan", " suv", " jeep", " humvee", " hmmwv", " tank", " apc", " halftrack",
                 " motorcycle", " bus ", " van ", " tractor", " forklift", " buggy", " armoured vehicle", " armored vehicle")


def fix_category(category, *texts):
    """Characters, environments, weapons, aircraft and helicopters stand as filed; a prop or vehicle whose words say
    otherwise is re-filed."""
    if category not in ("prop", "vehicle", None, ""):
        return category
    blob = " " + " ".join(t or "" for t in texts).lower() + " "
    if any(w in blob for w in HELICOPTER_WORDS):
        return "helicopter"
    if any(w in blob for w in AIRCRAFT_WORDS):
        return "aircraft"
    if category in ("prop", "vehicle", None, "") and any(w in blob for w in WEAPON_WORDS):
        return "weapon"
    if category in ("prop", None, "") and any(w in blob for w in VEHICLE_WORDS):
        return "vehicle"
    return category
