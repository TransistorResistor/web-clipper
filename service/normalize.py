"""
Normalisation of the free-form metadata the clippers capture into canonical,
filterable forms.

Two messy fields get a canonical projection so clips can be sorted/filtered by
them regardless of how they were originally written:

  * dates    - many input shapes (ISO-8601, PDF ``D:YYYYMMDD...`` creation
               stamps, ``14 March 2026``, ``March 14, 2026``, numeric
               ``03/04/2026``, bare ``2026``) -> an ISO date plus a *precision*
               marker, so a year-only value doesn't masquerade as 1 January and
               ambiguous numeric dates are read **day-first** (DD/MM/YYYY, UK
               convention - see ``DAY_FIRST``).

  * markings - a handling marker, often compound and abbreviated
               (``Privileged data: LEGAL SENSITIVE NO CONTRACTORS``, or the same
               written ``LS NC``) -> an optional classification *level* plus a
               canonical *set* of caveats, so two clips carrying the same
               handling marker compare equal no matter how it was spelled.

A marking is resolved in two stages (see ``normalize_marking``):

  1. **Curated equivalence rules** - a lookup in ``markings.json`` mapping a set
     of equivalent whole-marker strings to an agreed decomposition. This is the
     authoritative path: "if you see ``O:S NC-LS`` it means OFFICIAL-SENSITIVE +
     {NO CONTRACTORS, LEGAL SENSITIVE}". Checked first, so it always wins.
  2. **Per-word vocabulary** - for anything not covered by a rule, the
     classification/caveat alias tables (also in ``markings.json``) are matched
     token by token, with a leading descriptor label split off.

``apply_normalization(meta)`` is the single entry point every ingest path calls:
it reads ``meta['date_published']`` / ``meta['marking']`` and writes back the
structured ``meta['date_norm']`` / ``meta['marking_norm']`` objects (so they ride
along in meta_json for display). ``columns_from_meta(meta)`` projects those into
the four dedicated ``clips`` columns the caller indexes for SQL filtering.

The vocabulary and equivalence rules live in ``markings.json`` next to this file
for easy editing; the dicts below are only a fallback when that file is absent.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

# Read ambiguous all-numeric dates (03/04/2026) day-first. Flip to False for
# month-first (US) input. ISO (YYYY-MM-DD) is never ambiguous and ignores this.
DAY_FIRST = True


# ---------------------------------------------------------------------------
# Marking vocabulary + equivalence rules (loaded from markings.json)
# ---------------------------------------------------------------------------
MARKINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "markings.json")

# Built-in fallback, used only when markings.json is missing or invalid.
# canonical form -> accepted spellings; matching treats '-', '/' and ':' as
# spaces and is case-insensitive. Single-letter abbreviations are omitted on
# purpose (a stray "S"/"C" would match too eagerly).
_DEFAULT_CLASSIFICATIONS = {
    "TOP SECRET": ["TOP SECRET", "TS"],
    "SECRET": ["SECRET"],
    "CONFIDENTIAL": ["CONFIDENTIAL"],
    "OFFICIAL-SENSITIVE": ["OFFICIAL SENSITIVE", "OFFICIAL-SENSITIVE",
                           "OFFICIAL: SENSITIVE", "O-S", "OS"],
    "OFFICIAL": ["OFFICIAL"],
    "RESTRICTED": ["RESTRICTED"],
    "PROTECTED": ["PROTECTED"],
    "UNCLASSIFIED": ["UNCLASSIFIED", "UNCLAS"],
    "PUBLIC": ["PUBLIC", "UNOFFICIAL"],
}
_DEFAULT_CAVEATS = {
    "LEGAL SENSITIVE": ["LEGAL SENSITIVE", "LEGAL PRIVILEGE", "LS"],
    "NO CONTRACTORS": ["NO CONTRACTORS", "NOCON", "NC"],
    "FOR OFFICIAL USE ONLY": ["FOR OFFICIAL USE ONLY", "FOUO"],
    "RECIPIENTS ONLY": ["RECIPIENTS ONLY", "ADDRESSEES ONLY"],
    "COMMERCIAL": ["COMMERCIAL", "COMMERCIAL IN CONFIDENCE"],
    "PERSONAL DATA": ["PERSONAL DATA", "PERSONAL"],
    "NOFORN": ["NOFORN", "NO FOREIGN NATIONALS"],
    "ORCON": ["ORCON", "ORIGINATOR CONTROLLED"],
}

# Populated by load_markings() (called at import; re-callable at runtime).
CLASSIFICATION_LEVELS: dict = {}
CAVEATS: dict = {}
_ALIAS_INDEX: list = []
_ALIAS_SET: set = set()             # for the label-vs-marking test
_LEVEL_PRIORITY: dict = {}
_EQUIVALENTS: dict = {}             # normalised match string -> {classification, caveats}


_SEP_RE = re.compile(r"[-–—/:]+")
_WS_RE = re.compile(r"\s+")


def _norm_phrase(s: str) -> str:
    """Canonical comparison form for a marking phrase: uppercase, with '-', '/',
    ':' and runs of whitespace all flattened to single spaces."""
    return _WS_RE.sub(" ", _SEP_RE.sub(" ", s or "")).strip().upper()


def _build_alias_index():
    """Flatten the vocabulary into (word_tuple, canonical, kind) entries sorted
    longest-phrase-first, so a greedy scan matches "LEGAL SENSITIVE" before the
    bare word "LEGAL". ``kind`` is 'level' or 'caveat'."""
    entries = []
    for canon, aliases in CLASSIFICATION_LEVELS.items():
        for a in aliases:
            entries.append((tuple(_norm_phrase(a).split()), canon, "level"))
    for canon, aliases in CAVEATS.items():
        for a in aliases:
            entries.append((tuple(_norm_phrase(a).split()), canon, "caveat"))
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    return entries


def _build_equivalents(rules):
    """Index equivalence rules by the normalised form of each 'match' string, so
    a whole-marker lookup is O(1) and case/punctuation-insensitive. Later rules
    win on a clash. ``match`` may be a string or a list of strings."""
    out = {}
    for rule in rules or []:
        matches = rule.get("match")
        if isinstance(matches, str):
            matches = [matches]
        decomp = {"classification": rule.get("classification") or None,
                  "caveats": list(rule.get("caveats") or [])}
        for mstr in (matches or []):
            key = _norm_phrase(mstr)
            if key:
                out[key] = decomp
    return out


def load_markings(path: str | None = None) -> None:
    """(Re)load the vocabulary + equivalence rules from markings.json and rebuild
    the lookup indexes. Falls back to the built-in defaults if the file is
    missing or unparseable. Call after editing the JSON in a long-running process."""
    global CLASSIFICATION_LEVELS, CAVEATS, _ALIAS_INDEX, _ALIAS_SET
    global _LEVEL_PRIORITY, _EQUIVALENTS
    data = {}
    try:
        with open(path or MARKINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    CLASSIFICATION_LEVELS = data.get("classifications") or dict(_DEFAULT_CLASSIFICATIONS)
    CAVEATS = data.get("caveats") or dict(_DEFAULT_CAVEATS)
    _ALIAS_INDEX = _build_alias_index()
    _ALIAS_SET = {e[0] for e in _ALIAS_INDEX}
    _LEVEL_PRIORITY = {c: i for i, c in enumerate(CLASSIFICATION_LEVELS)}
    _EQUIVALENTS = _build_equivalents(data.get("equivalents"))


reload_markings = load_markings        # readable alias


def _match_tokens(words):
    """Greedy longest-alias scan over a word list. Returns
    (levels, caveats, unknown) where levels/caveats are ordered canonical lists
    and unknown holds runs of words that matched nothing."""
    levels, caveats, unknown = [], [], []
    i, n = 0, len(words)
    run = []
    while i < n:
        hit = None
        for word_tuple, canon, kind in _ALIAS_INDEX:
            k = len(word_tuple)
            if k and tuple(words[i:i + k]) == word_tuple:
                hit = (canon, kind, k)
                break
        if hit:
            if run:
                unknown.append(" ".join(run)); run = []
            canon, kind, k = hit
            bucket = levels if kind == "level" else caveats
            if canon not in bucket:
                bucket.append(canon)
            i += k
        else:
            run.append(words[i])
            i += 1
    if run:
        unknown.append(" ".join(run))
    return levels, caveats, unknown


def normalize_marking(raw: str) -> dict:
    """Parse a handling-marker string into structured components.

    Returns a dict (always, even for empty input)::

        {
          'raw': original string,
          'label': leading descriptor stripped off (e.g. 'Privileged data'),
          'classification': single canonical level or None,
          'caveats': ordered, de-duplicated list of canonical caveats,
          'unknown': list of word-runs that matched no vocabulary entry,
          'normalized': canonical display string ('OFFICIAL // LEGAL SENSITIVE'),
        }
    """
    out = {"raw": raw or "", "label": "", "classification": None,
           "caveats": [], "unknown": [], "normalized": ""}
    if not raw or not raw.strip():
        return out

    s = raw.strip()

    # 1. Curated equivalence rule for the whole marker (markings.json). Highest
    #    priority: "if you see THIS, it means exactly THAT".
    eq = _EQUIVALENTS.get(_norm_phrase(s))
    if eq:
        return _apply_decomp(out, "", eq)

    # 2. Fall back to the per-word vocabulary. Colons/hyphens/slashes are already
    #    flattened to spaces by _norm_phrase, so "O:S" / "O-S/NC" decompose.
    levels, caveats, unknown = _match_tokens(_norm_phrase(s).split())

    # Only if something failed to parse AND there's a colon do we consider the
    # text before it a descriptor label ("Privileged data: ..."). It's stripped
    # solely when the left side isn't itself a marking and removing it strictly
    # *improves* the parse - so a colon inside an abbreviation ("O:S") or a real
    # marking ("OFFICIAL: SENSITIVE") is preserved, and "Note: see appendix"
    # (whose right side has no marking) is left whole. The remainder may itself
    # be a curated equivalent.
    if unknown and ":" in s:
        left, right = s.split(":", 1)
        left_words = tuple(_norm_phrase(left).split())
        if left_words and left_words not in _ALIAS_SET:
            eq_r = _EQUIVALENTS.get(_norm_phrase(right))
            if eq_r:
                return _apply_decomp(out, left.strip(), eq_r)
            r_levels, r_caveats, r_unknown = _match_tokens(_norm_phrase(right).split())
            if (r_levels or r_caveats) and len(r_unknown) < len(unknown):
                out["label"] = left.strip()
                levels, caveats, unknown = r_levels, r_caveats, r_unknown
    if levels:
        levels.sort(key=lambda c: _LEVEL_PRIORITY.get(c, 999))
        out["classification"] = levels[0]
    out["caveats"] = caveats
    out["unknown"] = unknown
    out["normalized"] = _join_norm(out["classification"], caveats)
    return out


def _join_norm(classification, caveats):
    """Canonical display string: 'LEVEL // CAVEAT // CAVEAT'."""
    parts = ([classification] if classification else []) + list(caveats)
    return " // ".join(parts)


def _apply_decomp(out, label, decomp):
    """Fill `out` from a curated equivalence rule (no unknown components)."""
    out["label"] = label
    out["classification"] = decomp.get("classification") or None
    out["caveats"] = list(decomp.get("caveats") or [])
    out["unknown"] = []
    out["normalized"] = _join_norm(out["classification"], out["caveats"])
    return out


# Load the vocabulary + equivalence rules once at import.
load_markings()


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

_MONTHS = {
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4, "MAY": 5, "JUN": 6, "JUNE": 6, "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8, "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "OCT": 10,
    "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11, "DEC": 12, "DECEMBER": 12,
}


def _result(y, m, d, precision):
    """Validate (y, m, d) and build a result dict, or None if out of range.
    Partial precisions fill the missing components with 1 (first-of-period) so
    the stored ``iso`` is always a real, sortable date."""
    try:
        iso = date(y, m or 1, d or 1).isoformat()
    except ValueError:
        return None
    return {"iso": iso, "precision": precision}


def normalize_date(raw: str, day_first: bool = DAY_FIRST) -> dict | None:
    """Parse a date string in any of the common shapes into
    ``{'iso': 'YYYY-MM-DD', 'precision': 'day'|'month'|'year'}`` or None.

    ``iso`` is always a full sortable date (partials use first-of-period);
    ``precision`` says how much of it to trust."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # PDF /Info dates: "D:YYYYMMDDHHmmSS+HH'mm'" (time/zone optional).
    m = re.match(r"^D:(\d{4})(\d{2})?(\d{2})?", s)
    if m:
        y = int(m.group(1))
        mo = int(m.group(2)) if m.group(2) else None
        d = int(m.group(3)) if m.group(3) else None
        prec = "day" if d else ("month" if mo else "year")
        return _result(y, mo, d, prec)

    # ISO-8601 (date, optionally followed by time/zone we ignore).
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)", s)
    if m:
        return _result(int(m.group(1)), int(m.group(2)), int(m.group(3)), "day")
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return _result(int(m.group(1)), int(m.group(2)), None, "month")

    # From here on a trailing time-of-day (and zone) is tolerated and ignored:
    # the value is split off on the first space or 'T'. Bare-year / month-only
    # cases below stay strict so "2026 wombats" doesn't parse.
    _TIME = r"(?:[ T].*)?$"      # optional " 09:30:00 +0000" / "T09:30Z" tail

    # Numeric year-first with / or . separators: YYYY/MM/DD.
    m = re.match(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})" + _TIME, s)
    if m:
        return _result(int(m.group(1)), int(m.group(2)), int(m.group(3)), "day")

    # Numeric little/middle-endian: DD/MM/YYYY or MM/DD/YYYY (also '.' or '-').
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})" + _TIME, s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:                       # 2-digit year -> 2000s
            y += 2000
        d, mo = (a, b) if day_first else (b, a)
        # If the assumed month is impossible but the day slot is a valid month,
        # swap (e.g. month-first reading of 25/12 -> day-first).
        if mo > 12 >= d:
            d, mo = mo, d
        return _result(y, mo, d, "day")

    # Textual day-month-year, optionally prefixed by a weekday so RFC-2822 /
    # email-header stamps parse: "Mon, 14 Mar 2026 09:30:00 +0000",
    # "14 March 2026", "4th July 2026".
    m = re.match(r"^(?:[A-Za-z]{3,9},?\s+)?(\d{1,2})(?:st|nd|rd|th)?\.?,?\s+"
                 r"([A-Za-z]{3,})\.?,?\s+(\d{4})" + _TIME, s)
    if m and m.group(2).upper() in _MONTHS:
        return _result(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)), "day")

    # Textual month-day-year: "March 14, 2026", "Mar 14 2026 9:30 AM".
    m = re.match(r"^([A-Za-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})" + _TIME, s)
    if m and m.group(1).upper() in _MONTHS:
        return _result(int(m.group(3)), _MONTHS[m.group(1).upper()], int(m.group(2)), "day")

    # Textual month + year only: "March 2026".
    m = re.match(r"^([A-Za-z]{3,})\.?\s+(\d{4})$", s)
    if m and m.group(1).upper() in _MONTHS:
        return _result(int(m.group(2)), _MONTHS[m.group(1).upper()], None, "month")

    # Bare year.
    m = re.match(r"^(\d{4})$", s)
    if m:
        return _result(int(m.group(1)), None, None, "year")

    return None


# ---------------------------------------------------------------------------
# Entry points used by the ingest paths
# ---------------------------------------------------------------------------

def apply_normalization(meta: dict) -> dict:
    """Populate ``meta['date_norm']`` and ``meta['marking_norm']`` in place from
    the raw ``date_published`` / ``marking`` fields, and return ``meta``.

    Idempotent: safe to call more than once. PDF paths that only have a raw
    ``creationDate`` are handled too."""
    raw_date = meta.get("date_published") or meta.get("creationDate") or ""
    meta["date_norm"] = normalize_date(raw_date) or {}
    meta["marking_norm"] = normalize_marking(meta.get("marking") or "")
    return meta


def columns_from_meta(meta: dict) -> dict:
    """Derive the four dedicated ``clips`` column values from a meta dict,
    normalising first if it hasn't been. Empty components map to None so the
    columns stay NULL rather than storing ''/'[]'.

    Returns ``{date_published_norm, date_precision, marking_class,
    marking_caveats}`` where ``marking_caveats`` is a JSON array string."""
    if "date_norm" not in meta or "marking_norm" not in meta:
        apply_normalization(meta)
    dn = meta.get("date_norm") or {}
    mn = meta.get("marking_norm") or {}
    caveats = mn.get("caveats") or []
    return {
        "date_published_norm": dn.get("iso") or None,
        "date_precision": dn.get("precision") or None,
        "marking_class": mn.get("classification") or None,
        "marking_caveats": json.dumps(caveats) if caveats else None,
    }


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(f"\nINPUT: {arg!r}")
        print("  date   :", normalize_date(arg))
        print("  marking:", json.dumps(normalize_marking(arg), indent=4))
