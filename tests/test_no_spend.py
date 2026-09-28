"""No-spend mode and the Claude Code / Codex model backends: nothing paid is called, CLI replies become messages."""
import base64
import json
import os
import tempfile

import pytest

from mastersmith import config, llm_cli, pricing
from mastersmith.fal import Fal, FalError


def test_no_spend_refuses_every_paid_fal_call_and_keeps_uploads_local(monkeypatch):
    monkeypatch.setattr(config, "NO_SPEND", True)
    fal = Fal(key="unused", log=lambda *_: None)
    with pytest.raises(FalError, match="refused"):
        fal.run("fal-ai/esrgan", {"image_url": "x"})
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.png")
        open(p, "wb").write(b"x")
        url = fal.upload(p)
        assert url.startswith("file:///") and fal.uploads[url] == os.path.abspath(p)


def test_no_spend_picks_the_models_on_this_pc(monkeypatch):
    monkeypatch.setattr(config, "NO_SPEND", True)
    class S:
        seed_vendor, picture_model, premium, category = "hitem3d3mv", "fal-ai/nano-banana-pro", True, "weapon"
    assert pricing.seed_vendor(S())["key"] == "local"
    assert pricing.edit_model(S()) == config.LOCAL_PICTURE_MODEL == pricing.concept_model(S())


def test_cli_transcript_writes_pictures_and_tool_replies_become_tool_calls():
    png = base64.b64encode(b"\x89PNG fake").decode()
    msgs = [{"role": "system", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}]}]
    with tempfile.TemporaryDirectory() as d:
        text, pics = llm_cli.transcript(msgs, d)
        assert pics == ["picture_1.png"] and os.path.exists(os.path.join(d, "picture_1.png"))
        assert "[picture 1: picture_1.png]" in text and "### INSTRUCTIONS" in text
    tools = [{"type": "function", "function": {"name": "set_brief", "parameters": {}}}]
    m = llm_cli._as_message('Sure. {"tool_calls": [{"name": "set_brief", "arguments": {"name": "Crate"}}]}', tools)
    assert m["tool_calls"][0]["function"]["name"] == "set_brief"
    assert json.loads(m["tool_calls"][0]["function"]["arguments"]) == {"name": "Crate"}
    assert llm_cli._as_message('{"content": "hello"}', tools) == {"role": "assistant", "content": "hello"}
    assert llm_cli.claude_model("anthropic/claude-opus-5.5") == "opus"


def test_paid_pictures_let_only_fal_picture_models_through_no_spend(monkeypatch):
    monkeypatch.setattr(config, "NO_SPEND", True)
    monkeypatch.setattr(config, "PAID_PICTURES", True)
    from mastersmith.fal import _is_picture_model
    assert _is_picture_model("fal-ai/nano-banana-2/edit") and not _is_picture_model("fal-ai/esrgan")
    fal = Fal(key="unused", log=lambda *_: None)
    with pytest.raises(FalError, match="refused"):
        fal.run("tripo3d/h3.1/image-to-3d", {"image_url": "x"})
    class S:
        seed_vendor, picture_model, premium, category = "tripo", "google/gemini-3-pro-image", False, "weapon"
    assert pricing.seed_vendor(S())["key"] == "local"                 # meshes stay free
    assert pricing.edit_model(S()) == config.EDIT_MODEL                # an OpenRouter picture model is never used
