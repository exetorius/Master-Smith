"""Skills are Markdown files with a small YAML-ish front matter the pipeline reads (reference view,
seed knobs, finishing defaults) and a body the agent and the picture stage read as guidance."""
import json
import re
from pathlib import Path

from .. import config


def load(category):
    path = config.SKILLS_DIR / ("%s.md" % category)
    if not path.exists():
        path = config.SKILLS_DIR / "prop.md"
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                if v[:1] in "[{":
                    try:
                        meta[k.strip()] = json.loads(v)
                        continue
                    except ValueError:
                        pass
                if v.lower() in ("true", "false"):
                    v = v.lower() == "true"
                else:
                    try:
                        v = float(v) if "." in v else int(v)
                    except ValueError:
                        pass
                meta[k.strip()] = v
    return {"category": category, "meta": meta, "body": body.strip()}


def all_categories():
    return sorted(p.stem for p in config.SKILLS_DIR.glob("*.md"))
