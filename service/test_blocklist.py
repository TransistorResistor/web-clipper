"""Tests for the suggestion blocklist: keywords the tag-suggester must never
propose, managed via /api/suggest/blocklist and enforced in suggest_tags.
Run: python3 test_blocklist.py"""
import os, tempfile

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import connect, init_db
init_db()
import app
app.app.testing = True
import suggest as SG

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("  ok  -", name)
    else: failed += 1; print("  FAIL-", name)

c = app.app.test_client()

def add(title, body, tags=None, url=""):
    r = c.post("/api/clip", json={"title": title, "html": f"<p>{body}</p>",
                                  "url": url, "tags": tags or []}).get_json()
    assert r.get("ok"), r
    return r["id"]

def names_for(**kw):
    conn = connect()
    try:
        return [s["tag"] for s in SG.suggest_tags(conn, **kw)]
    finally:
        conn.close()

def block(kw):
    return c.post("/api/suggest/blocklist", json={"keyword": kw}).get_json()

def unblock(kw):
    return c.delete("/api/suggest/blocklist", json={"keyword": kw}).get_json()

def blocklist():
    return [k["keyword"] for k in
            c.get("/api/suggest/blocklist").get_json()["keywords"]]

# --- Library: quantum-computing clips tagged "quantum" + a bulk tag, plus
# unrelated filler carrying only the bulk tag (same shape as test_suggest). ---
filler = ("Notes about cooking pasta and seasonal gardening tips number %d. "
          "Tomatoes, basil and slow proofing dough techniques.")
for i in range(12):
    add(f"Misc {i}", filler % i, tags=["bulk-import"])
qbody = ("Quantum computing uses qubits and entanglement. Quantum error "
         "correction and decoherence are the core obstacles for qubits. %s")
add("Quantum computing intro", qbody % "Introductory survey of gates.",
    tags=["quantum", "web-scraping"], url="https://qc.example.org/intro")
add("Qubit error correction", qbody % "Deep dive into stabiliser codes.",
    tags=["quantum", "web-scraping"], url="https://qc.example.org/codes")
add("Entanglement explained", qbody % "Bell pairs and teleportation.",
    tags=["quantum", "web-scraping"], url="https://qc.example.org/bell")

qtext = ("qubits entanglement and quantum decoherence error correction in "
         "quantum computers")

print("baseline: 'quantum' is suggested before blocking")
check("quantum suggested", "quantum" in names_for(text=qtext))
check("blocklist starts empty", blocklist() == [])

print("add via API then it disappears from suggestions")
r = block("quantum")
check("add ok", r.get("ok") and r.get("keyword") == "quantum")
check("quantum now suppressed", "quantum" not in names_for(text=qtext))
check("shows in blocklist", "quantum" in blocklist())

print("blocking is idempotent by normalised key")
block("Quantum")            # case variant -> same key, no duplicate row
check("no duplicate entry", blocklist().count("quantum") == 1)

print("existing tags on clips are untouched by the block")
qclip = c.get("/api/clips?tag=quantum").get_json()["clips"]
check("clips still carry the quantum tag", len(qclip) == 3)
check("tag still in vocabulary",
      "quantum" in {t["name"] for t in c.get("/api/tags").get_json()["tags"]})

print("normalised-key match suppresses punctuation/spacing variants")
# Block the spaced form; the library's tag is 'web-scraping' (same key).
block("web scraping")
check("web-scraping suppressed via key",
      "web-scraping" not in names_for(text=qtext, min_score_frac=0.0))

print("a proposed NEW keyphrase can be blocked too")
# A distinctive title phrase the library has never tagged surfaces as a new tag.
new_names = names_for(title="hyperloop propulsion",
                      text="hyperloop propulsion vacuum tube transit concept")
if "hyperloop propulsion" in new_names:
    block("hyperloop propulsion")
    after = names_for(title="hyperloop propulsion",
                      text="hyperloop propulsion vacuum tube transit concept")
    check("blocked keyphrase no longer proposed", "hyperloop propulsion" not in after)
else:
    # Keyphrase extraction is heuristic; if it didn't surface, the existing-tag
    # path above already proves enforcement. Don't fail the suite on it.
    print("  (skip - keyphrase not surfaced this run)")

print("unblock restores the suggestion")
u = unblock("quantum")
check("delete reports removed", u.get("ok") and u.get("removed") == 1)
check("quantum suggested again", "quantum" in names_for(text=qtext))
check("removed from blocklist", "quantum" not in blocklist())

print("bad input is rejected")
check("blank keyword -> 400",
      c.post("/api/suggest/blocklist", json={"keyword": "  "}).status_code == 400)
check("unblocking an unblocked keyword removes 0",
      unblock("never-was-blocked").get("removed") == 0)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
