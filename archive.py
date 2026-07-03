"""Archive every source file in the Clipper project into readable snapshots.

Produces, in your OneDrive folder:
  * clipper_archive.docx  -- one heading per file, full text below it
  * clipper_archive.pdf   -- the same layout as a PDF, plus an embedded
                             exact-text payload so it can be restored too
  * unarchive.py          -- a standalone restorer (also embedded at the top
                             of both documents)

Each file is delimited by a heading line of the form
    === FILE: <relative\\path> ===
so the un-archiver can split the archive back into individual files.

Restore is byte-exact from either the .docx or the .pdf:
  * .docx  -- file text is read straight from the paragraphs.
  * .pdf   -- the exact text is pulled from an embedded attachment, NOT from
              the printed glyphs (the source contains emoji/symbols the PDF
              font can't render, so the visible pages are a readable copy only).

Run:  python archive.py
"""
import io
import os

from docx import Document
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from pypdf import PdfReader, PdfWriter

# .bas (Outlook VBA macro) is archived alongside the usual text/source types.
TEXT_FILE_EXTENSIONS = {'.py', '.md', '.xml', '.html', '.css', '.js', '.yml', '.yaml',
                        '.ini', '.cfg', '.toml', '.txt', '.conf', '.sh', '.bas'}

# Exact filenames to include regardless of extension, mapped to their archive label
INCLUDE_FILENAMES = {
    'Dockerfile': 'dockerfile_1',
}

# Exact relative paths to archive even though their extension isn't in
# TEXT_FILE_EXTENSIONS - config/data files we want captured by name (e.g.
# markings.json, the marking vocabulary). Compared against each file's path
# relative to the project root and archived under that same path, so they
# restore to the right place. Listing the path (not just the basename) avoids
# sweeping in same-named files elsewhere (model config.json, corpus*.json, ...).
INCLUDE_PATHS = {
    os.path.join('service', 'markings.json'),
}

# Directories to skip entirely.
#   TestDocs   -- text corpus documents (test fixtures)
#   rt_test    -- round-trip test harness and its generated archive parts
SKIP_DIRS = {'__pycache__', '.git', 'instance', 'uploads', 'reports', 'notifications',
             '.pytest_cache', 'node_modules', '.venv', 'venv', '.claude', 'TestDocs',
             'rt_test'}

# Exact filenames to skip. The corpus builders are excluded alongside the
# prefix-based test/eval/demo scripts below.
SKIP_FILES = {'archive_output.docx', 'vocab.txt',
              'build_crosscut_corpus.py', 'build_eval_corpus.py'}

# Filename prefixes to skip: test, evaluation, and demo scaffolding scripts.
SKIP_PREFIXES = ('test_', 'eval_', 'demo_')

TITLE = 'Clipper — Archived Source Files'

# File-boundary marker. Doubles as the (human-readable) heading for each file
# and as the machine-parseable delimiter the un-archiver splits on.
MARKER_PREFIX = '=== FILE: '
MARKER_SUFFIX = ' ==='

# Output artefacts (written into OneDrive). Skipped from the walk by name.
DOCX_NAME = 'clipper_archive.docx'
PDF_NAME = 'clipper_archive.pdf'
UNARCHIVER_NAME = 'unarchive.py'

# Name of the exact-text payload embedded inside the PDF (read back on restore).
ATTACHMENT_NAME = 'clipper_archive_payload.txt'

FONT_NAME = 'Courier'
FONT_SIZE = 8
WRAP_COLS = 110  # hard-wrap width for the PDF's visible layer (Courier 8pt on A4)

# Standalone restorer. Stored verbatim (raw string) so it is emitted exactly as
# written -- it is both dropped next to the archive and embedded as the first
# entry inside the documents. Outer quotes are triple-single so the script's own
# triple-double docstring survives untouched.
UNARCHIVER_SOURCE = r'''"""Clipper un-archiver — restore the source tree from a Clipper archive.

Usage:
    python unarchive.py [ARCHIVE] [OUTPUT_DIR]

ARCHIVE defaults to clipper_archive.docx in the current folder; OUTPUT_DIR
defaults to ./clipper_restored. Both .docx and .pdf archives restore byte-exact:
  * .docx needs python-docx   (pip install python-docx)
  * .pdf  needs pypdf         (pip install pypdf) — the exact text is read from
          an embedded attachment, not from the printed page.
A plain-text export of the archive is also accepted.

Files in the archive are delimited by heading lines of the form
    === FILE: <relative\\path> ===
Everything between one marker and the next is written back to that path.
"""
import os
import re
import sys

MARKER = re.compile(r'^=== FILE: (.*) ===\s*$')
ATTACHMENT_NAME = 'clipper_archive_payload.txt'


def iter_lines(path):
    low = path.lower()
    if low.endswith('.docx'):
        from docx import Document
        for para in Document(path).paragraphs:
            # A whole file is stored as one paragraph with embedded newlines,
            # so split each paragraph into its physical lines.
            for line in para.text.split('\n'):
                yield line
    elif low.endswith('.pdf'):
        from pypdf import PdfReader
        reader = PdfReader(path)
        data = None
        attachments = getattr(reader, 'attachments', None) or {}
        if ATTACHMENT_NAME in attachments:
            blob = attachments[ATTACHMENT_NAME]
            if isinstance(blob, list):
                blob = blob[0]
            data = blob.decode('utf-8')
        if data is None:
            # Fallback: read the visible pages. This is lossy for any glyph the
            # PDF font could not render (emoji, arrows, etc.) — the embedded
            # payload above is the authoritative source.
            sys.stderr.write('WARNING: no embedded payload; falling back to '
                             'visible text (may be lossy)\n')
            data = '\n'.join((p.extract_text() or '') for p in reader.pages)
        for line in data.split('\n'):
            yield line
    else:
        with open(path, 'r', encoding='utf-8') as handle:
            for line in handle.read().split('\n'):
                yield line


def restore(archive_path, output_dir):
    current = None
    buffer = []
    count = 0

    def flush():
        if current is None:
            return 0
        dest = os.path.join(output_dir, *current.replace('\\', '/').split('/'))
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, 'w', encoding='utf-8', newline='\n') as out:
            out.write('\n'.join(buffer))
        print('Restored', current)
        return 1

    for line in iter_lines(archive_path):
        match = MARKER.match(line)
        if match:
            count += flush()
            current = match.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    count += flush()
    print('\nRestored %d file(s) to %s' % (count, output_dir))


if __name__ == '__main__':
    archive = sys.argv[1] if len(sys.argv) > 1 else 'clipper_archive.docx'
    out = sys.argv[2] if len(sys.argv) > 2 else 'clipper_restored'
    restore(archive, out)
'''

INTRO_LINES = [
    "This document is a readable snapshot of the Clipper source tree.",
    "Each file below starts with a heading line:  === FILE: <path> ===",
    "To restore: save the 'unarchive.py' listing (the first entry) to a file, then run:",
    "    python unarchive.py <clipper_archive.docx | clipper_archive.pdf>  [output_dir]",
    "Restore is byte-exact from either format (.docx needs python-docx, .pdf needs pypdf).",
    "Note: the PDF's exact text lives in an embedded attachment; the printed pages are a",
    "readable copy only (some symbols/emoji can't be drawn by the PDF font).",
]


def is_text_file(filename):
    return (os.path.splitext(filename)[1].lower() in TEXT_FILE_EXTENSIONS
            or filename in INCLUDE_FILENAMES)


def marker(label):
    return MARKER_PREFIX + label + MARKER_SUFFIX


def collect_sections(project_root, skip_basenames):
    """Return [(label, content), ...]; the un-archiver is always first."""
    sections = [(UNARCHIVER_NAME, UNARCHIVER_SOURCE)]

    for root, dirs, files in os.walk(project_root):
        # Prune skipped directories in-place so os.walk won't descend into them
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)

        for file in sorted(files):
            full_path = os.path.join(root, file)
            relative_path = os.path.relpath(full_path, project_root)
            if (file in SKIP_FILES or file in skip_basenames
                    or file.startswith(SKIP_PREFIXES)):
                continue
            # Include recognised text/source files, plus any explicitly-listed
            # extra paths whose extension isn't a text type (see INCLUDE_PATHS).
            if not is_text_file(file) and relative_path not in INCLUDE_PATHS:
                continue

            label = INCLUDE_FILENAMES.get(file, relative_path)
            print(f"Adding {relative_path} as {label}")

            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                content = "[UNABLE TO READ FILE: ENCODING ERROR]"

            sections.append((label, content))

    return sections


def build_payload(sections):
    """Exact, marker-delimited text stream. Inverse of the un-archiver's parser:
    joining markers and content with a single '\n' is losslessly recoverable."""
    parts = []
    for label, content in sections:
        parts.append(marker(label))
        parts.append(content)
    return "\n".join(parts)


def build_docx(path, sections):
    document = Document()
    document.add_heading(TITLE, 0)

    for line in INTRO_LINES:
        document.add_paragraph(line)

    for label, content in sections:
        # The marker is the heading: human-readable AND the parse delimiter.
        document.add_heading(marker(label), level=2)
        document.add_paragraph(content)

    document.save(path)


def _safe(text):
    """Down-map to what the built-in PDF font (WinAnsi/cp1252) can draw, so the
    visible layer never raises. Restore never uses this — it reads the payload."""
    return text.encode('cp1252', 'replace').decode('cp1252')


def _hard_wrap(text, width):
    return [text[i:i + width] for i in range(0, len(text), width)] or ['']


def build_pdf(path, sections):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    left = 30.0
    top = height - 30
    bottom = 40.0
    line_h = FONT_SIZE + 2

    y = top

    def draw(text, font=FONT_NAME, size=FONT_SIZE):
        nonlocal y
        if y < bottom:
            c.showPage()
            y = top
        c.setFont(font, size)
        c.drawString(left, y, text)
        y -= size + 2

    # Title + intro
    c.setFont('Helvetica-Bold', 14)
    c.drawString(left, y, _safe(TITLE))
    y -= 22
    for line in INTRO_LINES:
        for chunk in _hard_wrap(_safe(line), 100):
            draw(chunk, 'Helvetica', 9)
    y -= 8

    for label, content in sections:
        # Keep the marker with at least one content line on the same page.
        if y < bottom + line_h * 2:
            c.showPage()
            y = top
        draw(marker(label), 'Courier-Bold', 9)

        for raw in content.split('\n'):
            if raw == '':
                if y < bottom:
                    c.showPage()
                    y = top
                y -= line_h
                continue
            for chunk in _hard_wrap(_safe(raw.replace('\t', '    ')), WRAP_COLS):
                draw(chunk)

    c.save()
    buf.seek(0)

    # Embed the exact, unicode-safe text so the PDF is restorable.
    reader = PdfReader(buf)
    writer = PdfWriter()
    writer.append(reader)
    writer.add_attachment(ATTACHMENT_NAME, build_payload(sections).encode('utf-8'))
    with open(path, 'wb') as f:
        writer.write(f)


def main():
    project_root = os.path.dirname(os.path.abspath(__file__))

    # Write the archive straight into OneDrive so it syncs without a manual move.
    onedrive = os.environ.get('OneDrive') or os.path.join(os.path.expanduser('~'), 'OneDrive')
    output_dir = onedrive if os.path.isdir(onedrive) else project_root

    docx_path = os.path.join(output_dir, DOCX_NAME)
    pdf_path = os.path.join(output_dir, PDF_NAME)
    unarchiver_path = os.path.join(output_dir, UNARCHIVER_NAME)

    skip_basenames = {DOCX_NAME, PDF_NAME, UNARCHIVER_NAME}
    sections = collect_sections(project_root, skip_basenames)

    build_docx(docx_path, sections)
    build_pdf(pdf_path, sections)
    with open(unarchiver_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(UNARCHIVER_SOURCE)

    print(f"\nArchived {len(sections)} file(s).")
    print("  docx     :", docx_path)
    print("  pdf      :", pdf_path)
    print("  restorer :", unarchiver_path)


if __name__ == "__main__":
    main()
