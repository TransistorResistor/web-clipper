"""One-shot backfill of content_text for clips ingested before two fixes:

* HTML clips: content_text used to come from a local strip_html that skipped
  entity decoding and table rendering; the unified extractor is
  search.html_to_text. Re-extract from content_html and refresh content_hash
  (text-based, so cross-path dedupe agrees with new ingests), word_count and
  reading_minutes.
* PDF clips (userscript-pdf / upload-pdf / outlook-pdf): content_text used to
  be just the title, which hid PDF bodies from the library FTS box and gave
  the embedding cache a title-only vector. Re-extract from the primary PDF
  (via the asset_text cache, so PyMuPDF runs at most once per asset).
  content_hash stays the PDF bytes' sha256 - dedupe semantics unchanged.

Stale clip_embeddings rows for changed clips are dropped so the next suggest
call re-embeds them (a PDF clip's hash doesn't change, so the cache would
never self-invalidate). The FTS index follows automatically - the UPDATE
triggers reindex the row and preserve its tags.

Idempotent - a second run finds nothing to change.

    python backfill_content_text.py             # default DB ($CLIPPER_DB or repo root)
    python backfill_content_text.py --dry-run   # report what would change, write nothing
    python backfill_content_text.py --db other.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3

from db import connect, init_db
import search as search_engine

WORD_RE = re.compile(r"\w+", re.UNICODE)
PDF_SOURCES = ("userscript-pdf", "upload-pdf", "outlook-pdf")
# Mirror app.pdf_content_text's cap (not imported: app pulls in Flask).
PDF_TEXT_CAP = 500_000


def _wc_rm(text: str) -> tuple[int, int]:
    wc = len(WORD_RE.findall(text))
    return wc, max(1, round(wc / 230))


def _drop_embedding(conn, clip_id: int) -> None:
    try:
        conn.execute("DELETE FROM clip_embeddings WHERE clip_id = ?", (clip_id,))
    except sqlite3.OperationalError:
        pass  # cache table not created yet - nothing stale to drop


def _pdf_text(conn, clip_id: int) -> str | None:
    row = conn.execute(
        "SELECT ca.asset_sha, ca.filename, a.mime FROM clip_assets ca "
        "JOIN assets a ON a.sha256 = ca.asset_sha "
        "WHERE ca.clip_id = ? AND ca.role = 'pdf-primary' LIMIT 1",
        (clip_id,)).fetchone()
    if not row:
        return None
    return search_engine.extract_asset_text(
        conn, row["asset_sha"], row["mime"], row["filename"] or "")


def backfill(conn, dry_run: bool = False) -> tuple[int, int, int]:
    """Returns (total_clips, html_updated, pdf_updated)."""
    search_engine.ensure_cache(conn)
    rows = conn.execute(
        "SELECT id, source, title, content_html, content_text FROM clips").fetchall()
    html_updated = pdf_updated = 0
    for r in rows:
        if r["source"] in PDF_SOURCES:
            pdf_text = _pdf_text(conn, r["id"])
            if not pdf_text:
                continue  # asset missing or nothing extractable (scanned PDF)
            new_text = pdf_text.strip()[:PDF_TEXT_CAP] or (r["title"] or "")
            kind = "pdf"
        else:
            if not r["content_html"]:
                continue  # text-only clip: nothing to re-extract from
            new_text = search_engine.html_to_text(r["content_html"])
            kind = "html"
        if new_text == (r["content_text"] or ""):
            continue
        if kind == "html":
            html_updated += 1
        else:
            pdf_updated += 1
        if dry_run:
            continue
        wc, rm = _wc_rm(new_text)
        if kind == "html":
            # Text-based hash, kept in step with what new ingests compute.
            import hashlib
            conn.execute(
                "UPDATE clips SET content_text = ?, content_hash = ?, "
                "word_count = ?, reading_minutes = ? WHERE id = ?",
                (new_text, hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
                 wc, rm, r["id"]))
        else:
            # content_hash stays the PDF bytes' sha256 (dedupe key).
            conn.execute(
                "UPDATE clips SET content_text = ?, word_count = ?, "
                "reading_minutes = ? WHERE id = ?",
                (new_text, wc, rm, r["id"]))
        _drop_embedding(conn, r["id"])
    if not dry_run:
        conn.commit()
    return len(rows), html_updated, pdf_updated


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="path to clipper.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db(args.db)
    conn = connect(args.db)
    try:
        total, n_html, n_pdf = backfill(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "would update" if args.dry_run else "updated"
    print(f"{total} clips scanned; {verb} content_text on "
          f"{n_html} HTML clip(s) and {n_pdf} PDF clip(s).")
