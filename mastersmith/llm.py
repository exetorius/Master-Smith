"""The few model calls some helpers still make (a picture check, the old planner and review paths), answered by a
coding-agent CLI on this PC - Claude Code or Codex (llm_cli.py) - on the owner's subscription, at $0 a call."""
import base64
import json
import os
import re

from . import config


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, key=None, log=print):
        if config.LLM_BACKEND not in ("claude-code", "codex"):
            raise LLMError("MASTERSMITH_LLM must be claude-code or codex, not %r" % config.LLM_BACKEND)
        self.log = log
        self.calls = []
        self.stage = ""                  # the pipeline names the stage; every call carries it for the cost breakdown

    def chat(self, messages, model=None, tools=None, max_tokens=1200, temperature=0.3, json_only=False, effort="low"):
        """Returns the assistant message dict (content, tool_calls) and records its cost.

        Effort is pinned low - the planner fills a form and the checker answers yes/no questions - and JSON answers
        are requested as such. `max_tokens` and `temperature` are kept for the callers; the CLIs do not take them."""
        from . import llm_cli
        try:
            msg, rec = llm_cli.chat(messages, model or config.LLM_MODEL, tools=tools, json_only=json_only,
                                    effort=effort, log=self.log)
        except llm_cli.CLIError as exc:
            raise LLMError(str(exc))
        self.calls.append({**rec, "stage": self.stage})
        return msg

    def vision(self, prompt, image_paths, model=None, max_tokens=1500, effort="low", json_only=True):
        """One question about one or more local images; returns the text."""
        parts = [{"type": "text", "text": prompt}]
        for p in image_paths:
            ext = os.path.splitext(p)[1].lower().lstrip(".")
            mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            parts.append({"type": "image_url", "image_url": {"url": "data:image/%s;base64,%s" % (mime, b64)}})
        msg = self.chat([{"role": "user", "content": parts}], model=model or config.VISION_MODEL,
                        max_tokens=max_tokens, temperature=0.1, json_only=json_only, effort=effort)
        return msg.get("content") or ""

    def spent(self):
        return round(sum(c["usd"] for c in self.calls), 6)


def extract_json(text):
    """The first JSON object in a model reply (models love to wrap it in prose or fences)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
