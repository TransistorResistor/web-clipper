#!/usr/bin/env python3
"""End-to-end test: build a docx with image, ingest via migrator, verify."""
import base64
import os
import sys
import subprocess
import tempfile
from pathlib import Path

# Build a minimal docx using docx-js, then run the migrator on the directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "service"))

from db import connect, init_db, get_db_path  # noqa: E402

# Tiny red png for embed test
RED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

def build_docx(out_path: Path, extra_text: str = ""):
    """Create a tiny test .docx by emulating one with python's zipfile.

    extra_text makes the document's content unique - identical content is
    now deduped by the migrator, so distinct pages need distinct text."""
    import zipfile
    # A docx is a ZIP with a known set of files. We hand-roll the minimum.
    content_types = '''<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    main_rels = '''<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    doc_rels = '''<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>'''
    doc_xml = '''<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
  xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>My Test Page</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section A</w:t></w:r></w:p>
    <w:p><w:r><w:t>Hello from OneNote, this is some body text about Python and SQLite.EXTRA_TEXT</w:t></w:r></w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline distT="0" distB="0" distL="0" distR="0">
            <wp:extent cx="914400" cy="914400"/>
            <wp:docPr id="1" name="img"/>
            <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
                <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
                  <pic:blipFill><a:blip r:embed="rId1"/></pic:blipFill>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
  </w:body>
</w:document>'''
    doc_xml = doc_xml.replace("EXTRA_TEXT", (" " + extra_text) if extra_text else "")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", main_rels)
        z.writestr("word/document.xml", doc_xml)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/media/image1.png", RED_PNG)


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Two pages in a "section" subfolder so the migrator picks up the
        # folder name as a tag. Distinct body text (identical content would
        # be deduped) but the same embedded image (exercises asset dedup).
        # The third file duplicates the first, so it must be skipped.
        (td / "Section A").mkdir()
        build_docx(td / "Section A" / "My Test Page.docx")
        build_docx(td / "Section A" / "Another Page.docx",
                   extra_text="This second page has different words entirely.")
        build_docx(td / "Section A" / "Re-exported Copy.docx")

        # Use a temp DB so we don't clobber an existing one.
        db_path = td / "test.db"
        os.environ["CLIPPER_DB"] = str(db_path)
        init_db()

        # Run the migrator's folder mode
        result = subprocess.run(
            [sys.executable, str(HERE / "migrate.py"), "folder", str(td),
             "--tag", "test-migration"],
            env={**os.environ, "CLIPPER_DB": str(db_path)},
            capture_output=True, text=True,
        )
        print("STDOUT:", result.stdout)
        if result.returncode:
            print("STDERR:", result.stderr)
            return 1

        # Now query the DB to verify
        conn = connect(str(db_path))
        rows = conn.execute(
            "SELECT id, title, word_count FROM clips ORDER BY id"
        ).fetchall()
        print(f"\n{len(rows)} clips in DB:")
        for r in rows:
            print(f"  #{r['id']}: {r['title']} ({r['word_count']} words)")
        if len(rows) != 2:
            print(f"EXPECTED 2 clips (3 files, 1 duplicate skipped), got {len(rows)}")
            return 1
        if "skipped 1 duplicate" not in result.stdout:
            print("EXPECTED migrator output to report 1 skipped duplicate")
            return 1

        # Check tags
        print("\nTags:")
        for r in conn.execute("""
            SELECT t.name, COUNT(*) n FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id
            GROUP BY t.id ORDER BY t.name
        """):
            print(f"  {r['name']}: {r['n']}")

        # Check FTS works
        print("\nSearch 'Python':")
        for r in conn.execute(
            "SELECT c.id, c.title FROM clips_fts JOIN clips c ON c.id = clips_fts.rowid "
            "WHERE clips_fts MATCH 'Python'"
        ):
            print(f"  #{r['id']}: {r['title']}")

        # Check assets
        n_assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        n_links = conn.execute("SELECT COUNT(*) FROM clip_assets").fetchone()[0]
        print(f"\n{n_assets} unique assets, {n_links} clip-asset links (dedup working? {n_assets == 1 and n_links == 2})")

        # Verify the HTML had its image rewritten to /asset/<sha>
        html = conn.execute("SELECT content_html FROM clips WHERE id = 1").fetchone()[0]
        print(f"\nHTML preview (first 200 chars): {html[:200]}")

        return 0


if __name__ == "__main__":
    sys.exit(main())
