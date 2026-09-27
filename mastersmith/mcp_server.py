"""Master Smith as an MCP server, so another model can be the director.

Claude Code, Codex or any MCP client gets the director's tools (set_brief, make_reference, build, import_model,
job_status, read_skill, ask_customer, balance) and its system prompt, and drives a session of the running API. The
pictures, meshes, Blender finish and vision checks still run in the API exactly as they do for the web chat; only
the director's own thinking moves to the client, which is where OpenRouter spend goes during testing.

    python -m mastersmith mcp                       # stdio; MASTERSMITH_API_URL / MASTERSMITH_API_KEY / MASTERSMITH_SESSION

Every chat driven this way is recorded by `record_turn`, so it appears in the web's sidebar like any other."""
import json
import os

import requests

from mcp.server.mcpserver import MCPServer

API = (os.environ.get("MASTERSMITH_API_URL") or "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("MASTERSMITH_API_KEY", "").strip()
SESSION = os.environ.get("MASTERSMITH_SESSION", "").strip() or "director"

server = MCPServer("master-smith")


def _headers():
    h = {"Content-Type": "application/json"}
    if KEY:
        h["Authorization"] = "Bearer " + KEY
    return h


def _post(path, body):
    r = requests.post(API + path, headers=_headers(), data=json.dumps(body), timeout=900)
    if r.status_code >= 400:
        return {"error": "%s answered %d: %s" % (path, r.status_code, r.text[:400])}
    return r.json()


def _get(path):
    r = requests.get(API + path, headers=_headers(), timeout=60)
    if r.status_code >= 400:
        return {"error": "%s answered %d: %s" % (path, r.status_code, r.text[:400])}
    return r.json()


def _tool(name, args):
    out = _post("/v1/sessions/%s/tool" % SESSION, {"name": name, "args": args})
    return json.dumps(out, default=str)


@server.tool()
def director_prompt() -> str:
    """Read this FIRST and follow it: the director's system prompt (how a job goes, the remedies, what to ask), the
    current brief and the last job of this session."""
    return json.dumps(_get("/v1/sessions/%s/prompt" % SESSION), default=str)


@server.tool()
def set_brief(name: str, description: str, category: str, style: str = "realistic", engine: str = "unreal",
              tri_budget: int = 0, size_m: float = 0.0, reference_images: list[str] | None = None, search_query: str = "",
              research: bool | None = None, single_picture: bool | None = None, premium: bool = False, glass: bool | None = None,
              rig: bool | None = None, notes: str = "", edit_instructions: str = "", retexture: bool = False,
              retexture_parts: list[dict] | None = None, protect_parts: list[dict] | None = None,
              texture_fixes: list[str] | None = None, remove_parts: list[str] | None = None,
              add_parts: list[dict] | None = None) -> str:
    """Set or update the build brief. Returns the brief as understood and the worst-case estimate."""
    args = {k: v for k, v in locals().items() if v not in (None, "", 0, 0.0, False, [])}
    if add_parts is not None:
        args["add_parts"] = add_parts  # [] deliberately removes earlier additions on the next re-finish
    return _tool("set_brief", args)


@server.tool()
def make_reference() -> str:
    """Draw the reference picture(s) for the current brief (cents) and return their URLs for the customer to approve."""
    return _tool("make_reference", {})


@server.tool()
def build(confirm_removal: bool = False) -> str:
    """Build the current brief: seeds from the approved reference when there is one; queues the job and returns its id.
    With new remove_parts it answers a red-on-render preview first; call again with confirm_removal=true to proceed."""
    return _tool("build", {"confirm_removal": confirm_removal})


@server.tool()
def import_model(path: str, name: str, category: str, description: str = "", engine: str = "unreal", size_m: float = 0.0,
                 tri_budget: int = 0, glass: bool | None = None, rig: bool | None = None, notes: str = "") -> str:
    """Finish a model file the customer has (a path from /v1/uploads): oriented, scaled, glass, LODs, collision, maps."""
    args = {k: v for k, v in locals().items() if v not in (None, "", 0, 0.0)}
    return _tool("import_model", args)


@server.tool()
def job_status(job_id: str = "") -> str:
    """Status, log tail and results of a queued or finished job (the last one when job_id is empty)."""
    return _tool("job_status", {"job_id": job_id} if job_id else {})


@server.tool()
def plan_repair(job_id: str = "") -> str:
    """Read a completed job and propose targeted repair edits. No building or generation fees."""
    return _tool("plan_repair", {"job_id": job_id} if job_id else {})


@server.tool()
def read_skill(category: str) -> str:
    """The guidance for an asset category (weapon, vehicle, aircraft, helicopter, character, prop, environment)."""
    return _tool("read_skill", {"category": category})


@server.tool()
def ask_customer(question: str, options: list[str]) -> str:
    """Register a question with 2-5 options for the customer; the web shows them as numbered buttons."""
    return _tool("ask_customer", {"question": question, "options": options})


@server.tool()
def balance() -> str:
    """What the fal.ai and OpenRouter accounts have left and what this instance has spent."""
    return _tool("balance", {})


@server.tool()
def record_turn(user: str, reply: str) -> str:
    """Save one exchange (the customer's words and your reply) so this chat is kept and shows in the web's sidebar.
    Call it at the end of every turn."""
    return json.dumps(_post("/v1/sessions/%s/turn" % SESSION, {"user": user, "reply": reply}), default=str)


@server.tool()
def upload_picture(path: str) -> str:
    """Upload a local picture or model file to the API and return the path to put in reference_images / import_model."""
    with open(path, "rb") as f:
        r = requests.post(API + "/v1/uploads", headers={k: v for k, v in _headers().items() if k != "Content-Type"},
                          files={"file": (os.path.basename(path), f)}, timeout=300)
    return r.text


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
