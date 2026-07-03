"""
Demo: semantic (embedding) vs lexical (FTS) tag suggestion.

Shows the embedding ranker's value - matching on MEANING, not shared words -
and the graceful fallback when the bundled weights aren't present.

Run against the real library:
    CLIPPER_DB=../clipper.db python demo_embed.py
or point at any clipper DB via CLIPPER_DB. With no DB it uses an empty temp one
(so you still see the backend status + fallback behaviour).

Force the lexical fallback (to compare, or to simulate a weights-less host):
    CLIPPER_EMBED_MODEL_PATH=/nonexistent python demo_embed.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

if not os.environ.get("CLIPPER_DB"):
    import tempfile
    os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
    from db import init_db
    init_db()

import embed
import suggest as SG
from db import connect

# (title, body) probes spanning topics the demo library may or may not cover.
PROBES = [
    ("Attention Is All You Need",
     "we propose the Transformer, a neural sequence transduction model based "
     "wholly on attention mechanisms for machine translation, dispensing with "
     "recurrence and convolutions entirely"),
    ("Domestic rabbit husbandry",
     "raising backyard rabbits: hutch design, common breeds, gestation, and "
     "feeding a healthy doe and her kits"),
    ("Clumping bamboo in the garden",
     "propagating and containing running versus clumping bamboo groves, "
     "rhizome barriers, and cold-hardy cultivars"),
    ("Household budgeting basics",
     "an emergency fund, automating savings, and the factors a central bank "
     "weighs before raising interest rates"),
]


def fmt(suggestions):
    ex = [s["tag"] for s in suggestions if not s.get("new")]
    nw = [s["tag"] for s in suggestions if s.get("new")]
    return f"existing={ex or '—'}  new={nw[:3] or '—'}"


def main():
    st = embed.status()
    print("=" * 74)
    print("EMBEDDING BACKEND")
    print(f"  available : {st['available']}")
    print(f"  model     : {st['model']}  (dim {st['dim']})")
    print(f"  path      : {st['path']}")
    if not st["available"]:
        print(f"  reason    : {st['reason']}")
        print("  -> suggestions will use the lexical FTS ranker (fallback).")
    print("=" * 74)

    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    print(f"library: {n} clips\n")

    for title, body in PROBES:
        print(f"▶ {title}")
        for label, kw in (("blended (fts+embed)", dict(use_embeddings=True)),
                          ("lexical (FTS only) ", dict(use_embeddings=False))):
            dbg: dict = {}
            sug = SG.suggest_tags(conn, title=title, text=body, limit=6,
                                  _debug=dbg, **kw)
            print(f"    {label} [ran: {dbg['ranker']:9}]  {fmt(sug)}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
