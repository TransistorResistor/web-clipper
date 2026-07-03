"""A/B audit of keyphrase (new-tag) proposal across four arms:
  RAKE              - the shipped extractor (`suggest.extract_keyphrases`);
  RAKE+clean        - + junk-stripping the candidate pool (`clean_keyphrase_candidates`);
  SEM               - RAKE pool re-ranked by embedding centrality (`rank_keyphrases_semantic`);
  SEM+clean         - clean the pool, then re-rank.

The 2x2 separates two effects: how much the CLEANING buys (RAKE -> RAKE+clean)
and how much the embedding RE-RANK buys on top (RAKE+clean -> SEM+clean).

The keyphrase signal proposes tags the library doesn't yet have, so there is no
clean ground truth. As a RELATIVE proxy we treat each clip's OWN tags as
"keywords a human chose for this document" and measure how well each arm's top
phrases line up with them - fair across arms on the same clips, even though the
absolute numbers are a proxy (many human tags are abstractive / designations
neither extractor can produce, lowering all arms equally).

A phrase MATCHES a gold tag when their normalised keys are equal, or all of the
gold tag's word-tokens appear in the phrase ("espresso machine" covers "espresso").
Metrics over clips with >=1 tag and some text: hit@k, MRR, prec@k, recall@k.

Usage:
  python eval_keyphrases.py            # the 2x2 table
  python eval_keyphrases.py --sweep    # sweep rake_blend for the SEM+clean arm
  python eval_keyphrases.py --show 12  # RAKE vs SEM+clean side-by-side, N clips
"""
import os, re, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("CLIPPER_DB", os.path.abspath(os.path.join("..", "clipper.db")))
import suggest as SG          # noqa: E402
import embed as EMB           # noqa: E402
from db import connect, tag_key  # noqa: E402

TEXT_CHARS = 4000
K = 5                         # proposal window scored
POOL = 25                     # candidate pool the clean/rerank arms work over
_WORDS = re.compile(r"[A-Za-z0-9]+")


def _toks(s: str) -> set:
    return {tag_key(w) for w in _WORDS.findall(s.lower())} - {""}


def _matches(phrase: str, gold: str) -> bool:
    pk, gk = tag_key(phrase), tag_key(gold)
    if not pk or not gk:
        return False
    if pk == gk:
        return True
    gt = _toks(gold)
    return bool(gt) and gt <= _toks(phrase)


def _any_gold(phrase: str, golds: set) -> bool:
    return any(_matches(phrase, g) for g in golds)


def produce(conn, total, ti, t, clean, semantic, k=K, blend=0.5, lam=0.6):
    """Ordered top-k phrases for one arm of the 2x2."""
    kp_title = SG._keyphrase_title(ti) if clean else ti
    pool = POOL if (clean or semantic) else k
    cands = SG.extract_keyphrases(t, title=kp_title, max_phrases=pool)
    if clean:
        cands = SG.clean_keyphrase_candidates(conn, cands, total)
    if semantic:
        cands = SG.rank_keyphrases_semantic(cands, t, ti, rake_blend=blend,
                                            mmr_lambda=lam)
    return [p for p, _ in cands[:k]]


def _score(phrases: list[str], golds: set, k: int = K):
    top = phrases[:k]
    ranks = [i for i, p in enumerate(top, 1) if _any_gold(p, golds)]
    hit = 1 if ranks else 0
    mrr = 1.0 / ranks[0] if ranks else 0.0
    prec = len(ranks) / len(top) if top else 0.0
    recovered = sum(1 for g in golds if any(_matches(p, g) for p in top))
    recall = recovered / len(golds) if golds else 0.0
    return hit, mrr, prec, recall


def _load(conn):
    clips = conn.execute(
        "SELECT id, COALESCE(title,'') ti, COALESCE(content_text,'') t FROM clips"
    ).fetchall()
    tags_by = {}
    for r in conn.execute(
            "SELECT ct.clip_id, t.name FROM clip_tags ct JOIN tags t ON t.id=ct.tag_id"):
        tags_by.setdefault(r["clip_id"], set()).add(r["name"])
    rows = []
    for c in clips:
        golds = tags_by.get(c["id"], set())
        if golds and (c["ti"].strip() or c["t"].strip()):
            rows.append((c["id"], c["ti"], c["t"][:TEXT_CHARS], golds))
    return rows


def _aggregate(conn, total, rows, clean, semantic, blend=0.5):
    H = M = P = R = 0.0
    n = 0
    for _cid, ti, t, golds in rows:
        ph = produce(conn, total, ti, t, clean, semantic, blend=blend)
        h, mr, p, r = _score(ph, golds)
        H += h; M += mr; P += p; R += r; n += 1
    n = n or 1
    return dict(hit=H / n, mrr=M / n, prec=P / n, recall=R / n)


def main():
    conn = connect()
    if not EMB.available():
        print("Embedding backend unavailable - SEM arms are no-ops. embed.status():",
              EMB.status())
        conn.close(); return
    total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    rows = _load(conn)
    print(f"DB: {os.environ['CLIPPER_DB']}")
    print(f"scored clips (>=1 tag + text): {len(rows)}   "
          f"embedder: {EMB.model_id()}   K={K}")
    if len(rows) < 4:
        print("\n(too few tagged clips for a meaningful A/B - add a corpus first)")
        conn.close(); return

    if "--show" in sys.argv:
        i = sys.argv.index("--show")
        n = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit() else 10
        shown = 0
        print("\nRAKE vs SEM+clean (clips where they differ). * = matches a gold tag")
        for _cid, ti, t, golds in rows:
            a = produce(conn, total, ti, t, clean=False, semantic=False)
            b = produce(conn, total, ti, t, clean=True, semantic=True)
            if a == b:
                continue
            shown += 1
            mark = lambda ph: [("*" if _any_gold(p, golds) else " ") + p for p in ph]
            print(f"\n  {ti[:64]!r}")
            print(f"    gold      : {sorted(golds)}")
            print(f"    RAKE      : {mark(a)}")
            print(f"    SEM+clean : {mark(b)}")
            if shown >= n:
                break
        print(f"\n({shown} differing clips shown)")
        conn.close(); return

    if "--sweep" in sys.argv:
        print("\nrake_blend sweep for the SEM+clean arm (1.0 == RAKE+clean order):")
        print(f"  {'blend':>6} {'hit@'+str(K):>7} {'MRR':>6} {'prec':>6} {'recall':>7}")
        for b in (0.0, 0.25, 0.5, 0.75, 1.0):
            a = _aggregate(conn, total, rows, clean=True, semantic=True, blend=b)
            print(f"  {b:>6.2f} {a['hit']:>7.0%} {a['mrr']:>6.2f} "
                  f"{a['prec']:>6.0%} {a['recall']:>7.0%}")
        conn.close(); return

    arms = [("RAKE", False, False), ("RAKE+clean", True, False),
            ("SEM", False, True), ("SEM+clean", True, True)]
    print(f"\n  {'mode':>10} {'hit@'+str(K):>7} {'MRR':>6} {'prec@'+str(K):>7} "
          f"{'recall@'+str(K):>9}")
    base = None
    for name, clean, semantic in arms:
        a = _aggregate(conn, total, rows, clean, semantic)
        if base is None:
            base = a
        print(f"  {name:>10} {a['hit']:>7.0%} {a['mrr']:>6.2f} "
              f"{a['prec']:>7.0%} {a['recall']:>9.0%}")
    print("\n  (RAKE->RAKE+clean = the cleaning gain; RAKE+clean->SEM+clean = the "
          "rerank gain.\n   --show to eyeball, --sweep to tune rake_blend.)")
    conn.close()


if __name__ == "__main__":
    main()
