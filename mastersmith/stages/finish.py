"""Running a headless Blender script with a JSON argument file. The one-seed finish (prepare.py / finish.py)
that lived here went with the service (2026-09-28); assemblies run blender/assemble.py through _blender()."""
import json
import os
import subprocess
import shutil

from .. import config

BLENDER_DIR = config.ROOT / "mastersmith" / "blender"


def _blender(job, script, args, tag, timeout=2400):
    if not os.path.exists(config.BLENDER_BIN):
        raise RuntimeError("Blender not found at %s (set BLENDER_BIN)" % config.BLENDER_BIN)
    args_path = os.path.join(job.work_dir, "%s_args.json" % tag)
    with open(args_path, "w") as f:
        json.dump(args, f, indent=1)
    cmd = [config.BLENDER_BIN, *config.BLENDER_FLAGS, "--python", str(BLENDER_DIR / script), "--", args_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Blender %s ran past %d s and was stopped" % (script, timeout))
    log_path = os.path.join(job.work_dir, "%s.log" % tag)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout or "")
        f.write("\n--- stderr ---\n")
        f.write(proc.stderr or "")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-12:])
        err = "\n".join((proc.stderr or "").splitlines()[-6:])
        raise RuntimeError("Blender %s failed (exit %s); see %s\n%s\n%s" % (script, proc.returncode, log_path, tail, err))
