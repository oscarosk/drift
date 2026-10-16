"""
Turn Drift's drafted OWNERS changes into a real patch file a maintainer can apply.

Edits are made to the original OWNERS text line by line, so comments, ordering and formatting
survive. After editing, the file is parsed again and checked: the approver must be gone from
`approvers`, present in `emeritus_approvers`, and each new reviewer present in `reviewers`.
If any check fails, that directory is skipped with a reason instead of producing a bad patch.

Only plain OWNERS files are edited. Files that use `filters:` and approvers listed through a
team alias (OWNERS_ALIASES) are reported for manual follow-up.

    git apply drift-owners.patch
"""
from __future__ import annotations

import difflib
import re

import yaml

TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(#.*)?$")
ITEM = re.compile(r"^(\s*)-\s+([^\s#]+)\s*(#.*)?$")


def _blocks(lines):
    """Top-level key -> (start index of 'key:' line, index after its last item, item indent)."""
    out, i = {}, 0
    while i < len(lines):
        m = TOP_KEY.match(lines[i])
        if m:
            key, start, j, indent = m.group(1), i, i + 1, None
            while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("#") or ITEM.match(lines[j])):
                im = ITEM.match(lines[j])
                if im and indent is None:
                    indent = im.group(1)
                j += 1
            end = j  # trim trailing blank / comment lines off the block
            while end > start + 1 and not ITEM.match(lines[end - 1]):
                end -= 1
            out[key] = (start, end, indent if indent is not None else "  ")
            i = j
        else:
            i += 1
    return out


def _remove_item(lines, key, handle):
    b = _blocks(lines).get(key)
    if not b:
        return False
    for k in range(b[0] + 1, b[1]):
        m = ITEM.match(lines[k])
        if m and m.group(2).lower() == handle.lower():
            del lines[k]
            return True
    return False


def _add_item(lines, key, handle):
    b = _blocks(lines).get(key)
    if b:
        existing = {ITEM.match(l).group(2).lower() for l in lines[b[0] + 1:b[1]] if ITEM.match(l)}
        if handle.lower() not in existing:
            lines.insert(b[1], f"{b[2]}- {handle}")
        return
    if lines and lines[-1].strip():
        lines.append("")
    lines += [f"{key}:", f"  - {handle}"]


def edit_owners(text: str, remove: list[str], add_reviewers: list[str]):
    """Return (new_text, skipped) where skipped lists handles that couldn't be edited safely."""
    lines = text.splitlines()
    skipped = []
    for h in remove:
        if _remove_item(lines, "approvers", h):
            _add_item(lines, "emeritus_approvers", h)
        else:
            skipped.append((h, "not listed directly under approvers (probably via an alias)"))
    for h in add_reviewers:
        _add_item(lines, "reviewers", h)
    new = "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")
    return new, skipped


def verify(new_text: str, removed: list[str], added: list[str]) -> str | None:
    """None if the edited file says exactly what we intended, else a reason."""
    try:
        d = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        return f"edited file no longer parses: {e}"
    low = lambda xs: {str(x).split("#")[0].strip().lower() for x in (xs or [])}  # noqa: E731
    appr, emer, rev = low(d.get("approvers")), low(d.get("emeritus_approvers")), low(d.get("reviewers"))
    for h in removed:
        if h.lower() in appr or h.lower() not in emer:
            return f"{h} was not moved to emeritus_approvers cleanly"
    for h in added:
        if h.lower() not in rev:
            return f"{h} was not added to reviewers"
    return None


def owners_patch(changes: list[dict]) -> dict:
    """changes: [{dir, owners_text, has_filters, remove, add_reviewers, evidence}]
    Returns {"patch": unified diff text, "applied": [...], "skipped": [...]}."""
    parts, applied, skipped = [], [], []
    for c in changes:
        d, text = c["dir"], c.get("owners_text") or ""
        if not text:
            skipped.append({"dir": d, "reason": "OWNERS text not in the graph"}); continue
        if c.get("has_filters"):
            skipped.append({"dir": d, "reason": "uses filters:, edit by hand"}); continue
        new, sk = edit_owners(text, c.get("remove", []), c.get("add_reviewers", []))
        removed = [h for h in c.get("remove", []) if h not in {s[0] for s in sk}]
        reason = verify(new, removed, c.get("add_reviewers", []))
        if reason:
            skipped.append({"dir": d, "reason": reason}); continue
        if new == text:
            skipped.append({"dir": d, "reason": "nothing to change"}); continue
        path = "OWNERS" if d == "." else f"{d}/OWNERS"
        diff = difflib.unified_diff(text.splitlines(keepends=True), new.splitlines(keepends=True),
                                    fromfile=f"a/{path}", tofile=f"b/{path}")
        parts.append("".join(diff))
        applied.append({"dir": d, "removed": removed, "added_reviewers": c.get("add_reviewers", []),
                        "evidence": c.get("evidence", [])})
        skipped += [{"dir": d, "reason": f"{h}: {why}"} for h, why in sk]
    return {"patch": "".join(parts), "applied": applied, "skipped": skipped}
