"""
Web API + UI for Drift.

  GET  /                     the app (web/index.html)
  POST /api/ask              {question, session?} -> agent answer with path, trace, memory
  POST /api/reviewers        {paths?: "a/b.go c/d", pr?: 123} -> who should review this change
  GET  /api/areas            every OWNERS directory with its health
  GET  /api/dir?path=...     who_owns + who_knows + drafted fix for one directory
  GET  /api/cleanup          drafted fixes for the most drifted directories
  POST /api/patch            {dirs: [...]} -> a git-apply-able patch (download)
  POST /api/checkfix         {dirs: [...]} -> apply those drafts to a graph copy: what gets unstuck, what breaks
  POST /api/whatif           {handles: "@a @b"} -> if they stepped back, where approval would get stuck
  GET  /api/summary          org-wide numbers + backtest results
  GET  /api/health

Run:  uvicorn orggraph.api:app --port 8000      (then open http://localhost:8000)
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from orggraph.agent import Agent
from orggraph.render import template
from orggraph.tools import Tools

ROOT = os.path.join(os.path.dirname(__file__), "..")
@contextlib.asynccontextmanager
async def lifespan(_app):
    warm()
    yield


app = FastAPI(title="Drift", lifespan=lifespan)
_agents: dict[str, Agent] = {}
_tools: Tools | None = None
_cache: dict = {}
_lock = threading.Lock()

# ---- limits for a public deployment ------------------------------------------------------
PER_MINUTE = int(os.getenv("DRIFT_PER_MINUTE", "20"))     # requests per visitor per minute on the busy endpoints
LLM_PER_DAY = int(os.getenv("DRIFT_LLM_PER_DAY", "300"))  # model calls per day; after that the offline router answers
_hits: dict[str, collections.deque] = {}
_llm_day = {"day": "", "n": 0}


def limited(request: Request) -> JSONResponse | None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    with _lock:
        if len(_hits) > 5000:
            _hits.clear()
        q = _hits.setdefault(ip, collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PER_MINUTE:
            return JSONResponse({"error": "Too many requests: wait a minute and try again."}, status_code=429)
        q.append(now)
    return None


def llm_allowed() -> bool:
    today = time.strftime("%Y-%m-%d")
    with _lock:
        if _llm_day["day"] != today:
            _llm_day.update(day=today, n=0)
        _llm_day["n"] += 1
        return _llm_day["n"] <= LLM_PER_DAY


_model = {"status": "not configured"}


def check_model():
    """One tiny call so /api/health (and the server log) say whether the model is usable."""
    a = Agent(session="health")
    if not a.key:
        return
    try:
        a._chat([{"role": "user", "content": "Reply with: ok"}])
        _model["status"] = f"ok ({a.model})"
    except Exception as e:
        _model["status"] = f"unavailable: {e}"[:220]
    print("model check:", _model["status"], flush=True)


def warm():
    """Fill the caches in the background so the first visitor doesn't wait for them."""
    def run():
        try:
            areas(); cleanup(); summary()
            _check_fix([x["dir"] for x in cleanup()["data"]["queue"]])
        except Exception as e:  # the app still works; the page will compute on demand
            print("warm-up failed:", e, flush=True)
        try:
            check_model()
        except Exception as e:
            print("model check failed:", e, flush=True)
    threading.Thread(target=run, daemon=True).start()


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


class WhatIf(BaseModel):
    handles: str


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "web", "index.html"))


@app.get("/api/health")
def health():
    try:
        return {"ok": True, "as_of": tools().as_of, "model": _model["status"]}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    if (r := limited(request)):
        return r
    q = body.question.strip()[:500]
    if not q:
        return JSONResponse({"error": "Type a question first."}, status_code=400)
    key = (body.session or "web")[:40]
    if key not in _agents and len(_agents) > 500:
        _agents.clear()
    agent = _agents.setdefault(key, Agent(session=key))
    if agent.key:
        agent.use_llm = llm_allowed()
    return agent.ask(q)


@app.post("/api/reviewers")
def reviewers(body: Reviewers, request: Request):
    if (r := limited(request)):
        return r
    if not (body.paths or body.pr):
        return JSONResponse({"error": "Paste file paths or a PR number."}, status_code=400)
    return _pack(tools().suggest_reviewers(paths=(body.paths or "")[:5000], pr=body.pr))


@app.get("/api/areas")
def areas():
    return cached("areas", lambda: _pack(tools().areas()))


@app.get("/api/dir")
def directory(path: str, request: Request):
    if (r := limited(request)):
        return r
    t = tools()
    owns = t.who_owns(path)
    if owns.outcome != "answered_with_path":
        return {"owns": _pack(owns)}
    d = owns.data["dir"]
    return {"owns": _pack(owns), "knows": _pack(t.who_knows(d)), "draft": _pack(t.propose_owners_update(d)),
            "group": _pack(t.working_group(d))}


@app.get("/api/cleanup")
def cleanup():
    return cached("cleanup", lambda: _pack(tools().cleanup_queue(limit=40)))


@app.post("/api/patch")
def patch(body: Patch, request: Request):
    if (r := limited(request)):
        return r
    out = tools().owners_patch([d for d in body.dirs if isinstance(d, str)][:60])
    if not out.data.get("patch"):
        return JSONResponse({"error": out.note, "skipped": out.data.get("skipped", [])}, status_code=422)
    return PlainTextResponse(out.data["patch"], media_type="text/x-diff",
                             headers={"Content-Disposition": 'attachment; filename="drift-owners.patch"'})


@app.post("/api/whatif")
def what_if(body: WhatIf, request: Request):
    """What if these people stepped back? Simulated on a GRAPH.COPY that is deleted afterwards."""
    if (r := limited(request)):
        return r
    return _pack(tools().what_if(body.handles[:300]))


@app.post("/api/checkfix")
def check_fix(body: Patch, request: Request):
    """Apply the drafted changes for these directories to a GRAPH.COPY and report what changes."""
    if (r := limited(request)):
        return r
    return _check_fix(body.dirs)


def _check_fix(dirs):
    dirs = sorted({d for d in dirs if isinstance(d, str)})[:60]
    return cached(("checkfix", tuple(dirs)), lambda: _pack(tools().check_fix(dirs)))


@app.get("/api/summary")
def summary():
    s = cached("summary", lambda: _pack(tools().drift_summary(limit=12)))
    def load(name):
        path = os.path.join(ROOT, "results", name)
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None
    groups = load("groups.json")
    if groups:
        groups = {k: groups[k] for k in ("as_of", "dirs_grouped", "cross_sig_groups", "check")} | {"count": len(groups["groups"])}
    return {"summary": s, "backtest": load("backtest.json"), "reviewer_eval": load("reviewer_eval.json"), "groups": groups}
