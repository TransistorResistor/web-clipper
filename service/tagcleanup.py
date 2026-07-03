"""Tag-vocabulary cleanup candidates (Phase 1).

Conservative, HIGH-PRECISION detectors over the tag list + each tag's clip set,
so the manage panel surfaces a few genuine consolidation / hygiene candidates
instead of the noise that naive subset- or edit-distance signals produce on a
sparse, code-heavy library (verified: pure subset flags `Chickens`⊆`Bamboo`,
edit-distance flags `F-35`~`F-15`, and 58/78 tags are singletons - all useless).

Two signals only:

  MERGE candidate - a tag name that is a leading token-prefix of another
    (`F-35` -> `F-35 Program`, `ea-18g` -> `ea-18g growler`), i.e. one reads as
    a sub-tag of the other. Clip-set CONTAINMENT (every child clip also carries
    the parent) upgrades the confidence but is never used on its own. Surfaced
    as "consider merging child -> parent" - the user confirms or dismisses (they
    may be intentional facets).

  FORMAT outlier - a tag whose designation token is ALL lowercase (`f15d`,
    `ea-18g`): unambiguously malformed. Mixed-case (`MiG-29`, `F/A-18E/F`) is
    LEFT ALONE - that's often the correct spelling. Surfaced as "rename to ...".

Pure functions (no DB) so they're trivially testable; `cleanup_report(conn)`
wires them to the database and the dismissed-set (persisted in app_settings).
Merges/renames themselves reuse the existing /api/tags/bulk operations.
"""
from __future__ import annotations

import json
import re
import sqlite3

from db import get_setting, set_setting

_DISMISS_KEY = "tag_cleanup_dismissed"
# A designation token: starts with letters, then a digit (optionally a separator
# between), e.g. f15d, ea-18g, F-15E, MiG-29. Used to find code-shaped tokens.
_DESIGNATION_RE = re.compile(r"[A-Za-z]+[-/]?\d[A-Za-z0-9/\-]*")


def _is_prefix_family(parent: str, child: str) -> bool:
    """True if `parent` is a leading token-prefix of `child` - child continues
    after a separator (`F-35` -> `F-35 Program`, `NATO` -> `NATO-US relations`).
    Min length 3 so trivial prefixes don't fire."""
    p, c = parent.lower(), child.lower()
    return (len(parent) >= 3 and c != p and c.startswith(p)
            and c[len(p)] in " -/")


def merge_candidates(names: list[str], tag_clips: dict[str, set]) -> list[dict]:
    """Token-prefix consolidation candidates, strongest first. Each:
    {sig, tags:[child,parent], target, reason, detail, score}. `score` 2 when
    clip-set containment confirms the name relationship, else 1."""
    out, seen = [], set()
    for parent in names:
        for child in names:
            if parent == child or not _is_prefix_family(parent, child):
                continue
            sig = "merge:" + "|".join(sorted([parent, child]))
            if sig in seen:
                continue
            seen.add(sig)
            cc, cp = tag_clips.get(child, set()), tag_clips.get(parent, set())
            subset = bool(cc) and cc <= cp        # every child clip also a parent clip
            out.append({
                "sig": sig, "tags": [child, parent], "target": parent,
                "reason": "name+subset" if subset else "name",
                "score": 2 if subset else 1,
                "detail": (f"every '{child}' clip is also tagged '{parent}'" if subset
                           else f"'{child}' reads as a sub-tag of '{parent}'"),
            })
    out.sort(key=lambda c: (-c["score"], c["target"].lower(), c["tags"][0].lower()))
    return out


def _suggest_upcase(name: str) -> str:
    """Uppercase only the all-lowercase designation tokens in `name`, leaving
    real words alone: 'ea-18g growler' -> 'EA-18G growler'."""
    def fix(m):
        tok = m.group(0)
        return tok.upper() if tok == tok.lower() else tok
    return _DESIGNATION_RE.sub(fix, name)


def format_outliers(names: list[str]) -> list[dict]:
    """Tags carrying an ALL-lowercase designation token (malformed codes like
    `f15d`, `ea-18g`). Mixed-case designations (`MiG-29`) are intentionally NOT
    flagged. Each: {sig, tag, suggest, detail}."""
    out = []
    for n in names:
        bad = [t for t in _DESIGNATION_RE.findall(n)
               if t == t.lower() and any(ch.isalpha() for ch in t)]
        if bad:
            sug = _suggest_upcase(n)
            if sug != n:
                out.append({"sig": "fmt:" + n, "tag": n, "suggest": sug,
                            "detail": f"lowercase designation {bad[0]!r} - usually uppercase"})
    out.sort(key=lambda c: c["tag"].lower())
    return out


# --- DB wiring + dismissed-set -------------------------------------------

def _load(conn: sqlite3.Connection):
    names = [r["name"] for r in conn.execute("SELECT name FROM tags")]
    tag_clips: dict[str, set] = {}
    for r in conn.execute(
            "SELECT t.name, ct.clip_id FROM tags t "
            "JOIN clip_tags ct ON ct.tag_id = t.id"):
        tag_clips.setdefault(r["name"], set()).add(r["clip_id"])
    return names, tag_clips


def dismissed_set(conn: sqlite3.Connection) -> set:
    raw = get_setting(conn, _DISMISS_KEY)
    try:
        return set(json.loads(raw)) if raw else set()
    except (ValueError, TypeError):
        return set()


def dismiss(conn: sqlite3.Connection, sig: str) -> None:
    s = dismissed_set(conn)
    s.add(sig)
    set_setting(conn, _DISMISS_KEY, json.dumps(sorted(s)))


def cleanup_report(conn: sqlite3.Connection) -> dict:
    """Live cleanup candidates, with dismissed signatures filtered out. Stale
    dismissed sigs (whose tags no longer exist) are harmless - they just never
    match a live candidate."""
    names, tag_clips = _load(conn)
    dis = dismissed_set(conn)
    return {
        "merge": [c for c in merge_candidates(names, tag_clips) if c["sig"] not in dis],
        "format": [c for c in format_outliers(names) if c["sig"] not in dis],
    }
