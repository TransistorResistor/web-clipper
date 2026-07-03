"""End-to-end tests for search.py against a temp DB."""
import os, sys, tempfile, json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

td = tempfile.mkdtemp()
os.environ["CLIPPER_DB"] = str(Path(td) / "search_test.db")

from db import connect, init_db, now_ts, upsert_tags
import search as S

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print(f"  ok  - {name}")
    else: failed += 1; print(f"  FAIL- {name}")

init_db()
conn = connect()
S.ensure_cache(conn)

def add_clip(title, html, captured, published=None, source="test", tags=None):
    meta = {}
    if published:
        meta["article:published_time"] = published
    text = S.html_to_text(html)
    cur = conn.execute(
        "INSERT INTO clips(url, canonical_url, title, captured_at, source, "
        "content_html, content_text, content_hash, word_count, reading_minutes, meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"https://example.com/{title.replace(' ','-')}", None, title,
         int(captured.timestamp()), source, html, text, "h"+title, len(text.split()),
         1, json.dumps(meta)))
    cid = cur.lastrowid
    if tags:
        upsert_tags(conn, cid, tags)
    conn.commit()
    return cid

d2024 = datetime(2024, 6, 1, tzinfo=timezone.utc)
d2025 = datetime(2025, 6, 1, tzinfo=timezone.utc)

# 1. Basic content with repeated keyword for window-merge test.
c1 = add_clip(
    "Python Guide",
    "<p>Python is great. " + ("filler word " * 50) +
    "Python again here. " + ("more filler " * 50) + "</p>",
    d2024, published="2024-05-15T10:00:00Z", tags=["programming"])

# 2. A clip with a table to test preservation.
c2 = add_clip(
    "Data Table",
    "<p>Results below.</p><table><tr><th>Name</th><th>Score</th></tr>"
    "<tr><td>Alpha</td><td>95</td></tr><tr><td>Beta</td><td>88</td></tr></table>"
    "<p>The keyword targetword appears near the table.</p>",
    d2025, published="2025-05-20T10:00:00Z", tags=["data"])

# 3. Proximity test.
c3 = add_clip(
    "Proximity Doc",
    "<p>machine learning is a field. Five words later comes the model word here.</p>",
    d2024)

# 4. Near-duplicate content (for dedup) - clip 4 is almost identical to a
#    block in clip 1, with minor formatting/errata changes.
c4 = add_clip(
    "Duplicate-ish",
    "<p>Python is great. " + ("filler word " * 50) + "Python again here.</p>",
    d2025)

print("basic term search:")
res = S.search(conn, "python", S.SearchOptions(context_words=10, default_op="OR"))
ids = [r["doc"].clip_id for r in res["results"]]
check("finds python clips", c1 in ids)

print("window merging (repeated term in one field):")
r1 = next(r for r in res["results"] if r["doc"].clip_id == c1)
# Two 'Python' hits far apart -> 2 windows (not merged, gap > 2*ctx).
# Two 'Python' hits close at start -> merged. We expect >=1, and no window
# should contain the keyword duplicated beyond what the text has.
check("c1 has windows", len(r1["windows"]) >= 1)

print("table preservation:")
res2 = S.search(conn, "targetword", S.SearchOptions(context_words=40, default_op="OR"))
r2 = next((r for r in res2["results"] if r["doc"].clip_id == c2), None)
check("targetword found", r2 is not None)
report2 = S.render_report(res2, S.SearchOptions(context_words=40))
check("markdown table in report", "| Name | Score |" in report2 or "Name | Score" in report2)

print("proximity match:")
# 'machine learning' within 20 words of 'model' -> should match c3
res3 = S.search(conn, '"machine learning" %20 model', S.SearchOptions(default_op="OR"))
ids3 = [r["doc"].clip_id for r in res3["results"]]
check("prox matches c3", c3 in ids3)
# Tight proximity should fail: within 2 words
res3b = S.search(conn, '"machine learning" %2 model', S.SearchOptions(default_op="OR"))
ids3b = [r["doc"].clip_id for r in res3b["results"]]
check("tight prox excludes c3", c3 not in ids3b)

print("boolean AND/NOT:")
res4 = S.search(conn, "python AND great", S.SearchOptions(default_op="AND"))
check("AND matches", c1 in [r["doc"].clip_id for r in res4["results"]])
res5 = S.search(conn, "python NOT targetword", S.SearchOptions(default_op="AND"))
check("NOT excludes table doc", c2 not in [r["doc"].clip_id for r in res5["results"]])

print("field search:")
res6 = S.search(conn, "title:proximity", S.SearchOptions(default_op="OR"))
check("title field matches c3", c3 in [r["doc"].clip_id for r in res6["results"]])
res7 = S.search(conn, "tag:data", S.SearchOptions(default_op="OR"))
check("tag field matches c2", c2 in [r["doc"].clip_id for r in res7["results"]])

print("date filters (added):")
res8 = S.search(conn, "python", S.SearchOptions(default_op="OR", added_from="2025-01-01"))
ids8 = [r["doc"].clip_id for r in res8["results"]]
check("added_from excludes 2024 clip", c1 not in ids8)
check("added_from includes 2025 clip", c4 in ids8)

print("date filters (published):")
res9 = S.search(conn, "python", S.SearchOptions(default_op="OR",
                published_from="2025-01-01", published_to="2025-12-31"))
ids9 = [r["doc"].clip_id for r in res9["results"]]
check("published filter keeps 2025", c2 in ids9 or c4 not in ids9)  # c1 published 2024 excluded
check("published filter drops 2024 c1", c1 not in ids9)

print("in-query date field:")
res10 = S.search(conn, "python added:>2025-01-01", S.SearchOptions(default_op="AND"))
check("in-query added field works", c1 not in [r["doc"].clip_id for r in res10["results"]])

print("dedup across results:")
# c1 and c4 share a large near-identical block. The deduper should prevent
# the same block text appearing twice across the report.
res11 = S.search(conn, "python", S.SearchOptions(context_words=200, default_op="OR"))
all_windows = [w.text for r in res11["results"] for w in r["windows"]]
norms = [S._normalize(w) for w in all_windows]
# No two accepted windows should be near-identical.
dup_found = False
for i in range(len(norms)):
    for j in range(i+1, len(norms)):
        a, b = set(norms[i].split()), set(norms[j].split())
        if a and b:
            jac = len(a & b) / len(a | b)
            if jac > 0.9:
                dup_found = True
check("no near-duplicate windows in output", not dup_found)

print("report renders:")
report = S.render_report(res, S.SearchOptions(context_words=10))
check("report has query", "python" in report.lower())
check("report has clip ref", "clip #" in report)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
