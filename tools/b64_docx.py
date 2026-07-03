#!/usr/bin/env python3
"""
b64_docx.py — pack any file into a Word document as base64 text, and unpack
it again. Round-trippable: `encode` then `decode` reproduces the original
bytes exactly.

The encoded document holds the base64 in a small monospaced font, wrapped to
fixed-width lines between `----- BEGIN BASE64 -----` / `----- END BASE64 -----`
markers, with a short header and a decode one-liner. This is the format used
to ship clipper.zip inside a .docx.

Usage
-----
  # Pack a file into a .docx
  python3 b64_docx.py encode clipper.zip clipper-zip-b64.docx
  python3 b64_docx.py encode clipper.zip out.docx --title "Clipper archive"

  # Unpack it back to the original bytes
  python3 b64_docx.py decode clipper-zip-b64.docx recovered.zip

  # Verify a docx round-trips against the original without writing output
  python3 b64_docx.py verify clipper.zip clipper-zip-b64.docx

Only dependency is python-docx (`pip install python-docx`). Decoding also
works on the plain-text block alone (see --from-text), so a recipient who
only has the pasted base64 can still recover the file with the standard
library.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
import textwrap

BEGIN_MARKER = "----- BEGIN BASE64 -----"
END_MARKER = "----- END BASE64 -----"
DEFAULT_WIDTH = 100          # base64 chars per line
BODY_FONT = "Consolas"
BODY_SIZE_PT = 8             # the small font the archive lines are set in
DECODE_ONE_LINER = (
    "python3 -c \"import base64,re;"
    "open('out.bin','wb').write(base64.b64decode("
    "re.sub(r'[^A-Za-z0-9+/=]','',open('pasted.txt').read())))\""
)


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def encode(src_path: str, out_path: str, title: str | None = None,
           width: int = DEFAULT_WIDTH, body_size_pt: int = BODY_SIZE_PT) -> dict:
    """Read src_path, base64-encode it, and write a .docx containing the
    encoding. Returns a small summary dict."""
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_LINE_SPACING

    with open(src_path, "rb") as fh:
        raw = fh.read()
    b64 = base64.b64encode(raw).decode("ascii")
    lines = textwrap.wrap(b64, width) or [""]
    sha = hashlib.sha256(raw).hexdigest()

    if title is None:
        title = f"{src_path} — base64 archive"

    doc = Document()

    # US Letter, 0.75" margins (matches the original builder).
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    for attr in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(section, attr, Inches(0.75))

    # Default font.
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11)

    doc.add_heading(title, level=1)

    p = doc.add_paragraph()
    p.add_run(
        "The block below is a base64 encoding of the original file. To "
        "reconstruct: copy only the lines between the BEGIN and END markers "
        "(not the markers themselves) into a file 'pasted.txt', then run:"
    )

    mono = doc.add_paragraph()
    run = mono.add_run("  " + DECODE_ONE_LINER)
    run.font.name = "Consolas"
    run.font.size = Pt(10)

    meta = doc.add_paragraph()
    meta.add_run(
        f"Original size: {len(raw)} bytes · sha256: {sha}"
    ).font.size = Pt(9)

    doc.add_paragraph()  # spacer

    begin = doc.add_paragraph()
    r = begin.add_run(BEGIN_MARKER)
    r.bold = True
    r.font.name = "Consolas"
    r.font.size = Pt(10)

    # The base64 body: one paragraph per line, tight spacing, small mono font.
    for line in lines:
        para = doc.add_paragraph()
        para.paragraph_format.space_before = Pt(0)
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
        run = para.add_run(line)
        run.font.name = BODY_FONT
        run.font.size = Pt(body_size_pt)

    end = doc.add_paragraph()
    r = end.add_run(END_MARKER)
    r.bold = True
    r.font.name = "Consolas"
    r.font.size = Pt(10)

    doc.save(out_path)
    return {"bytes": len(raw), "b64_chars": len(b64),
            "lines": len(lines), "sha256": sha, "out": out_path}


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def _extract_b64_from_paragraphs(paragraphs: list[str]) -> str | None:
    """Pull the base64 text from between the BEGIN/END markers. Returns the
    (possibly empty) base64 string if the markers are found, or None if they
    are not. An empty string is a valid result (the original file was empty).

    We require the markers rather than guessing: a marker-less "find the
    base64-looking lines" heuristic will happily decode ordinary prose (which
    is all valid base64 characters) into garbage, which is worse than a clear
    error. Documents produced by encode() always carry the markers."""
    text = "\n".join(paragraphs)
    m = re.search(re.escape(BEGIN_MARKER) + r"(.*?)" + re.escape(END_MARKER),
                  text, re.DOTALL)
    if m is None:
        return None
    return re.sub(r"\s", "", m.group(1))


def decode_docx(docx_path: str, out_path: str) -> dict:
    """Read a .docx produced by encode(), recover the original bytes, write
    them to out_path. Returns a summary dict including the sha256, and the
    embedded sha256 if the header recorded one (for verification)."""
    from docx import Document

    doc = Document(docx_path)
    paragraphs = [p.text for p in doc.paragraphs]
    b64 = _extract_b64_from_paragraphs(paragraphs)
    if b64 is None:
        raise ValueError("No base64 block (BEGIN/END markers) found in document")
    raw = base64.b64decode(b64)

    # Look for a recorded sha256 in the header line, if present.
    embedded_sha = None
    for line in paragraphs:
        m = re.search(r"sha256:\s*([0-9a-f]{64})", line)
        if m:
            embedded_sha = m.group(1)
            break

    with open(out_path, "wb") as fh:
        fh.write(raw)

    actual_sha = hashlib.sha256(raw).hexdigest()
    return {"bytes": len(raw), "sha256": actual_sha,
            "embedded_sha256": embedded_sha,
            "sha_ok": (embedded_sha is None or embedded_sha == actual_sha),
            "out": out_path}


def decode_text(text_path: str, out_path: str) -> dict:
    """Recover bytes from a plain-text file containing the base64 block
    (with or without the markers). Standard library only."""
    with open(text_path, "r", encoding="utf-8", errors="replace") as fh:
        paragraphs = fh.read().split("\n")
    b64 = _extract_b64_from_paragraphs(paragraphs)
    if b64 is None:
        raise ValueError("No base64 block (BEGIN/END markers) found in text")
    raw = base64.b64decode(b64)
    with open(out_path, "wb") as fh:
        fh.write(raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "out": out_path}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pack/unpack a file as base64 inside a .docx")
    sub = ap.add_subparsers(dest="cmd", required=True)

    enc = sub.add_parser("encode", help="pack a file into a .docx")
    enc.add_argument("src")
    enc.add_argument("out")
    enc.add_argument("--title", default=None)
    enc.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                     help=f"base64 chars per line (default {DEFAULT_WIDTH})")
    enc.add_argument("--size", type=int, default=BODY_SIZE_PT,
                     help=f"body font size in pt (default {BODY_SIZE_PT})")

    dec = sub.add_parser("decode", help="recover a file from a .docx (or --from-text)")
    dec.add_argument("doc")
    dec.add_argument("out")
    dec.add_argument("--from-text", action="store_true",
                     help="treat input as a plain-text base64 block, not a .docx")

    ver = sub.add_parser("verify", help="check a .docx round-trips against the original")
    ver.add_argument("src")
    ver.add_argument("doc")

    args = ap.parse_args(argv)

    try:
        return _dispatch(args)
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # python-docx raises PackageNotFoundError (not a FileNotFoundError) for
        # a missing/invalid .docx; surface it cleanly rather than as a traceback.
        name = type(e).__name__
        if name == "PackageNotFoundError":
            print(f"error: not a valid .docx file: {args.doc}", file=sys.stderr)
        else:
            print(f"error: {name}: {e}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.cmd == "encode":
        info = encode(args.src, args.out, title=args.title,
                      width=args.width, body_size_pt=args.size)
        print(f"Wrote {info['out']}: {info['bytes']} bytes -> "
              f"{info['lines']} lines of base64 (sha256 {info['sha256'][:12]}…)")
        return 0

    if args.cmd == "decode":
        if args.from_text:
            info = decode_text(args.doc, args.out)
            print(f"Wrote {info['out']}: {info['bytes']} bytes "
                  f"(sha256 {info['sha256'][:12]}…)")
        else:
            info = decode_docx(args.doc, args.out)
            status = "ok" if info["sha_ok"] else "MISMATCH vs embedded sha!"
            note = ""
            if info["embedded_sha256"]:
                note = f", embedded-sha {status}"
            print(f"Wrote {info['out']}: {info['bytes']} bytes "
                  f"(sha256 {info['sha256'][:12]}…{note})")
            if not info["sha_ok"]:
                return 2
        return 0

    if args.cmd == "verify":
        with open(args.src, "rb") as fh:
            src_sha = hashlib.sha256(fh.read()).hexdigest()
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".bin")
        try:
            info = decode_docx(args.doc, tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        ok = info["sha256"] == src_sha
        print(f"source sha256: {src_sha}")
        print(f"docx   sha256: {info['sha256']}")
        print("ROUND-TRIP OK" if ok else "MISMATCH")
        return 0 if ok else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
