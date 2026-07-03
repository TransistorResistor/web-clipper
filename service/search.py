"""
Search engine for the clipper.

Pipeline:
  1. Parse the query (query.py) into an AST.
  2. Pull candidate clips from SQLite (cheap pre-filter on date fields).
  3. For each candidate, assemble its full searchable text:
       title + content_text + extracted text of every attached
       PDF / .docx / .txt / .md / .csv asset. HTML and docx tables are
       rendered as Markdown and kept inline.
  4. Evaluate the AST against the assembled text + metadata.
  5. For matches, extract context windows around each hit, merge
     overlapping windows within a document, then dedup across documents.
  6. Emit a single Markdown (or plain-text) report.

Text extraction caches into the `asset_text` table so repeat searches are
fast and PyMuPDF only runs once per PDF.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import unescape
from typing import Optional

from query import parse_query, collect_terms
import normalize

# PyMuPDF is imported lazily so the module loads even when it's absent
# (e.g. searching a DB with no PDFs). Install: pip install pymupdf
try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except Exception:
    HAVE_FITZ = False


# ---------------------------------------------------------------------------
# Cache table for extracted asset text
# ---------------------------------------------------------------------------

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_text (
    sha256      TEXT PRIMARY KEY REFERENCES assets(sha256),
    text        TEXT,
    extracted_at INTEGER
);
"""


def ensure_cache(conn: sqlite3.Connection):
    # Plain execute (not executescript): executescript force-commits any
    # pending transaction, which would break atomicity for callers that
    # invoke this inside an open transaction (e.g. clip deletion cleanup).
    conn.execute(CACHE_SCHEMA)


# ---------------------------------------------------------------------------
# Tokenization for matching (word offsets matter for proximity + windows)
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"\w+", re.UNICODE)


def word_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) char offsets of each word token."""
    return [(m.start(), m.end()) for m in WORD_RE.finditer(text)]


@lru_cache(maxsize=512)
def _term_regex(term: str) -> re.Pattern:
    """Word-boundary regex for a (lowercased) term or phrase.

    Used by BOTH document matching and context windowing so the two agree:
    'cat' matches the word 'cat' but not 'concatenate'. Lookarounds instead
    of \\b so terms that start/end with non-word chars (`>2024`, `c++`)
    still anchor correctly. Spaces in phrases match any whitespace run.
    """
    escaped = re.escape(term.lower()).replace(r"\ ", " ").replace(" ", r"\s+")
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)")


# ---------------------------------------------------------------------------
# HTML / docx → text + markdown tables
# ---------------------------------------------------------------------------

def html_to_text(html: str) -> str:
    """Strip HTML to text, but render <table> as a Markdown table inline."""
    # Pull tables out first, replacing each with a placeholder + markdown.
    out_parts: list[str] = []
    pos = 0
    for m in re.finditer(r"<table\b.*?</table>", html, re.DOTALL | re.IGNORECASE):
        out_parts.append(_strip_tags(html[pos:m.start()]))
        out_parts.append("\n\n" + html_table_to_md(m.group(0)) + "\n\n")
        pos = m.end()
    out_parts.append(_strip_tags(html[pos:]))
    text = "".join(out_parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip_tags(html: str) -> str:
    no_script = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                       flags=re.DOTALL | re.IGNORECASE)
    # Turn block elements into newlines so words don't run together.
    block = re.sub(r"</(p|div|h[1-6]|li|tr|br|section|article)>", "\n", no_script,
                   flags=re.IGNORECASE)
    block = re.sub(r"<br\s*/?>", "\n", block, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", block)
    text = unescape(text)
    # Collapse runs of spaces/tabs but keep newlines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text


def html_table_to_md(table_html: str) -> str:
    """Convert one <table> to a Markdown table (best effort)."""
    rows = re.findall(r"<tr\b.*?</tr>", table_html, re.DOTALL | re.IGNORECASE)
    md_rows: list[list[str]] = []
    for r in rows:
        cells = re.findall(r"<t[hd]\b.*?</t[hd]>", r, re.DOTALL | re.IGNORECASE)
        md_rows.append([
            re.sub(r"\s+", " ", _strip_tags(c)).strip().replace("|", "\\|")
            for c in cells
        ])
    if not md_rows:
        return ""
    width = max(len(r) for r in md_rows)
    for r in md_rows:
        r += [""] * (width - len(r))
    lines = ["| " + " | ".join(md_rows[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for r in md_rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# docx text extraction (reuses the same XML approach as the migrator)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_to_text(raw: bytes) -> str:
    import zipfile
    from xml.etree import ElementTree as ET
    out: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except Exception:
        return ""
    body = root.find(f"{{{W_NS}}}body")
    if body is None:
        return ""

    def qn(tag): return f"{{{W_NS}}}{tag}"

    for el in body.iter():
        if el.tag == qn("tbl"):
            # Render a table as markdown.
            md_rows = []
            for tr in el.findall(qn("tr")):
                cells = []
                for tc in tr.findall(qn("tc")):
                    txt = "".join(t.text or "" for t in tc.iter(qn("t")))
                    cells.append(txt.strip().replace("|", "\\|"))
                md_rows.append(cells)
            if md_rows:
                width = max(len(r) for r in md_rows)
                for r in md_rows:
                    r += [""] * (width - len(r))
                out.append("\n| " + " | ".join(md_rows[0]) + " |")
                out.append("| " + " | ".join(["---"] * width) + " |")
                for r in md_rows[1:]:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
    # Paragraph text (separate pass; tables above duplicate a little but
    # that's harmless for search and we de-dup visually in output).
    paras = []
    for p in body.findall(qn("p")):
        txt = "".join(t.text or "" for t in p.iter(qn("t")))
        if txt.strip():
            paras.append(txt)
    return ("\n".join(paras) + "\n" + "\n".join(out)).strip()


def pdf_to_text(raw: bytes) -> str:
    if not HAVE_FITZ:
        return ""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        return ""
    parts = []
    for page in doc:
        # "text" preserves reading order reasonably; tables become
        # whitespace-separated which is good enough for keyword search.
        parts.append(page.get_text("text"))
    doc.close()
    return "\n".join(parts).strip()


def plain_to_text(raw: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return ""


def extract_asset_text(conn: sqlite3.Connection, sha: str, mime: str,
                       filename: str = "") -> str:
    """Extract (and cache) the text of one asset."""
    cached = conn.execute("SELECT text FROM asset_text WHERE sha256 = ?", (sha,)).fetchone()
    if cached and cached["text"] is not None:
        return cached["text"]
    blob = conn.execute("SELECT bytes FROM assets WHERE sha256 = ?", (sha,)).fetchone()
    if not blob:
        return ""
    raw = blob["bytes"]
    name = (filename or "").lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        text = pdf_to_text(raw)
    elif "word" in mime or name.endswith(".docx"):
        text = docx_to_text(raw)
    elif mime.startswith("text/") or name.endswith((".txt", ".md", ".csv", ".tsv", ".log")):
        text = plain_to_text(raw)
    else:
        text = ""
    conn.execute(
        "INSERT OR REPLACE INTO asset_text(sha256, text, extracted_at) VALUES (?, ?, ?)",
        (sha, text, int(datetime.now().timestamp())),
    )
    # Persist the cache row immediately: the search path is otherwise
    # read-only and never commits, so without this the extraction work is
    # rolled back on connection close and PyMuPDF re-runs every search.
    conn.commit()
    return text


# ---------------------------------------------------------------------------
# Assembling a document's full searchable text
# ---------------------------------------------------------------------------

@dataclass
class Section:
    """A named chunk of a document's text (a 'field' for windowing)."""
    label: str       # e.g. "content", "pdf:report.pdf", "title"
    text: str


@dataclass
class Doc:
    clip_id: int
    title: str
    url: str
    captured_at: int
    published: Optional[str]
    source: str
    tags: list[str]
    meta: dict
    user_note: str = ""
    sections: list[Section] = field(default_factory=list)

    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.sections if s.text)


def assemble_doc(conn: sqlite3.Connection, row: sqlite3.Row) -> Doc:
    meta = {}
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except Exception:
        pass
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id WHERE ct.clip_id = ?",
        (row["id"],))]
    # Tolerate rows from an older DB that predates the user_note column.
    try:
        user_note = (row["user_note"] or "")
    except (IndexError, KeyError):
        user_note = ""
    doc = Doc(
        clip_id=row["id"], title=row["title"] or "", url=row["url"] or "",
        captured_at=row["captured_at"], source=row["source"] or "",
        # Prefer the normalised ISO date (handles the many input formats);
        # fall back to the raw fields for clips ingested before normalisation.
        published=((meta.get("date_norm") or {}).get("iso")
                   or meta.get("article:published_time") or meta.get("published")),
        tags=tags, meta=meta, user_note=user_note,
    )
    # Title as its own section so title-only matches still window cleanly.
    if doc.title:
        doc.sections.append(Section("title", doc.title))
    # The user note is searchable too (it also leads the rendered entry).
    if doc.user_note:
        doc.sections.append(Section("note", doc.user_note))
    # Attached assets (collected before the body so a PDF clip's content_text
    # - a stored copy of the PDF's text - isn't searched twice, see below).
    asset_sections: list[Section] = []
    for a in conn.execute(
        "SELECT ca.asset_sha, ca.role, ca.filename, asset.mime "
        "FROM clip_assets ca JOIN assets asset ON asset.sha256 = ca.asset_sha "
        "WHERE ca.clip_id = ? AND ca.role IN ('pdf-primary','attachment')",
        (row["id"],)
    ):
        txt = extract_asset_text(conn, a["asset_sha"], a["mime"], a["filename"] or "")
        if txt:
            label = f"{a['role']}:{a['filename'] or a['asset_sha'][:8]}"
            asset_sections.append(Section(label, txt))
    # Body: content_text IS the html_to_text extraction, stored at ingest
    # (tables rendered as Markdown; see app.py / backfill_content_text.py), so
    # use it directly rather than re-converting the whole library's HTML on
    # every search. Rows that predate that convention fall back to converting
    # here. A body that is just a prefix of an asset's text (a PDF clip's
    # stored copy, possibly capped) is skipped - the asset section covers it.
    body = row["content_text"] or ""
    if not body and row["content_html"]:
        body = html_to_text(row["content_html"])
    if body and not any(s.text.startswith(body) for s in asset_sections):
        doc.sections.append(Section("content", body))
    doc.sections.extend(asset_sections)
    return doc


# ---------------------------------------------------------------------------
# AST evaluation
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> Optional[datetime]:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=d.tzinfo or timezone.utc)
        except Exception:
            continue
    # Last resort: leading YYYY-MM-DD inside a longer ISO string.
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    return None


def _end_of_day(d: Optional[datetime]) -> Optional[datetime]:
    """Make a date-only (midnight) upper bound inclusive of that whole day.

    '..2024-06-01' and '<=2024-06-01' should include clips from any time on
    June 1st, not just the midnight instant. Bounds with an explicit time
    component are left untouched.
    """
    if d is not None and d.hour == 0 and d.minute == 0 and d.second == 0:
        return d + timedelta(days=1, seconds=-1)
    return d


def _date_match(spec: str, value_dt: Optional[datetime]) -> bool:
    """spec like '>2024-01-01', '<2025-01-01', '2024-01-01..2025-01-01', '2024-01-01'."""
    if value_dt is None:
        return False
    spec = spec.strip()
    if ".." in spec:
        lo, hi = spec.split("..", 1)
        lo_d, hi_d = _parse_date(lo), _end_of_day(_parse_date(hi))
        if lo_d and value_dt < lo_d:
            return False
        if hi_d and value_dt > hi_d:
            return False
        return True
    if spec.startswith(">="):
        d = _parse_date(spec[2:]); return d is not None and value_dt >= d
    if spec.startswith("<="):
        d = _end_of_day(_parse_date(spec[2:])); return d is not None and value_dt <= d
    if spec.startswith(">"):
        d = _parse_date(spec[1:]); return d is not None and value_dt > d
    if spec.startswith("<"):
        d = _parse_date(spec[1:]); return d is not None and value_dt < d
    # Exact day.
    d = _parse_date(spec)
    if not d:
        return False
    return value_dt.date() == d.date()


def evaluate(node, doc: Doc, haystack_lower: str) -> bool:
    """Return True if the document satisfies the query AST."""
    if node is None:
        return True
    t = node["type"]
    if t == "or":
        return any(evaluate(c, doc, haystack_lower) for c in node["children"])
    if t == "and":
        return all(evaluate(c, doc, haystack_lower) for c in node["children"])
    if t == "not":
        return not evaluate(node["child"], doc, haystack_lower)
    if t in ("term", "phrase"):
        return _term_regex(node["text"]).search(haystack_lower) is not None
    if t == "prox":
        return _prox_match(node, haystack_lower)
    if t == "field":
        return _field_match(node, doc)
    return False


def _field_match(node, doc: Doc) -> bool:
    name, value = node["name"], node["value"]
    v = value.lower()
    if name == "title":
        return v in doc.title.lower()
    if name == "url":
        return v in doc.url.lower()
    if name == "source":
        return v in doc.source.lower()
    if name == "tag":
        return any(v == tag.lower() or v in tag.lower() for tag in doc.tags)
    if name == "added":
        return _date_match(value, datetime.fromtimestamp(doc.captured_at, tz=timezone.utc))
    if name == "published":
        return _date_match(value, _parse_date(doc.published or ""))
    if name == "marking":
        return _marking_match(value, doc)
    if name == "caveat":
        return _caveat_match(value, doc)
    if name == "class":
        return _class_match(value, doc)
    if name in ("report", "report_number"):
        return v in str(doc.meta.get("report_number") or "").lower()
    # Unknown field: treat as a plain term against the title for safety.
    return v in doc.title.lower()


@lru_cache(maxsize=256)
def _query_marking(value: str) -> tuple:
    """Parse a marking query value once (cached). The value is constant across
    the per-document evaluate() loop, so this avoids re-parsing the same query
    for every candidate. Returns (classification, frozenset(caveats))."""
    qn = normalize.normalize_marking(value)
    return qn["classification"], frozenset(qn["caveats"])


def _marking_match(value: str, doc: Doc) -> bool:
    """Match marking:<value> against the whole marker - raw text, normalised
    display, classification or any caveat - and accept abbreviations by routing
    the query through the same vocabulary (marking:ls finds LEGAL SENSITIVE)."""
    mn = doc.meta.get("marking_norm") or {}
    v = value.lower()
    hay = " ".join([str(doc.meta.get("marking") or ""),
                    mn.get("normalized") or ""]).lower()
    if v and v in hay:
        return True
    q_class, q_caveats = _query_marking(value)
    if q_class and q_class == mn.get("classification"):
        return True
    return bool(q_caveats & set(mn.get("caveats") or []))


def _caveat_match(value: str, doc: Doc) -> bool:
    """Match caveat:<value> against the clip's canonical caveat set, alias-aware
    (caveat:nc matches NO CONTRACTORS)."""
    cav = (doc.meta.get("marking_norm") or {}).get("caveats") or []
    v = value.lower()
    if v and any(v in c.lower() for c in cav):
        return True
    return bool(_query_marking(value)[1] & set(cav))


def _class_match(value: str, doc: Doc) -> bool:
    """Match class:<value> against the clip's single classification level,
    alias-aware (class:os matches OFFICIAL-SENSITIVE)."""
    cls = (doc.meta.get("marking_norm") or {}).get("classification")
    if not cls:
        return False
    q_class = _query_marking(value)[0]
    if q_class:
        return q_class == cls
    return value.lower() in cls.lower()


def _prox_match(node, haystack_lower: str) -> bool:
    """Unordered proximity: every term appears, and there's at least one
    arrangement where all terms fall within k words of each other."""
    # `is not None`, not truthiness: %0 legitimately means "adjacent".
    k = node["k"] if node["k"] is not None else 10
    terms = [c["text"].lower() for c in node["terms"] if c["type"] in ("term", "phrase")]
    if len(terms) < 2:
        return all(_term_regex(t).search(haystack_lower) for t in terms)

    # Map each term/phrase to the word indices where it starts, using the
    # same word-boundary regex as evaluate() so matching stays consistent.
    spans = word_spans(haystack_lower)
    positions: list[list[int]] = []
    for term in terms:
        idxs = []
        for m in _term_regex(term).finditer(haystack_lower):
            wi = _char_to_word_index(spans, m.start())
            if wi is not None:
                idxs.append(wi)
        if not idxs:
            return False
        positions.append(sorted(set(idxs)))

    # Sliding check: is there a pick of one index per term whose max-min <= k?
    return _within_window(positions, k)


def _char_to_word_index(spans: list[tuple[int, int]], ci: int) -> Optional[int]:
    # Binary-ish scan; spans is sorted by start.
    lo, hi = 0, len(spans) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if spans[mid][0] >= ci:
            best = mid; hi = mid - 1
        else:
            lo = mid + 1
    return best


def _within_window(positions: list[list[int]], k: int) -> bool:
    """Given sorted index lists (one per term), is there a selection with
    span <= k? Classic 'smallest range covering elements from k lists'."""
    import heapq
    heap = []
    cur_max = -1
    for li, lst in enumerate(positions):
        heapq.heappush(heap, (lst[0], li, 0))
        cur_max = max(cur_max, lst[0])
    while True:
        cur_min, li, ei = heapq.heappop(heap)
        if cur_max - cur_min <= k:
            return True
        if ei + 1 == len(positions[li]):
            return False  # one list exhausted; no better window possible
        nxt = positions[li][ei + 1]
        cur_max = max(cur_max, nxt)
        heapq.heappush(heap, (nxt, li, ei + 1))


# ---------------------------------------------------------------------------
# Context windows + within-doc merging
# ---------------------------------------------------------------------------

@dataclass
class Window:
    section: str
    start_word: int
    end_word: int
    text: str


def find_term_word_hits(terms: list[str], full_lower: str, spans) -> list[int]:
    """Return sorted word indices where any positive term/phrase occurs.

    Uses the same word-boundary regex as evaluate(), so any document that
    matched on a plain term also gets windows for it (and vice versa).
    """
    hits: set[int] = set()
    for term in terms:
        for m in _term_regex(term).finditer(full_lower):
            wi = _char_to_word_index(spans, m.start())
            if wi is not None:
                hits.add(wi)
    return sorted(hits)


def windows_for_section(section: Section, terms: list[str],
                        ctx_words: int) -> list[Window]:
    """Build merged context windows around every term hit in a section."""
    spans = word_spans(section.text)
    if not spans:
        return []
    full_lower = section.text.lower()
    hits = find_term_word_hits(terms, full_lower, spans)
    if not hits:
        return []

    # Turn each hit into a [start,end] word interval, clamp to section bounds.
    n = len(spans)
    intervals = [(max(0, h - ctx_words), min(n - 1, h + ctx_words)) for h in hits]
    # Merge overlapping/touching intervals so multiple matches in one field
    # become one longer span with no duplicated text.
    intervals.sort()
    merged: list[list[int]] = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    out = []
    for s, e in merged:
        char_start = spans[s][0]
        char_end = spans[e][1]
        text = section.text[char_start:char_end]
        text = _snap_table_rows(section.text, char_start, char_end, text)
        out.append(Window(section.label, s, e, text.strip()))
    return out


def _snap_table_rows(full: str, char_start: int, char_end: int, text: str) -> str:
    """If the window boundary falls inside a Markdown table row, extend it
    outward to the row boundaries so the table stays well-formed."""
    if "|" not in text:
        return text
    # Extend start backward to the beginning of its line.
    s = char_start
    while s > 0 and full[s - 1] != "\n":
        s -= 1
    # Extend end forward to the end of its line.
    e = char_end
    while e < len(full) and full[e] != "\n":
        e += 1
    return full[s:e]


# ---------------------------------------------------------------------------
# Cross-result dedup
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Aggressively normalize for near-duplicate comparison."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)   # drop punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _shingles(norm: str, size: int = 8) -> set[str]:
    words = norm.split()
    if len(words) < size:
        return {norm} if norm else set()
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


class Deduper:
    """Tracks accepted windows; decides whether a new one adds material info.

    Strategy: compute word-shingle sets. If a candidate's shingles are almost
    entirely already covered by some accepted window (>= contain_threshold),
    it's a duplicate UNLESS it is materially longer (adds new shingles beyond
    a small errata margin), in which case we keep it (the extra start/end info
    is retained because we store the full longer window)."""

    def __init__(self, contain_threshold: float = 0.9, growth_margin: float = 0.15):
        self.accepted_shingles: list[set[str]] = []
        self.contain_threshold = contain_threshold
        self.growth_margin = growth_margin

    def consider(self, text: str) -> bool:
        norm = _normalize(text)
        sh = _shingles(norm)
        if not sh:
            return False
        for acc in self.accepted_shingles:
            overlap = len(sh & acc)
            frac_of_new_covered = overlap / len(sh)
            if frac_of_new_covered >= self.contain_threshold:
                # Candidate is largely contained in something we already have.
                # Keep only if it grows meaningfully beyond the accepted set.
                new_beyond = len(sh - acc) / len(sh)
                if new_beyond <= self.growth_margin:
                    return False  # essentially a duplicate / minor errata
                # Materially longer: accept and fold its shingles in so we
                # don't later re-admit the same extension.
                acc |= sh
                return True
        self.accepted_shingles.append(set(sh))
        return True


# ---------------------------------------------------------------------------
# Top-level search + report
# ---------------------------------------------------------------------------

@dataclass
class SearchOptions:
    context_words: int = 30
    default_op: str = "OR"
    added_from: Optional[str] = None
    added_to: Optional[str] = None
    published_from: Optional[str] = None
    published_to: Optional[str] = None
    output_format: str = "md"          # 'md' or 'txt'
    max_results: int = 1000


def _fts_match_expr(terms: Optional[list[str]]) -> Optional[str]:
    """Build an FTS5 MATCH expression that ORs the query's positive terms, each
    quoted so it's taken literally (no stray FTS operators). Returns None when
    there is nothing to match on, so the caller scans everything instead.

    Recall note: FTS5 is porter-stemmed, so this matches a SUPERSET of the exact
    word-boundary matcher run per-candidate downstream - it only ever prunes
    clips that cannot match, never a true hit."""
    if not terms:
        return None
    quoted = []
    for t in terms:
        # Collapse embedded quotes/whitespace; a term is matched as a phrase so
        # multi-word phrases ("machine learning") stay intact.
        cleaned = " ".join(t.replace('"', " ").split())
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted) if quoted else None


def _prefilter_rows(conn, opts: SearchOptions, terms: Optional[list[str]] = None):
    """Cheap SQL pre-filter that narrows the candidate clips before the costly
    per-document assembly + AST evaluation. Both narrowings are safe: they only
    ever drop clips that cannot match, so the exact evaluation is unchanged.

      * date-added: an indexed range on captured_at.
      * full-text: when the query has positive terms, keep only clips whose
        FTS-indexed text (title/content/tags/note) matches any of them, UNION
        clips carrying a PDF/attachment asset - whose extracted text is searched
        too but may not live in content_text, so FTS can't see it. Term-less
        queries (field-only, pure-NOT, empty) skip this and scan everything.

    Published-date lives in JSON and is filtered in Python (it honours partial-
    date precision and un-backfilled clips), so it is not prefiltered here."""
    base = "SELECT * FROM clips WHERE 1=1"
    date_sql, date_params = "", []
    if opts.added_from:
        d = _parse_date(opts.added_from)
        if d:
            date_sql += " AND captured_at >= ?"; date_params.append(int(d.timestamp()))
    if opts.added_to:
        d = _end_of_day(_parse_date(opts.added_to))
        if d:
            date_sql += " AND captured_at <= ?"; date_params.append(int(d.timestamp()))
    tail = " ORDER BY captured_at DESC"

    match = _fts_match_expr(terms)
    if match is not None:
        fts_sql = (" AND (id IN (SELECT rowid FROM clips_fts WHERE clips_fts MATCH ?)"
                   " OR id IN (SELECT clip_id FROM clip_assets"
                   " WHERE role IN ('pdf-primary','attachment')))")
        try:
            return conn.execute(base + date_sql + fts_sql + tail,
                                date_params + [match]).fetchall()
        except sqlite3.OperationalError:
            pass  # unparseable MATCH expr -> fall through to a full scan
    return conn.execute(base + date_sql + tail, date_params).fetchall()


def search(conn: sqlite3.Connection, query_str: str, opts: SearchOptions):
    """Run a search, returning a structured result list."""
    ensure_cache(conn)
    ast = parse_query(query_str, default_op=opts.default_op)
    terms = collect_terms(ast)

    rows = _prefilter_rows(conn, opts, terms)
    results = []
    deduper = Deduper()

    for row in rows:
        doc = assemble_doc(conn, row)

        # Apply publication-date range from options (in addition to any
        # published: field already inside the query).
        if opts.published_from or opts.published_to:
            pdt = _parse_date(doc.published or "")
            if opts.published_from:
                lo = _parse_date(opts.published_from)
                if not pdt or (lo and pdt < lo):
                    continue
            if opts.published_to:
                hi = _end_of_day(_parse_date(opts.published_to))
                if not pdt or (hi and pdt > hi):
                    continue

        haystack = doc.full_text()
        haystack_lower = haystack.lower()
        if not evaluate(ast, doc, haystack_lower):
            continue

        # Gather + merge windows per section, then dedup across everything.
        doc_windows = []
        for sec in doc.sections:
            for w in windows_for_section(sec, terms, opts.context_words):
                if deduper.consider(w.text):
                    doc_windows.append(w)
        if not doc_windows and terms:
            # Matched via fields/proximity but no plain-term hit to window;
            # include a short head of the content so the result isn't empty.
            head = haystack[:600].strip()
            if head and deduper.consider(head):
                doc_windows.append(Window("context", 0, 0, head))

        if doc_windows or not terms:
            results.append({"doc": doc, "windows": doc_windows})
        if len(results) >= opts.max_results:
            break

    return {"query": query_str, "ast": ast, "terms": terms, "results": results}


def render_report(search_result: dict, opts: SearchOptions) -> str:
    md = opts.output_format != "txt"
    lines: list[str] = []
    q = search_result["query"]
    n = len(search_result["results"])
    total_windows = sum(len(r["windows"]) for r in search_result["results"])

    if md:
        lines.append(f"# Search results")
        lines.append("")
        lines.append(f"**Query:** `{q}`  ")
        lines.append(f"**Matches:** {n} documents, {total_windows} context blocks  ")
        lines.append(f"**Context:** {opts.context_words} words around each hit  ")
        if opts.added_from or opts.added_to:
            lines.append(f"**Date added filter:** {opts.added_from or '…'} → {opts.added_to or '…'}  ")
        if opts.published_from or opts.published_to:
            lines.append(f"**Published filter:** {opts.published_from or '…'} → {opts.published_to or '…'}  ")
        lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("")
        lines.append("---")
    else:
        lines.append("SEARCH RESULTS")
        lines.append(f"Query: {q}")
        lines.append(f"Matches: {n} documents, {total_windows} context blocks")
        lines.append("=" * 60)

    for r in search_result["results"]:
        doc = r["doc"]
        added = datetime.fromtimestamp(doc.captured_at, tz=timezone.utc).strftime("%Y-%m-%d")
        pub = ""
        pdt = _parse_date(doc.published or "")
        if pdt:
            pub = pdt.strftime("%Y-%m-%d")
        if md:
            lines.append("")
            lines.append(f"## {doc.title or '(untitled)'}  ")
            meta_bits = [f"clip #{doc.clip_id}", f"added {added}"]
            if pub:
                meta_bits.append(f"published {pub}")
            if doc.source:
                meta_bits.append(doc.source)
            if doc.tags:
                meta_bits.append("tags: " + ", ".join(doc.tags))
            lines.append("*" + " · ".join(meta_bits) + "*  ")
            if doc.url:
                lines.append(f"<{doc.url}>  ")
            lines.append("")
            # The user's own comment leads the entry, as context for what
            # follows. Rendered as a labelled blockquote (each line quoted).
            if doc.user_note:
                note_lines = doc.user_note.splitlines() or [doc.user_note]
                lines.append(f"> **User comment:** {note_lines[0].strip()}")
                for extra in note_lines[1:]:
                    lines.append(f"> {extra.strip()}")
                lines.append("")
            for w in r["windows"]:
                lines.append(f"**[{w.section}]**")
                lines.append("")
                # Preserve markdown tables verbatim; quote prose.
                if "|" in w.text and "---" in w.text:
                    lines.append(w.text)
                else:
                    for para in w.text.split("\n"):
                        if para.strip():
                            lines.append("> " + para.strip())
                    lines.append("")
                lines.append("")
            lines.append("---")
        else:
            lines.append("")
            lines.append(f"### {doc.title or '(untitled)'} [clip #{doc.clip_id}]")
            lines.append(f"    added {added}" + (f" | published {pub}" if pub else ""))
            if doc.url:
                lines.append(f"    {doc.url}")
            if doc.user_note:
                first, *rest = doc.user_note.splitlines() or [doc.user_note]
                lines.append(f"    User comment: {first.strip()}")
                for extra in rest:
                    lines.append(f"    {extra.strip()}")
            for w in r["windows"]:
                lines.append(f"  [{w.section}]")
                for para in w.text.split("\n"):
                    if para.strip():
                        lines.append("    " + para.strip())
            lines.append("-" * 60)

    if n == 0:
        lines.append("")
        lines.append("_No matches._" if md else "No matches.")
    return "\n".join(lines) + "\n"
