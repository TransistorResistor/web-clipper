"""Tests for tag-vocabulary management: rename / merge / delete across the
library, via /api/tags/bulk. Run: python3 test_tags_manage.py"""
import os, tempfile

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import connect, init_db
init_db()
import app
app.app.testing = True

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("  ok  -", name)
    else: failed += 1; print("  FAIL-", name)

c = app.app.test_client()

def add(title, body, tags):
    return c.post("/api/clip", json={"title": title, "html": f"<p>{body}</p>",
                                     "tags": tags}).get_json()["id"]

def tags_of(cid):
    return c.get(f"/api/clips/{cid}").get_json()["tags"]

def all_tags():
    return {t["name"]: t["n"] for t in c.get("/api/tags").get_json()["tags"]}

# Library: distinct bodies (so no dedupe), overlapping tag vocab. All tag
# names here have distinct normalised keys so upsert's near-duplicate
# merging doesn't pre-collapse the setup.
a = add("Alpha", "alpha body one unique", ["python", "research", "draft"])
b = add("Beta", "beta body two unique", ["python", "scraping"])
d = add("Gamma", "gamma body three unique", ["draft", "misc"])

print("rename: plain rename of a tag everywhere")
r = c.post("/api/tags/bulk", json={"op": "rename", "from": "misc", "to": "notes"}).get_json()
check("rename ok", r.get("ok") and r["status"] == "renamed")
check("clip shows new name", "notes" in tags_of(d) and "misc" not in tags_of(d))
check("old name gone from vocab", "misc" not in all_tags() and "notes" in all_tags())

print("rename onto an existing (different) tag becomes a merge")
r = c.post("/api/tags/bulk", json={"op": "rename", "from": "research", "to": "draft"}).get_json()
check("status merged", r.get("ok") and r["status"] == "merged")
check("alpha now carries target only", "draft" in tags_of(a) and "research" not in tags_of(a))
check("research removed from vocab", "research" not in all_tags())
check("draft count now 2", all_tags().get("draft") == 2)  # a, d

print("rename to a normalised-key variant merges (no near-duplicate created)")
# 'web-scraping' shares the key of an existing tag created here.
g = add("Web doc", "web doc body unique", ["web-scraping"])
r = c.post("/api/tags/bulk", json={"op": "rename", "from": "scraping", "to": "web scraping"}).get_json()
check("merged into existing key match", r.get("ok") and r["status"] == "merged")
check("no 'web scraping' duplicate", "web scraping" not in all_tags())
check("scraping folded into web-scraping", "web-scraping" in tags_of(b) and "scraping" not in tags_of(b))

print("cosmetic rename (case only) applies in place")
r = c.post("/api/tags/bulk", json={"op": "rename", "from": "python", "to": "Python"}).get_json()
check("renamed in place", r.get("ok") and r["status"] == "renamed")
names = list(all_tags().keys())
check("vocab shows new casing", "Python" in names and "python" not in names)

print("merge: fold several tags into one (created if new)")
e = add("Delta", "delta body four unique", ["py3", "cpython"])
r = c.post("/api/tags/bulk", json={"op": "merge", "names": ["py3", "cpython", "Python"],
                                   "into": "python-lang"}).get_json()
check("merge ok", r.get("ok"))
check("sources merged count", r["sources_merged"] == 3)
check("delta carries target", set(tags_of(e)) == {"python-lang"})
check("sources gone", all(n not in all_tags() for n in ("py3", "cpython", "Python")))
check("target count aggregates", all_tags().get("python-lang") == 3)  # a, b, e

print("merge into one of the sources keeps that name")
f1 = add("Eps", "eps body five unique", ["js"])
f2 = add("Zeta", "zeta body six unique", ["javascript"])
r = c.post("/api/tags/bulk", json={"op": "merge", "names": ["js", "javascript"],
                                   "into": "javascript"}).get_json()
check("merge into existing source ok", r.get("ok"))
check("js gone, javascript kept", "js" not in all_tags() and all_tags().get("javascript") == 2)

print("delete: remove a tag from every clip")
r = c.post("/api/tags/bulk", json={"op": "delete", "names": ["notes"]}).get_json()
check("delete ok", r.get("ok") and r["tags_deleted"] == 1)
check("clip lost the tag", "notes" not in tags_of(d))
check("vocab dropped it", "notes" not in all_tags())

print("FTS reflects renamed/merged tags (search by tag name)")
# 'python-lang' should now be searchable; old 'python' should not match.
res = c.get("/api/clips", query_string={"q": "python-lang"}).get_json()
check("search finds merged tag", any(cl["id"] in (a, b, e) for cl in res["clips"]))
res_old = c.get("/api/clips", query_string={"q": "cpython"}).get_json()
check("old tag name no longer indexed", not res_old["clips"])

print("validation")
check("unknown op 400", c.post("/api/tags/bulk", json={"op": "nope"}).status_code == 400)
check("rename missing fields 400",
      c.post("/api/tags/bulk", json={"op": "rename", "from": "x"}).status_code == 400)
check("merge missing into 400",
      c.post("/api/tags/bulk", json={"op": "merge", "names": ["a"]}).status_code == 400)
check("delete empty names 400",
      c.post("/api/tags/bulk", json={"op": "delete", "names": []}).status_code == 400)
check("rename of absent tag is a no-op, not an error",
      c.post("/api/tags/bulk", json={"op": "rename", "from": "ghost", "to": "z"}).get_json()["status"] == "source_missing")

print("tag cleanup: detectors")
import tagcleanup as TC
# Token-prefix merge candidates, with containment upgrading the score.
_names = ["F-35", "F-35 Program", "NATO", "NATO-US relations", "Chickens", "Bamboo"]
_clips = {"F-35": {1, 2, 3}, "F-35 Program": {1, 2}, "NATO": {9},
          "NATO-US relations": {10}, "Chickens": {5}, "Bamboo": {5, 6, 7}}
_mc = TC.merge_candidates(_names, _clips)
_by_target = {tuple(sorted(m["tags"])): m for m in _mc}
check("token-prefix family is a merge candidate",
      ("F-35", "F-35 Program") in _by_target)
check("containment upgrades the reason to name+subset",
      _by_target[("F-35", "F-35 Program")]["reason"] == "name+subset")
check("merge target is the parent (shorter) tag",
      _by_target[("F-35", "F-35 Program")]["target"] == "F-35")
check("name-only prefix (disjoint clips) still surfaced, lower score",
      _by_target[("NATO", "NATO-US relations")]["reason"] == "name"
      and _by_target[("NATO", "NATO-US relations")]["score"] == 1)
check("pure clip-subset WITHOUT a name relationship is NOT flagged (noise)",
      ("Bamboo", "Chickens") not in _by_target)

# Format outliers: all-lowercase designations only; mixed-case left alone.
_fo = {f["tag"]: f for f in TC.format_outliers(
    ["f15d", "ea-18g", "ea-18g growler", "MiG-29", "F-15E", "5th generation", "Bamboo"])}
check("all-lowercase code flagged", "f15d" in _fo and "ea-18g" in _fo)
check("suggestion uppercases the code token", _fo["ea-18g growler"]["suggest"] == "EA-18G growler")
check("mixed-case designation NOT flagged (MiG-29 is correct)", "MiG-29" not in _fo)
check("uppercase designation NOT flagged", "F-15E" not in _fo)
check("ordinal/word not mistaken for a code", "5th generation" not in _fo and "Bamboo" not in _fo)

print("tag cleanup: endpoint + dismiss")
add("Jet A", "jet body alpha unique", ["F-35", "F-35 Program"])
add("Jet B", "jet body bravo unique", ["F-35", "ea-18g"])
_rep = c.get("/api/tags/cleanup").get_json()
_sigs = [m["sig"] for m in _rep["merge"]]
check("endpoint surfaces the F-35 / F-35 Program merge",
      any(set(m["tags"]) == {"F-35", "F-35 Program"} for m in _rep["merge"]))
check("endpoint surfaces the ea-18g format outlier",
      any(f["tag"] == "ea-18g" for f in _rep["format"]))
_sig = next(m["sig"] for m in _rep["merge"] if set(m["tags"]) == {"F-35", "F-35 Program"})
check("dismiss requires a signature",
      c.post("/api/tags/cleanup", json={}).status_code == 400)
check("dismiss ok", c.post("/api/tags/cleanup", json={"dismiss": _sig}).get_json()["ok"])
_rep2 = c.get("/api/tags/cleanup").get_json()
check("dismissed candidate no longer returned",
      not any(m["sig"] == _sig for m in _rep2["merge"]))
# A format outlier is fixed with a cosmetic RENAME (same key, new display) -
# merging would be a no-op since the keys collide. After it the tag vanishes.
c.post("/api/tags/bulk", json={"op": "rename", "from": "ea-18g", "to": "EA-18G"})
check("renamed tag drops out of format outliers",
      not any(f["tag"] == "ea-18g" for f in c.get("/api/tags/cleanup").get_json()["format"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
