"""
Web API + UI for Drift.

  GET  /                     the single-page UI (web/index.html)
  POST /api/ask              {question, session?} -> agent answer with path, trace, memory
  GET  /api/dir?path=...     who_owns + who_knows + propose_owners_update for one directory
  GET  /api/summary          org-wide drift + backtest results
  GET  /api/health           graph reachable?

Run:  uvicorn orggraph.api:app --reload      (then open http://localhost:8000)
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from orggraph.agent import Agent
from orggraph.render import template
from orggraph.tools import Tools

ROOT = os.path.join(os.path.dirname(__file__), "..")
app = FastAPI(title="Drift")
_agents: dict[str, Agent] = {}
_tools: Tools | None = None


def tools() -> Tools:
    global _tools
    if _tools is None:
        _tools = Tools()
    return _tools


def _pack(a):
    d = a.to_dict()
    d["text"] = template(a)
    return d


class Ask(BaseModel):
    question: str
    session: str | None = None


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
    key = body.session or "web"
    agent = _agents.setdefault(key, Agent(session=key))
    return agent.ask(q)


@app.get("/api/dir")
def directory(path: str):
    t = tools()
    owns = t.who_owns(path)
    if owns.outcome != "answered_with_path":
        return {"owns": _pack(owns)}
    d = owns.data["dir"]
    return {"owns": _pack(owns), "knows": _pack(t.who_knows(d)), "draft": _pack(t.propose_owners_update(d))}


@app.get("/api/summary")
def summary():
    s = tools().drift_summary(limit=12)
    bt_path = os.path.join(ROOT, "results", "backtest.json")
    backtest = json.load(open(bt_path, encoding="utf-8")) if os.path.exists(bt_path) else None
    return {"summary": _pack(s), "backtest": backtest}
