"""
Drift as an MCP server: any MCP client (Claude Desktop, Cursor, VS Code, ...) can call the same
fixed tools the Drift agent uses. The client picks a tool and fills arguments; it never sends
Cypher. Every tool returns the answer, its outcome, the graph path and the query trace.

Run (stdio):   python -m orggraph.mcp_server
Example client config:
  {"mcpServers": {"drift": {"command": "python", "args": ["-m", "orggraph.mcp_server"],
                            "cwd": "/path/to/drift"}}}
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from orggraph.render import template
from orggraph.tools import Tools

server = MCPServer(
    name="drift",
    title="Drift: ownership drift for Kubernetes",
    instructions=("Answers who officially owns a directory of kubernetes/kubernetes, who actually does the "
                  "work there, where ownership records have gone stale, who should review a change, and why a KEP "
                  "was decided. Every answer includes the graph path and the queries that produced it. "
                  "Activity covers merged PRs in kubernetes/kubernetes only."),
)
_tools: Tools | None = None
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def tools() -> Tools:
    global _tools
    if _tools is None:
        _tools = Tools()
    return _tools


def _out(answer) -> dict:
    d = answer.to_dict()
    return {"answer": template(answer), "outcome": d["outcome"], "data": d["data"],
            "paths": d["paths"], "note": d["note"], "trace": d["trace"]}


@server.tool(annotations=READ_ONLY)
def who_owns(path: str) -> dict:
    """Official owners of a directory or file in kubernetes/kubernetes: the chain of OWNERS files up to the root."""
    return _out(tools().who_owns(path))


@server.tool(annotations=READ_ONLY)
def who_knows(path: str, months: int = 12) -> dict:
    """Listed approvers with their recent activity (silent if none) and unlisted people doing the work (shadow owners)."""
    return _out(tools().who_knows(path, months))


@server.tool(annotations=READ_ONLY)
def drift_summary(months: int = 12, limit: int = 10) -> dict:
    """Org-wide: share of approvers with no recent activity, and the directories where ownership is most stale."""
    return _out(tools().drift_summary(months, limit))


@server.tool(annotations=READ_ONLY)
def why(kep: int) -> dict:
    """A Kubernetes enhancement proposal (KEP): authors, approvers, owning SIG, milestones, related KEPs."""
    return _out(tools().why(kep))


@server.tool(annotations=READ_ONLY)
def propose_owners_update(path: str, months: int = 12, min_prs: int = 10) -> dict:
    """Draft (never apply) an OWNERS change for a directory, with evidence for every added or removed line."""
    return _out(tools().propose_owners_update(path, months, min_prs))


@server.tool(annotations=READ_ONLY)
def suggest_reviewers(paths: str = "", pr: int | None = None, months: int = 6) -> dict:
    """Who should review a change: give the file paths it touches (or a merged PR number). Returns people with
    recent review and approval work in those areas, and whether each area's listed approvers are still active."""
    return _out(tools().suggest_reviewers(paths=paths, pr=pr, months=months))


@server.tool(annotations=READ_ONLY)
def working_group(path: str) -> dict:
    """Areas worked on by the same people as this directory (a community found with FalkorDB's label
    propagation), the SIG labels they carry, and the people doing most of the review work."""
    return _out(tools().working_group(path))


@server.tool(annotations=READ_ONLY)
def what_if(handles: str) -> dict:
    """What if these people (GitHub handles, e.g. "@deads2k") stepped back? Simulated on a copy of the
    graph (GRAPH.COPY), which is deleted afterwards: areas whose approval would wait only on silent
    approvers, where approval moves up the OWNERS chain, and who already reviews there."""
    return _out(tools().what_if(handles))


@server.tool(annotations=READ_ONLY)
def check_fix(dirs: list[str]) -> dict:
    """Apply Drift's drafted OWNERS changes for these directories to a copy of the graph and report which
    areas get unstuck and whether any would be left waiting on silent approvers. Nothing real changes."""
    return _out(tools().check_fix(dirs))


@server.tool(annotations=READ_ONLY)
def recall(path: str = "", handle: str = "", kep: int | None = None) -> dict:
    """What Drift has already established about a directory, person or KEP, in earlier sessions by any
    client (the web app, the CLI agent or another MCP client): the remembered answers, when, and who they named."""
    from orggraph.memory import Memory
    m = Memory().recall(path=path or None, handle=handle or None, kep=kep)
    return {"outcome": "answered_from_memory" if m else "not_in_graph", "findings": m,
            "note": "" if m else "Nothing remembered about that yet."}


@server.tool(annotations=READ_ONLY)
def owners_patch(dirs: list[str]) -> dict:
    """A git-apply-able patch of drafted OWNERS changes for these directories, with evidence in its header.
    Returned as text for a human to review; nothing is applied."""
    a = tools().owners_patch(dirs)
    return {"outcome": a.outcome, "patch": a.data.get("patch", ""), "skipped": a.data.get("skipped", []), "note": a.note}


if __name__ == "__main__":
    server.run("stdio")
