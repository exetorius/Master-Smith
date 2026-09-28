"""Stage 6: the customer's package. A README in the delivery folder with the import steps for their
engine, and one zip with everything."""
import json
import os
import zipfile

UE_NOTES = """Unreal Engine import
- Static mesh: import SM_{name}.fbx. Unreal reads UCX_SM_{name}_01 in the same file as simple collision;
  untick "Auto Generate Collision". Import SM_{name}_LOD1.fbx and _LOD2.fbx as LODs of the same asset
  (LOD Settings > Import LOD Level), or enable Nanite and skip the LODs.
- Textures: T_{name}_BC (sRGB), T_{name}_N (Normal Map compression, sRGB off), T_{name}_ORM
  (Masks compression, sRGB off; R = occlusion, G = roughness, B = metallic).
- Materials: the mesh carries slot MI_{name}. {glass_note}
{rig_note}
- Forward: +X. Origin: {origin}. Real size: {dims} m.
"""

UNITY_GODOT_NOTES = """Unity / Godot import
- Import SM_{name}.glb (Y-up, metres). The UCX_ hull is not included in the GLB; use a convex mesh collider.
- Textures are embedded in the GLB and also provided as PNGs (T_{name}_BC, _N, _ORM: G = roughness, B = metallic).
- Forward: +X (Blender) which is -Z after the glTF Y-up conversion in most importers; check on import.
{rig_note}
"""


def write_package(spec, report, result, delivery_dir):
    glass = report.get("glass")
    glass_note = ("Slot MI_{name}_Glass covers the glass (%d faces%s): assign a translucent material instance to it."
                  % (glass.get("faces", 0), (", %d built pane(s)" % glass["panes"]) if glass.get("panes") else "")) if glass else "No glass slot."
    if report.get("tiles"):
        glass_note += (" A seamless tiling material set (T_{name}_Tile_BaseColor/Normal/Roughness/Metallic, plus Height) ships next to "
                       "the atlas; in the engine, layer it over the base material as surface detail on the large flat faces.")
    rig = result.get("rig") or {}
    if rig.get("status") == "rigged" and spec.category == "character":
        rig_note = ("- Skeletal mesh: import SK_{name}.fbx (humanoid skeleton). Import the A_{name}_* files as animations onto "
                    "that skeleton (walking, running).")
    elif rig.get("status") == "rigged" and report.get("sockets"):
        rig_note = ("- Skeletal mesh: import SK_{name}.fbx; bones root + %s are attach points (muzzle flash, hands, optics). "
                    "Positions also in meta.json." % ", ".join(s["name"] for s in report["sockets"]))
    elif rig.get("status") == "rigged":
        rig_note = ("- Skeletal mesh: import SK_{name}.fbx; bones: %s. Rotate a wheel bone about its local Y to spin it."
                    % ", ".join(rig.get("bones", [])))
    else:
        rig_note = "- Static asset; no skeleton."
    name = spec.name
    dims = " x ".join("%.2f" % d for d in (report.get("dimensions_m") or []))
    origin = "bottom centre (sits on the floor)" if spec.category != "weapon" else "centre of the body"
    text = "%s - built by Master Smith\n\n%s\n%s\nReviewer: %s/10 %s\n%s\nGate: %s\n" % (
        name, spec.description,
        (UE_NOTES if spec.engine == "unreal" else UNITY_GODOT_NOTES).format(
            name=name, glass_note=glass_note.format(name=name), rig_note=rig_note.format(name=name), origin=origin, dims=dims),
        (result.get("review") or {}).get("score"), (result.get("review") or {}).get("verdict", ""),
        "\n".join("- " + i for i in (result.get("review") or {}).get("issues", [])),
        "ok" if (result.get("gate") or {}).get("ok") else "; ".join((result.get("gate") or {}).get("warnings", [])))
    readme = os.path.join(delivery_dir, "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(text)
    zpath = os.path.join(delivery_dir, "%s.zip" % name)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, _dn, fns in os.walk(delivery_dir):
            for fn in sorted(fns):
                if fn.endswith(".zip"):
                    continue
                full = os.path.join(dp, fn)
                z.write(full, os.path.relpath(full, delivery_dir))
    with open(os.path.join(delivery_dir, "manifest.json"), "w") as f:
        json.dump({"name": name, "spec": spec.to_dict(), "files": sorted(os.listdir(delivery_dir)),
                   "review": result.get("review"), "gate": result.get("gate"), "rig": {k: v for k, v in rig.items() if k != "notes"}},
                  f, indent=1, default=str)
    return {"readme": "README.txt", "zip": os.path.basename(zpath)}
