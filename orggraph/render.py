"""
Turning tool results into words, safely.

  template(answer)       a sentence built only from the rows: always correct, a bit flat
  check(text, answer)    rejects model text that names people, directories or decisions the
                         graph did not return, or that claims findings when the result was empty

The agent asks the model for a sentence, runs check(), and falls back to template() on failure,
so it degrades to a flatter answer rather than a wrong one.
"""
from __future__ import annotations

import json
import re

HANDLE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]{0,38})")
PATHLIKE = re.compile(r"\b((?:pkg|cmd|staging|test|plugin|hack|cluster|api|build|third_party|vendor|docs)/[\w./-]+)")
KEPREF = re.compile(r"\bKEP[- ]?#?(\d{2,5})\b", re.I)


def known_names(answer) -> tuple[set, set, set]:
    """Every handle, path and KEP number that appears anywhere in the tool result."""
    blob = json.dumps(answer.to_dict(), default=str)
    handles = set(re.findall(r'"handle":\s*"([^"]+)"', blob))
    handles |= set(re.findall(r'"@([A-Za-z0-9-]+)', blob))
    for key in ("approvers", "remove", "add_reviewers"):
        for level in [answer.data] + list(answer.data.get("levels", [])):
            v = level.get(key) if isinstance(level, dict) else None
            if isinstance(v, list):
                handles |= {x for x in v if isinstance(x, str)}
    paths = set(re.findall(r'"(?:dir|path)":\s*"([^"]+)"', blob)) | set(re.findall(r'"([a-z_]+/[\w./-]+)"', blob))
    keps = set(re.findall(r'"kep":\s*(\d+)', blob)) | set(re.findall(r"KEP-(\d+)", blob))
    return {h.lower() for h in handles}, paths, keps


def check(text: str, answer) -> tuple[bool, str]:
    handles, paths, keps = known_names(answer)
    invented = {h.lower() for h in HANDLE.findall(text)} - handles
    if invented:
        return False, f"named people not in the result: {sorted(invented)}"
    bad_paths = {p.rstrip(".,;:)") for p in PATHLIKE.findall(text)} - paths
    if bad_paths:
        return False, f"named directories not in the result: {sorted(bad_paths)}"
    bad_keps = set(KEPREF.findall(text)) - keps
    if bad_keps:
        return False, f"named decisions not in the result: {sorted(bad_keps)}"
    if answer.outcome in ("not_in_graph", "unknown_entity") and not re.search(r"\b(no|not|none|couldn't|can't|isn't|unknown)\b", text, re.I):
        return False, "claimed a finding when the graph returned nothing"
    return True, "ok"


def _people(items, key="handle", n=4):
    return ", ".join(f"@{x[key]}" for x in items[:n])


def template(answer) -> str:
    d, o, t = answer.data, answer.outcome, answer.tool
    if o == "unknown_entity":
        s = d.get("suggestions") or []
        return (answer.note or "That isn't in the graph.") + (f" Did you mean: {', '.join(map(str, s[:3]))}?" if s else "")
    if o == "not_in_graph":
        return answer.note or "The graph has nothing connecting that."
    if t == "who_owns":
        lv = d["levels"]
        own = next((x for x in lv if x["approvers"]), None)
        first = f"{d['dir']} is approved by {', '.join('@' + a for a in own['approvers'][:5])}" + \
                (f" (listed in {own['dir']}/OWNERS)" if own and own["dir"] != d["dir"] else "") if own else f"{d['dir']} lists no approvers"
        sig = next((x["sigs"] for x in lv if x["sigs"]), [])
        return first + (f", owned by {', '.join(sig)}." if sig else ".") + f" The chain has {len(lv)} level(s)."
    if t == "who_knows":
        silent = [a for a in d["approvers"] if a["status"] == "silent"]
        msg = f"{d['dir']}: {len(silent)} of {len(d['approvers'])} listed approvers have no reviews, approvals or merged PRs in kubernetes/kubernetes since {d['since']}"
        msg += f" ({_people(silent)})." if silent else "."
        if d["shadow_owners"]:
            top = d["shadow_owners"][0]
            msg += f" Not listed but doing the work: {_people(d['shadow_owners'], n=3)}; @{top['handle']} worked on {top['prs_here']} merged PRs here."
        return msg
    if t == "drift_summary":
        w = d["worst_dirs"][:3]
        return (f"{d['silent']} of {d['approvers']} approvers ({d['silent'] / max(1, d['approvers']):.0%}) have no reviews, approvals or merged PRs "
                f"in kubernetes/kubernetes since {d['since']}. Most affected: "
                + "; ".join(f"{x['dir']} ({x['silent']}/{x['approvers']} silent, {x['prs']} PRs)" for x in w) + ".")
    if t == "route":
        p = d["people"]
        return (f"For {d['dir']}, send it to {_people(p, n=3)}: they did the most review and approval work there since {d['since']} "
                f"(@{p[0]['handle']}: {p[0]['reviewed']} PRs reviewed, last on {p[0]['last_active']}).")
    if t == "why":
        silent = [x for x in d["people"] if x["status"] == "silent"]
        appr = [x for x in d["people"] if "approver" in x["roles"]]
        ms = d["milestones"][-1] if d["milestones"] else None
        msg = (f"KEP-{d['kep']} \"{d['title']}\" is owned by {d['owning_sig']}, status {d['status']}"
               + (f", reached {ms['stage']} in v{ms['version']}" if ms else "") + f"; approved by {_people(appr)}.")
        if silent:
            msg += f" Of its people, {_people(silent)} show no activity in kubernetes/kubernetes in the last 12 months."
        return msg
    if t == "propose_owners_update":
        return (f"Draft for {d['dir']}: move {len(d['remove'])} silent approver(s) to emeritus"
                + (f" ({', '.join('@' + h for h in d['remove'])})" if d["remove"] else "")
                + f" and add {len(d['add_reviewers'])} reviewer(s)"
                + (f" ({', '.join('@' + h for h in d['add_reviewers'])})" if d["add_reviewers"] else "")
                + ". Every line has its evidence; nothing is applied without a human.")
    if t == "suggest_reviewers":
        p = d["people"]
        silent = sorted({h for a in d["areas"] for h in a["silent_approvers"]})
        msg = (f"For a change touching {', '.join(a['dir'] for a in d['areas'][:3])}"
               + (f" and {len(d['areas']) - 3} more" if len(d["areas"]) > 3 else "")
               + f", ask {_people(p, n=3)}: they reviewed the most there since {d['since']}"
               + f" (@{p[0]['handle']}: {p[0]['reviewed']} PRs reviewed, {p[0]['approvals']} approvals).")
        if silent:
            msg += f" Listed approvers with no activity in the last year: {', '.join('@' + h for h in silent[:4])}."
        return msg
    if t == "areas":
        a = d["areas"]
        return f"{sum(x['state'] == 'drifting' for x in a)} of {len(a)} owned directories have silent approvers."
    if t == "cleanup_queue":
        q = d["queue"]
        return (f"{len(q)} drafted OWNERS fixes: {sum(len(x['remove']) for x in q)} silent approver listings to move to emeritus "
                f"and {sum(len(x['add_reviewers']) for x in q)} reviewers to add.")
    return answer.note or "Done."
