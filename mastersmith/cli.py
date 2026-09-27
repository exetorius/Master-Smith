"""Command line: chat with the director, see the spend, run a brief straight through, or finish a model you have.
    python -m mastersmith chat
    python -m mastersmith run --name Crate --category prop --description "weathered oak ammunition crate" --yes
    python -m mastersmith import my_model.glb --name Crate --category prop
    python -m mastersmith spend
"""
import argparse
import json
import os
import sys

from . import config, pricing
from .spec import Spec
from .providers import ProviderBalanceLow
from .wallet import Wallet


def _log(msg):
    print("  " + msg, flush=True)


def cmd_chat(a):
    from .agent import Director
    wallet = Wallet()
    d = Director(a.user, wallet, log=_log, model=a.model)
    print("Master Smith - describe the game asset you want. Balance: %d credits. Ctrl+C to quit." % wallet.balance(a.user))
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye (chat cost this session $%.4f)" % d.chat_cost_usd())
            return
        if not text:
            continue
        try:
            reply = d.turn(text)
        except Exception as exc:  # noqa: BLE001 - a chat must not die on one bad turn
            reply = "Something went wrong: %s" % str(exc)[:300]
        print("\nsmith> " + reply)


def cmd_spend(a):
    """What this machine's keys have spent, job by job (the ledger keeps score; nothing is charged)."""
    w = Wallet()
    print("spent so far: $%.2f" % (-w.balance(a.user) / 100))
    for ts, kind, credits, usd, note in w.history(a.user, limit=a.limit):
        print("  %-8s %7.2f  %s  %s" % (kind, -credits / 100, ("$%.3f" % usd) if usd is not None else "      ", note or ""))


def cmd_run(a):
    from .pipeline import build
    spec = Spec(name=a.name, description=a.description, category=a.category, style=a.style, engine=a.engine,
                tri_budget=a.tris, size_m=a.size, reference_image=a.reference or "", multiview=not a.single_view,
                premium=a.premium)
    est = pricing.estimate(spec)
    w = Wallet()
    print("brief: %s" % json.dumps(spec.to_dict()))
    print("estimate: %d credits ($%.2f worst case); balance %d" % (est["credits"], est["usd"], w.balance(a.user)))
    if not a.yes:
        if input("build? [y/N] ").strip().lower() != "y":
            return
    try:
        r = build(spec, a.user, w, log=_log)
    except ProviderBalanceLow as exc:
        sys.exit("provider balance too low: %s" % exc)
    print(json.dumps({k: r.get(k) for k in ("status", "delivery_dir", "review", "bill", "error")}, indent=1, default=str))


def cmd_import(a):
    """Finish an existing GLB/glTF/FBX/OBJ/.blend for the engine: orient, scale, glass, LODs, collision, maps, previews."""
    from .pipeline import rework
    name = a.name or "".join(ch for ch in os.path.splitext(os.path.basename(a.path))[0] if ch.isalnum()) or "Imported"
    spec = Spec(name=name, description=a.description or "imported model", category=a.category, engine=a.engine,
                tri_budget=a.tris, size_m=a.size if a.size else -1, glass=a.glass, rig=a.rig)
    print("brief: %s" % json.dumps(spec.to_dict()))
    try:
        r = rework(os.path.abspath(a.path), spec, a.user, Wallet(), log=_log)
    except ProviderBalanceLow as exc:
        sys.exit("refused: %s" % exc)
    print(json.dumps({k: r.get(k) for k in ("status", "delivery_dir", "review", "bill", "error")}, indent=1, default=str))


def cmd_rerun(a):
    from .pipeline import refinish
    overrides = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        overrides[k] = {"true": True, "false": False}.get(v.lower(), v) if k in ("glass", "rig", "multiview", "premium")             else (int(v) if k == "tri_budget" else float(v) if k == "size_m" else v)
    w = Wallet()
    r = refinish(a.job_dir, a.user, w, overrides, log=_log)
    print(json.dumps({k: r.get(k) for k in ("status", "delivery_dir", "rig", "review", "bill", "error")}, indent=1, default=str))


def cmd_mcp(_a):
    """Expose the director's tools over MCP (stdio) so Claude Code, Codex or any MCP client can be the director."""
    from .mcp_server import main
    main()


def cmd_serve(a):
    import uvicorn
    uvicorn.run("mastersmith.service:app", host=a.host, port=a.port, log_level="info")


def cmd_worker(_a):
    from .worker import Worker
    print("master smith worker: waiting for jobs (Ctrl+C to stop)")
    Worker().run()


def main(argv=None):
    p = argparse.ArgumentParser(prog="mastersmith", description="Master Smith: prompt in, game-ready model out.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat")
    c.add_argument("--user", default="local")
    c.add_argument("--model", default=None, help="director model (OpenRouter id); default %s" % config.DIRECTOR_MODEL)
    c.set_defaults(fn=cmd_chat)
    sp = sub.add_parser("spend", help="what this machine's keys have spent, job by job")
    sp.add_argument("--user", default="local")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_spend)
    r = sub.add_parser("run")
    r.add_argument("--user", default="local")
    r.add_argument("--name", required=True)
    r.add_argument("--description", required=True)
    r.add_argument("--category", default="prop")
    r.add_argument("--style", default="realistic")
    r.add_argument("--engine", default="unreal")
    r.add_argument("--tris", type=int, default=0)
    r.add_argument("--size", type=float, default=0.0)
    r.add_argument("--reference", default="")
    r.add_argument("--single-view", action="store_true")
    r.add_argument("--premium", action="store_true")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_run)
    im = sub.add_parser("import", help="finish a model file you already have (GLB, glTF, FBX, OBJ or a delivered .blend)")
    im.add_argument("path")
    im.add_argument("--user", default="local")
    im.add_argument("--name", default="")
    im.add_argument("--description", default="")
    im.add_argument("--category", default="prop")
    im.add_argument("--engine", default="unreal")
    im.add_argument("--tris", type=int, default=0)
    im.add_argument("--size", type=float, default=0.0, help="scale to this many metres; default keeps the file's size")
    im.add_argument("--glass", action="store_true")
    im.add_argument("--rig", action="store_true")
    im.set_defaults(fn=cmd_import)
    rr = sub.add_parser("rerun", help="re-finish an existing job's seed with a changed brief (no new seed bought)")
    rr.add_argument("job_dir")
    rr.add_argument("--user", default="local")
    rr.add_argument("--set", action="append", help="field=value, e.g. rig=true glass=true tri_budget=80000")
    rr.set_defaults(fn=cmd_rerun)
    sv = sub.add_parser("serve", help="run the HTTP service with an in-process worker")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)
    wk = sub.add_parser("worker", help="run a standalone build worker")
    wk.set_defaults(fn=cmd_worker)
    mc = sub.add_parser("mcp", help="MCP server (stdio): Claude Code / Codex / any MCP client drives the director's tools")
    mc.set_defaults(fn=cmd_mcp)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
