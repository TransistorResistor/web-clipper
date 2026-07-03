"""
The decisive adapter test: a CROSS-CUTTING tag axis, orthogonal to topic.

Lesson from corpus.json (broad fields) and corpus_hard.json (fine intra-field
tasks): a generic encoder already separates anything *topical* - broad or fine -
because each topic/task has a distinctive vocabulary. So those corpora give the
tag-supervised adapter no headroom.

Where a generic embedding genuinely fails is a label that is NOT a topic: here,
document FUNCTION - "Review" vs "Primary research" - spread across many topics.
A review about NLP reads, semantically, much like primary NLP papers, so raw kNN
pulls it toward same-topic primaries and mislabels its function. The adapter must
learn a "review-ness" direction that is orthogonal to the dominant topical axis -
exactly the regime it was designed for.

Each work is tagged [topic, function]. The eval's decisive number is recall on
the FUNCTION tags (Review / Primary research); topic tags are easy for everyone
and just provide realistic multi-label context.

Run:  python build_crosscut_corpus.py [per_cell]   (default 28)
"""
import json, sys, time
from collections import Counter
from build_eval_corpus import _get, resolve_concept, reconstruct_abstract, MAILTO

sys.stdout.reconfigure(encoding="utf-8")

TOPICS = ["Machine learning", "Genetics", "Cancer", "Climate change",
          "Macroeconomics", "Materials science"]
# (function tag, OpenAlex type filter)
FUNCS = [("Review", "review"), ("Primary research", "article")]


def fetch(cid, wtype, n):
    sel = "id,title,abstract_inverted_index"
    url = (f"https://api.openalex.org/works?filter=concepts.id:{cid},"
           f"type:{wtype},has_abstract:true,language:en&per-page={n}"
           f"&select={sel}&mailto={MAILTO}")
    return _get(url).get("results", [])


def main():
    per_cell = int(sys.argv[1]) if len(sys.argv) > 1 else 28
    works = {}
    for tname in TOPICS:
        try:
            cid, canon = resolve_concept(tname)
        except Exception as e:
            print(f"  topic {tname} ERR {e!r}"); continue
        for fname, wtype in FUNCS:
            try:
                rs = fetch(cid, wtype, per_cell)
            except Exception as e:
                print(f"  {tname}/{fname} ERR {e!r}"); continue
            kept = 0
            for w in rs:
                text = reconstruct_abstract(w.get("abstract_inverted_index"))
                if len(text) < 250:
                    continue
                wid = w["id"].rsplit("/", 1)[-1]
                rec = works.setdefault(wid, {"id": wid,
                        "title": (w.get("title") or "")[:300],
                        "text": text, "tags": set()})
                rec["tags"].update([canon, fname])
                kept += 1
            print(f"  {tname:18} {fname:16} +{kept}")
            time.sleep(0.3)
    corpus = [{**w, "tags": sorted(w["tags"])} for w in works.values()]
    json.dump(corpus, open("corpus_crosscut.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    supp = Counter(t for c in corpus for t in c["tags"])
    print(f"\n  corpus: {len(corpus)} clips, {len(supp)} tags -> corpus_crosscut.json")
    print("  support:", dict(supp.most_common()))


if __name__ == "__main__":
    main()
