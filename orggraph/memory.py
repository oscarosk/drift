"""
Memory is more nodes. When Drift establishes something about a directory, person or decision,
it writes a Finding and links it to what it's about:

  (:Finding {text, tool, args, at, as_of, session})-[:ABOUT]->(:Dir | :Person | :KEP)

Next session, before answering, it looks for findings about the same thing and returns them
with the date they were learned ("answered_from_memory"). Because the facts underneath can
change, a remembered finding is shown next to a fresh re-check, never instead of it.
"""
from __future__ import annotations

import datetime as dt
import json

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
        n = 0
        for label, prop, value in targets:
            extra = " AND x.repo = 'kubernetes'" if label == "Dir" else ""
            res = self.g.query(f"""
                MATCH (x:{label}) WHERE x.{prop} = $v{extra}
                OPTIONAL MATCH (old:Finding {{text: $text}})-[:ABOUT]->(x)
                WITH x WHERE old IS NULL
                CREATE (f:Finding {{text: $text, tool: $tool, args: $args, at: $at, as_of: $as_of, session: $session}})
                CREATE (f)-[:ABOUT]->(x)
                RETURN count(f)""", {
                "v": value, "text": text, "tool": answer.tool, "args": json.dumps(answer.args),
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "as_of": d.get("as_of", ""), "session": session}, timeout=TIMEOUT_MS)
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
            RETURN f.text, f.tool, f.at, f.as_of, f.session ORDER BY f.at DESC LIMIT $limit""",
                               {"v": v, "limit": limit}, timeout=TIMEOUT_MS).result_set
        return [{"text": t, "tool": tool, "learned_at": at, "data_as_of": as_of, "session": s}
                for t, tool, at, as_of, s in rows]
