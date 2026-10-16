"""
Web API + UI for Drift.

  GET  /                     the app (web/index.html)
  POST /api/ask              {question, session?} -> agent answer with path, trace, memory
  POST /api/reviewers        {paths?: "a/b.go c/d", pr?: 123} -> who should review this change
  GET  /api/areas            every OWNERS directory with its health
  GET  /api/dir?path=...     who_owns + who_knows + drafted fix for one directory
  GET  /api/cleanup          drafted fixes for the most drifted directories
  POST /api/patch            {dirs: [...]} -> a git-apply-able patch (download)
  GET  /api/summary          org-wide numbers + backtest results
  GET  /api/health

Run:  uvicorn orggraph.api:app --port 8000      (then open http://localhost:8000)
"""
from __future__ import annotations

import json
import os
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from orggraph.agent import Agent
from orggraph.render import template
from orggraph.tools import Tools

ROOT = os.path.join(os.path.dirname(__file__), "..")
app = FastAPI(title="Drift")
_agents: dict[str, Agent] = {}
_tools: Tools | None = None
_cache: dict = {}
_lock = threading.Lock()


def tools() -> Tools:
    global _tools
    if _tools is None:
        _tools = Tools()
    return _tools


def cached(key, fn):
    """Results only change when the graph is rebuilt, so cache per data date."""
    k = (key, tools().as_of)
    with _lock:
        if k not in _cache:
            _cache[k] = fn()
        return _cache[k]


def _pack(a):
    d = a.to_dict()
    d["text"] = template(a)
    return d


class Ask(BaseModel):
    question: str
    session: str | None = None


class Reviewers(BaseModel):
    paths: str | None = None
    pr: int | None = None


class Patch(BaseModel):
    dirs: list[str]


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "web", "index.html"))


@app.get("/api/health")
def health():
    try:
        return {"ok": True, "as_of": tools().as_of}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/api/ask")
def ask(body: Ask):
    q = body.question.strip()[:500]
    if not q:
        return JSONResponse({"error": "Type a question first."}, status_code=400)
    key = (body.session or "web")[:40]
    if key not in _agents and len(_agents) > 500:
        _agents.clear()
    agent = _agents.setdefault(key, Agent(session=key))
    return agent.ask(q)


@app.post("/api/reviewers")
def reviewers(body: Reviewers):
    if not (body.paths or body.pr):
        return JSONResponse({"error": "Paste file paths or a PR number."}, status_code=400)
    return _pack(tools().suggest_reviewers(paths=(body.paths or "")[:5000], pr=body.pr))


@app.get("/api/areas")
def areas():
    return cached("areas", lambda: _pack(tools().areas()))


@app.get("/api/dir")
def directory(path: str):
    t = tools()
    owns = t.who_owns(path)
    if owns.outcome != "answered_with_path":
        return {"owns": _pack(owns)}
    d = owns.data["dir"]
    return {"owns": _pack(owns), "knows": _pack(t.who_knows(d)), "draft": _pack(t.propose_owners_update(d))}


@app.get("/api/cleanup")
def cleanup():
    return cached("cleanup", lambda: _pack(tools().cleanup_queue(limit=40)))


@app.post("/api/patch")
def patch(body: Patch):
    out = tools().owners_patch([d for d in body.dirs if isinstance(d, str)][:60])
    if not out.data.get("patch"):
        return JSONResponse({"error": out.note, "skipped": out.data.get("skipped", [])}, status_code=422)
    return PlainTextResponse(out.data["patch"], media_type="text/x-diff",
                             headers={"Content-Disposition": 'attachment; filename="drift-owners.patch"'})


@app.get("/api/summary")
def summary():
    s = cached("summary", lambda: _pack(tools().drift_summary(limit=12)))
    bt_path = os.path.join(ROOT, "results", "backtest.json")
    backtest = json.load(open(bt_path, encoding="utf-8")) if os.path.exists(bt_path) else None
    return {"summary": s, "backtest": backtest}
