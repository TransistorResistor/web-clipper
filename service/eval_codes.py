"""
Broader jargon evaluation across alphanumeric-code domains (chemical formulae,
gene/protein symbols, aircraft designations) comparing the LEXICAL (fts),
BLENDED (fts+embeddings), and SEMANTIC-LED rankers.

Two parts:
  1. ARTIFACT PROBE - cosine among minimal "code-only" sentences, where the
     subword-shape artifact bites hardest (little context to rescue it).
  2. DISAMBIGUATION - a library holding an entity AND its shape-sibling; a short
     query mentions ONE code. Which code-tag does each ranker attach? This is
     where the blend's exact-token precision should beat the semantic leg.

Run:  python eval_codes.py
"""
import os, glob, tempfile, sys
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("CLIPPER_DB", tempfile.mktemp(suffix=".db"))
from db import connect, init_db
init_db()
import embed, suggest as SG
from app import app

DOCDIR = os.path.join("..", "TestDocs")
# slug -> (code, domain, shape-group). Shape-group members share leading tokens.
ENT = {
 "water":("H2O","chem","H2"), "hydrogen-peroxide":("H2O2","chem","H2"),
 "sulfuric-acid":("H2SO4","chem","H2"), "sodium-hydroxide":("NaOH","chem","Na"),
 "sodium-chloride":("NaCl","chem","Na"), "ammonia":("NH3","chem",""),
 "gene_brca1":("BRCA1","gene","BRCA"), "gene_brca2":("BRCA2","gene","BRCA"),
 "gene_tp53":("TP53","gene",""), "gene_egfr":("EGFR","gene",""),
 "gene_kras":("KRAS","gene",""),
 "air_f22":("F-22","air","F"), "air_f35":("F-35","air","F"),
 "air_b52":("B-52","air",""), "air_737":("737","air","7x7"),
 "air_747":("747","air","7x7"),
}
CODES = {c for c, _, _ in ENT.values()}


def load():
    out = {}
    for slug in ENT:
        p = os.path.join(DOCDIR, slug + ".txt")
        if os.path.exists(p):
            out[slug] = open(p, encoding="utf-8").read()
    return out


def part1_artifact():
    import numpy as np
    print("=" * 78)
    print("1. ARTIFACT PROBE - cosine of minimal code-only sentences")
    print("   (shared subword prefix inflates similarity when context is thin)\n")
    groups = {
      "chem H2*": [("H2O", "the synthesis of H2O"),
                   ("H2O2", "the synthesis of H2O2"),
                   ("H2SO4", "the synthesis of H2SO4"),
                   ("NaOH", "the synthesis of NaOH")],
      "gene BRCA*": [("BRCA1", "a mutation in the BRCA1 gene"),
                     ("BRCA2", "a mutation in the BRCA2 gene"),
                     ("TP53", "a mutation in the TP53 gene"),
                     ("KRAS", "a mutation in the KRAS gene")],
      "aircraft F-*": [("F-22", "the F-22 fighter aircraft"),
                       ("F-35", "the F-35 fighter aircraft"),
                       ("B-52", "the B-52 bomber aircraft"),
                       ("737", "the 737 airliner aircraft")],
    }
    for gname, items in groups.items():
        labels = [c for c, _ in items]
        V = embed.embed([s for _, s in items])
        print(f"  [{gname}] cosine matrix:")
        print("        " + "".join(f"{l:>8}" for l in labels))
        for i, l in enumerate(labels):
            row = "".join(f"{float(V[i]@V[j]):8.2f}" for j in range(len(labels)))
            print(f"    {l:>6}{row}")
        print()


def _build_library(docs):
    path = tempfile.mktemp(suffix=".db")
    init_db(db_path=path)
    old = os.environ.get("CLIPPER_DB")
    os.environ["CLIPPER_DB"] = path
    cl = app.test_client()
    for slug, text in docs.items():
        code, dom, _ = ENT[slug]
        cl.post("/api/clip", json={"title": code, "html": f"<p>{text[:6000]}</p>",
                                   "tags": [code, dom]})
    if old is not None:
        os.environ["CLIPPER_DB"] = old
    return connect(db_path=path)


def _top_codes(conn, query, **kw):
    sug = SG.suggest_tags(conn, text=query, limit=10, suggest_new=False, **kw)
    return [s["tag"] for s in sug if s["tag"] in CODES][:3]


def part2_disambig(docs):
    conn = _build_library(docs)
    # (query sentence, expected code, sibling code present in library)
    PROBES = [
      ("hydrogen peroxide H2O2 is used as a mild oxidizer and bleaching agent",
       "H2O2", "H2O"),
      ("the BRCA2 protein loads RAD51 onto DNA during homologous recombination",
       "BRCA2", "BRCA1"),
      ("the carrier-based F/A-18 multirole fighter flown by the US Navy",
       "F/A-18", "F-22"),
      ("the Boeing 747 is a four-engine wide-body long-haul airliner",
       "747", "737"),
      ("caustic soda NaOH is a strong base used to digest samples", "NaOH", "NaCl"),
    ]
    print("=" * 78)
    print("2. DISAMBIGUATION - library holds the entity AND its shape-sibling.")
    print("   Does each ranker attach the RIGHT code? (✓ correct / ✗ sibling)\n")
    print(f"  {'query mentions':10}{'sib':8}{'fts':16}{'blend':16}{'emb-led':16}")
    for q, want, sib in PROBES:
        cols = []
        for kw in (dict(use_embeddings=False), dict(embed_blend=0.5),
                   dict(embed_blend=1.0)):
            tops = _top_codes(conn, q, **kw)
            top = tops[0] if tops else "-"
            mark = "✓" if top == want else ("✗SIB" if top == sib else "·")
            cols.append(f"{top} {mark}")
        print(f"  {want:10}{sib:8}{cols[0]:16}{cols[1]:16}{cols[2]:16}")
    print()
    # SPARSE regime: a near-code-only query, where Part 1 showed embeddings
    # nearly equate shape-siblings. This isolates the blend's value - the FTS
    # exact-token leg should break the tie the embedding leg can't.
    SPARSE = [
      ("synthesis of H2O2", "H2O2", "H2O"),
      ("the BRCA2 variant", "BRCA2", "BRCA1"),
      ("BRCA1", "BRCA1", "BRCA2"),          # emb-led conflates -> BRCA2; FTS rescues
      ("the F-35 programme", "F-35", "F-22"),
      ("a Boeing 747", "747", "737"),       # FTS picks 737; embedding rescues
    ]
    print("  -- SPARSE queries (minimal context; the hard case) --")
    print(f"  {'query mentions':10}{'sib':8}{'fts':16}{'blend':16}{'emb-led':16}")
    for q, want, sib in SPARSE:
        cols = []
        for kw in (dict(use_embeddings=False), dict(embed_blend=0.5),
                   dict(embed_blend=1.0)):
            tops = _top_codes(conn, q, **kw)
            top = tops[0] if tops else "-"
            mark = "✓" if top == want else ("✗SIB" if top == sib else "·")
            cols.append(f"{top} {mark}")
        print(f"  {want:10}{sib:8}{cols[0]:16}{cols[1]:16}{cols[2]:16}")
    conn.close()
    print()
    print("  fts = lexical only · blend = fts+embeddings · emb-led = embeddings (blend=1)")


if __name__ == "__main__":
    print("embedding backend:", embed.available(), "|", embed.status()["model"], "\n")
    docs = load()
    print(f"loaded {len(docs)} docs across domains:",
          ", ".join(sorted({d for _, d, _ in (ENT[s] for s in docs)})), "\n")
    part1_artifact()
    part2_disambig(docs)
