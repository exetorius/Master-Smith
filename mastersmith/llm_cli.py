"""Model calls answered by a coding-agent CLI on this PC: Claude Code (`claude -p`) or Codex (`codex exec`), on the
owner's own subscription. llm.LLM routes here; MASTERSMITH_LLM picks "claude-code" or "codex".

Each call is one headless run in a fresh temporary folder. The OpenAI-style messages become one transcript; pictures
become files (Claude Code reads them with its Read tool, Codex gets them attached with -i). Tool calling, which the
old chat director needed, is asked for as a JSON reply and turned back into OpenAI tool_calls. Nothing is billed here:
every call is recorded at $0."""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid

from . import config

TIMEOUT = int(os.environ.get("MASTERSMITH_CLI_TIMEOUT", "900"))


class CLIError(Exception):
    pass


def _exe(name):
    path = shutil.which(name)
    if not path:
        raise CLIError("the %s CLI is not installed or not on PATH (MASTERSMITH_LLM=%s)" % (name, config.LLM_BACKEND))
    return path


def claude_model(model):
    """A model name -> the Claude Code alias (opus / sonnet / haiku). MASTERSMITH_CLAUDE_MODEL forces one for every call."""
    if config.CLAUDE_CODE_MODEL:
        return config.CLAUDE_CODE_MODEL
    m = (model or "").lower()
    for alias in ("opus", "sonnet", "haiku"):
        if alias in m:
            return alias
    return config.CLAUDE_CODE_DEFAULT       # no alias named -> the default


def _effort(effort):
    e = str(effort or "low").lower()
    return e if e in ("low", "medium", "high", "xhigh", "max") else "low"


def transcript(messages, folder):
    """The messages as one prompt text, with every picture written into `folder`. -> (text, [picture files])"""
    lines, pictures = [], []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        body = []
        for p in parts:
            if p.get("type") == "text":
                body.append(p.get("text") or "")
            elif p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url") or ""
                mm = re.match(r"data:image/(\w+);base64,(.*)", url, re.S)
                if not mm:
                    body.append("[a picture that could not be attached]")
                    continue
                ext = {"jpeg": "jpg"}.get(mm.group(1), mm.group(1))
                name = "picture_%d.%s" % (len(pictures) + 1, ext)
                with open(os.path.join(folder, name), "wb") as f:
                    f.write(base64.b64decode(mm.group(2)))
                pictures.append(name)
                body.append("[picture %d: %s]" % (len(pictures), name))
        if role == "assistant" and m.get("tool_calls"):
            calls = [{"name": c["function"]["name"], "arguments": json.loads(c["function"].get("arguments") or "{}")}
                     for c in m["tool_calls"]]
            body.append("(called tools: %s)" % json.dumps(calls))
        head = {"system": "INSTRUCTIONS", "user": "USER", "assistant": "ASSISTANT (you, earlier)",
                "tool": "TOOL RESULT%s" % (" for %s" % m.get("name") if m.get("name") else "")}.get(role, str(role).upper())
        lines.append("### %s\n%s" % (head, "\n".join(b for b in body if b)))
    return "\n\n".join(lines), pictures


def tool_instructions(tools):
    specs = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
              "parameters": t["function"].get("parameters", {})} for t in tools or []]
    return ("\n\n### HOW TO ANSWER\nYou are the assistant in the conversation above. You may call these tools:\n%s\n"
            "Reply with exactly ONE JSON object and nothing else: either {\"tool_calls\": [{\"name\": \"...\", "
            "\"arguments\": {...}}]} to call one or more tools, or {\"content\": \"your reply to the user\"} to answer "
            "the user." % json.dumps(specs))


def _as_message(text, tools):
    """The CLI's final text -> an OpenAI-style assistant message (content, tool_calls)."""
    if not tools:
        return {"role": "assistant", "content": text}
    j = None
    for cand in re.findall(r"\{.*\}", text or "", re.S)[:1]:
        try:
            j = json.loads(cand)
        except json.JSONDecodeError:
            j = None
    if isinstance(j, dict) and isinstance(j.get("tool_calls"), list) and j["tool_calls"]:
        calls = [{"id": "call_%s" % uuid.uuid4().hex[:12], "type": "function",
                  "function": {"name": str(c.get("name")), "arguments": json.dumps(c.get("arguments") or {})}}
                 for c in j["tool_calls"] if isinstance(c, dict) and c.get("name")]
        return {"role": "assistant", "content": j.get("content") or "", "tool_calls": calls}
    if isinstance(j, dict) and "content" in j:
        return {"role": "assistant", "content": str(j.get("content") or "")}
    return {"role": "assistant", "content": text}


def chat(messages, model, tools=None, json_only=False, effort="low", log=print):
    """One model call through the configured CLI. -> (assistant message dict, record for the cost log)"""
    folder = tempfile.mkdtemp(prefix="ms_llm_")
    try:
        text, pictures = transcript(messages, folder)
        if pictures and config.LLM_BACKEND == "claude-code":
            text = ("The pictures named below are files in the current folder: read every one of them with the Read "
                    "tool before you answer.\n\n") + text
        elif pictures:
            text = "The pictures are attached in order (picture 1 is the first attachment).\n\n" + text
        if tools:
            text += tool_instructions(tools)
        elif json_only:
            text += "\n\nAnswer with one JSON object only: no prose, no code fences."
        if config.LLM_BACKEND == "claude-code":
            alias = claude_model(model)
            cmd = [_exe("claude"), "-p", "--output-format", "json", "--model", alias, "--effort", _effort(effort),
                   "--tools", "Read", "--allowedTools", "Read", "--strict-mcp-config", "--no-session-persistence"]
            label = "claude-code/%s" % alias
        else:
            out_file = os.path.join(folder, "answer.txt")
            cmd = [_exe("codex"), "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "-C", folder,
                   "-o", out_file, "-c", "model_reasoning_effort=%s" % {"xhigh": "high", "max": "high"}.get(_effort(effort), _effort(effort))]
            if config.CODEX_MODEL:
                cmd += ["-m", config.CODEX_MODEL]
            for p in pictures:
                cmd += ["-i", os.path.join(folder, p)]
            cmd.append("-")
            label = "codex/%s" % (config.CODEX_MODEL or "default")
        last = None
        for attempt in range(2):
            try:
                r = subprocess.run(cmd, input=text, cwd=folder, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                last = "%s timed out after %d s" % (label, TIMEOUT)
                continue
            if r.returncode != 0:
                last = "%s exited %d: %s" % (label, r.returncode, (r.stderr or r.stdout)[-400:])
                continue
            if config.LLM_BACKEND == "claude-code":
                try:
                    d = json.loads(r.stdout)
                except json.JSONDecodeError:
                    last = "%s gave no JSON result: %s" % (label, r.stdout[-300:])
                    continue
                if d.get("is_error"):
                    last = "%s: %s" % (label, str(d.get("result"))[:300])
                    continue
                answer = d.get("result") or ""
            else:
                answer = open(out_file, encoding="utf-8", errors="replace").read() if os.path.exists(out_file) else ""
                if not answer.strip():
                    last = "%s wrote no answer" % label
                    continue
            return _as_message(answer, tools), {"model": label, "usd": 0.0, "tokens_in": None, "tokens_out": None}
        raise CLIError(last or "%s failed" % label)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
