"""Regression tests for the bug-fix batch (cache persistence, FK cleanup,
FTS fallback, revision spam, sanitization, query parsing, date bounds).
Run: python3 test_fixes.py"""
import os, tempfile, io, base64
from datetime import datetime, timezone

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import connect, init_db
init_db()
import app
app.app.testing = True
import search as S
from query import parse_query

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("  ok  -", name)
    else: failed += 1; print("  FAIL-", name)

c = app.app.test_client()

print("query parser: field values containing colons")
ast = parse_query("url:https://example.com/page")
check("url: keeps full value", ast["type"] == "field" and ast["value"] == "https://example.com/page")
ast = parse_query("marking:official")
check("marking: is a field", ast["type"] == "field" and ast["name"] == "marking")
ast = parse_query("C:\\foo something", default_op="OR")
check("unknown prefix stays a term", ast["children"][0]["type"] == "term")

print("search: term matching is word-boundary in eval AND windowing")
conn = connect()
S.ensure_cache(conn)
conn.execute(
    "INSERT INTO clips(title, captured_at, content_text, content_html) VALUES (?,?,?,?)",
    ("Cats", int(datetime(2024, 6, 1, 12, tzinfo=timezone.utc).timestamp()),
     "concatenate the strings please", "<p>concatenate the strings please</p>"))
conn.execute(
    "INSERT INTO clips(title, captured_at, content_text, content_html) VALUES (?,?,?,?)",
    ("CatDoc", int(datetime(2024, 6, 1, 12, tzinfo=timezone.utc).timestamp()),
     "my cat sat on the mat", "<p>my cat sat on the mat</p>"))
conn.commit()
res = S.search(conn, "cat", S.SearchOptions(default_op="OR"))
titles = [r["doc"].title for r in res["results"]]
check("'cat' skips 'concatenate'", "Cats" not in titles)
check("'cat' matches the word", "CatDoc" in titles)

print("search: small proximity k is not coerced to 10")
node = parse_query("alpha %0 beta")
check("k=0 parses and is honored (old bug coerced to 10)",
      node["k"] == 0 and not S._prox_match(node, "alpha one two beta"))
node1 = parse_query("alpha %1 beta")
check("k=1 matches adjacent words", S._prox_match(node1, "alpha beta gamma"))
check("k=1 rejects separated words", not S._prox_match(node1, "alpha one two beta"))

print("search: added_to includes the whole end day")
res = S.search(conn, "cat", S.SearchOptions(default_op="OR", added_to="2024-06-01"))
check("same-day clip included", "CatDoc" in [r["doc"].title for r in res["results"]])

print("search: asset_text cache persists across connections")
conn.execute("INSERT INTO assets(sha256, mime, bytes, byte_size, created_at) "
             "VALUES ('aa11', 'text/plain', ?, 5, 0)", (b"hello",))
conn.commit()
S.extract_asset_text(conn, "aa11", "text/plain")
conn.close()
conn2 = connect()
row = conn2.execute("SELECT text FROM asset_text WHERE sha256 = 'aa11'").fetchone()
check("cache row survived close", row is not None and row["text"] == "hello")
conn2.close()

print("delete: works after asset text has been cached (FK chain)")
r = c.post("/api/clip", json={"title": "T", "html": "<p>doc with attachment qzx</p>",
    "attachments": [{"filename": "n.txt", "mime": "text/plain",
                     "b64": base64.b64encode(b"note body").decode()}]}).get_json()
cid = r["id"]
conn3 = connect()
sha = conn3.execute("SELECT asset_sha FROM clip_assets WHERE clip_id = ? AND role='attachment'",
                    (cid,)).fetchone()["asset_sha"]
S.extract_asset_text(conn3, sha, "text/plain")   # populate + commit cache
conn3.close()
resp = c.delete(f"/api/clips/{cid}")
check("delete returns 200", resp.status_code == 200)
conn3 = connect()
check("asset gone", conn3.execute("SELECT 1 FROM assets WHERE sha256=?", (sha,)).fetchone() is None)
check("asset_text gone", conn3.execute("SELECT 1 FROM asset_text WHERE sha256=?", (sha,)).fetchone() is None)
conn3.close()

print("library search: FTS syntax errors fall back instead of 500")
for bad in ['"unclosed', 'c++', 'AND NOT (']:
    resp = c.get("/api/clips", query_string={"q": bad})
    check(f"q={bad!r} -> 200", resp.status_code == 200)

print("library search: junk int params fall back to defaults")
resp = c.get("/api/clips", query_string={"limit": "abc", "offset": "-5"})
check("junk limit/offset -> 200", resp.status_code == 200)

print("PATCH: tag-only saves write no revision; content edits write one")
cid = c.post("/api/clip", json={"title": "Rev", "html": "<p>original body zzq</p>"}).get_json()["id"]
c.patch(f"/api/clips/{cid}", json={"tags": ["a"]})
c.patch(f"/api/clips/{cid}", json={"tags": ["a", "b"]})
conn4 = connect()
n = conn4.execute("SELECT COUNT(*) FROM revisions WHERE clip_id=?", (cid,)).fetchone()[0]
check("no revisions after tag-only saves", n == 0)
c.patch(f"/api/clips/{cid}", json={"title": "Rev 2"})
n = conn4.execute("SELECT COUNT(*) FROM revisions WHERE clip_id=?", (cid,)).fetchone()[0]
check("one revision after title edit", n == 1)
conn4.close()

print("sanitization: event handlers and script vectors stripped")
cid = c.post("/api/clip", json={"title": "X", "html":
    '<p onclick="alert(1)">hi qqv</p><img src="x" onerror="alert(2)">'
    '<script>alert(3)</script><a href="javascript:alert(4)">link</a>'}).get_json()["id"]
html = c.get(f"/api/clips/{cid}").get_json()["content_html"]
check("onclick stripped", "onclick" not in html)
check("onerror stripped", "onerror" not in html)
check("script stripped", "<script" not in html.lower())
check("javascript: href stripped", "javascript:" not in html.lower())

print("assets: unsafe MIME types are not served inline")
conn5 = connect()
conn5.execute("INSERT INTO assets(sha256, mime, bytes, byte_size, created_at) "
              "VALUES ('bb22', 'text/html', ?, 20, 0)", (b"<script>bad</script>",))
conn5.commit(); conn5.close()
resp = c.get("/asset/bb22")
check("text/html served as octet-stream", resp.mimetype == "application/octet-stream")
check("forced download", "attachment" in (resp.headers.get("Content-Disposition") or ""))

print("annotations: GET list exists, POST to missing clip 404s")
check("GET annotations 200", c.get(f"/api/clips/{cid}/annotations").status_code == 200)
check("POST to missing clip 404", c.post("/api/clips/999999/annotations",
                                         json={"quote": "x"}).status_code == 404)

print("empty-content clips do not false-duplicate each other")
r1 = c.post("/api/clip", json={"title": "Img1", "html": '<img src="a.png">'}).get_json()
r2 = c.post("/api/clip", json={"title": "Img2", "html": '<img src="b.png">'}).get_json()
check("both image-only clips created", r1.get("ok") and r2.get("ok"))

print("user notes: stored, FTS-searchable, in export, edited in place")
ncid = c.post("/api/clip", json={
    "title": "Field log entry",
    "html": "<p>The quarterly survey records wingspanxyz measurements.</p>",
    "tags": ["fieldlog"],
    "note": "Reviewed by Tim ospreywatch priority"}).get_json()["id"]
det = c.get(f"/api/clips/{ncid}").get_json()
check("note stored and returned on create",
      det.get("user_note") == "Reviewed by Tim ospreywatch priority")

def _qids(q):  # clip ids matching a library FTS query
    return [cl["id"] for cl in
            c.get("/api/clips", query_string={"q": q}).get_json()["clips"]]
# 'ospreywatch' appears ONLY in the note - finding the clip proves the note is
# indexed in FTS (both the library quick search and the advanced search).
check("note text is FTS-searchable in the library", ncid in _qids("ospreywatch"))
check("advanced search finds a clip by its note",
      c.get("/api/search", query_string={"q": "ospreywatch"}).get_json()["count"] >= 1)

# Edit the note in place: no revision; tags survive in FTS; new note indexed.
c.patch(f"/api/clips/{ncid}", json={"note": "Updated ospreywatch totals reviewed"})
det = c.get(f"/api/clips/{ncid}").get_json()
check("note updated via PATCH", det.get("user_note") == "Updated ospreywatch totals reviewed")
connN = connect()
nrev = connN.execute("SELECT COUNT(*) FROM revisions WHERE clip_id=?", (ncid,)).fetchone()[0]
connN.close()
check("note-only PATCH writes no revision", nrev == 0)
# 'fieldlog' is only the tag (not in title/body/note) - still matching proves
# the note-edit's FTS refresh didn't drop the tags column.
check("tags survive in FTS after a note edit", ncid in _qids("fieldlog"))
check("the edited note is still FTS-searchable", ncid in _qids("ospreywatch"))

# The note leads the clip's entry in a search export, prefixed "User comment:".
res_md = c.get("/api/search", query_string={"q": "wingspanxyz"}).get_json()
check("md report leads the entry with the user comment",
      "**User comment:** Updated ospreywatch totals reviewed" in res_md["report"])
res_txt = c.get("/api/search", query_string={"q": "wingspanxyz", "format": "txt"}).get_json()
check("txt report shows the user comment",
      "User comment: Updated ospreywatch totals reviewed" in res_txt["report"])

# The suggest endpoint accepts a note (its tag-gen effect is unit-tested in
# test_suggest); here we just verify the plumbing accepts it.
check("suggest endpoint accepts a note",
      c.post("/api/tags/suggest", json={"title": "x", "text": "y", "note": "z"}).status_code == 200)

# Clearing the note drops it from the row AND from the FTS index.
c.patch(f"/api/clips/{ncid}", json={"note": "   "})
check("blank note clears to null", not c.get(f"/api/clips/{ncid}").get_json().get("user_note"))
check("cleared note no longer FTS-searchable", ncid not in _qids("ospreywatch"))

print("migration: user_note column added to a pre-existing clips table")
import sqlite3 as _sq
_old = tempfile.mktemp(suffix=".db")
_oc = _sq.connect(_old)
# The original clips schema, before the user_note column existed.
_oc.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY, url TEXT, "
            "canonical_url TEXT, title TEXT NOT NULL, captured_at INTEGER NOT NULL, "
            "source TEXT, content_html TEXT, content_text TEXT, content_hash TEXT, "
            "word_count INTEGER, reading_minutes INTEGER, language TEXT, "
            "meta_json TEXT, embedding BLOB)")
_oc.commit(); _oc.close()
init_db(_old)
_oc = _sq.connect(_old)
_cols = {r[1] for r in _oc.execute("PRAGMA table_info(clips)")}
_oc.close()
check("migration adds user_note to an old DB", "user_note" in _cols)

print("migration: clips_fts gains a note column and is repopulated")
_old2 = tempfile.mktemp(suffix=".db")
_o2 = _sq.connect(_old2)
# Original clips schema + the OLD 3-column FTS index (no note column).
_o2.executescript(
    "CREATE TABLE clips (id INTEGER PRIMARY KEY, url TEXT, canonical_url TEXT, "
    "title TEXT NOT NULL, captured_at INTEGER NOT NULL, source TEXT, "
    "content_html TEXT, content_text TEXT, content_hash TEXT, word_count INTEGER, "
    "reading_minutes INTEGER, language TEXT, meta_json TEXT, embedding BLOB);"
    "CREATE VIRTUAL TABLE clips_fts USING fts5(title, content_text, tags, "
    "tokenize='porter unicode61');")
_o2.execute("INSERT INTO clips(title, captured_at, content_text) VALUES (?,?,?)",
            ("Old clip", 0, "body about migrationtoken"))
_o2.execute("INSERT INTO clips_fts(rowid, title, content_text, tags) "
            "SELECT id, title, content_text, '' FROM clips")
_o2.commit(); _o2.close()
init_db(_old2)
_o2 = _sq.connect(_old2)
_fts2 = {r[1] for r in _o2.execute("PRAGMA table_info(clips_fts)")}
check("clips_fts gained the note column", "note" in _fts2)
check("existing clip still searchable after the FTS rebuild",
      _o2.execute("SELECT rowid FROM clips_fts WHERE clips_fts MATCH 'migrationtoken'"
                  ).fetchone() is not None)
_o2.close()

print("FTS: tags survive a meta-only UPDATE on clips (trigger regression)")
tcid = c.post("/api/clip", json={"title": "Trig", "html": "<p>trigger body qxw</p>",
                                 "tags": ["triggertag"]}).get_json()["id"]
connT = connect()
check("tag indexed after create", connT.execute(
    "SELECT 1 FROM clips_fts WHERE rowid=? AND clips_fts MATCH 'tags:triggertag'",
    (tcid,)).fetchone() is not None)
# A meta_json-only update (what /reviewed and backfill_norm do) used to fire
# the AU trigger with tags='' and wipe the clip's tags from the index.
connT.execute("UPDATE clips SET meta_json = '{}' WHERE id = ?", (tcid,))
connT.commit()
check("tag still indexed after meta-only update", connT.execute(
    "SELECT 1 FROM clips_fts WHERE rowid=? AND clips_fts MATCH 'tags:triggertag'",
    (tcid,)).fetchone() is not None)
connT.close()

print("migration: old tags='' FTS triggers are swapped and the index repaired")
_old3 = tempfile.mktemp(suffix=".db")
_o3 = _sq.connect(_old3)
# A 4-column FTS index (so the note migration stays quiet) with the OLD
# triggers, plus a clip whose FTS tags were already blanked by the bug.
_o3.executescript(
    "CREATE TABLE clips (id INTEGER PRIMARY KEY, url TEXT, canonical_url TEXT, "
    "title TEXT NOT NULL, captured_at INTEGER NOT NULL, source TEXT, "
    "content_html TEXT, content_text TEXT, content_hash TEXT, word_count INTEGER, "
    "reading_minutes INTEGER, language TEXT, meta_json TEXT, user_note TEXT, "
    "embedding BLOB);"
    "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE);"
    "CREATE TABLE clip_tags (clip_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, "
    "PRIMARY KEY (clip_id, tag_id));"
    "CREATE VIRTUAL TABLE clips_fts USING fts5(title, content_text, tags, note, "
    "tokenize='porter unicode61');"
    "CREATE TRIGGER clips_fts_au AFTER UPDATE ON clips BEGIN "
    "DELETE FROM clips_fts WHERE rowid = old.id; "
    "INSERT INTO clips_fts(rowid, title, content_text, tags, note) "
    "VALUES (new.id, new.title, COALESCE(new.content_text, ''), '', "
    "COALESCE(new.user_note, '')); END;")
_o3.execute("INSERT INTO clips(id, title, captured_at, content_text) VALUES (1, 'Old', 0, 'b')")
_o3.execute("INSERT INTO tags(id, name) VALUES (1, 'lostzzy')")
_o3.execute("INSERT INTO clip_tags(clip_id, tag_id) VALUES (1, 1)")
_o3.execute("INSERT INTO clips_fts(rowid, title, content_text, tags, note) "
            "VALUES (1, 'Old', 'b', '', '')")   # tags already wiped by the bug
_o3.commit(); _o3.close()
init_db(_old3)
_o3 = _sq.connect(_old3)
_sql = _o3.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                   "AND name='clips_fts_au'").fetchone()[0]
check("old trigger replaced by the tags-preserving one", "GROUP_CONCAT" in _sql)
check("blanked tags restored by the rebuild",
      _o3.execute("SELECT rowid FROM clips_fts WHERE clips_fts MATCH 'tags:lostzzy'"
                  ).fetchone() is not None)
_o3.close()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
