"""
Fine-grained discrimination test: in a library crowded with near-identical
tags (F-15 / F-15E / F-15EX / F-15J; 737 / 737 MAX / 737 NG / 737 Classic),
when an article is about ONE specific variant, does THAT variant's tag surface
to the user, or is it lost among its siblings?

Method: index each variant (article first-half, tagged with its code), then
query with a HELD-OUT slice (second-half) of one variant - simulating a fresh
document about it. Report the rank of the correct code and the top-5 codes, for
the lexical (fts), blended, and semantic-led rankers.

Run:  python eval_variants.py
"""
import os, tempfile, sys
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("CLIPPER_DB", tempfile.mktemp(suffix=".db"))
from db import connect, init_db
init_db()
import embed, suggest as SG
from app import app

DOCDIR = os.path.join("..", "TestDocs")
# slug -> code. Crowded aircraft library: two tight families + distractors.
ENT = {
 "air_f15":"F-15", "air_f15e":"F-15E", "air_f15ex":"F-15EX", "air_f15j":"F-15J",
 "air_f16":"F-16", "air_f14":"F-14", "air_f22":"F-22", "air_f35":"F-35",
 "air_f18":"F/A-18", "air_b52":"B-52",
 "air_737":"737", "air_737max":"737 MAX", "air_737ng":"737 NG",
 "air_737classic":"737 Classic", "air_747":"747",
}
CODES = set(ENT.values())
FAMILY = {  # the confusable siblings each query competes against
 "F-15":"F-15*","F-15E":"F-15*","F-15EX":"F-15*","F-15J":"F-15*",
 "737":"737*","737 MAX":"737*","737 NG":"737*","737 Classic":"737*",
}


def load():
    out = {}
    for slug in ENT:
        p = os.path.join(DOCDIR, slug + ".txt")
        if os.path.exists(p):
            out[slug] = open(p, encoding="utf-8").read()
    return out


def build(docs, half):
    """Library of article FIRST-halves, each tagged with its code + aircraft."""
    path = tempfile.mktemp(suffix=".db")
    init_db(db_path=path)
    old = os.environ.get("CLIPPER_DB")
    os.environ["CLIPPER_DB"] = path
    cl = app.test_client()
    for slug, text in docs.items():
        cl.post("/api/clip", json={"title": ENT[slug],
            "html": f"<p>{text[:half]}</p>", "tags": [ENT[slug], "aircraft"]})
    if old is not None:
        os.environ["CLIPPER_DB"] = old
    return connect(db_path=path)


def code_ranking(conn, title, query, **kw):
    sug = SG.suggest_tags(conn, title=title, text=query, limit=25,
                          suggest_new=False, **kw)
    return [s["tag"] for s in sug if s["tag"] in CODES]


def run(docs):
    HALF = 6000
    conn = build(docs, HALF)
    # A user regex covering aircraft designations, incl. digit-only / multi-word
    # ones the alnum-mix rule can't catch ("737 MAX") and future types ("F-47").
    air_re = r"\b(?:[A-Z]{1,2}[-/]?\d+[A-Z]{0,3}|7\d7(?:\s(?:MAX|NG|Classic|Original))?)\b"
    rankers = (("blend (no code)", dict(embed_blend=0.5, code_boost=1.0)),
               ("+code boost", dict(embed_blend=0.5, code_boost=2.5)),
               ("+code+regex", dict(embed_blend=0.5, code_boost=2.5, code_regex=air_re)))
    hits = {name: [0, 0] for name, _ in rankers}   # [hit@1, hit@3]
    total = 0
    print(f"  {'query (variant)':14}{'ranker':9}{'rank':6}{'top-5 codes (← surfaced to user)'}")
    print("  " + "-" * 76)
    for slug, code in ENT.items():
        if slug not in docs:
            continue
        # The article's title line (e.g. "... F-15E Strike Eagle (F-15E)") -
        # where the discriminating designation lives - plus a held-out body
        # slice, simulating a fresh document about this variant.
        title = docs[slug].split("\n", 1)[0]
        held = docs[slug][HALF:HALF + 6000]
        if len(held) < 400:           # too short to be a fair held-out query
            continue
        total += 1
        first = True
        for name, kw in rankers:
            order = code_ranking(conn, title, held, **kw)
            rank = (order.index(code) + 1) if code in order else 0
            if rank == 1: hits[name][0] += 1
            if rank and rank <= 3: hits[name][1] += 1
            tag = "MISS" if rank == 0 else f"#{rank}"
            top5 = ", ".join(order[:5])
            head = f"{code:14}" if first else " " * 14
            print(f"  {head}{name:9}{tag:6}{top5}")
            first = False
        print()
    conn.close()
    print("  " + "=" * 76)
    print(f"  Correct variant tag surfaced (n={total}):")
    for name, _ in rankers:
        h1, h3 = hits[name]
        print(f"    {name:9} #1: {h1}/{total}   top-3: {h3}/{total}")


if __name__ == "__main__":
    print("embedding backend:", embed.available(), "|", embed.status()["model"])
    docs = load()
    print(f"library: {len(docs)} aircraft variants "
          f"(F-15 x{sum(1 for s in docs if 'f15' in s)}, "
          f"737 x{sum(1 for s in docs if '737' in s)} + distractors)\n")
    run(docs)
