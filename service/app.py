"""
Flask service for the local clipper.

Endpoints:
  POST /api/clip         - userscript posts a clip here
  POST /api/attach-pdf   - userscript posts a PDF here
  GET  /api/clips        - list/search clips
  GET  /api/clips/<id>   - clip detail (JSON)
  PATCH /api/clips/<id>  - edit title/content/tags, writes a revision
  DELETE /api/clips/<id> - delete a clip (and its assets if orphaned)
  GET  /api/tags         - all tag names
  GET/POST/DELETE /api/suggest/blocklist - manage suppressed suggestion keywords
  GET  /api/outlook/status - whether the Outlook bridge can run
  POST /api/outlook/import - import mail items (PDF attachments / bodies) by EntryID
  GET  /api/review/pending - clips awaiting tag review
  POST /api/clips/<id>/reviewed - clear a clip's review-pending flag
  POST /api/upload/preview - stage a PDF/MHTML file, return an editable preview
  POST /api/upload/commit  - commit a staged upload
  POST /api/upload/bulk    - one-shot PDF import (recursive folder ingest)
  GET/POST /api/settings - read/update app settings (outlook_review)
  GET  /review           - tag-review queue page
  POST /api/clips/<id>/annotations - add an annotation
  GET  /api/clips/<id>/annotations - list annotations
  DELETE /api/annotations/<id>     - remove one annotation
  GET  /asset/<sha256>   - serve an asset by hash
  GET  /                 - the viewer (server-rendered HTML)
  GET  /clip/<id>        - detail page for one clip
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from html import escape as html_escape
from urllib.parse import urlparse

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
)
from io import BytesIO

from db import (
    add_blocked_keyword, connect, delete_tags, find_duplicate, gc_orphan_tags,
    get_db_path, get_setting, init_db, list_blocked_keywords, merge_tags, now_ts,
    refresh_fts_tags, remove_blocked_keyword, rename_tag, set_setting,
    transaction, upsert_tags, write_norm_columns,
)
import search as search_engine
import ingest as ingest_mod
import normalize as normalize_mod
import suggest as suggest_mod
import tagcleanup as tagcleanup_mod

# NOTE: no CORS. The userscript talks to this service via GM_xmlhttpRequest,
# which is exempt from CORS, and the viewer pages are same-origin. A CORS
# wildcard here would let any website the browser visits read the whole
# library and POST/DELETE clips - so we deliberately leave cross-origin
# requests blocked by the browser's default policy.
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clipper")


@app.after_request
def security_headers(resp):
    # Block stored clips from phoning home (remote images/scripts) and stop
    # MIME sniffing on served assets. Inline scripts are our own templates'.
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if resp.mimetype == "text/html":
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'")
    return resp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"\w+", re.UNICODE)
DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)

# Metadata fields the detail page lets the user correct after ingest. Editing
# any of them re-runs normalization so the filterable date/marking columns track
# the change; 'url' additionally mirrors onto the clips.url / canonical_url
# columns (the list view and detail header read those).
_EDITABLE_META_FIELDS = ("marking", "date_published", "report_number", "url")

# --- HTML sanitization (defense in depth) ----------------------------------
# The userscript strips <script>/<style> elements but not inline event
# handlers, and /api/clip accepts HTML from anything that can reach
# localhost. The viewer renders content_html directly, so scrub the obvious
# script vectors at ingest time. Regex-based, so not bulletproof against a
# determined attacker - but it removes the practical XSS routes without
# adding a dependency.
_BAD_TAG_PAIR_RE = re.compile(
    r"<(script|style|iframe|object|embed|noscript)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE)
_BAD_TAG_OPEN_RE = re.compile(
    r"</?(script|iframe|object|embed)\b[^>]*>", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(
    r"""\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_JS_URL_ATTR_RE = re.compile(
    r"""\s+(?:href|src|xlink:href|action|formaction)\s*=\s*(["']?)\s*javascript:[^"'>]*\1""",
    re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """Strip script-bearing tags, on* event attributes and javascript: URLs."""
    out = _BAD_TAG_PAIR_RE.sub(" ", html)
    out = _BAD_TAG_OPEN_RE.sub(" ", out)
    out = _EVENT_ATTR_RE.sub(" ", out)
    out = _JS_URL_ATTR_RE.sub(" ", out)
    return out


def int_arg(source, name: str, default: int, lo: int = 0, hi: int = 10**9) -> int:
    """Read an int param defensively: junk input falls back to the default
    instead of raising into a 500; the result is clamped to [lo, hi]."""
    try:
        val = int(source.get(name, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(val, hi))


def float_arg(source, name: str, default: float,
              lo: float = 0.0, hi: float = 10.0) -> float:
    """Float counterpart to int_arg: junk falls back to default, clamped."""
    try:
        val = float(source.get(name, default))
    except (TypeError, ValueError):
        val = default
    return max(lo, min(val, hi))


def bool_arg(value, default: bool = True) -> bool:
    """Coerce a JSON value (bool, number, or string) to a flag."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def fts_quote(q: str) -> str:
    """Quote each token so FTS5 treats user text literally (no operators)."""
    toks = [t.replace('"', '""') for t in q.split()]
    return " ".join(f'"{t}"' for t in toks)


def cleanup_orphan_assets(conn) -> None:
    """Delete assets no longer referenced by any clip, plus their cached
    extracted text (asset_text references assets, so it must go first or
    the foreign key would block the asset delete)."""
    search_engine.ensure_cache(conn)
    conn.execute("""DELETE FROM asset_text WHERE sha256 NOT IN
                    (SELECT DISTINCT asset_sha FROM clip_assets)""")
    conn.execute("""DELETE FROM assets WHERE sha256 NOT IN
                    (SELECT DISTINCT asset_sha FROM clip_assets)""")


# Stored content_text always comes from search_engine.html_to_text: one
# extractor for every ingest path, so the same page clipped, uploaded as
# MHTML, or imported from email produces the same text - hence the same
# content_hash (dedupe works across paths), the same word count, and
# entity-decoded, table-preserving FTS text. (An older local strip_html
# skipped entity decoding and tables; the two drifted.)

def word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def reading_minutes(text: str) -> int:
    # 230 wpm is the median for English adults; good enough across the board.
    return max(1, round(word_count(text) / 230))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# PDF clips used to store just the title as content_text, which made them
# invisible to the library search box (FTS indexes content_text) and gave the
# embedding cache a title-only vector. Store the PDF's extracted text instead,
# capped so a pathological scan can't bloat the clips table; dedupe still keys
# on the PDF bytes' sha256, so content_hash semantics are unchanged.
_PDF_TEXT_CAP = 500_000


def pdf_content_text(title: str, pdf_text: str) -> tuple[str, int, int]:
    """(content_text, word_count, reading_minutes) for a PDF clip. Falls back
    to the title when no text could be extracted (scanned/image PDFs)."""
    text = (pdf_text or "").strip()[:_PDF_TEXT_CAP] or title
    return text, word_count(text), reading_minutes(text)


def warm_asset_text(conn, sha: str, text: str) -> None:
    """Seed the asset_text cache with text already extracted at ingest, so the
    first advanced search doesn't re-run PyMuPDF over the same bytes. Call
    inside the ingest transaction, after store_asset (FK on assets)."""
    search_engine.ensure_cache(conn)
    conn.execute(
        "INSERT OR REPLACE INTO asset_text(sha256, text, extracted_at) "
        "VALUES (?, ?, ?)", (sha, text or "", now_ts()))


def store_asset(conn, raw: bytes, mime: str) -> str:
    sha = sha256_bytes(raw)
    exists = conn.execute("SELECT 1 FROM assets WHERE sha256 = ?", (sha,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO assets(sha256, mime, bytes, byte_size, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sha, mime, raw, len(raw), now_ts()),
        )
    return sha


def rewrite_data_uri_images(conn, clip_id: int, html: str) -> tuple[str, int]:
    """Extract base64 data URIs from <img src=...>, store in assets, rewrite to /asset/<sha>.

    Handles both single-quoted, double-quoted, and unquoted src attributes.
    """
    extracted = 0

    def store_data_uri(src: str) -> str | None:
        """Return the replacement src for a data: URI, or None if not extractable."""
        m = DATA_URI_RE.match(src)
        if not m:
            return None
        mime = m.group(1).strip()
        try:
            raw = base64.b64decode(re.sub(r"\s", "", m.group(2)))
        except Exception:
            log.warning("could not decode data URI for clip %s", clip_id)
            return None
        sha = store_asset(conn, raw, mime)
        conn.execute(
            "INSERT OR IGNORE INTO clip_assets(clip_id, asset_sha, role, filename) "
            "VALUES (?, ?, 'inline-image', '')",
            (clip_id, sha),
        )
        return f"/asset/{sha}"

    def repl(match: re.Match) -> str:
        nonlocal extracted
        quote, src = match.group(1), match.group(2)
        new_src = store_data_uri(src)
        if new_src is None:
            return match.group(0)
        extracted += 1
        return f'src={quote}{new_src}{quote}'

    # Match src="..." or src='...'. Unquoted srcs with data URIs effectively
    # don't occur in practice (commas, semicolons in the URI break unquoted parsing).
    rewritten = re.sub(r'src=(["\'])(data:[^"\']+)\1', repl, html)
    return rewritten, extracted


def extract_meta(meta: dict, html: str) -> dict:
    """Pull a few useful Open-Graph-ish fields from HTML, merge with caller meta."""
    out = dict(meta or {})
    for prop in ("article:author", "og:site_name", "og:type", "article:published_time"):
        if prop in out:
            continue
        # Attribute order varies in the wild: property before content or after.
        m = (re.search(
                rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE)
             or re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
                html, re.IGNORECASE))
        if m:
            out[prop] = m.group(1)
    m = (re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
         or re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', html, re.IGNORECASE))
    if m and "canonical_url" not in out:
        out["canonical_url"] = m.group(1)
    # Canonicalise messy date_published / marking into structured, filterable
    # forms (meta['date_norm'], meta['marking_norm']) before storage.
    normalize_mod.apply_normalization(out)
    return out


def store_norm_columns(conn, clip_id: int, meta: dict) -> None:
    """Project a clip's normalised date/marking onto its dedicated columns.

    Call right after the INSERT, with the same meta dict that went into
    meta_json. Cheap UPDATE rather than threading four extra params through
    every INSERT statement; keeps the column values and meta_json in lock-step.
    """
    write_norm_columns(conn, clip_id, normalize_mod.columns_from_meta(meta))


# ---------------------------------------------------------------------------
# Clip ingestion
# ---------------------------------------------------------------------------

@app.route("/api/clip", methods=["POST"])
def api_clip():
    """
    Body JSON:
      {
        "html": "...",                    # required
        "title": "...",
        "url": "...",
        "source": "userscript-article" | "userscript-full" | ...,
        "tags": ["a", "b"],
        "meta": { ... }                   # arbitrary extra metadata
      }
    """
    body = request.get_json(force=True)
    html = body.get("html") or ""
    if not html:
        return jsonify({"error": "html required"}), 400
    html = sanitize_html(html)

    title = (body.get("title") or "Untitled").strip()
    url = body.get("url") or ""
    source = body.get("source") or "userscript"
    tags = body.get("tags") or []
    note = (body.get("note") or "").strip() or None
    meta = extract_meta(body.get("meta") or {}, html)
    canonical = meta.get("canonical_url") or url

    text = search_engine.html_to_text(html)
    wc = word_count(text)
    rm = reading_minutes(text)
    content_hash = sha256_bytes(text.encode("utf-8"))

    # Dedupe (unless force=true): same content_hash, or an attached PDF whose
    # sha already backs an existing clip's primary PDF.
    attachments = body.get("attachments") or []
    incoming_primary_sha = None
    for att in attachments:
        if (att.get("mime") == "application/pdf") or \
           (att.get("filename", "").lower().endswith(".pdf")):
            try:
                incoming_primary_sha = sha256_bytes(base64.b64decode(att["b64"]))
            except Exception:
                pass
            break

    conn = connect()
    try:
        if not body.get("force"):
            # Pages with no extractable text (image-only clips) all hash the
            # same; don't let that produce false duplicate matches.
            dup = find_duplicate(conn, content_hash=content_hash if text else None,
                                 primary_sha=incoming_primary_sha)
            if dup:
                return jsonify({"ok": False, "duplicate": dup,
                                "message": "Duplicate of clip #%d" % dup["id"]}), 200
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO clips(url, canonical_url, title, captured_at, source,
                                  content_html, content_text, content_hash,
                                  word_count, reading_minutes, meta_json, user_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (url, canonical, title, now_ts(), source, html, text,
                 content_hash, wc, rm, json.dumps(meta), note),
            )
            clip_id = cur.lastrowid
            store_norm_columns(conn, clip_id, meta)

            # Pull base64 images out of the HTML into the assets table.
            new_html, extracted = rewrite_data_uri_images(conn, clip_id, html)
            if extracted:
                conn.execute(
                    "UPDATE clips SET content_html = ? WHERE id = ?",
                    (new_html, clip_id),
                )

            # Attachments: optional array of {filename, mime, b64} entries.
            # Each one is decoded, stored in `assets` (deduped by sha256),
            # and linked to the clip with role='attachment' (or
            # 'pdf-primary' for the first PDF, so the viewer renders it).
            attached_count = 0
            saw_pdf_primary = False
            for att in attachments:
                try:
                    raw = base64.b64decode(att["b64"])
                except Exception:
                    log.warning("attachment %s: bad base64, skipping", att.get("filename"))
                    continue
                if not raw:
                    continue
                mime = att.get("mime") or "application/octet-stream"
                filename = att.get("filename") or "attachment"
                sha = store_asset(conn, raw, mime)
                # First PDF on the clip is the "primary" so the viewer iframes it.
                role = "attachment"
                if mime == "application/pdf" and not saw_pdf_primary:
                    role = "pdf-primary"
                    saw_pdf_primary = True
                conn.execute(
                    "INSERT OR IGNORE INTO clip_assets(clip_id, asset_sha, role, filename) "
                    "VALUES (?, ?, ?, ?)",
                    (clip_id, sha, role, filename),
                )
                attached_count += 1

            if tags:
                upsert_tags(conn, clip_id, tags)
        return jsonify({"ok": True, "id": clip_id,
                        "extracted_images": extracted,
                        "attachments_stored": attached_count})
    except Exception as e:
        log.exception("clip failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/attach-pdf", methods=["POST"])
def api_attach_pdf():
    """Multipart: file=<pdf>, title=, url=, tags=<comma-separated>."""
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    pdf_bytes = f.read()
    if not pdf_bytes:
        return jsonify({"error": "empty file"}), 400

    title = (request.form.get("title") or f.filename or "PDF").strip()
    url = request.form.get("url") or ""
    tags = [t.strip() for t in (request.form.get("tags") or "").split(",") if t.strip()]
    note = (request.form.get("note") or "").strip() or None
    meta = {"filename": f.filename, "is_pdf": True}
    # Optional extra metadata passed in as JSON (e.g. extractor output).
    extra_meta_raw = request.form.get("meta")
    if extra_meta_raw:
        try:
            extra = json.loads(extra_meta_raw)
            if isinstance(extra, dict):
                meta.update(extra)
        except Exception:
            log.warning("attach-pdf: ignoring unparseable meta JSON")
    normalize_mod.apply_normalization(meta)

    conn = connect()
    try:
        pdf_sha = sha256_bytes(pdf_bytes)
        if request.form.get("force") not in ("1", "true", "True"):
            dup = find_duplicate(conn, content_hash=pdf_sha, primary_sha=pdf_sha)
            if dup:
                return jsonify({"ok": False, "duplicate": dup,
                                "message": "Duplicate of clip #%d" % dup["id"]}), 200
        pdf_text = search_engine.pdf_to_text(pdf_bytes)
        ctext, wc, rm = pdf_content_text(title, pdf_text)
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO clips(url, canonical_url, title, captured_at, source,
                                  content_html, content_text, content_hash,
                                  word_count, reading_minutes, meta_json, user_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (url, url, title, now_ts(), "userscript-pdf",
                 f'<p>PDF attachment: <strong>{html_escape(title)}</strong></p>',
                 ctext, pdf_sha, wc, rm, json.dumps(meta), note),
            )
            clip_id = cur.lastrowid
            store_norm_columns(conn, clip_id, meta)
            sha = store_asset(conn, pdf_bytes, "application/pdf")
            warm_asset_text(conn, sha, pdf_text)
            conn.execute(
                "INSERT INTO clip_assets(clip_id, asset_sha, role, filename) "
                "VALUES (?, ?, 'pdf-primary', ?)",
                (clip_id, sha, f.filename or "document.pdf"),
            )
            if tags:
                upsert_tags(conn, clip_id, tags)
        return jsonify({"ok": True, "id": clip_id})
    except Exception as e:
        log.exception("attach-pdf failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read / search / edit
# ---------------------------------------------------------------------------

@app.route("/api/clips")
def api_clips():
    """Query params: q=, tag=, limit=, offset=, has_pdf=1"""
    q = (request.args.get("q") or "").strip()
    tag = (request.args.get("tag") or "").strip()
    limit = int_arg(request.args, "limit", 50, lo=1, hi=200)
    offset = int_arg(request.args, "offset", 0)
    has_pdf = request.args.get("has_pdf") == "1"
    # Structured filters over the normalised projection columns (see
    # normalize.py). marking='*' means "any classification present".
    marking = (request.args.get("marking") or "").strip()
    published_from = (request.args.get("published_from") or "").strip()
    published_to = (request.args.get("published_to") or "").strip()

    conn = connect()
    try:
        params: list = []
        # Explicit column list: the list view never shows content, and c.*
        # dragged the whole library's content_html/content_text/embedding off
        # disk on every keystroke of the search box just to pop them again.
        cols = ("c.id, c.url, c.canonical_url, c.title, c.captured_at, "
                "c.source, c.content_hash, c.word_count, c.reading_minutes, "
                "c.language, c.meta_json, c.user_note, c.date_published_norm, "
                "c.date_precision, c.marking_class, c.marking_caveats")
        # When q is provided, join FTS and rank by BM25.
        if q:
            sql = f"""
                SELECT {cols}, bm25(clips_fts) AS rank,
                       snippet(clips_fts, 1, '<mark>', '</mark>', '…', 16) AS snippet
                FROM clips_fts JOIN clips c ON c.id = clips_fts.rowid
                WHERE clips_fts MATCH ?
            """
            params.append(q)
        else:
            sql = f"SELECT {cols}, NULL AS rank, NULL AS snippet FROM clips c WHERE 1=1"

        if tag:
            sql += " AND c.id IN (SELECT clip_id FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id WHERE t.name = ? COLLATE NOCASE)"
            params.append(tag)
        for key, negate in (("has", False), ("missing", True)):
            for field in request.args.getlist(key):
                if field in ("tag", "tags"):
                    expr = "EXISTS (SELECT 1 FROM clip_tags ct WHERE ct.clip_id = c.id)"
                elif field in ("title", "url"):
                    expr = f"TRIM(COALESCE(c.{field}, '')) != ''"
                else:
                    expr = ("EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.meta_json) "
                            "THEN c.meta_json ELSE '{}' END) m WHERE m.key = ? "
                            "AND m.value IS NOT NULL AND TRIM(CAST(m.value AS TEXT)) "
                            "NOT IN ('', '[]', '{}', 'null'))")
                    params.append(field)
                sql += " AND " + ("NOT (" + expr + ")" if negate else "(" + expr + ")")
        if has_pdf:
            sql += " AND c.id IN (SELECT clip_id FROM clip_assets WHERE role = 'pdf-primary')"
        if marking:
            if marking == "*":
                sql += " AND c.marking_class IS NOT NULL"
            else:
                sql += " AND c.marking_class = ? COLLATE NOCASE"
                params.append(marking)
        # date_published_norm is a sortable ISO date, so string comparison is a
        # correct range test (day precision for the inclusive upper bound).
        if published_from:
            sql += " AND c.date_published_norm >= ?"
            params.append(published_from)
        if published_to:
            sql += " AND c.date_published_norm <= ?"
            params.append(published_to)

        sql += " ORDER BY " + ("rank" if q else "c.captured_at DESC") + " LIMIT ? OFFSET ?"
        params += [limit, offset]
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if not q:
                raise
            # The raw text wasn't valid FTS5 syntax (unbalanced quote, stray
            # operator like `c++`). Retry with every token quoted so it's
            # treated literally instead of 500ing.
            params[0] = fts_quote(q)
            rows = conn.execute(sql, params).fetchall()

        # Batch the per-clip decorations (tags, PDF badge): one query each for
        # the whole page instead of two per row.
        ids = [r["id"] for r in rows]
        tags_by_clip: dict[int, list[str]] = {}
        pdf_ids: set[int] = set()
        if ids:
            ph = ",".join("?" * len(ids))
            for tr in conn.execute(
                    f"SELECT ct.clip_id, t.name FROM clip_tags ct "
                    f"JOIN tags t ON t.id = ct.tag_id "
                    f"WHERE ct.clip_id IN ({ph}) ORDER BY t.name", ids):
                tags_by_clip.setdefault(tr["clip_id"], []).append(tr["name"])
            pdf_ids = {pr["clip_id"] for pr in conn.execute(
                f"SELECT DISTINCT clip_id FROM clip_assets "
                f"WHERE role = 'pdf-primary' AND clip_id IN ({ph})", ids)}

        results = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.pop("meta_json") or "{}")
            except Exception:
                d["meta"] = {}
            d["tags"] = tags_by_clip.get(r["id"], [])
            d["has_pdf"] = r["id"] in pdf_ids
            results.append(d)
        return jsonify({"clips": results, "limit": limit, "offset": offset})
    finally:
        conn.close()


@app.route("/api/clips/<int:clip_id>")
def api_clip_detail(clip_id):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if not r:
            abort(404)
        d = dict(r)
        d.pop("embedding", None)
        try:
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
        except Exception:
            d["meta"] = {}
        d["tags"] = [
            row["name"] for row in conn.execute(
                "SELECT t.name FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id "
                "WHERE ct.clip_id = ? ORDER BY t.name", (clip_id,))
        ]
        d["assets"] = [
            dict(row) for row in conn.execute(
                "SELECT ca.role, ca.filename, ca.asset_sha, a.mime, a.byte_size "
                "FROM clip_assets ca JOIN assets a ON a.sha256 = ca.asset_sha "
                "WHERE ca.clip_id = ?", (clip_id,))
        ]
        d["annotations"] = [
            dict(row) for row in conn.execute(
                "SELECT * FROM annotations WHERE clip_id = ? ORDER BY id", (clip_id,))
        ]
        return jsonify(d)
    finally:
        conn.close()


@app.route("/api/clips/<int:clip_id>", methods=["PATCH"])
def api_clip_update(clip_id):
    body = request.get_json(force=True)
    conn = connect()
    try:
        with transaction(conn):
            current = conn.execute(
                "SELECT title, content_html FROM clips WHERE id = ?", (clip_id,)
            ).fetchone()
            if not current:
                abort(404)
            if "content_html" in body:
                body["content_html"] = sanitize_html(body["content_html"] or "")
            updates = []
            params = []
            for field in ("title", "content_html"):
                if field in body:
                    updates.append(f"{field} = ?")
                    params.append(body[field])
            if "content_html" in body:
                text = search_engine.html_to_text(body["content_html"])
                updates += ["content_text = ?", "content_hash = ?",
                            "word_count = ?", "reading_minutes = ?"]
                params += [text, sha256_bytes(text.encode("utf-8")),
                           word_count(text), reading_minutes(text)]
            if updates:
                # Write the old version into revisions before changing it.
                # Only when title/content actually change - tag-only saves
                # used to spam an identical revision per save.
                conn.execute(
                    "INSERT INTO revisions(clip_id, saved_at, title, content_html, note) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (clip_id, now_ts(), current["title"], current["content_html"],
                     body.get("revision_note", "")),
                )
                params.append(clip_id)
                conn.execute(f"UPDATE clips SET {', '.join(updates)} WHERE id = ?", params)
                # The AFTER UPDATE trigger handles title/content_text in FTS,
                # but we lose the tags column - refresh manually.
                refresh_fts_tags(conn, clip_id)
            if "title" in body:
                row = conn.execute("SELECT meta_json FROM clips WHERE id = ?", (clip_id,)).fetchone()
                meta = json.loads(row["meta_json"] or "{}")
                meta["title"] = body["title"]
                conn.execute("UPDATE clips SET meta_json = ? WHERE id = ?", (json.dumps(meta), clip_id))
            if "tags" in body:
                upsert_tags(conn, clip_id, body["tags"])
            # The user note edits in place like tags (no revision row): it's a
            # margin comment on the clip, not part of its versioned content. The
            # AFTER UPDATE trigger reindexes FTS but blanks the tags column, so
            # refresh to restore tags (and write the new note) into clips_fts.
            if "note" in body:
                conn.execute("UPDATE clips SET user_note = ? WHERE id = ?",
                             ((body.get("note") or "").strip() or None, clip_id))
                refresh_fts_tags(conn, clip_id)
            # Editable metadata (marking, date_published, report_number, url).
            # Merge onto stored meta_json, re-normalise so the structured
            # date/marking projections + filter columns stay in lock-step, and
            # write both back. Not versioned - descriptive metadata, not content.
            if "meta" in body and isinstance(body["meta"], dict):
                incoming = body["meta"]
                row = conn.execute("SELECT meta_json FROM clips WHERE id = ?",
                                   (clip_id,)).fetchone()
                try:
                    meta = json.loads(row["meta_json"] or "{}")
                except Exception:
                    meta = {}
                for k in _EDITABLE_META_FIELDS:
                    if k not in incoming:
                        continue
                    v = (incoming.get(k) or "").strip()
                    if v:
                        meta[k] = v
                    else:
                        meta.pop(k, None)   # blank clears the field
                normalize_mod.apply_normalization(meta)
                write_norm_columns(conn, clip_id,
                                   normalize_mod.columns_from_meta(meta),
                                   meta_json=json.dumps(meta))
                if "url" in incoming:
                    newurl = (incoming.get("url") or "").strip()
                    conn.execute(
                        "UPDATE clips SET url = ?, canonical_url = ? WHERE id = ?",
                        (newurl, newurl, clip_id))
        refresh_fts_tags(conn, clip_id)
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/clips/<int:clip_id>", methods=["DELETE"])
def api_clip_delete(clip_id):
    conn = connect()
    try:
        with transaction(conn):
            # 404 for a missing id, like PATCH and the annotation POST do.
            if not conn.execute("SELECT 1 FROM clips WHERE id = ?",
                                (clip_id,)).fetchone():
                abort(404)
            conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
            # Orphan cleanup - assets and tags that lost their last reference.
            cleanup_orphan_assets(conn)
            gc_orphan_tags(conn)
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/clips/bulk", methods=["POST"])
def api_clips_bulk():
    """Bulk operations over multiple clips. Body JSON:
        { ids: [1,2,3], op: "delete" }
        { ids: [...], op: "add_tags",    tags: ["a","b"] }
        { ids: [...], op: "remove_tags", tags: ["a"] }
        { ids: [...], op: "set_tags",    tags: ["a","b"] }   # replace
    Returns { ok, affected }."""
    body = request.get_json(force=True)
    ids = body.get("ids") or []
    op = body.get("op")
    tags = [t.strip() for t in (body.get("tags") or []) if t and t.strip()]
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids required"}), 400
    # Coerce to ints defensively.
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400

    conn = connect()
    try:
        with transaction(conn):
            if op == "delete":
                qs = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM clips WHERE id IN ({qs})", ids)
                cleanup_orphan_assets(conn)
                gc_orphan_tags(conn)
            elif op in ("add_tags", "remove_tags", "set_tags"):
                if op != "remove_tags" and not tags:
                    return jsonify({"error": "tags required"}), 400
                for cid in ids:
                    if not conn.execute("SELECT 1 FROM clips WHERE id = ?", (cid,)).fetchone():
                        continue
                    current = [r["name"] for r in conn.execute(
                        "SELECT t.name FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id "
                        "WHERE ct.clip_id = ?", (cid,))]
                    cur_lower = {t.lower() for t in current}
                    if op == "set_tags":
                        new_tags = tags
                    elif op == "add_tags":
                        new_tags = current + [t for t in tags if t.lower() not in cur_lower]
                    else:  # remove_tags
                        drop = {t.lower() for t in tags}
                        new_tags = [t for t in current if t.lower() not in drop]
                    upsert_tags(conn, cid, new_tags)
            else:
                return jsonify({"error": "unknown op: %s" % op}), 400
        return jsonify({"ok": True, "affected": len(ids)})
    except Exception as e:
        log.exception("bulk op failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/tags")
def api_tags():
    conn = connect()
    try:
        # HAVING n > 0: orphan tags are garbage-collected on writes, but
        # keep zero-use rows out of autocomplete on older DBs regardless.
        rows = conn.execute("""
            SELECT t.name, COUNT(ct.clip_id) AS n
            FROM tags t LEFT JOIN clip_tags ct ON ct.tag_id = t.id
            GROUP BY t.id HAVING n > 0 ORDER BY n DESC, t.name
        """).fetchall()
        return jsonify({"tags": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/markings")
def api_markings():
    """Distinct classification levels in the library, with clip counts, for the
    library filter dropdown. Drawn from the indexed marking_class column."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT marking_class AS name, COUNT(*) AS n FROM clips "
            "WHERE marking_class IS NOT NULL AND marking_class != '' "
            "GROUP BY marking_class ORDER BY n DESC, name").fetchall()
        return jsonify({"markings": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/tags/bulk", methods=["POST"])
def api_tags_bulk():
    """Manage the tag vocabulary itself (not clip membership). Body JSON:
        { op: "rename", from: "old", to: "new" }
        { op: "merge",  names: ["a","b"], into: "target" }
        { op: "delete", names: ["a","b"] }
    rename/merge fold into an existing target when the new name matches one
    (by name or normalised key); otherwise they create/rename it. delete
    unlinks the tags from every clip and drops them. Returns { ok, ... }
    with op-specific counts."""
    body = request.get_json(force=True, silent=True) or {}
    op = body.get("op")
    # Validate before opening a transaction.
    if op == "rename":
        frm, to = (body.get("from") or "").strip(), (body.get("to") or "").strip()
        if not frm or not to:
            return jsonify({"error": "rename needs 'from' and 'to'"}), 400
    elif op == "merge":
        names = [n for n in (body.get("names") or []) if (n or "").strip()]
        into = (body.get("into") or "").strip()
        if not names or not into:
            return jsonify({"error": "merge needs 'names' and 'into'"}), 400
    elif op == "delete":
        names = [n for n in (body.get("names") or []) if (n or "").strip()]
        if not names:
            return jsonify({"error": "delete needs 'names'"}), 400
    else:
        return jsonify({"error": "unknown op: %s" % op}), 400

    conn = connect()
    try:
        with transaction(conn):
            if op == "rename":
                res = rename_tag(conn, frm, to)
            elif op == "merge":
                res = merge_tags(conn, names, into)
            else:  # delete
                res = delete_tags(conn, names)
        return jsonify({"ok": True, "op": op, **res})
    except Exception as e:
        log.exception("tag bulk op failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/tags/cleanup", methods=["GET", "POST"])
def api_tags_cleanup():
    """Tag-vocabulary cleanup candidates (see tagcleanup.py).

        GET                    -> {merge:[{sig,tags,target,reason,detail,score}],
                                   format:[{sig,tag,suggest,detail}]}
        POST {dismiss: "<sig>"} -> hide a candidate permanently -> {ok, dismissed}

    The candidates are conservative and high-precision; acting on one is done
    through the existing /api/tags/bulk merge (or rename) operations, after which
    the candidate disappears on its own. Dismiss is for the ones you want to keep
    as-is."""
    conn = connect()
    try:
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            sig = (body.get("dismiss") or "").strip()
            if not sig:
                return jsonify({"error": "dismiss signature required"}), 400
            with transaction(conn):
                tagcleanup_mod.dismiss(conn, sig)
            return jsonify({"ok": True, "dismissed": sig})
        return jsonify(tagcleanup_mod.cleanup_report(conn))
    finally:
        conn.close()


@app.route("/api/tags/suggest", methods=["POST"])
def api_tags_suggest():
    """Offline tag suggestions. Body JSON:
        { text: "...", title: "...", note: "...",
          chosen: ["a","b"], exclude_clip_id: 12, limit: 8,
          suggest_new: true, max_new: 3,
          content_weight, predict_weight, cooccur_weight,
          keyphrase_weight, min_score_frac }
    Returns { suggestions: [{tag, score, why, new?}, ...] }.

    Core inputs optional. text+title drive content-similarity and term->tag
    prediction, chosen drives co-occurrence; suggest_new toggles
    keyphrase-based NEW-tag proposals. The *_weight / max_new / min_score_frac
    knobs are optional tuning overrides - omit them to use suggest_tags'
    defaults. When exclude_clip_id is given, the document context is assembled
    SERVER-SIDE from the stored clip - title, content text, the user note, and
    cached attachment text (so PDF clips, whose content_text is just the title,
    get real suggestions). Client-sent text/note is only a fallback then.
    `note` is treated like the title (see suggest_tags)."""
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "")
    title = (body.get("title") or "")
    note = (body.get("note") or "")
    chosen = body.get("chosen") or []
    exclude_clip_id = body.get("exclude_clip_id")
    limit = int_arg(body, "limit", 12, lo=1, hi=25)

    # Optional tuning overrides - only forwarded when the caller sent them,
    # so suggest_tags' own defaults stay authoritative otherwise.
    opts: dict = {}
    for name in ("content_weight", "predict_weight", "cooccur_weight",
                 "keyphrase_weight", "mention_weight"):
        if name in body:
            opts[name] = float_arg(body, name, 1.0, lo=0.0, hi=10.0)
    if "min_score_frac" in body:
        opts["min_score_frac"] = float_arg(body, "min_score_frac", 0.25,
                                            lo=0.0, hi=1.0)
    if "max_new" in body:
        opts["max_new"] = int_arg(body, "max_new", 3, lo=0, hi=25)
    if "suggest_new" in body:
        opts["suggest_new"] = bool_arg(body.get("suggest_new"))
    if "use_embeddings" in body:
        opts["use_embeddings"] = bool_arg(body.get("use_embeddings"))
    if "embed_blend" in body:
        opts["embed_blend"] = float_arg(body, "embed_blend", 0.5, lo=0.0, hi=1.0)
    # Embedding floor: a request value wins; otherwise the persisted slider
    # setting is applied below (once the connection is open); else the default.
    if "min_embed_sim" in body:
        opts["min_embed_sim"] = suggest_mod.clamp_embed_floor(body.get("min_embed_sim"))
    if "code_boost" in body:
        opts["code_boost"] = float_arg(body, "code_boost", 2.5, lo=1.0, hi=10.0)
    if "code_frac_cap" in body:
        opts["code_frac_cap"] = float_arg(body, "code_frac_cap", 0.5, lo=0.0, hi=1.0)
    # Code regex: per-request override, else a server-wide default from the
    # environment (so an operator can set one designation pattern globally).
    code_regex = body.get("code_regex")
    if code_regex is None:
        code_regex = os.environ.get("CLIPPER_CODE_REGEX")
    if code_regex:
        opts["code_regex"] = str(code_regex)

    conn = connect()
    try:
        # Apply the persisted embedding-floor slider when the request didn't
        # set one explicitly (request override > saved setting > code default).
        if "min_embed_sim" not in opts:
            saved_floor = get_setting(conn, "embed_floor")
            if saved_floor is not None:
                opts["min_embed_sim"] = suggest_mod.clamp_embed_floor(saved_floor)
        if exclude_clip_id is not None:
            row = conn.execute(
                "SELECT title, url, content_text, user_note FROM clips WHERE id = ?",
                (exclude_clip_id,)).fetchone()
            if row:
                title = title or row["title"] or ""
                # The clip's stored note feeds suggestions like its title; a
                # note sent on the request (live editing) takes precedence.
                if not note:
                    note = row["user_note"] or ""
                search_engine.ensure_cache(conn)  # asset_text may not exist yet
                parts = [row["content_text"] or ""]
                for a in conn.execute(
                    "SELECT ca.asset_sha, ca.filename, asset.mime "
                    "FROM clip_assets ca JOIN assets asset ON asset.sha256 = ca.asset_sha "
                    "WHERE ca.clip_id = ? AND ca.role IN ('pdf-primary','attachment')",
                        (exclude_clip_id,)):
                    t = search_engine.extract_asset_text(
                        conn, a["asset_sha"], a["mime"], a["filename"] or "")
                    if t:
                        parts.append(t[:20000])
                assembled = "\n".join(p for p in parts if p)
                if assembled.strip():
                    text = assembled
        dbg: dict = {}
        suggestions = suggest_mod.suggest_tags(
            conn, text=text[:40000], title=title, note=note[:4000],
            chosen_tags=chosen, exclude_clip_id=exclude_clip_id,
            limit=limit, _debug=dbg, **opts,
        )
        # `ranker` tells the UI/demo whether the semantic or lexical path ran.
        return jsonify({"suggestions": suggestions,
                        "ranker": dbg.get("ranker", "fts")})
    except Exception as e:
        log.exception("tag suggest failed")
        return jsonify({"error": str(e), "suggestions": []}), 500
    finally:
        conn.close()


@app.route("/api/settings/embed-floor", methods=["GET", "POST"])
def api_embed_floor():
    """The semantic on-/off-topic floor (`min_embed_sim`) as a persisted slider.

        GET                    -> {floor, min, max, default, source}
        POST {floor: 0.90}     -> clamp to [min,max], persist -> {floor, source:'set'}
        POST {autotune: true}  -> run the conservative autotune, persist the
                                  recommendation -> {floor, source:'autotune', autotune:{table,...}}

    A higher floor admits only closer clips as tag donors (less cross-type
    bleed, fewer suggestions). The autotune climbs for precision and stops just
    before a genuine donor would be starved (biased high; see
    suggest.autotune_embed_floor)."""
    meta = {"min": suggest_mod.EMBED_FLOOR_MIN, "max": suggest_mod.EMBED_FLOOR_MAX,
            "default": suggest_mod.EMBED_FLOOR_DEFAULT}
    conn = connect()
    try:
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            if bool_arg(body.get("autotune"), default=False):
                rec = suggest_mod.autotune_embed_floor(conn)
                floor = suggest_mod.clamp_embed_floor(rec["recommended"])
                with transaction(conn):
                    set_setting(conn, "embed_floor", floor)
                return jsonify({"floor": floor, "source": "autotune",
                                "autotune": rec, **meta})
            floor = suggest_mod.clamp_embed_floor(body.get("floor"))
            with transaction(conn):
                set_setting(conn, "embed_floor", floor)
            return jsonify({"floor": floor, "source": "set", **meta})
        saved = get_setting(conn, "embed_floor")
        floor = (suggest_mod.clamp_embed_floor(saved) if saved is not None
                 else suggest_mod.EMBED_FLOOR_DEFAULT)
        return jsonify({"floor": floor,
                        "source": "setting" if saved is not None else "default", **meta})
    finally:
        conn.close()


@app.route("/api/suggest/blocklist", methods=["GET", "POST", "DELETE"])
def api_suggest_blocklist():
    """Manage the suggestion blocklist - keywords the tag-suggester must never
    propose again (matched by normalised key, so variants are covered too).

        GET    -> { keywords: [{keyword, created_at}, ...] }
        POST   { keyword: "x" }  -> add   -> { ok, keyword }
        DELETE { keyword: "x" }  -> remove -> { ok, removed }

    The add path is what the userscript / viewer call when the user bans a
    suggested keyword. It only filters what gets PROPOSED; tags already on
    clips, and hand-typed tags, are untouched."""
    conn = connect()
    try:
        if request.method == "GET":
            return jsonify({"keywords": list_blocked_keywords(conn)})

        body = request.get_json(force=True, silent=True) or {}
        keyword = (body.get("keyword") or "").strip()
        if not keyword:
            return jsonify({"error": "keyword required"}), 400

        if request.method == "POST":
            with transaction(conn):
                stored = add_blocked_keyword(conn, keyword)
            if stored is None:
                return jsonify({"error": "keyword not usable"}), 400
            return jsonify({"ok": True, "keyword": stored})
        else:  # DELETE
            with transaction(conn):
                removed = remove_blocked_keyword(conn, keyword)
            return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        log.exception("blocklist op failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Outlook (classic desktop) import - optional, isolated. The COM-facing code
# lives in outlook.py and is imported LAZILY inside these handlers, so any
# problem with it (pywin32 absent, Outlook unreachable, even an import error)
# is contained to /api/outlook/* and never affects the rest of the service.
# ---------------------------------------------------------------------------

# Setting key: whether an Outlook import flags its clips for tag review (and
# the ribbon button pops the review window). Default on; stored in app_settings.
_REVIEW_SETTING = "outlook_review"


def _review_enabled(conn) -> bool:
    return get_setting(conn, _REVIEW_SETTING, "1") != "0"


def _att_is_pdf(att: dict) -> bool:
    return (att.get("mime") == "application/pdf"
            or (att.get("filename") or "").lower().endswith(".pdf"))


def _outlook_meta(item: dict) -> dict:
    """Email provenance for a clip's meta_json. Sender goes here for display
    only - deliberately kept out of the title/tag-suggestion context, where
    author names make poor tags."""
    return {
        "source": "outlook",
        "outlook_entry_id": item.get("entry_id"),
        "email_subject": item.get("subject"),
        "email_from_name": item.get("sender_name"),
        "email_from_address": item.get("sender_email"),
        "email_received": item.get("received_iso"),
    }


def _import_pdf_attachment(conn, att: dict, item: dict, force: bool,
                           review: bool = False) -> dict:
    """Import one PDF attachment as its own clip, with PDF metadata extraction
    and keyword auto-tagging - the same treatment an uploaded PDF gets. Deduped
    by the PDF's sha256. When `review`, the clip is flagged review_pending so it
    surfaces on the /review page for tag confirmation."""
    raw = att["bytes"]
    pdf_sha = sha256_bytes(raw)
    base = {"kind": "pdf", "filename": att.get("filename")}
    if not force:
        dup = find_duplicate(conn, content_hash=pdf_sha, primary_sha=pdf_sha)
        if dup:
            return {**base, "status": "duplicate", "duplicate": dup}
    pdf_text = search_engine.pdf_to_text(raw)
    meta = ingest_mod.extract_pdf_metadata(raw, pdf_text)
    meta.update(_outlook_meta(item))
    if review:
        meta["review_pending"] = True
    normalize_mod.apply_normalization(meta)
    title = (meta.get("title") or att.get("filename")
             or item.get("subject") or "PDF").strip()
    keywords = meta.get("keywords", [])
    ctext, wc, rm = pdf_content_text(title, pdf_text)
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO clips(url, canonical_url, title, captured_at, source,
               content_html, content_text, content_hash, word_count,
               reading_minutes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("", "", title, now_ts(), "outlook-pdf",
             f"<p>PDF: <strong>{html_escape(title)}</strong></p>",
             ctext, pdf_sha, wc, rm, json.dumps(meta)))
        clip_id = cur.lastrowid
        store_norm_columns(conn, clip_id, meta)
        sha = store_asset(conn, raw, "application/pdf")
        warm_asset_text(conn, sha, pdf_text)
        conn.execute(
            "INSERT INTO clip_assets(clip_id, asset_sha, role, filename) "
            "VALUES (?, ?, 'pdf-primary', ?)",
            (clip_id, sha, att.get("filename") or "document.pdf"))
        if keywords:
            upsert_tags(conn, clip_id, keywords)
    return {**base, "status": "imported", "id": clip_id, "auto_tags": keywords}


def _import_email_body(conn, item: dict, force: bool,
                       review: bool = False) -> dict:
    """Import the email body as an HTML clip. Non-PDF, non-inline attachments
    are attached to it; inline images already folded into the HTML as data URIs
    get pulled into assets by the usual extractor. Deduped by body text hash.
    When `review`, the clip is flagged review_pending for the /review page."""
    html = sanitize_html(item.get("html") or "")
    text = search_engine.html_to_text(html)
    if not text.strip():
        return {"kind": "body", "status": "skipped", "message": "empty body"}
    content_hash = sha256_bytes(text.encode("utf-8"))
    if not force:
        dup = find_duplicate(conn, content_hash=content_hash)
        if dup:
            return {"kind": "body", "status": "duplicate", "duplicate": dup}
    title = (item.get("subject") or "Email").strip()
    meta = _outlook_meta(item)
    if item.get("received_iso"):
        meta["article:published_time"] = item["received_iso"]
    if review:
        meta["review_pending"] = True
    # Email "published" date is the received timestamp; normalise it + marking.
    if item.get("received_iso") and not meta.get("date_published"):
        meta["date_published"] = item["received_iso"]
    normalize_mod.apply_normalization(meta)
    wc = word_count(text)
    rm = reading_minutes(text)
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO clips(url, canonical_url, title, captured_at, source,
               content_html, content_text, content_hash, word_count,
               reading_minutes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("", "", title, now_ts(), "outlook-email", html, text,
             content_hash, wc, rm, json.dumps(meta)))
        clip_id = cur.lastrowid
        store_norm_columns(conn, clip_id, meta)
        new_html, extracted = rewrite_data_uri_images(conn, clip_id, html)
        if extracted:
            conn.execute("UPDATE clips SET content_html = ? WHERE id = ?",
                         (new_html, clip_id))
        attached = 0
        for att in item.get("attachments", []):
            if att.get("inline") or _att_is_pdf(att):
                continue  # inline imgs are in the HTML; PDFs become own clips
            raw = att.get("bytes")
            if not raw:
                continue
            sha = store_asset(conn, raw, att.get("mime") or "application/octet-stream")
            conn.execute(
                "INSERT OR IGNORE INTO clip_assets(clip_id, asset_sha, role, filename) "
                "VALUES (?, ?, 'attachment', ?)",
                (clip_id, sha, att.get("filename") or "attachment"))
            attached += 1
    return {"kind": "body", "status": "imported", "id": clip_id,
            "attachments_stored": attached}


@app.route("/api/outlook/status")
def api_outlook_status():
    """Whether the Outlook bridge can run (pywin32 importable). Mirrors
    /api/embed/status; the UI uses it to show/hide the feature. Never 500s."""
    try:
        import outlook as outlook_mod
        return jsonify(outlook_mod.available())
    except Exception as e:
        return jsonify({"available": False, "reason": f"import failed: {e!r}"})


@app.route("/api/outlook/import", methods=["POST"])
def api_outlook_import():
    """Import one or more Outlook mail items into the library. Body JSON:
        { entry_ids: ["...", ...],   # Outlook EntryIDs (from the ribbon macro)
          include_body: false,       # also clip the email body as HTML
          force: false }             # bypass duplicate detection

    Each PDF attachment becomes its own clip (with metadata + keyword tags);
    with include_body, the body is an extra HTML clip carrying the non-PDF
    attachments. Returns 200 with a per-item result breakdown even when some
    items fail, so a bad EntryID or an attachment error never sinks the batch.

    `review` (optional) overrides the server's `outlook_review` setting for this
    call. When review is in effect, imported clips are flagged for tag review;
    the response carries `review: true` + `review_url` so the ribbon button can
    pop the tagging window, and `imported_ids` lists the new clips.
    """
    body = request.get_json(force=True, silent=True) or {}
    entry_ids = body.get("entry_ids")
    if isinstance(entry_ids, str):
        entry_ids = [entry_ids]
    entry_ids = [str(e) for e in (entry_ids or []) if e]
    if not entry_ids:
        return jsonify({"error": "entry_ids required"}), 400
    include_body = bool_arg(body.get("include_body"), default=False)
    force = bool_arg(body.get("force"), default=False)

    try:
        import outlook as outlook_mod
    except Exception as e:
        # Importing the bridge module itself failed - report cleanly, don't 500.
        log.exception("outlook module import failed")
        return jsonify({"error": f"outlook integration unavailable: {e}"}), 503

    items, fetch_errors = outlook_mod.fetch_items(entry_ids)
    results = []
    imported_ids = []
    conn = connect()
    try:
        review = (bool_arg(body.get("review")) if "review" in body
                  else _review_enabled(conn))
        for item in items:
            sub_results = []
            try:
                for att in item.get("attachments", []):
                    if att.get("inline") or not _att_is_pdf(att):
                        continue
                    sub_results.append(
                        _import_pdf_attachment(conn, att, item, force, review))
                if include_body:
                    sub_results.append(
                        _import_email_body(conn, item, force, review))
                if not sub_results:
                    sub_results.append({"status": "skipped",
                                        "message": "no PDF attachments"
                                        + ("" if include_body else "; enable body import")})
            except Exception as e:
                # Contain a single bad item; the rest of the batch still imports.
                log.exception("outlook import failed for %s", item.get("entry_id"))
                sub_results.append({"status": "error", "message": str(e)})
            for x in sub_results:
                if x.get("status") == "imported" and x.get("id"):
                    imported_ids.append(x["id"])
            results.append({"entry_id": item.get("entry_id"),
                            "subject": item.get("subject"), "items": sub_results})
    finally:
        conn.close()

    return jsonify({"ok": True, "imported": len(imported_ids),
                    "imported_ids": imported_ids,
                    "review": bool(review and imported_ids),
                    "review_url": "/review",
                    "results": results, "fetch_errors": fetch_errors})


# ---------------------------------------------------------------------------
# Tag-review queue - clips flagged review_pending (currently Outlook imports).
# The /review page presents each with the suggestion-backed tag widget so the
# user confirms/adds tags. The pending flag lives in meta_json, so no schema
# change and it travels with the clip.
# ---------------------------------------------------------------------------

def _meta_of(conn, clip_id: int) -> dict:
    row = conn.execute("SELECT meta_json FROM clips WHERE id = ?",
                       (clip_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["meta_json"] or "{}")
    except Exception:
        return {}


@app.route("/api/review/pending")
def api_review_pending():
    """List clips awaiting tag review, newest first. A cheap LIKE prefilter on
    meta_json then an exact check in Python (avoids depending on the JSON1
    extension)."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, source, url, content_text, captured_at, meta_json "
            "FROM clips WHERE meta_json LIKE '%review_pending%' "
            "ORDER BY captured_at DESC, id DESC").fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta_json"] or "{}")
            except Exception:
                meta = {}
            if not meta.get("review_pending"):
                continue
            tags = [t["name"] for t in conn.execute(
                "SELECT t.name FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id "
                "WHERE ct.clip_id = ? ORDER BY t.name", (r["id"],))]
            has_pdf = bool(conn.execute(
                "SELECT 1 FROM clip_assets WHERE clip_id = ? AND role = 'pdf-primary'",
                (r["id"],)).fetchone())
            out.append({
                "id": r["id"], "title": r["title"], "source": r["source"],
                "tags": tags, "has_pdf": has_pdf,
                "from": meta.get("email_from_name") or "",
                "excerpt": (r["content_text"] or "")[:200],
            })
        return jsonify({"pending": out})
    finally:
        conn.close()


@app.route("/api/clips/<int:clip_id>/reviewed", methods=["POST"])
def api_clip_reviewed(clip_id):
    """Clear a clip's review_pending flag (it's been tagged / dismissed)."""
    conn = connect()
    try:
        with transaction(conn):
            if not conn.execute("SELECT 1 FROM clips WHERE id = ?",
                                (clip_id,)).fetchone():
                abort(404)
            meta = _meta_of(conn, clip_id)
            meta.pop("review_pending", None)
            conn.execute("UPDATE clips SET meta_json = ? WHERE id = ?",
                         (json.dumps(meta), clip_id))
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Read/update app settings. Currently just `outlook_review` (bool).
        GET  -> { outlook_review: bool }
        POST { outlook_review: bool } -> persists, returns the new value."""
    conn = connect()
    try:
        if request.method == "POST":
            body = request.get_json(force=True, silent=True) or {}
            if "outlook_review" in body:
                with transaction(conn):
                    set_setting(conn, _REVIEW_SETTING,
                                "1" if bool_arg(body.get("outlook_review")) else "0")
        return jsonify({"outlook_review": _review_enabled(conn)})
    finally:
        conn.close()


@app.route("/review")
def viewer_review():
    return render_template("review.html")


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

@app.route("/api/clips/<int:clip_id>/annotations")
def api_annotation_list(clip_id):
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone():
            abort(404)
        rows = conn.execute(
            "SELECT * FROM annotations WHERE clip_id = ? ORDER BY id", (clip_id,)
        ).fetchall()
        return jsonify({"annotations": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/clips/<int:clip_id>/annotations", methods=["POST"])
def api_annotation_add(clip_id):
    body = request.get_json(force=True)
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone():
            abort(404)
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO annotations(clip_id, created_at, quote, prefix, suffix,
                                        pdf_page, pdf_rect, note, color)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clip_id, now_ts(),
                 body.get("quote"), body.get("prefix"), body.get("suffix"),
                 body.get("pdf_page"), json.dumps(body.get("pdf_rect")) if body.get("pdf_rect") else None,
                 body.get("note", ""), body.get("color", "#ffeb3b")),
            )
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        conn.close()


@app.route("/api/annotations/<int:anno_id>", methods=["DELETE"])
def api_annotation_delete(anno_id):
    conn = connect()
    try:
        with transaction(conn):
            if not conn.execute("SELECT 1 FROM annotations WHERE id = ?",
                                (anno_id,)).fetchone():
                abort(404)
            conn.execute("DELETE FROM annotations WHERE id = ?", (anno_id,))
        return jsonify({"ok": True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Asset serving
# ---------------------------------------------------------------------------

# MIME types we are willing to serve inline. Anything else (in particular
# text/html, which would execute as same-origin) is forced to download as
# an opaque blob. SVG can carry script, so it gets a no-script CSP.
_INLINE_MIME_PREFIXES = ("image/",)
_INLINE_MIMES = {"application/pdf", "text/plain"}


_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


@app.route("/asset/<sha>")
def asset(sha):
    # Content-addressed: the URL *is* the sha256 of the bytes, so the payload
    # for a given URL can never change. Cache it hard, and answer a
    # revalidation from the ETag alone - no DB read, no blob transfer.
    if sha in request.if_none_match:
        return "", 304, {"ETag": f'"{sha}"',
                         "Cache-Control": _ASSET_CACHE_CONTROL}
    conn = connect()
    try:
        row = conn.execute("SELECT bytes, mime FROM assets WHERE sha256 = ?", (sha,)).fetchone()
        if not row:
            abort(404)
        mime = (row["mime"] or "application/octet-stream").lower()
        inline = mime in _INLINE_MIMES or mime.startswith(_INLINE_MIME_PREFIXES)
        resp = send_file(
            BytesIO(row["bytes"]),
            mimetype=mime if inline else "application/octet-stream",
            as_attachment=not inline,
            download_name=sha[:12] + "_" + (mime.split("/")[-1]),
        )
        resp.set_etag(sha)
        resp.headers["Cache-Control"] = _ASSET_CACHE_CONTROL
        if mime == "image/svg+xml":
            resp.headers["Content-Security-Policy"] = \
                "default-src 'none'; style-src 'unsafe-inline'"
        return resp
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Advanced search (full-text over clips + attachment bodies)
# ---------------------------------------------------------------------------

def _opts_from_request(args) -> "search_engine.SearchOptions":
    return search_engine.SearchOptions(
        context_words=int_arg(args, "context", 30, lo=0, hi=500),
        default_op=(args.get("default_op", "OR") or "OR").upper(),
        added_from=args.get("added_from") or None,
        added_to=args.get("added_to") or None,
        published_from=args.get("published_from") or None,
        published_to=args.get("published_to") or None,
        output_format=args.get("format", "md"),
        max_results=int_arg(args, "max_results", 1000, lo=1, hi=5000),
    )


@app.route("/api/search")
def api_search():
    """Run an advanced search. Query params:
        q=<query>, context=<words>, default_op=OR|AND,
        added_from=, added_to=, published_from=, published_to=,
        format=md|txt, download=1 (force file response)
    Returns JSON {report, count, terms} unless download=1, which streams
    the report as a downloadable file.
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q required"}), 400
    opts = _opts_from_request(request.args)
    conn = connect()
    try:
        try:
            result = search_engine.search(conn, q, opts)
        except Exception as e:
            log.exception("search failed")
            return jsonify({"error": f"query error: {e}"}), 400
        report = search_engine.render_report(result, opts)
        count = len(result["results"])
        if request.args.get("download") == "1":
            ext = "txt" if opts.output_format == "txt" else "md"
            mime = "text/plain" if ext == "txt" else "text/markdown"
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            return send_file(
                BytesIO(report.encode("utf-8")), mimetype=mime,
                as_attachment=True, download_name=f"search-{stamp}.{ext}",
            )
        return jsonify({"report": report, "count": count, "terms": result["terms"]})
    finally:
        conn.close()


@app.route("/search")
def viewer_search():
    return render_template("search.html")


# ---------------------------------------------------------------------------
# Drag-and-drop file ingestion (PDF / MHTML) with preview-before-commit
# ---------------------------------------------------------------------------

# In-memory staging: token -> {created, kind, filename, bytes, html, meta, ...}
# Lost on restart by design. Swept on access; entries older than the TTL go.
_STAGE: dict[str, dict] = {}
_STAGE_TTL = 1800  # 30 minutes


def _sweep_stage():
    cutoff = time.time() - _STAGE_TTL
    for tok in [t for t, v in _STAGE.items() if v["created"] < cutoff]:
        _STAGE.pop(tok, None)


def _corrections_for_scope(conn, scope: str) -> dict:
    rows = conn.execute(
        "SELECT field, value FROM metadata_corrections WHERE scope = ?", (scope,)
    ).fetchall()
    return {r["field"]: r["value"] for r in rows}


def _scope_for(kind: str, meta: dict) -> str:
    """Decide the correction scope: host for HTML/MHTML, ext for PDF."""
    if kind == "mhtml":
        url = meta.get("url") or ""
        host = urlparse(url).hostname if url else ""
        return f"host:{host}" if host else "ext:mhtml"
    return "ext:pdf"


# How much body text to hand the tag-suggester from an upload, matching the
# userscript's webpage context (articleSuggestContext) so PDFs/MHTML get the
# same quality of suggestions a clipped page does.
_SUGGEST_TEXT_CHARS = 4000
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
# Boilerplate "titles" that tools stamp on exports - treated as no title at
# all, so we fall back to the document's visual headings instead.
_GENERIC_TITLE_RE = re.compile(
    r"^(untitled|document\s*\d*|doc\s*\d*|new\s+document|presentation|"
    r"powerpoint\s+presentation|slide\s*\d*|title|draft|final|report|"
    r"chapter\s*\d*|page\s*\d*|\d+)$", re.IGNORECASE)


def _is_generic_title(title: str) -> bool:
    """A title too generic to be useful (blank, 'Untitled', 'Document1',
    'Microsoft Word - x.docx', a bare filename, …)."""
    t = (title or "").strip()
    if len(t) < 4:
        return True
    low = t.lower()
    if "microsoft word" in low or low.endswith(
            (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt", ".rtf")):
        return True
    return bool(_GENERIC_TITLE_RE.match(t))


def _suggest_title(kind: str, filename: str, meta: dict, headings: str = "") -> str:
    """Title-weighted suggestion context for an upload.

    The tag suggester weights its `title` argument heavily - title terms are
    tripled for similarity and drive the keyphrase title + cross-field boosts,
    the same treatment a webpage's extracted <title> gets. An upload has no
    single page title, so we fold the title-like fields into one: the filename
    (usually the best descriptor of a PDF), the document's own title, and its
    report number. When the metadata title is missing or generic, the
    document's visual `headings` (largest-font text, see
    `ingest.extract_pdf_headings`) stand in for it. Author names are
    deliberately excluded: they make poor topical tags and, folded into the
    tripled title weight, leaked author surnames out as suggested tags. Other
    non-topical metadata (dates, page counts, classification markings) is
    excluded too. Fields are joined one per line so each stays a separate
    keyphrase candidate; blanks and case-insensitive duplicates are dropped."""
    bits = []
    if kind == "pdf":
        # An .mht/.mhtml filename is just an archive name; a PDF's filename is
        # usually the most descriptive thing about it.
        bits.append(_EXT_RE.sub("", filename or "").strip())
    title = (meta.get("title") or "").strip()
    if title and not _is_generic_title(title):
        bits.append(title)
    elif headings:
        bits.append(headings)        # blank/generic metadata title -> headings
    bits.append((meta.get("report_number") or "").strip())
    seen, out = set(), []
    for b in bits:
        for line in (b.split("\n") if b else []):
            line = line.strip()
            k = line.lower()
            if line and k not in seen:
                seen.add(k)
                out.append(line)
    return "\n".join(out)


def _suggest_text(meta: dict, body_text: str) -> str:
    """Body context for tag suggestion: the document body, prefixed with the
    PDF's authored abstract (Info /Subject) when present and not already at the
    body's head. The abstract is dense, on-topic prose that often lives only in
    the metadata or deep in the body - past the suggestion window - so folding
    it in front feeds the keyphrase and term->tag signals the document's own
    summary of what it's about."""
    body = (body_text or "")[:_SUGGEST_TEXT_CHARS]
    abstract = (meta.get("subject") or "").strip()
    if abstract and abstract.lower() not in body.lower():
        body = abstract + "\n\n" + body
    return body


@app.route("/api/upload/preview", methods=["POST"])
def api_upload_preview():
    """Accept a PDF or MHTML file, parse it, extract metadata, apply any
    remembered corrections, stage the result, and return a preview.
    The bytes are held in memory under a token until commit."""
    _sweep_stage()
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400
    fname = f.filename or "upload"
    lower = fname.lower()

    conn = connect()
    try:
        # Detect type by extension + sniffing.
        is_pdf = lower.endswith(".pdf") or raw[:5] == b"%PDF-"
        is_mhtml = (lower.endswith(".mht") or lower.endswith(".mhtml")
                    or b"Content-Type: multipart/related" in raw[:2000]
                    or b"MIME-Version:" in raw[:500])

        if is_pdf:
            pdf_text = search_engine.pdf_to_text(raw)
            meta = ingest_mod.extract_pdf_metadata(raw, pdf_text)
            kind = "pdf"
            body_html = None
            body_text = pdf_text
            text_preview = pdf_text[:1000]
            stage_content_hash = sha256_bytes(raw)
            stage_primary_sha = stage_content_hash
        elif is_mhtml:
            parsed = ingest_mod.parse_mhtml(raw)
            meta = ingest_mod.extract_html_metadata(parsed["html"], parsed["base_url"])
            meta["inlined_resources"] = parsed["inlined_resources"]
            kind = "mhtml"
            # Sanitize here (not at commit) so the staged HTML, the dedupe
            # hash, and what eventually gets stored are all consistent.
            body_html = sanitize_html(parsed["html"])
            mhtml_text = search_engine.html_to_text(body_html)
            body_text = mhtml_text
            text_preview = mhtml_text[:1000]
            # Empty-text archives would all hash identically; skip the
            # content-hash dedupe signal in that case.
            stage_content_hash = (sha256_bytes(mhtml_text.encode("utf-8"))
                                  if mhtml_text else None)
            stage_primary_sha = None
        else:
            return jsonify({"error": "unsupported file type; need .pdf or .mht/.mhtml"}), 400

        # Duplicate detection (reported in the preview so the UI can flag it;
        # the commit will skip unless forced).
        dup = find_duplicate(conn, content_hash=stage_content_hash,
                             primary_sha=stage_primary_sha)

        # Apply remembered corrections for this scope (don't override a value
        # the extractor actually found, unless the correction is for a field
        # the extractor left blank OR the user previously corrected it).
        scope = _scope_for(kind, meta)
        corrections = _corrections_for_scope(conn, scope) if kind != "pdf" else {}
        applied = {}
        for field, val in corrections.items():
            # Pre-fill corrected fields; mark them so the UI can show "remembered".
            if not meta.get(field):
                meta[field] = val
                applied[field] = val

        token = secrets.token_urlsafe(16)
        _STAGE[token] = {
            "created": time.time(),
            "kind": kind,
            "filename": fname,
            "bytes": raw,
            "html": body_html,
            "meta": meta,
            "scope": scope,
            "text_preview": text_preview,
            # Full extracted text, so commit doesn't re-parse the document
            # (and PDF commits can store it as content_text / asset_text).
            "body_text": body_text,
            "content_hash": stage_content_hash,
            "primary_sha": stage_primary_sha,
        }

        # Editable preview payload. We expose the standard fields plus the
        # candidate breakdown so the user understands where marking came from.
        editable = {
            "title": meta.get("title") or "",
            "url": meta.get("url") or "",
            "report_number": meta.get("report_number") or "",
            "date_published": meta.get("date_published") or "",
            "marking": meta.get("marking") or "",
        }

        # When a PDF has no usable metadata title, derive a title-like context
        # from its largest-font headings (cheap: only when actually needed).
        headings = ""
        if kind == "pdf" and _is_generic_title(meta.get("title")):
            try:
                headings = ingest_mod.extract_pdf_headings(raw, pdf_text)
            except Exception:
                log.warning("heading extraction failed", exc_info=True)

        return jsonify({
            "token": token,
            "kind": kind,
            "filename": fname,
            "byte_size": len(raw),
            "editable": editable,
            "remembered_fields": list(applied.keys()),
            "marking_candidates": meta.get("marking_candidates", {}),
            "extra": {k: meta[k] for k in ("page_count", "pdf_author",
                                           "inlined_resources", "metadata_extractor")
                      if k in meta},
            "text_preview": text_preview,
            # Suggestion context: body text (with the PDF abstract folded in) +
            # title-weighted fields (falling back to visual headings), so the
            # upload page gets the same quality of tag suggestions a clipped
            # webpage does.
            "suggest_text": _suggest_text(meta, body_text),
            "suggest_title": _suggest_title(kind, fname, meta, headings),
            # Keywords authored into the file's metadata are pre-applied as
            # tags (the user can remove any before committing), not merely
            # suggested.
            "auto_tags": meta.get("keywords", []),
            "duplicate": dup,
        })
    except Exception as e:
        log.exception("upload preview failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/upload/commit", methods=["POST"])
def api_upload_commit():
    """Commit a previously-previewed upload. Body JSON:
        { token, fields: {title,url,report_number,date_published,marking},
          tags: [...] }
    Records any field the user changed from the auto-extracted value as a
    correction for the file's scope."""
    _sweep_stage()
    body = request.get_json(force=True)
    token = body.get("token")
    staged = _STAGE.get(token)
    if not staged:
        return jsonify({"error": "unknown or expired token"}), 404

    fields = body.get("fields") or {}
    tags = body.get("tags") or []
    kind = staged["kind"]
    raw = staged["bytes"]
    meta = dict(staged["meta"])
    auto_editable = {
        "title": staged["meta"].get("title") or "",
        "url": staged["meta"].get("url") or "",
        "report_number": staged["meta"].get("report_number") or "",
        "date_published": staged["meta"].get("date_published") or "",
        "marking": staged["meta"].get("marking") or "",
    }

    # Merge the (possibly edited) fields back into meta, then re-normalise so a
    # user-corrected date/marking is reflected in the structured projections.
    for k in ("title", "url", "report_number", "date_published", "marking"):
        if k in fields:
            meta[k] = fields[k]
    normalize_mod.apply_normalization(meta)

    title = (fields.get("title") or staged["meta"].get("title")
             or staged["filename"]).strip()
    url = fields.get("url") or staged["meta"].get("url") or ""

    conn = connect()
    try:
        if not body.get("force"):
            dup = find_duplicate(conn, content_hash=staged.get("content_hash"),
                                 primary_sha=staged.get("primary_sha"))
            if dup:
                _STAGE.pop(token, None)
                return jsonify({"ok": False, "skipped": True, "duplicate": dup,
                                "message": "Skipped: duplicate of clip #%d" % dup["id"]}), 200
        with transaction(conn):
            if kind == "mhtml":
                html = staged["html"] or ""
                text = search_engine.html_to_text(html)
                cur = conn.execute(
                    """INSERT INTO clips(url, canonical_url, title, captured_at, source,
                       content_html, content_text, content_hash, word_count,
                       reading_minutes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (url, url, title, now_ts(), "upload-mhtml", html, text,
                     sha256_bytes(text.encode("utf-8")), word_count(text),
                     reading_minutes(text), json.dumps(meta)))
                clip_id = cur.lastrowid
                store_norm_columns(conn, clip_id, meta)
                # Pull inlined data-URI images into assets (reuse the helper).
                new_html, extracted = rewrite_data_uri_images(conn, clip_id, html)
                if extracted:
                    conn.execute("UPDATE clips SET content_html = ? WHERE id = ?",
                                 (new_html, clip_id))
            else:  # pdf
                pdf_text = staged.get("body_text") or ""
                ctext, wc, rm = pdf_content_text(title, pdf_text)
                cur = conn.execute(
                    """INSERT INTO clips(url, canonical_url, title, captured_at, source,
                       content_html, content_text, content_hash, word_count,
                       reading_minutes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (url, url, title, now_ts(), "upload-pdf",
                     f"<p>PDF: <strong>{html_escape(title)}</strong></p>",
                     ctext, sha256_bytes(raw), wc, rm,
                     json.dumps(meta)))
                clip_id = cur.lastrowid
                store_norm_columns(conn, clip_id, meta)
                sha = store_asset(conn, raw, "application/pdf")
                warm_asset_text(conn, sha, pdf_text)
                conn.execute(
                    "INSERT INTO clip_assets(clip_id, asset_sha, role, filename) "
                    "VALUES (?, ?, 'pdf-primary', ?)",
                    (clip_id, sha, staged["filename"]))

            if tags:
                upsert_tags(conn, clip_id, tags)

            # Remember any field the user changed from the auto value.
            scope = staged["scope"]
            for k in (() if kind == "pdf" else ("title", "url", "report_number", "date_published", "marking")):
                new_val = (fields.get(k) or "").strip()
                if new_val and new_val != (auto_editable.get(k) or "").strip():
                    conn.execute(
                        "INSERT INTO metadata_corrections(scope, field, value, updated_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(scope, field) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        (scope, k, new_val, now_ts()))
        _STAGE.pop(token, None)
        return jsonify({"ok": True, "id": clip_id})
    except Exception as e:
        log.exception("upload commit failed")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/upload/bulk", methods=["POST"])
def api_upload_bulk():
    """One file of a recursive folder ingest - the upload page's folder picker
    posts every PDF under the chosen root here, one request per file, so the
    UI can show live progress and one bad file can't sink the batch. Multipart:
        file=<pdf>              required
        relpath=<a/b/c.pdf>     path relative to the picked root (provenance)
        tags=<comma-separated>  extra tags for every imported clip (optional)
        review=1                flag the clip for the /review tagging queue

    No preview round-trip: the file is parsed, deduped (by the PDF bytes'
    sha256, like attach-pdf) and committed in one shot, with the same metadata
    extraction + keyword auto-tagging an uploaded or emailed PDF gets.
    Remembered ext:pdf corrections pre-fill fields the extractor left blank.
    Duplicates report the existing clip and are skipped - there is no force,
    so re-running a folder after adding files imports only the new ones.
    Malformed requests (missing/empty/non-PDF file) are 400; everything else
    answers 200 with {ok, status: 'imported'|'duplicate', ...}."""
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400
    relpath = (request.form.get("relpath") or f.filename or "").replace("\\", "/")
    fname = relpath.rsplit("/", 1)[-1] or "document.pdf"
    if not (fname.lower().endswith(".pdf") or raw[:5] == b"%PDF-"):
        return jsonify({"error": "not a PDF: %s" % fname}), 400
    shared_tags = [t.strip() for t in
                   (request.form.get("tags") or "").split(",") if t.strip()]
    review = request.form.get("review") in ("1", "true", "True")

    conn = connect()
    try:
        pdf_sha = sha256_bytes(raw)
        dup = find_duplicate(conn, content_hash=pdf_sha, primary_sha=pdf_sha)
        if dup:
            return jsonify({"ok": True, "status": "duplicate", "duplicate": dup,
                            "relpath": relpath})
        pdf_text = search_engine.pdf_to_text(raw)
        meta = ingest_mod.extract_pdf_metadata(raw, pdf_text)
        meta["filename"] = fname
        meta["is_pdf"] = True
        if relpath and relpath != fname:
            meta["relative_path"] = relpath   # where it sat under the root
        if review:
            meta["review_pending"] = True
        # Remembered PDF corrections fill fields the extractor left blank
        # (same rule as the upload preview).
        for field, val in _corrections_for_scope(conn, "ext:pdf").items():
            if not meta.get(field):
                meta[field] = val
        normalize_mod.apply_normalization(meta)
        # A blank/boilerplate metadata title falls back to the filename - for
        # a PDF the filename is usually the most descriptive thing about it.
        title = (meta.get("title") or "").strip()
        if _is_generic_title(title):
            title = _EXT_RE.sub("", fname).strip() or title or "PDF"
        keywords = meta.get("keywords", [])
        tags = keywords + [t for t in shared_tags
                           if t.lower() not in {k.lower() for k in keywords}]
        ctext, wc, rm = pdf_content_text(title, pdf_text)
        with transaction(conn):
            cur = conn.execute(
                """INSERT INTO clips(url, canonical_url, title, captured_at, source,
                   content_html, content_text, content_hash, word_count,
                   reading_minutes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ("", "", title, now_ts(), "upload-pdf",
                 f"<p>PDF: <strong>{html_escape(title)}</strong></p>",
                 ctext, pdf_sha, wc, rm, json.dumps(meta)))
            clip_id = cur.lastrowid
            store_norm_columns(conn, clip_id, meta)
            sha = store_asset(conn, raw, "application/pdf")
            warm_asset_text(conn, sha, pdf_text)
            conn.execute(
                "INSERT INTO clip_assets(clip_id, asset_sha, role, filename) "
                "VALUES (?, ?, 'pdf-primary', ?)", (clip_id, sha, fname))
            if tags:
                upsert_tags(conn, clip_id, tags)
        return jsonify({"ok": True, "status": "imported", "id": clip_id,
                        "title": title, "tags": tags, "auto_tags": keywords,
                        "relpath": relpath})
    except Exception as e:
        log.exception("bulk upload failed for %s", relpath)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/upload")
def viewer_upload():
    return render_template("upload.html")


# ---------------------------------------------------------------------------
# Viewer (server-rendered)
# ---------------------------------------------------------------------------

@app.route("/")
def viewer_index():
    return render_template("index.html")


@app.route("/clip/<int:clip_id>")
def viewer_detail(clip_id):
    return render_template("detail.html", clip_id=clip_id)


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/embed/status")
def api_embed_status():
    """Report whether the semantic embedding ranker is active (weights bundled
    and loadable) or the service is on the lexical FTS fallback."""
    try:
        import embed as embed_mod
        return jsonify(embed_mod.status())
    except Exception as e:
        return jsonify({"available": False, "reason": f"import failed: {e!r}"})


def _startup_banner() -> None:
    """Tell the operator exactly which library file this instance is using, so
    an unexpected CLIPPER_DB or launch directory can't silently point at the
    wrong (or a brand-new) database without them noticing."""
    path = os.path.abspath(get_db_path())
    existed = os.path.exists(path)
    init_db()
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    overridden = " (from $CLIPPER_DB)" if os.environ.get("CLIPPER_DB") else ""
    n_clips = 0
    conn = connect()
    try:
        n_clips = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    except Exception:
        pass
    finally:
        conn.close()
    log.info("=" * 68)
    log.info("Clipper library: %s%s", path, overridden)
    log.info("  %s file, %.1f MB, %d clip(s)",
             "existing" if existed else "NEW (created now)", size_mb, n_clips)
    log.info("  viewer: http://127.0.0.1:5577/")
    log.info("=" * 68)


if __name__ == "__main__":
    _startup_banner()
    app.run(host="127.0.0.1", port=5577, debug=False)
