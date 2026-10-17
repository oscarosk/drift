"""
Memory is more nodes. When Drift establishes something about a directory, person or decision,
it writes a Finding and links it to what it's about:

  (:Finding {text, tool, args, at, as_of, session, people})-[:ABOUT]->(:Dir | :Person | :KEP)
  (:Finding)-[:CITES]->(:Person)          every person the answer named, so memory joins the facts

The same graph serves every client: the web app, the CLI agent and the MCP server read and write the
same Findings, so what one learns, the others can recall.

Next session, before answering, it looks for findings about the same thing and returns them
with the date they were learned ("answered_from_memory"). Because the facts underneath can
change, a remembered finding is shown next to a fresh re-check, never instead of it.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from orggraph.db import graph

TIMEOUT_MS = 30000


class Memory:
    def __init__(self, graph_name: str | None = None):
        self.g = graph(graph_name) if graph_name else graph()

    def remember(self, answer, text: str, session: str) -> int:
        """Store one finding per directory / person / KEP the answer was about (skipping exact repeats). Returns count."""
        d = answer.data or {}
        targets = []
        if d.get("dir"):
            targets.append(("Dir", "path", d["dir"]))
        if d.get("kep"):
            targets.append(("KEP", "number", d["kep"]))
        for key in ("remove", "add_reviewers"):
            targets += [("Person", "handle", h) for h in d.get(key, [])]
        named = sorted({h.lower() for h in re.findall(r"@([A-Za-z0-9][A-Za-z0-9-]*)", text)})
        n = 0
        for label, prop, value in targets:
            extra = " AND x.repo = 'kubernetes'" if label == "Dir" else ""
            res = self.g.query(f"""
                MATCH (x:{label}) WHERE x.{prop} = $v{extra}
                OPTIONAL MATCH (old:Finding {{text: $text}})-[:ABOUT]->(x)
                WITH x WHERE old IS NULL
                CREATE (f:Finding {{text: $text, tool: $tool, args: $args, at: $at, as_of: $as_of, session: $session, people: $people}})
                CREATE (f)-[:ABOUT]->(x)
                WITH f
                OPTIONAL MATCH (p:Person) WHERE p.handle IN $people
                FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | CREATE (f)-[:CITES]->(p))
                RETURN count(DISTINCT f)""", {
                "v": value, "text": text, "tool": answer.tool, "args": json.dumps(answer.args),
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "as_of": d.get("as_of", ""), "session": session, "people": named}, timeout=TIMEOUT_MS)
            n += res.result_set[0][0] if res.result_set else 0
        return n

    def recall(self, *, path: str | None = None, handle: str | None = None, kep: int | None = None, limit: int = 5):
        """Findings about one thing, newest first."""
        if path:
            match, v = "(x:Dir {repo: 'kubernetes', path: $v})", path
        elif handle:
            match, v = "(x:Person {handle: $v})", handle.lower().lstrip("@")
        elif kep is not None:
            match, v = "(x:KEP {number: $v})", int(kep)
        else:
            return []
        rows = self.g.ro_query(f"""
            MATCH (f:Finding)-[:ABOUT]->{match}
            RETURN f.text, f.tool, f.at, f.as_of, f.session, f.people ORDER BY f.at DESC LIMIT $limit""",
                               {"v": v, "limit": limit}, timeout=TIMEOUT_MS).result_set
        return [{"text": t, "tool": tool, "learned_at": at, "data_as_of": as_of, "session": s, "people": people or []}
                for t, tool, at, as_of, s, people in rows]

    @staticmethod
    def changes(memory: list, text_now: str) -> list:
        """Mark each remembered finding with what changed since: people it named that today's answer
        doesn't (dropped), and people today's answer names that it didn't (added)."""
        now = {h.lower() for h in re.findall(r"@([A-Za-z0-9][A-Za-z0-9-]*)", text_now)}
        for m in memory:
            then = set(m.get("people") or [])
            m["same_as_now"] = m["text"].strip() == text_now.strip()
            m["added"], m["dropped"] = sorted(now - then), sorted(then - now)
        return memory
