"""
The Drift agent: question in, answer + evidence out.

  1. Pick a tool. With OPENAI_API_KEY set, the model picks from the fixed catalogue via function
     calling (it never sees or writes Cypher). Without a key, a small keyword router does it,
     so the product still works offline and in tests.
  2. Run the tool. Names are resolved against the graph first (see tools.py).
  3. Check memory for earlier findings about the same thing.
  4. Write the answer. The model drafts one or two sentences from the tool result only;
     render.check() rejects any sentence naming people / directories / decisions the graph
     didn't return, and render.template() is used instead.
  5. Remember: answered results are written back as Finding nodes.
"""
from __future__ import annotations

import json
import os
import re
import uuid

import requests

from orggraph.memory import Memory
from orggraph.render import check, template
from orggraph.tools import CATALOGUE, Tools

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
SYSTEM = """You are Drift, an assistant that answers questions about who owns, who actually works on,
and why things were decided in the Kubernetes project, using ONLY the tools provided.
Pick exactly one tool. Paths are directories in the kubernetes/kubernetes repo, e.g. pkg/kubelet."""
WRITE = """Write 1-2 plain sentences answering the question using ONLY facts in the tool result JSON.
Refer to people as @handle and only to people, directories and KEPs that appear in the result.
If the outcome is not_in_graph or unknown_entity, say plainly that the graph doesn't have it.
Never judge people; describe what the records and activity show."""


def _tool_schemas():
    out = []
    for name, spec in CATALOGUE.items():
        props = {}
        for arg, desc in spec["args"].items():
            typ = "integer" if desc.startswith("int") or arg in ("kep",) else "string"
            props[arg] = {"type": typ, "description": desc}
        required = [a for a in spec["args"] if a in ("path", "kep")]
        out.append({"type": "function", "function": {"name": name, "description": spec["desc"],
                                                      "parameters": {"type": "object", "properties": props, "required": required}}})
    return out


PATH_RE = re.compile(r"((?:pkg|cmd|staging|test|plugin|hack|cluster|api|build)/[\w./-]+)")
KEP_RE = re.compile(r"\bkep[- ]?#?(\d{2,5})\b|\b(\d{3,5})\b", re.I)


def route_offline(question: str):
    """Keyword router used when no model is configured."""
    q = question.lower()
    path = (PATH_RE.search(question) or [None])[0]
    if path:
        path = path.rstrip(".,?;:)")
    m = KEP_RE.search(question)
    kep = (m.group(1) or m.group(2)) if m else None
    if ("kep" in q or "decision" in q or "decid" in q) and kep:
        return "why", {"kep": int(kep)}
    if path and re.search(r"\b(draft|propose|fix|update)\b", q):
        return "propose_owners_update", {"path": path}
    if path and re.search(r"\b(review|send|route|assign|who should)\b", q):
        paths = [m.rstrip(".,?;:)") for m in PATH_RE.findall(question)]
        if len(paths) > 1 or "." in paths[0].split("/")[-1]:  # several paths or a file: a concrete change
            return "suggest_reviewers", {"paths": " ".join(paths)}
        return "route", {"path": path}
    handles = re.findall(r"@([A-Za-z0-9][A-Za-z0-9-]*)", question)
    if handles and re.search(r"\b(what if|steps? back|stepped back|leaves?|left|quits?|departs?|gone|retire)", q):
        return "what_if", {"handles": " ".join("@" + h for h in handles)}
    if path and re.search(r"\b(works? with|working group|same people|team|related|together)\b", q):
        return "working_group", {"path": path}
    if path and re.search(r"\b(knows|really|actually|silent|stale|shadow|active|drift)\b", q):
        return "who_knows", {"path": path}
    if path:
        return "who_owns", {"path": path}
    if re.search(r"\b(drift|stale|silent|overall|worst|summary|org)\b", q):
        return "drift_summary", {}
    return None, {}


class Agent:
    def __init__(self, graph_name: str | None = None, session: str | None = None, use_llm: bool | None = None):
        self.tools = Tools(graph_name)
        self.memory = Memory(graph_name)
        self.session = session or uuid.uuid4().hex[:8]
        self.key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.use_llm = bool(self.key) if use_llm is None else use_llm
        self.last_error = ""

    # ---- model calls (plain HTTP, no SDK) ----------------------------------------------
    def _chat(self, messages, tools=None):
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body |= {"tools": tools, "tool_choice": "required"}
        r = requests.post(OPENAI_URL, json=body, timeout=60, headers={"Authorization": f"Bearer {self.key}"})
        if r.status_code != 200:
            try:
                detail = r.json().get("error", {}).get("message", "")
            except ValueError:
                detail = r.text
            detail = re.sub(r"sk-[A-Za-z0-9*_-]+", "[key]", detail)       # never echo any part of the key
            err = f"OpenAI {r.status_code}: {detail[:200]}"
            print("model call failed:", err, flush=True)   # shows in the server log (e.g. Render)
            raise RuntimeError(err)
        return r.json()["choices"][0]["message"]

    def pick_tool(self, question: str):
        if not self.use_llm:
            return (*route_offline(question), "keyword router")
        try:
            msg = self._chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}], _tool_schemas())
            call = msg["tool_calls"][0]["function"]
            return call["name"], json.loads(call["arguments"] or "{}"), f"model {self.model}"
        except Exception as e:  # network / quota / bad output: fall back quietly, keep the reason for the log
            self.last_error = str(e)[:200]
            name, args = route_offline(question)
            return name, args, "keyword router"

    def write(self, question: str, answer) -> tuple[str, str]:
        fallback = template(answer)
        if not self.use_llm:
            return fallback, "template"
        try:
            payload = json.dumps(answer.to_dict(), default=str)[:12000]
            text = self._chat([{"role": "system", "content": WRITE},
                               {"role": "user", "content": f"Question: {question}\nTool result: {payload}"}])["content"].strip()
        except Exception as e:
            self.last_error = str(e)[:200]
            return fallback, "template"
        ok, why = check(text, answer)
        return (text, "model, checked") if ok else (fallback, f"template (model sentence rejected: {why})")

    # ---- one turn ---------------------------------------------------------------------
    def ask(self, question: str, remember: bool = True) -> dict:
        self.last_error = ""
        name, args, picker = self.pick_tool(question)
        if not name or name not in CATALOGUE:
            return {"question": question, "outcome": "unknown_entity", "text":
                    "I can answer who owns or really works on a directory (e.g. pkg/kubelet), where ownership is stale, "
                    "who to route work to, why a KEP was decided, or draft an OWNERS fix.", "picked_by": picker}
        fn = getattr(self.tools, CATALOGUE[name]["fn"])
        args = {k: v for k, v in args.items() if k in CATALOGUE[name]["args"]}
        answer = fn(**args)
        memory = []
        d = answer.data or {}
        if d.get("dir"):
            memory = self.memory.recall(path=d["dir"])
        elif d.get("kep"):
            memory = self.memory.recall(kep=d["kep"])
        text, written_by = self.write(question, answer)
        memory = Memory.changes(memory, text)  # an earlier answer is only news if it differs from today's
        if remember and answer.outcome == "answered_with_path":
            self.memory.remember(answer, text, self.session)
        return {"question": question, "tool": name, "args": answer.args, "outcome": answer.outcome,
                "text": text, "written_by": written_by, "picked_by": picker, "data": answer.data,
                "paths": answer.paths, "trace": answer.trace, "note": answer.note,
                "memory": memory, "session": self.session, "model_error": self.last_error}


if __name__ == "__main__":
    import sys
    a = Agent()
    for q in sys.argv[1:] or ["Who really knows pkg/kubelet/cm/cpumanager?"]:
        r = a.ask(q)
        print(f"\nQ: {q}\n-> {r.get('tool')} {r.get('args')} [{r['outcome']}; picked by {r['picked_by']}; written by {r.get('written_by')}]")
        print("A:", r["text"])
        for m in r.get("memory", [])[:2]:
            print(f"   remembered ({m['learned_at'][:10]}): {m['text'][:140]}")
