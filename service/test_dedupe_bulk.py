"""Tests for dedupe-on-ingest and bulk operations. Run: python3 test_dedupe_bulk.py"""
import os, tempfile, io, base64

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import init_db
init_db()
import app
app.app.testing = True

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("  ok  -", name)
    else: failed += 1; print("  FAIL-", name)

c = app.app.test_client()

def make_pdf(text):
    import fitz
    d = fitz.open(); p = d.new_page(); p.insert_text((72, 72), text)
    b = d.tobytes(); d.close(); return b

print("dedupe: /api/clip content_hash")
clip = {"title": "A", "url": "https://x/a", "html": "<p>same body text</p>"}
r1 = c.post("/api/clip", json=clip).get_json()
check("first insert ok", r1.get("ok") and r1.get("id"))
r2 = c.post("/api/clip", json=clip).get_json()
check("second flagged duplicate", r2.get("ok") is False and r2["duplicate"]["matched_on"] == "content_hash")
r3 = c.post("/api/clip", json={**clip, "force": True}).get_json()
check("force overrides", r3.get("ok") and r3["id"] != r1["id"])

print("dedupe: attach-pdf by sha")
pdf = make_pdf("pdf body one")
def post_pdf(force=False):
    data = {"file": (io.BytesIO(pdf), "a.pdf"), "title": "PDF"}
    if force: data["force"] = "1"
    return c.post("/api/attach-pdf", data=data, content_type="multipart/form-data").get_json()
check("first pdf ok", post_pdf().get("ok"))
check("dup pdf flagged", post_pdf().get("ok") is False)
check("force pdf ok", post_pdf(force=True).get("ok"))

print("dedupe: cross-path pdf-primary via attachments")
r = c.post("/api/clip", json={"title": "wrap", "html": "<p>unrelated body</p>",
    "attachments": [{"filename": "a.pdf", "mime": "application/pdf",
                     "b64": base64.b64encode(pdf).decode()}]}).get_json()
check("matched on pdf-primary", r.get("ok") is False and r["duplicate"]["matched_on"] == "pdf-primary")

print("dedupe: upload preview + commit skip")
pdf2 = make_pdf("upload body two")
prev = c.post("/api/upload/preview", data={"file": (io.BytesIO(pdf2), "b.pdf")},
              content_type="multipart/form-data").get_json()
check("preview no dup initially", prev["duplicate"] is None)
com = c.post("/api/upload/commit", json={"token": prev["token"], "fields": prev["editable"], "tags": []}).get_json()
check("commit ok", com.get("ok"))
prev2 = c.post("/api/upload/preview", data={"file": (io.BytesIO(pdf2), "b.pdf")},
               content_type="multipart/form-data").get_json()
check("preview now flags dup", prev2["duplicate"] and prev2["duplicate"]["id"] == com["id"])
com2 = c.post("/api/upload/commit", json={"token": prev2["token"], "fields": prev2["editable"], "tags": []}).get_json()
check("commit skips dup", com2.get("ok") is False and com2.get("skipped"))
prev3 = c.post("/api/upload/preview", data={"file": (io.BytesIO(pdf2), "b.pdf")},
               content_type="multipart/form-data").get_json()
com3 = c.post("/api/upload/commit", json={"token": prev3["token"], "fields": prev3["editable"],
                                          "tags": [], "force": True}).get_json()
check("force commit ok", com3.get("ok"))

print("bulk: add/remove/set/delete")
ids = [c.post("/api/clip", json={"title": f"B{i}", "html": f"<p>body {i} qqq</p>"}).get_json()["id"] for i in range(4)]
check("add_tags", c.post("/api/clips/bulk", json={"ids": ids[:3], "op": "add_tags", "tags": ["x", "y"]}).get_json()["ok"])
d0 = c.get(f"/api/clips/{ids[0]}").get_json()
check("tags applied", "x" in d0["tags"] and "y" in d0["tags"])
c.post("/api/clips/bulk", json={"ids": ids[:3], "op": "remove_tags", "tags": ["x"]})
d0 = c.get(f"/api/clips/{ids[0]}").get_json()
check("tag removed", "x" not in d0["tags"] and "y" in d0["tags"])
c.post("/api/clips/bulk", json={"ids": [ids[3]], "op": "set_tags", "tags": ["only"]})
check("set replaces", c.get(f"/api/clips/{ids[3]}").get_json()["tags"] == ["only"])
c.post("/api/clips/bulk", json={"ids": [ids[0]], "op": "add_tags", "tags": ["y", "z"]})
check("add dedupes", c.get(f"/api/clips/{ids[0]}").get_json()["tags"].count("y") == 1)
c.post("/api/clips/bulk", json={"ids": ids[:2], "op": "delete"})
check("delete removes", c.get(f"/api/clips/{ids[0]}").status_code == 404)
check("delete spares others", c.get(f"/api/clips/{ids[2]}").status_code == 200)

print("bulk: validation")
check("empty ids -> 400", c.post("/api/clips/bulk", json={"ids": [], "op": "delete"}).status_code == 400)
check("bad op -> 400", c.post("/api/clips/bulk", json={"ids": [ids[2]], "op": "nope"}).status_code == 400)
check("non-int ids -> 400", c.post("/api/clips/bulk", json={"ids": ["a"], "op": "delete"}).status_code == 400)

print("bulk folder ingest: /api/upload/bulk imports, tags, dedupes")
def make_pdf_meta(text, **meta):
    import fitz
    d = fitz.open(); p = d.new_page(); p.insert_text((72, 72), text)
    if meta: d.set_metadata(meta)
    b = d.tobytes(); d.close(); return b

def post_bulk(pdf_bytes, relpath, extra=None):
    data = {"file": (io.BytesIO(pdf_bytes), relpath.rsplit("/", 1)[-1]),
            "relpath": relpath}
    data.update(extra or {})
    return c.post("/api/upload/bulk", data=data, content_type="multipart/form-data")

pdfF = make_pdf_meta("folder ingest body with zzkwprobe token",
                     keywords="alphakw; betakw")
r = post_bulk(pdfF, "root/sub/dir/report.pdf", {"tags": "sharedzz, betakw"}).get_json()
check("imported", r.get("ok") and r["status"] == "imported" and r.get("id"))
d = c.get(f"/api/clips/{r['id']}").get_json()
check("PDF keywords auto-applied as tags", "alphakw" in d["tags"] and "betakw" in d["tags"])
check("shared tag applied without duplicating a keyword",
      "sharedzz" in d["tags"] and d["tags"].count("betakw") == 1)
check("relative path recorded in meta", d["meta"].get("relative_path") == "root/sub/dir/report.pdf")
check("pdf-primary asset attached", any(a["role"] == "pdf-primary" for a in d["assets"]))
check("PDF body searchable in the library",
      r["id"] in [cl["id"] for cl in c.get("/api/clips", query_string={"q": "zzkwprobe"}).get_json()["clips"]])
r2 = post_bulk(pdfF, "root/other/copy.pdf").get_json()
check("re-ingesting the same bytes reports the duplicate",
      r2["status"] == "duplicate" and r2["duplicate"]["id"] == r["id"])

print("bulk folder ingest: review flag and validation")
pdfR = make_pdf_meta("review flag body qqrw")
r3 = post_bulk(pdfR, "root/rev.pdf", {"review": "1"}).get_json()
pend = c.get("/api/review/pending").get_json()["pending"]
check("review=1 surfaces the clip on the review queue",
      any(p["id"] == r3["id"] for p in pend))
r4 = post_bulk(make_pdf_meta("no review body qqnr"), "root/norev.pdf").get_json()
pend = c.get("/api/review/pending").get_json()["pending"]
check("default import is not flagged for review",
      not any(p["id"] == r4["id"] for p in pend))
check("missing file -> 400",
      c.post("/api/upload/bulk", data={}, content_type="multipart/form-data").status_code == 400)
check("non-PDF file -> 400",
      post_bulk(b"just some text bytes", "root/notes.txt").status_code == 400)
check("generic metadata title falls back to the filename",
      c.get(f"/api/clips/{r3['id']}").get_json()["title"] == "rev")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
