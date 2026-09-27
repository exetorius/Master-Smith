"""The HTTP surface with a fixed key from the environment, against a temporary data directory and no worker.
Nothing here reaches fal, OpenRouter or Blender."""
import importlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEY = "ms_test_made_up"


@pytest.fixture(scope="module")
def client():
    httpx = pytest.importorskip("httpx")  # noqa: F841 - the FastAPI test client needs it
    from fastapi.testclient import TestClient
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="ms-api-"))
    os.environ["MASTERSMITH_NO_WORKER"] = "1"           # read at startup: jobs queue but never run
    from mastersmith import config, providers
    service = importlib.import_module("mastersmith.service")
    from mastersmith.store import Store
    from mastersmith.wallet import Wallet
    # point the running module at a scratch data directory and a fixed key without re-importing anything
    saved = {k: getattr(config, k) for k in ("API_KEY", "DATA_DIR", "OUT_DIR", "UPLOADS_DIR", "DB_PATH")}
    config.API_KEY, config.DATA_DIR = KEY, tmp
    config.OUT_DIR, config.UPLOADS_DIR, config.DB_PATH = tmp / "out", tmp / "uploads", tmp / "test.db"
    service.store, service.wallet = Store(config.DB_PATH), Wallet(config.DB_PATH)
    providers._cache.update(at=9e12, data={"fal": {"usd": 50.0}, "openrouter": {"usd": 50.0}, "errors": {}, "checked": 0})
    with TestClient(service.app) as c:
        yield c
    for k, v in saved.items():
        setattr(config, k, v)
    providers._cache.update(at=0.0, data=None)


def test_fixed_key_is_required_and_accepted(client):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    me = client.get("/v1/me", headers={"X-API-Key": KEY}).json()
    assert me["user"] == "agent" and me["local_mode"] is False
    h = client.get("/healthz").json()
    assert h["ok"] and h["local_mode"] is False and h["worker"] is False


def test_old_havoc_status_does_not_repeat_false_acceptance(client):
    from mastersmith import service
    from mastersmith.spec import Spec
    spec = Spec(name="Havoc", description="gunship", category="aircraft",
                add_parts=[{"name": "Cabin", "phrase": "the cockpit interior", "anchor": "glass"}])
    row = {"id": "old-havoc", "status": "done", "kind": "rework", "spec": spec.to_dict(), "created": 0,
           "started": 0, "finished": 1, "error": None, "log": "", "result": {
               "review": {"score": 5, "verdict": "rebuild", "issues": ["bad cabin"]},
               "gate": {"ok": True, "warnings": []},
               "diagnosis": [{"finding": "reviewer 5/10: usable with the notes above", "fix": None}],
               "delivery": {"added_parts": [{"name": "Cabin", "faces_added": 5000}]}}}
    summary = service.job_view(row, "agent")["summary"]
    assert not summary["quality"]["accepted"]
    assert summary["gate"]["ok"] is False and summary["gate"]["technical_ok"] is True
    assert row["result"]["gate"]["ok"] is True
    assert "not accepted" in summary["diagnosis"][0]["finding"]
    assert "usable" not in str(summary["diagnosis"])
    assert summary["added_parts"][0]["name"] == "Cabin"
    assert row["result"]["diagnosis"][0]["finding"].startswith("reviewer 5/10: usable")  # history unchanged


def test_repair_plan_tool_returns_edits_without_queueing(client, monkeypatch, tmp_path):
    from mastersmith import service
    from mastersmith.spec import Spec
    from mastersmith.store import Store
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-live-key")
    monkeypatch.setattr(service, "store", Store(tmp_path / "repair-plan.db"))
    spec = Spec(name="Havoc", description="gunship", category="aircraft")
    service.store.enqueue("repair-plan-test", "agent", "build", spec.to_dict())
    service.store.finish("repair-plan-test", "done", result={
        "delivery": {"bake": {"normal_detail_std": 0.05}},
        "review": {"score": 4, "verdict": "rebuild", "issues": ["faceted baking artifacts"]}})
    count = len(service.store.jobs_for("agent"))
    response = client.post("/v1/sessions/repair-plan-test/tool", headers={"X-API-Key": KEY},
                           json={"name": "plan_repair", "args": {"job_id": "repair-plan-test"}})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["actions"][0]["changes"]["texture_fixes"] == ["preserve_seed_maps"]
    assert plan["automatic_build"] is False and plan["requires_confirmation"]
    assert len(service.store.jobs_for("agent")) == count
    service.store.db.close()


def test_config_hides_secrets(client):
    c = client.get("/v1/config", headers={"Authorization": "Bearer " + KEY}).json()
    assert c["keys_set"]["MASTERSMITH_API_KEY"] is True
    assert KEY not in str(c) and "SEED_MODEL" in c and "prop" in c["categories"]


def test_dry_run_estimates_without_queueing(client):
    H = {"Authorization": "Bearer " + KEY}
    r = client.post("/v1/jobs?dry_run=1", headers=H, json={"spec": {"name": "crate", "category": "prop", "description": "oak crate"}})
    assert r.status_code == 200
    j = r.json()
    assert j["dry_run"] and j["brief"]["name"] == "crate" and j["brief"]["tri_budget"] == 30000 and j["estimate_usd"] > 0 and j["steps"]
    assert j["providers"]["fal"]["usd"] == 50.0
    assert client.get("/v1/jobs", headers=H).json() == []


def test_queue_poll_log_and_debug(client):
    H = {"Authorization": "Bearer " + KEY}
    r = client.post("/v1/jobs", headers=H, json={"spec": {"name": "Crate", "category": "prop", "description": "oak crate"}})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert r.json()["status"] == "queued"
    job = client.get("/v1/jobs/%s" % jid, headers=H).json()
    assert job["status"] == "queued" and job["spec"]["name"] == "Crate"
    assert client.get("/v1/jobs/%s/log" % jid, headers=H).text == ""
    dbg = client.get("/v1/jobs/%s/debug" % jid, headers=H).json()
    assert dbg["id"] == jid and dbg["work_files"] == [] and dbg["blender_logs"] == {}
    assert client.get("/v1/jobs/%s/work/nothing.png" % jid, headers=H).status_code == 404
    assert client.get("/v1/jobs/unknown", headers=H).status_code == 404


def test_build_refused_when_a_provider_cannot_cover_it(client):
    from mastersmith import providers
    H = {"Authorization": "Bearer " + KEY}
    saved = dict(providers._cache["data"])
    providers._cache["data"] = {**saved, "fal": {"usd": 0.05}}
    try:
        r = client.post("/v1/jobs", headers=H, json={"spec": {"name": "Jet", "category": "aircraft", "description": "grey jet"}})
        assert r.status_code == 402 and "provider balance too low" in r.json()["detail"]["error"]
    finally:
        providers._cache["data"] = saved


def test_upload_rejects_other_types_and_keeps_models(client):
    H = {"Authorization": "Bearer " + KEY}
    r = client.post("/v1/uploads", headers=H, files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 415
    r = client.post("/v1/uploads", headers=H, files={"file": ("thing.glb", b"glTF\x02\x00\x00\x00", "model/gltf-binary")})
    assert r.status_code == 200 and r.json()["kind"] == "mesh" and os.path.isfile(r.json()["path"])
    r2 = client.post("/v1/jobs/import", headers=H, json={"path": r.json()["path"], "spec": {"category": "prop"}})
    assert r2.status_code == 200 and r2.json()["kind"] == "rework"
    bad = client.post("/v1/jobs/import", headers=H, json={"path": "C:/Windows/notepad.exe", "spec": {}})
    assert bad.status_code == 400


def test_sessions_are_absent_until_a_chat_turn(client):
    H = {"Authorization": "Bearer " + KEY}
    assert client.get("/v1/sessions/none", headers=H).status_code == 404
    assert client.delete("/v1/sessions/none", headers=H).json() == {"dropped": False}


def test_models_list_prices_and_the_env_default_first(client):
    from mastersmith import config, providers
    H = {"Authorization": "Bearer " + KEY}
    fake = {config.DIRECTOR_MODEL: {"name": "d", "context": 1, "in_per_m": 0.75, "out_per_m": 3.75, "tools": True, "vision": True},
            "deepseek/deepseek-v4.1-flash": {"name": "ds", "context": 1, "in_per_m": 0.1, "out_per_m": 0.5, "tools": True, "vision": True},
            "vendor/tool-only": {"name": "t", "context": 1, "in_per_m": 1, "out_per_m": 2, "tools": True, "vision": False},
            "vendor/vision-tools": {"name": "v", "context": 1, "in_per_m": 3, "out_per_m": 4, "tools": True, "vision": True}}
    providers._models_cache.update(at=9e12, data=fake)
    try:
        m = client.get("/v1/models", headers=H).json()
        ids = [d["id"] for d in m["director_models"]]
        assert ids[0] == config.DIRECTOR_MODEL and "(default)" in m["director_models"][0]["label"]
        assert "$0.75 in / $3.75 out per 1M" in m["director_models"][0]["label"]
        assert "deepseek/deepseek-v4.1-flash" in ids and "vendor/vision-tools" not in ids and not m["all_models"]
        m2 = client.get("/v1/models?all=1", headers=H).json()
        ids2 = [d["id"] for d in m2["director_models"]]
        assert ids2[0] == config.DIRECTOR_MODEL and "vendor/vision-tools" in ids2 and "vendor/tool-only" not in ids2 and m2["all_models"]
        assert [v["key"] for v in m["seed_vendors"]][0] == "tripo" and m["seed_vendors"][0]["usd"] == 0.6
    finally:
        providers._models_cache.update(at=0.0, data=None)


def test_jfif_and_webp_uploads_become_png(client):
    import io
    from PIL import Image
    H = {"Authorization": "Bearer " + KEY}
    for ext, fmt, mime in ((".jfif", "JPEG", "image/jpeg"), (".webp", "WEBP", "image/webp")):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, fmt)
        r = client.post("/v1/uploads", headers=H, files={"file": ("photo" + ext, buf.getvalue(), mime)})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["kind"] == "image" and j["name"] == "photo.png" and j["path"].endswith(".png") and os.path.isfile(j["path"])
        assert Image.open(j["path"]).size == (8, 8)
    bad = client.post("/v1/uploads", headers=H, files={"file": ("broken.jfif", b"not a picture", "image/jpeg")})
    assert bad.status_code == 415


def test_chats_persist_reload_and_delete(client):
    from mastersmith import service
    H = {"Authorization": "Bearer " + KEY}
    from mastersmith import config
    service.CHATS_DIR = config.DATA_DIR / "chats"           # the fixture moved DATA_DIR after import
    sess = service._session("chat-one", "agent")
    d = sess["director"]
    d.spec = service.Spec(name="Crate", description="oak crate", category="prop")
    d.last_job_id = "20260101_000000_abcdef"
    d.last_tools = [{"name": "make_reference", "args": {}, "result": '{"job_id": "20260101_000001_abc123", "status": "done"}'}]
    turn = {"at": 1.0, "user": "an oak crate", "attachments": [], "reply": "Brief set.",
            "turn": {"brief": d.spec.to_dict(), "last_job": d.last_job_id, "balance": 0, "providers": None, "pictures": [],
                     "pictures_kind": None, "reference_job": None, "settings": {}, "chat_cost_usd": 0.001, "tools": []}}
    state = service.save_chat("agent", "chat-one", sess, turn)
    assert state["title"] == "Crate" and state["jobs"] == ["20260101_000000_abcdef", "20260101_000001_abc123"]
    lst = client.get("/v1/chats", headers=H).json()
    assert [c["id"] for c in lst] == ["chat-one"] and lst[0]["turns"] == 1 and lst[0]["name"] == "Crate"
    # forget the warm session: the folder rebuilds it, behind a fresh system prompt, with the brief and the job
    service._sessions.pop("agent:chat-one", None)
    one = client.get("/v1/chats/chat-one", headers=H).json()
    assert one["turns"][0]["reply"] == "Brief set." and one["spec"]["name"] == "Crate"
    d2 = service._session("chat-one", "agent")["director"]
    assert d2.spec.name == "Crate" and d2.last_job_id == "20260101_000000_abcdef" and d2.messages[0]["role"] == "system"
    assert client.delete("/v1/chats/chat-one", headers=H).json() == {"deleted": True}
    assert client.get("/v1/chats/chat-one", headers=H).status_code == 404
    assert client.get("/v1/chats", headers=H).json() == []


def test_an_outside_model_can_drive_the_director_through_the_session_routes(client):
    from mastersmith import service, config
    H = {"Authorization": "Bearer " + KEY}
    service.CHATS_DIR = config.DATA_DIR / "chats"
    p = client.get("/v1/sessions/outside/prompt", headers=H).json()
    assert "You are Master Smith" in p["system_prompt"] and any(t["name"] == "ask_customer" for t in p["tools"]) and p["brief"] is None
    r = client.post("/v1/sessions/outside/tool", headers=H, json={"name": "set_brief", "args": {"name": "Barrel", "description": "oak barrel", "category": "prop"}})
    assert r.status_code == 200 and r.json()["brief"]["name"] == "Barrel" and r.json()["estimate_usd"] > 0
    r = client.post("/v1/sessions/outside/tool", headers=H, json={"name": "ask_customer", "args": {"question": "Size?", "options": ["1 m", "2 m"]}})
    assert r.json()["status"] == "asked"
    assert client.post("/v1/sessions/outside/tool", headers=H, json={"name": "nope", "args": {}}).status_code == 404
    t = client.post("/v1/sessions/outside/turn", headers=H, json={"user": "a barrel", "reply": "Set. How big? 1. 1 m 2. 2 m"}).json()
    assert t["recorded"] and t["chat"]["title"] == "Barrel"
    chat = client.get("/v1/chats/outside", headers=H).json()
    assert chat["turns"][0]["turn"]["question"] == {"question": "Size?", "options": ["1 m", "2 m"]}
    assert chat["turns"][0]["turn"]["settings"]["director_model"] == "external"
    assert [x["name"] for x in chat["turns"][0]["turn"]["tools"]] == ["set_brief", "ask_customer"]
    client.delete("/v1/chats/outside", headers=H)


def test_briefs_may_only_name_files_the_service_stored(client, tmp_path):
    from mastersmith import config, service
    H = {"Authorization": "Bearer " + KEY}
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    up = client.post("/v1/uploads", headers=H, files={"file": ("ok.png", outside.read_bytes(), "image/png")}).json()["path"]
    sneaky = os.path.join(config.UPLOADS_DIR, "..", "..", os.path.relpath(str(outside), str(config.DATA_DIR.parent)))
    for spec in ({"reference_image": str(outside)}, {"reference_images": [up, str(outside)]}, {"reference_image": sneaky},
                 {"reference_job": str(tmp_path)}, {"add_parts": [{"phrase": "a scope", "picture": str(outside)}]},
                 {"add_parts": [{"phrase": "a scope", "seed": str(outside)}]}):
        body = {"spec": {"name": "Crate", "description": "oak crate", **spec}}
        r = client.post("/v1/jobs", headers=H, json=body)
        assert r.status_code == 400 and r.json()["detail"]["bad_paths"], spec
        if "reference_job" not in spec:            # /v1/reference drops reference_job: it draws new pictures
            assert client.post("/v1/reference", headers=H, json=body).status_code == 400, spec
    assert service._foreign_paths({"reference_images": [up, "https://example.com/a.png"]}) == []
    job_dir = config.OUT_DIR / "Crate_x"
    job_dir.mkdir(parents=True, exist_ok=True)
    assert service._foreign_paths({"reference_job": str(job_dir)}) == []
    assert client.post("/v1/jobs", headers=H, json={"spec": {"name": "Crate", "description": "oak crate",
                                                             "reference_image": up}}).status_code == 200


def test_only_the_chat_origin_may_call_from_a_browser(client):
    H = {"Authorization": "Bearer " + KEY}
    evil = client.options("/v1/jobs", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers
    ok = client.get("/v1/me", headers={**H, "Origin": "http://localhost:3000"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_the_example_key_is_refused_at_start_up(client, monkeypatch):
    from mastersmith import config, service
    monkeypatch.setattr(config, "API_KEY", config.PLACEHOLDER_API_KEY)
    with pytest.raises(RuntimeError, match="example value"):
        service._start()


def test_every_blender_run_disables_embedded_scripts():
    import re
    from mastersmith import config
    assert "-Y" in config.BLENDER_FLAGS and "-b" in config.BLENDER_FLAGS
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mastersmith")
    for folder, _, files in os.walk(root):
        for f in files:
            if f.endswith(".py"):
                text = open(os.path.join(folder, f), encoding="utf-8").read()
                assert not re.search(r"BLENDER_BIN,\s*\"-b\"", text), f


def test_the_chat_says_which_key_is_missing_and_outside_directors_need_none(client, monkeypatch):
    from mastersmith import config, service
    H = {"Authorization": "Bearer " + KEY}
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    service.CHATS_DIR = config.DATA_DIR / "chats"
    r = client.post("/v1/chat", headers=H, json={"message": "a crate", "session_id": "nokey"})
    assert r.status_code == 503 and "OPENROUTER_API_KEY" in r.json()["detail"]
    r = client.post("/v1/sessions/nokey-mcp/tool", headers=H, json={"name": "set_brief", "args": {"name": "Crate", "description": "oak crate"}})
    assert r.status_code == 200 and r.json()["brief"]["name"] == "Crate"
    for sid in ("nokey", "nokey-mcp"):
        client.delete("/v1/chats/" + sid, headers=H)
