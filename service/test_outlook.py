"""Tests for the Outlook import bridge (/api/outlook/*). The COM layer
(outlook.fetch_items) is monkeypatched with fake mail items, so no Outlook or
pywin32 is needed; the real ingestion path (PDF metadata, dedup, body clip,
attachments) runs end to end. Run: python3 test_outlook.py"""
import os, tempfile, base64

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import connect, init_db
init_db()
import app
app.app.testing = True
import outlook  # the module the endpoint imports lazily; patch it here

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("  ok  -", name)
    else: failed += 1; print("  FAIL-", name)

c = app.app.test_client()

# --- Build a real PDF (with authored Keywords) so the PDF path runs for real.
pdf_bytes = None
try:
    import fitz
    _d = fitz.open(); _pg = _d.new_page()
    _pg.insert_text((72, 90), "Quarterly Logistics Review", fontsize=20)
    _pg.insert_textbox(fitz.Rect(72, 130, 520, 740),
                       ("supply chain throughput filler prose " * 30), fontsize=10)
    _d.set_metadata({"title": "Quarterly Logistics Review",
                     "keywords": "logistics, supply chain"})
    pdf_bytes = _d.tobytes(); _d.close()
except Exception as _e:
    print("  (PDF path skipped: PyMuPDF unavailable:", _e, ")")

# A 1x1 PNG as a data: URI, standing in for a cid: image already resolved by
# outlook.py - the body importer should extract it into an asset.
_PNG = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)).decode()

ITEMS = {
    "PDF1": {
        "entry_id": "PDF1", "subject": "Q3 numbers attached",
        "html": "<p>See attached.</p>", "received_ts": 1700000000,
        "received_iso": "2023-11-14T22:13:20", "sender_name": "Dana Ops",
        "sender_email": "dana@example.com",
        "attachments": [{"filename": "q3.pdf", "mime": "application/pdf",
                         "bytes": pdf_bytes or b"%PDF-1.4 not-real", "inline": False}],
    },
    "BODY1": {
        "entry_id": "BODY1", "subject": "Weekly note",
        "html": f'<p>Hello team, here is the weekly note.</p><img src="data:image/png;base64,{_PNG}">',
        "received_ts": 1700100000, "received_iso": "2023-11-16T01:40:00",
        "sender_name": "Sam Lead", "sender_email": "sam@example.com",
        "attachments": [
            {"filename": "logo.png", "mime": "image/png", "bytes": b"\x89PNG\r\n", "inline": True},
            {"filename": "notes.txt", "mime": "text/plain", "bytes": b"plain text attachment", "inline": False},
        ],
    },
}

def fake_fetch(entry_ids):
    items, errors = [], []
    for eid in entry_ids:
        if eid in ITEMS:
            items.append(ITEMS[eid])
        else:
            errors.append({"entry_id": eid, "error": "not found"})
    return items, errors

outlook.fetch_items = fake_fetch

def imp(payload):
    return c.post("/api/outlook/import", json=payload).get_json()

def clip(cid):
    return c.get(f"/api/clips/{cid}").get_json()

print("status endpoint never 500s and reports availability")
sr = c.get("/api/outlook/status")
check("status 200", sr.status_code == 200)
check("status has 'available' flag", "available" in sr.get_json())

print("validation: entry_ids required")
check("empty -> 400", c.post("/api/outlook/import", json={}).status_code == 400)

if pdf_bytes:
    print("PDF attachment becomes its own clip with keyword auto-tags")
    r = imp({"entry_ids": ["PDF1"]})
    check("ok + one imported", r.get("ok") and r["imported"] == 1)
    res = r["results"][0]["items"][0]
    check("pdf imported", res["kind"] == "pdf" and res["status"] == "imported")
    d = clip(res["id"])
    check("title from PDF metadata", d["title"] == "Quarterly Logistics Review")
    check("keywords auto-applied as tags",
          {t.lower() for t in d["tags"]} >= {"logistics", "supply chain"})
    check("source marked outlook-pdf", d["source"] == "outlook-pdf")
    check("sender recorded in meta (not as a tag)",
          d["meta"].get("email_from_name") == "Dana Ops"
          and "dana ops" not in {t.lower() for t in d["tags"]})
    check("has the pdf asset",
          any(a["role"] == "pdf-primary" for a in d["assets"]))

    print("re-importing the same PDF is deduped")
    r2 = imp({"entry_ids": ["PDF1"]})
    check("second import is a duplicate", r2["imported"] == 0
          and r2["results"][0]["items"][0]["status"] == "duplicate")

print("PDF-only email without body import + no PDF -> skipped, not error")
r = imp({"entry_ids": ["BODY1"]})  # include_body defaults False; BODY1 has no PDF
only = r["results"][0]["items"]
check("nothing imported from body-less request", r["imported"] == 0)
check("reported as skipped", only[0]["status"] == "skipped")

print("include_body imports the body as an HTML clip + non-PDF attachment + inline img")
r = imp({"entry_ids": ["BODY1"], "include_body": True})
body_res = [x for x in r["results"][0]["items"] if x.get("kind") == "body"]
check("body imported", body_res and body_res[0]["status"] == "imported")
bd = clip(body_res[0]["id"])
check("body source outlook-email", bd["source"] == "outlook-email")
check("received date in meta", bd["meta"].get("article:published_time") == "2023-11-16T01:40:00")
check("inline data-URI image extracted to an asset",
      any(a["role"] == "inline-image" for a in bd["assets"]))
check("non-PDF attachment stored",
      any(a["role"] == "attachment" and a["filename"] == "notes.txt" for a in bd["assets"]))

print("a bad EntryID is reported, not fatal; good ones still import")
r = imp({"entry_ids": ["NOPE", "BODY1"], "include_body": True})
check("fetch error surfaced", any(e["entry_id"] == "NOPE" for e in r["fetch_errors"]))
check("good item still processed", len(r["results"]) == 1)

print("isolation: a thrown error mid-item is contained to that item")
_orig = app._import_email_body
def boom(*a, **k): raise RuntimeError("simulated failure")
app._import_email_body = boom
try:
    r = imp({"entry_ids": ["BODY1"], "include_body": True})
    check("endpoint still 200 with structured error",
          r.get("ok") and any(x.get("status") == "error"
                              for x in r["results"][0]["items"]))
finally:
    app._import_email_body = _orig

# --- Tag-review flow + toggle -------------------------------------------------
# Fresh, distinct mail items so dedup doesn't interfere with these assertions.
ITEMS.update({
    f"REV{i}": {
        "entry_id": f"REV{i}", "subject": f"Review subject {i}",
        "html": f"<p>Distinct review body {i} about tidal lagoon turbines.</p>",
        "received_ts": 1700200000 + i, "received_iso": "2023-11-17T00:00:0%d" % i,
        "sender_name": f"Sender {i}", "sender_email": f"s{i}@example.com",
        "attachments": [],
    } for i in range(4)
})

def pending_ids():
    return {p["id"] for p in c.get("/api/review/pending").get_json()["pending"]}

print("settings: review is on by default")
check("GET /api/settings default on",
      c.get("/api/settings").get_json().get("outlook_review") is True)

print("review on: imported clip is flagged and listed for review")
r = imp({"entry_ids": ["REV0"], "include_body": True})
check("response says review + has ids and url",
      r.get("review") is True and r.get("imported_ids") and r.get("review_url") == "/review")
rev0 = r["imported_ids"][0]
pend = c.get("/api/review/pending").get_json()["pending"]
row0 = next((p for p in pend if p["id"] == rev0), None)
check("clip appears in the pending queue", row0 is not None)
check("queue row carries sender + source",
      row0 and row0["from"] == "Sender 0" and row0["source"] == "outlook-email")

print("saving tags + marking reviewed removes it from the queue")
c.patch(f"/api/clips/{rev0}", json={"tags": ["tidal-energy"]})
check("reviewed endpoint ok",
      c.post(f"/api/clips/{rev0}/reviewed").get_json().get("ok") is True)
check("gone from queue", rev0 not in pending_ids())
check("tag persisted on the clip", "tidal-energy" in clip(rev0)["tags"])

print("per-import override: review:false imports silently (not flagged)")
r = imp({"entry_ids": ["REV1"], "include_body": True, "review": False})
check("response review false", r.get("review") is False)
check("clip not in review queue", r["imported_ids"][0] not in pending_ids())

print("server toggle off: imports are not flagged for review")
check("POST settings off",
      c.post("/api/settings", json={"outlook_review": False}).get_json()
      .get("outlook_review") is False)
r = imp({"entry_ids": ["REV2"], "include_body": True})  # no per-call override
check("review off in response", r.get("review") is False)
check("not flagged pending", r["imported_ids"][0] not in pending_ids())
# Restore default for any later runs against a shared DB.
check("POST settings back on",
      c.post("/api/settings", json={"outlook_review": True}).get_json()
      .get("outlook_review") is True)

print("rest of the app is unaffected by the Outlook feature")
check("/api/tags still works", c.get("/api/tags").status_code == 200)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
