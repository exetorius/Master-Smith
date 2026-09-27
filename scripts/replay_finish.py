"""Re-run ONLY Blender against a saved job's seed, masks and purchased parts. No API/provider calls.

Use a separate empty output directory and mount the source data read-only when running in Docker.
This is a diagnostic artifact, not a paid job or a replacement for the original delivery.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_job", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--omit-additions", nargs="*", default=[])
    parser.add_argument("--preserve-seed-maps", action="store_true")
    parser.add_argument("--render-size", type=int, default=512)
    options = parser.parse_args()
    source, output = options.source_job.resolve(), options.output.resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("diagnostic output must be separate from the source job")
    if output.exists() and any(output.iterdir()):
        raise ValueError("diagnostic output must be empty; never overwrite a delivery")
    with (source / "work" / "finish_args.json").open() as f:
        args = json.load(f)
    output.mkdir(parents=True, exist_ok=True)
    args["out_dir"] = str(output / "delivery")
    args["render_size"] = options.render_size
    args["reproject"] = False  # replay must not write projection intermediates to the source work directory
    omit = set(options.omit_additions)
    args["add_parts"] = [p for p in args.get("add_parts") or [] if p.get("name") not in omit]
    args.setdefault("spec", {})["add_parts"] = [p for p in args["spec"].get("add_parts") or [] if p.get("name") not in omit]
    if options.preserve_seed_maps:
        args["texture_fixes"] = list(dict.fromkeys((args.get("texture_fixes") or []) + ["preserve_seed_maps"]))
        args["spec"]["texture_fixes"] = args["texture_fixes"]
    args_file = output / "finish_args.json"
    args_file.write_text(json.dumps(args, indent=2), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "mastersmith" / "blender" / "finish.py"
    with (output / "finish.log").open("w", encoding="utf-8") as log:
        result = subprocess.run([os.environ["BLENDER_BIN"], "-b", "--python", str(script), "--", str(args_file)],
                                stdout=log, stderr=subprocess.STDOUT, timeout=2400)
    if result.returncode or not (output / "delivery" / "report.json").exists():
        print((output / "finish.log").read_text(encoding="utf-8", errors="replace")[-6000:])
        return result.returncode or 1
    report = json.loads((output / "delivery" / "report.json").read_text())
    print(json.dumps({"output": str(output), "bake": report.get("bake"), "lods": report.get("lods"),
                      "review_renders": report.get("review_renders"), "inspection_renders": report.get("inspection_renders"),
                      "omitted_additions": sorted(omit), "provider_cost_usd": 0}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
