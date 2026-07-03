"""
Evaluate the semantic ranker on jargon / chemical formulae with REAL documents
(Wikipedia chemistry articles in ../TestDocs). Two questions:

  A. Does full-document context distinguish confusable-shape compounds, or do
     embeddings still conflate by shared subword pieces (H2O~H2O2~H2SO4 share
     'h ##2'; NaOH~NaCl share 'na')?
  B. In the actual suggester, leave-one-out: query each compound's text against
     a library of the OTHER compounds and see which compound's tag the ranker
     borrows - i.e. what it thinks the doc is "nearest" to. A shape-sibling is a
     bad (artifact) conflation; a chemically-sensible neighbour is acceptable.

Run:  python eval_jargon.py
"""
import os, glob, tempfile, sys
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("CLIPPER_DB", tempfile.mktemp(suffix=".db"))
from db import connect, init_db
init_db()
import embed, suggest as SG
from app import app as _app  # noqa: ensures same module wiring

DOCDIR = os.path.join("..", "TestDocs")
# slug -> formula, and shape-sibling groups (share leading subword tokens).
FORMULA = {"water":"H2O","hydrogen-peroxide":"H2O2","sulfuric-acid":"H2SO4",
           "sodium-hydroxide":"NaOH","sodium-chloride":"NaCl","ammonia":"NH3",
           "glucose":"C6H12O6","methane":"CH4","acetic-acid":"CH3COOH",
           "ethanol":"C2H5OH"}
SHAPE = {"water":"H2*","hydrogen-peroxide":"H2*","sulfuric-acid":"H2*",
         "sodium-hydroxide":"Na*","sodium-chloride":"Na*"}


def load():
    docs = {}
    for p in sorted(glob.glob(os.path.join(DOCDIR, "*.txt"))):
        slug = os.path.splitext(os.path.basename(p))[0]
        docs[slug] = open(p, encoding="utf-8").read()
    return docs


def part_a(docs):
    import numpy as np
    slugs = list(docs)
    V = embed.embed([docs[s] for s in slugs])
    print("=" * 78)
    print("A. Embedding nearest-neighbours among real chemistry articles")
    print("   (model reads ~the lead section; formula appears there)\n")
    for i, s in enumerate(slugs):
        sims = sorted(((float(V[i] @ V[j]), slugs[j])
                       for j in range(len(slugs)) if j != i), reverse=True)
        top = ", ".join(f"{n}({c:.2f})" for c, n in sims[:3])
        shp = SHAPE.get(s, "")
        flag = ""
        # flag if the #1 neighbour is a shape-sibling (potential artifact)
        if shp and SHAPE.get(sims[0][1]) == shp:
            flag = "  <-- #1 is a SHAPE-sibling"
        print(f"  {s:18}{FORMULA[s]:9} -> {top}{flag}")
    print()


def part_b(docs):
    print("=" * 78)
    print("B. TRUE leave-one-out: for each query, build a library of ONLY the")
    print("   other 9 compounds (query compound absent), then see which")
    print("   compound's tag the ranker borrows = what it's 'nearest' to.\n")
    print(f"  {'query':18}{'':9} {'EMBEDDING borrows':24} {'FTS borrows':18}")
    for slug in docs:
        others = {s: t for s, t in docs.items() if s != slug}
        emb = _loo_pick(others, docs[slug], use_embeddings=True)
        fts = _loo_pick(others, docs[slug], use_embeddings=False)
        def mark(pick):
            if pick is None:
                return "(nothing)"
            sib = SHAPE.get(slug) and SHAPE.get(pick) == SHAPE.get(slug)
            return f"{pick}{' [shape-sib]' if sib else ''}"
        print(f"  {slug:18}{FORMULA[slug]:9} {mark(emb):24} {mark(fts):18}")
    print()


def _loo_pick(library: dict, query_text: str, use_embeddings: bool):
    """Build a fresh DB from `library` (slug->text, each tagged with its slug +
    'chemistry'), query it, and return the top borrowed *compound* tag."""
    import tempfile
    path = tempfile.mktemp(suffix=".db")
    init_db(db_path=path)
    conn = connect(db_path=path)
    from app import app
    cl = app.test_client()
    # app's test_client writes to CLIPPER_DB; point it at this temp DB.
    old = os.environ.get("CLIPPER_DB")
    os.environ["CLIPPER_DB"] = path
    try:
        for slug, text in library.items():
            cl.post("/api/clip", json={"title": slug.replace('-', ' '),
                "html": f"<p>{text[:8000]}</p>", "tags": [slug, "chemistry"]})
        sug = SG.suggest_tags(conn, text=query_text[:6000], limit=8,
                              suggest_new=False, use_embeddings=use_embeddings)
        for s in sug:
            if s["tag"] != "chemistry" and s["tag"] in FORMULA:
                return s["tag"]
        return None
    finally:
        conn.close()
        if old is not None:
            os.environ["CLIPPER_DB"] = old


if __name__ == "__main__":
    print("embedding backend available:", embed.available(), "|", embed.status()["model"])
    docs = load()
    if not docs:
        print("No docs in", DOCDIR); sys.exit(1)
    part_a(docs)
    part_b(docs)
