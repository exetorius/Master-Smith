"""OpenRouter chat completions with tool calling and per-call cost capture (usage.cost)."""
import base64
import json
import time
import os
import re

import requests

from . import config

URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, key=None, log=print):
        self.key = key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.key:
            raise LLMError("OPENROUTER_API_KEY is not set (put it in .env)")
        self.log = log
        self.calls = []
        self.stage = ""                  # the pipeline names the stage; every call carries it for the cost breakdown

    def chat(self, messages, model=None, tools=None, max_tokens=1200, temperature=0.3, json_only=False, effort="low"):
        """Returns the assistant message dict (content, tool_calls) and records its cost.

        Reasoning models spend their thinking INSIDE max_tokens: Gemini 3.8 Flash used 477 of a 500-token
        budget on reasoning and cut the JSON answer off mid-word (2026-09-16). Effort is pinned low - the
        director fills a form and the checker answers yes/no questions - and JSON answers are requested as such."""
        body = {"model": model or config.DIRECTOR_MODEL, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "usage": {"include": True}, "reasoning": {"effort": effort}}
        if json_only:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        r = None
        for attempt in range(3):
            try:
                r = requests.post(URL, headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                                                "HTTP-Referer": "https://github.com/kevinpbuckley/Master-Smith", "X-Title": "Master Smith"},
                                  data=json.dumps(body), timeout=180)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                # one slow OpenRouter read killed a whole production job at "reading the brief" (M4A1, wave 19)
                if attempt == 2:
                    raise LLMError("openrouter unreachable after 3 attempts: %s" % str(exc)[:200])
                time.sleep(5 * (attempt + 1))
        if r.status_code != 200:
            raise LLMError("openrouter HTTP %d: %s" % (r.status_code, r.text[:400]))
        data = r.json()
        if data.get("error"):
            raise LLMError("openrouter: %s" % str(data["error"])[:400])
        usage = data.get("usage") or {}
        usd = float(usage.get("cost") or 0.0)
        self.calls.append({"model": body["model"], "usd": usd, "tokens_in": usage.get("prompt_tokens"),
                           "tokens_out": usage.get("completion_tokens"), "stage": self.stage})
        msg = data["choices"][0]["message"]
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
