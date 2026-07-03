"""
OneNote → SQLite migrator.

Two modes:
  com    - drive OneNote via its COM API to export pages as .docx + .pdf,
           then ingest. Windows only, OneNote 2016 must be installed.
  folder - point at a directory of already-exported .docx (and optionally
           .mht / .pdf) files and ingest them. Works anywhere.

The Word-document ingest pulls text, images, and embedded files into the
clips DB exactly as if a userscript had clipped them, so everything lives
in the same searchable store.

Usage:
    # Export and ingest a single .onepkg or .one file via COM
    python migrate.py com --notebook "C:/path/to/notebook.one"

    # Or export all open notebooks
    python migrate.py com --all

    # Ingest already-exported .docx files
    python migrate.py folder ./exported_pages --tag from-onenote
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

# We import db from the service so this script and the running service
# share one schema. If you'd rather migrate without the service installed,
# copy db.py into the migrator folder.
sys.path.insert(0, str(Path(__file__).parent.parent / "service"))
from db import connect, find_duplicate, init_db, now_ts, transaction, upsert_tags  # noqa: E402


class DuplicateSkipped(Exception):
    """Raised (inside the ingest transaction, so nothing is written) when a
    file matches an existing clip. Carries the duplicate info for reporting,
    which makes re-running the migrator over the same folder safe."""

    def __init__(self, dup: dict):
        self.dup = dup
        super().__init__(f"duplicate of clip #{dup['id']} ({dup['title']})")


# ---------------------------------------------------------------------------
# Word document parsing
# ---------------------------------------------------------------------------
# A .docx is a ZIP with the body in word/document.xml. We don't need full
# fidelity - we want title + paragraph text + image references - so we walk
# the XML directly rather than pulling in python-docx (which would be a
# dependency just for this).

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

def _qn(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def parse_docx(path: Path) -> dict:
    """Parse a .docx and return {title, html, images: [{filename, mime, bytes}], embeds}."""
    images: list[dict] = []
    embeds: list[dict] = []
    rels: dict[str, str] = {}  # rId -> target (e.g. media/image1.png)

    with zipfile.ZipFile(path) as z:
        # Relationships - links rId to the embedded file.
        try:
            rels_xml = z.read("word/_rels/document.xml.rels")
            rels_root = ET.fromstring(rels_xml)
            # The rels XML uses its own namespace; just walk and grab.
            for rel in rels_root:
                rels[rel.get("Id")] = rel.get("Target")
        except KeyError:
            pass

        # Body
        body_xml = z.read("word/document.xml")
        root = ET.fromstring(body_xml)
        body = root.find(_qn(W_NS, "body"))

        # Build HTML one paragraph at a time. We treat the first paragraph
        # styled as Title or Heading1 as the document title.
        title = None
        html_parts: list[str] = []
        for p in body.findall(_qn(W_NS, "p")):
            text = "".join(t.text or "" for t in p.iter(_qn(W_NS, "t")))
            style = p.find(f"{_qn(W_NS, 'pPr')}/{_qn(W_NS, 'pStyle')}")
            style_val = style.get(_qn(W_NS, "val")) if style is not None else ""

            # Images embedded in this paragraph
            for blip in p.iter(_qn(A_NS, "blip")):
                embed_id = blip.get(_qn(R_NS, "embed"))
                if not embed_id or embed_id not in rels:
                    continue
                target = rels[embed_id]
                # Targets in rels are relative to word/
                full = "word/" + target.lstrip("./")
                try:
                    img_bytes = z.read(full)
                    ext = full.rsplit(".", 1)[-1].lower()
                    mime = {
                        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "gif": "image/gif", "bmp": "image/bmp",
                    }.get(ext, "image/png")
                    sha = hashlib.sha256(img_bytes).hexdigest()
                    images.append({
                        "filename": full.rsplit("/", 1)[-1],
                        "mime": mime, "bytes": img_bytes, "sha": sha,
                    })
                    html_parts.append(f'<p><img src="cid:{sha}" alt=""></p>')
                except KeyError:
                    pass

            # Embedded OLE objects (attachments). Word stores these as
            # w:object > o:OLEObject > rels link to embeddings/oleObject*.bin
            # In Tim's OneNote-exported files, real attached files come
            # through here. We just extract them verbatim and offer them as
            # attachments on the clip.
            for ole in p.iter("{urn:schemas-microsoft-com:office:office}OLEObject"):
                ole_rid = ole.get(_qn(R_NS, "id"))
                if ole_rid and ole_rid in rels:
                    target = "word/" + rels[ole_rid].lstrip("./")
                    try:
                        ole_bytes = z.read(target)
                        sha = hashlib.sha256(ole_bytes).hexdigest()
                        embeds.append({
                            "filename": target.rsplit("/", 1)[-1],
                            "mime": "application/octet-stream",
                            "bytes": ole_bytes, "sha": sha,
                        })
                    except KeyError:
                        pass

            if text.strip():
                if style_val in ("Title", "Heading1", "Heading2") and title is None:
                    title = text.strip()
                # Map common styles to HTML tags
                tag = {
                    "Title": "h1", "Heading1": "h1", "Heading2": "h2",
                    "Heading3": "h3", "Heading4": "h4",
                }.get(style_val, "p")
                html_parts.append(f"<{tag}>{escape(text)}</{tag}>")

    return {
        "title": title or path.stem,
        "html": "\n".join(html_parts),
        "images": images,
        "embeds": embeds,
    }


def escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&#39;"))


# ---------------------------------------------------------------------------
# Ingest into SQLite
# ---------------------------------------------------------------------------

def ingest_docx(conn: sqlite3.Connection, path: Path,
                tags: list[str], extra_meta: dict | None = None,
                force: bool = False) -> int:
    """Parse one .docx and create a clip in the DB."""
    parsed = parse_docx(path)
    html = parsed["html"]

    # Dedupe before writing anything: the text (and so the hash) is the same
    # whether image srcs are still cid: placeholders or /asset/ URLs, since
    # tags are stripped entirely. Empty-text documents are exempt - they all
    # hash identically.
    text_probe = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    probe_hash = hashlib.sha256(text_probe.encode("utf-8")).hexdigest()
    if not force and text_probe:
        dup = find_duplicate(conn, content_hash=probe_hash)
        if dup:
            raise DuplicateSkipped(dup)

    # Replace cid: image references with real /asset/<sha> URLs once the
    # images are stored.
    for img in parsed["images"]:
        sha = img["sha"]
        exists = conn.execute("SELECT 1 FROM assets WHERE sha256 = ?", (sha,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO assets(sha256, mime, bytes, byte_size, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sha, img["mime"], img["bytes"], len(img["bytes"]), now_ts()),
            )
        html = html.replace(f'src="cid:{sha}"', f'src="/asset/{sha}"')

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    word_count = len(re.findall(r"\w+", text))
    reading_min = max(1, round(word_count / 230))
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    meta = {"migrated_from": str(path), **(extra_meta or {})}

    cur = conn.execute(
        """
        INSERT INTO clips(url, canonical_url, title, captured_at, source,
                          content_html, content_text, content_hash,
                          word_count, reading_minutes, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (None, None, parsed["title"], now_ts(), "migrator-onenote",
         html, text, content_hash, word_count, reading_min, json.dumps(meta)),
    )
    clip_id = cur.lastrowid

    # Link each image asset to the clip
    for img in parsed["images"]:
        conn.execute(
            "INSERT OR IGNORE INTO clip_assets(clip_id, asset_sha, role, filename) "
            "VALUES (?, ?, 'inline-image', ?)",
            (clip_id, img["sha"], img["filename"]),
        )
    # Embedded attachments
    for emb in parsed["embeds"]:
        exists = conn.execute("SELECT 1 FROM assets WHERE sha256 = ?", (emb["sha"],)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO assets(sha256, mime, bytes, byte_size, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (emb["sha"], emb["mime"], emb["bytes"], len(emb["bytes"]), now_ts()),
            )
        conn.execute(
            "INSERT INTO clip_assets(clip_id, asset_sha, role, filename) "
            "VALUES (?, ?, 'attachment', ?)",
            (clip_id, emb["sha"], emb["filename"]),
        )

    if tags:
        upsert_tags(conn, clip_id, tags)

    return clip_id


def ingest_pdf(conn: sqlite3.Connection, path: Path, tags: list[str],
               force: bool = False) -> int:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if not force:
        dup = find_duplicate(conn, content_hash=sha, primary_sha=sha)
        if dup:
            raise DuplicateSkipped(dup)
    cur = conn.execute(
        """
        INSERT INTO clips(url, canonical_url, title, captured_at, source,
                          content_html, content_text, content_hash,
                          word_count, reading_minutes, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (None, None, path.stem, now_ts(), "migrator-pdf",
         f"<p>PDF: {escape(path.name)}</p>", path.stem, sha, 0, 1,
         json.dumps({"migrated_from": str(path), "filename": path.name})),
    )
    clip_id = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO assets(sha256, mime, bytes, byte_size, created_at) "
                 "VALUES (?, 'application/pdf', ?, ?, ?)",
                 (sha, raw, len(raw), now_ts()))
    conn.execute("INSERT INTO clip_assets(clip_id, asset_sha, role, filename) "
                 "VALUES (?, ?, 'pdf-primary', ?)",
                 (clip_id, sha, path.name))
    if tags:
        upsert_tags(conn, clip_id, tags)
    return clip_id


# ---------------------------------------------------------------------------
# Folder ingest mode
# ---------------------------------------------------------------------------

def cmd_folder(args):
    init_db()
    conn = connect()
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 1
    files = sorted([
        p for p in folder.rglob("*")
        if p.suffix.lower() in {".docx", ".pdf"} and not p.name.startswith("~$")
    ])
    print(f"Found {len(files)} files to ingest")
    tags = args.tags or []
    force = bool(getattr(args, "force", False))
    ok = fail = skipped = 0
    for f in files:
        rel = f.relative_to(folder).as_posix()
        try:
            with transaction(conn):
                # Use parent folder name(s) as automatic tags so OneNote
                # section structure becomes searchable.
                auto_tags = list(tags)
                for part in f.relative_to(folder).parent.parts:
                    auto_tags.append(part)
                if f.suffix.lower() == ".docx":
                    cid = ingest_docx(conn, f, auto_tags,
                                      extra_meta={"source_path": rel},
                                      force=force)
                else:
                    cid = ingest_pdf(conn, f, auto_tags, force=force)
                print(f"  [{cid}] {rel}")
                ok += 1
        except DuplicateSkipped as e:
            print(f"  skip {rel}: {e} (use --force to ingest anyway)")
            skipped += 1
        except Exception as e:
            print(f"  FAILED {f}: {e}", file=sys.stderr)
            fail += 1
    print(f"\nIngested {ok}, skipped {skipped} duplicate(s), failed {fail}")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------------------
# COM export mode (Windows + OneNote 2016)
# ---------------------------------------------------------------------------

def cmd_com(args):
    """Drive OneNote to export every page as a .docx into a temp folder,
    then ingest from there."""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        print("pywin32 not installed. `pip install pywin32` and re-run from Windows.", file=sys.stderr)
        return 1

    app = win32com.client.Dispatch("OneNote.Application")
    # Build the same kind of XML the standalone clipper used, but here we
    # just need the list of pages to export.
    hierarchy_xml = app.GetHierarchy("", 4)  # hsPages
    # Pull ID and name per <one:Page> tag independently - attribute order
    # in the hierarchy XML is not guaranteed, and a combined regex that
    # assumed ID-before-name silently matched zero pages when it differed.
    pages: list[tuple[str, str]] = []
    for tag in re.findall(r"<one:Page\b[^>]*>", hierarchy_xml, re.IGNORECASE):
        m_id = re.search(r'\bID="([^"]+)"', tag)
        m_name = re.search(r'\bname="([^"]+)"', tag)
        if m_id and m_name:
            pages.append((m_id.group(1), m_name.group(1)))

    if not pages:
        print("No pages found - is OneNote running with a notebook open?", file=sys.stderr)
        return 1

    init_db()
    conn = connect()
    tmp = Path(tempfile.mkdtemp(prefix="onenote-export-"))
    print(f"Exporting {len(pages)} pages to {tmp}")

    # 4 = pfWord, the publish-format enum for .docx in the OneNote API.
    PF_WORD = 4
    force = bool(getattr(args, "force", False))
    ok = fail = skipped = 0
    try:
        for page_id, page_name in pages:
            safe = re.sub(r"[^\w. -]+", "_", page_name)[:80] or f"page_{ok}"
            out = tmp / f"{ok:04d}_{safe}.docx"
            try:
                app.Publish(page_id, str(out), PF_WORD, "")
            except Exception as e:
                print(f"  export failed for {page_name}: {e}", file=sys.stderr)
                fail += 1
                continue
            if not out.exists():
                print(f"  export silently dropped: {page_name}", file=sys.stderr)
                fail += 1
                continue
            try:
                with transaction(conn):
                    tags = (args.tags or []) + ["onenote-migrated"]
                    cid = ingest_docx(conn, out, tags,
                                      extra_meta={"onenote_page_id": page_id,
                                                  "onenote_page_name": page_name},
                                      force=force)
                    print(f"  [{cid}] {page_name}")
                    ok += 1
            except DuplicateSkipped as e:
                print(f"  skip {page_name}: {e} (use --force to ingest anyway)")
                skipped += 1
            except Exception as e:
                print(f"  ingest failed for {page_name}: {e}", file=sys.stderr)
                fail += 1
    finally:
        if args.keep_export:
            print(f"\nExport dir kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nIngested {ok}, skipped {skipped} duplicate(s), failed {fail}")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Migrate OneNote pages into the local SQLite clipper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("folder", help="Ingest a directory of .docx / .pdf files")
    f.add_argument("folder", help="Directory containing exported files")
    f.add_argument("--tag", dest="tags", action="append",
                   help="Add a tag to every clip (repeatable)")
    f.add_argument("--force", action="store_true",
                   help="Ingest files even if they duplicate existing clips")
    f.set_defaults(fn=cmd_folder)

    c = sub.add_parser("com", help="Drive OneNote to export then ingest (Windows)")
    c.add_argument("--notebook", help="Optional notebook file path filter (informational only)")
    c.add_argument("--all", action="store_true", help="Export every open notebook")
    c.add_argument("--tag", dest="tags", action="append",
                   help="Add a tag to every imported clip (repeatable)")
    c.add_argument("--keep-export", action="store_true",
                   help="Keep the temporary .docx exports for inspection")
    c.add_argument("--force", action="store_true",
                   help="Ingest pages even if they duplicate existing clips")
    c.set_defaults(fn=cmd_com)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
