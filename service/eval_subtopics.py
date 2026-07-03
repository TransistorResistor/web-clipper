"""Leave-one-out audit of narrow-topic tag selection against the LIVE library.

Run it after any corpus change or ranker tweak to see whether on-topic docs
recover their own tags and whether unrelated tags bleed in. Unlike eval_codes /
eval_variants (which build a synthetic corpus), this scores the *actual* DB
(CLIPPER_DB, else ../clipper.db) so it measures your real coverage.

Method (leave-one-out): for each clip C, treat C's own tags as ground truth,
exclude C as a donor, ask suggest_tags for tags from C's title+text, and score
each EXISTING-tag suggestion (brand-new keyphrase tags are a separate signal and
excluded) as:
  hit              - the tag is one of C's own tags;
  in-neighbourhood - the tag lives on a clip sharing >=1 tag with C (its
                     sub-topic cluster): a reasonable borrow;
  bleed            - neither: a cross-topic leak.

A clip is *coverable* only if a sibling (another clip sharing a tag) exists;
clips with none are DESERTS - unrecoverable without more donors, listed as the
action items, excluded from recall.

Usage:
  python eval_subtopics.py          # audit the live corpus
  python eval_subtopics.py --sweep  # + sweep _EMBED_RESCUE_MARGIN (re-tune)
"""
import os, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("CLIPPER_DB", os.path.abspath(os.path.join("..", "clipper.db")))
import suggest as SG          # noqa: E402
from db import connect        # noqa: E402

TEXT_CHARS = 4000
TOPK = 3                      # rank window for per-tag recovery


def _load(conn):
    clips = conn.execute(
        "SELECT id, COALESCE(title,'') ti, COALESCE(content_text,'') t FROM clips"
    ).fetchall()
    tags_by, clips_with_tag = {}, {}
    for r in conn.execute(
            "SELECT ct.clip_id, t.name FROM clip_tags ct JOIN tags t ON t.id=ct.tag_id"):
        tags_by.setdefault(r["clip_id"], set()).add(r["name"])
        clips_with_tag.setdefault(r["name"], set()).add(r["clip_id"])
    return clips, tags_by, clips_with_tag


def _neighbourhood(cid, tags_by, clips_with_tag):
    """Tags in cid's sub-topic cluster, plus the set of sibling clip ids."""
    sibs = set()
    for t in tags_by.get(cid, ()):
        sibs |= clips_with_tag.get(t, set())
    sibs.discard(cid)
    nb = set(tags_by.get(cid, set()))
    for s in sibs:
        nb |= tags_by.get(s, set())
    return nb, sibs


def score(conn, clips, tags_by, clips_with_tag, **sg):
    """One leave-one-out pass; `sg` is forwarded to suggest_tags (e.g.
    min_embed_sim=... for the floor sweep). Returns an aggregates dict."""
    agg = dict(n_cov=0, hit1=0, hit3=0, prec_sum=0.0, prec_n=0, bleed=0,
               exist_total=0, deserts=[], bleed_rows=[], per_tag={})
    for c in clips:
        cid, Tc = c["id"], tags_by.get(c["id"], set())
        if not Tc:
            continue
        nb, sibs = _neighbourhood(cid, tags_by, clips_with_tag)
        try:
            sug = SG.suggest_tags(conn, title=c["ti"], text=c["t"][:TEXT_CHARS],
                                  exclude_clip_id=cid, suggest_new=False, limit=12, **sg)
        except Exception:
            continue
        exist = [s["tag"] for s in sug]            # suggest_new=False => all existing
        agg["exist_total"] += len(exist)
        if exist:
            agg["prec_sum"] += sum(1 for t in exist if t in Tc) / len(exist)
            agg["prec_n"] += 1
        off = [t for t in exist if t not in nb]
        agg["bleed"] += len(off)
        if off:
            agg["bleed_rows"].append((len(off), cid, c["ti"][:32], off[:4]))
        if not sibs:                                # desert
            agg["deserts"].append((cid, c["ti"][:42], sorted(Tc)))
            continue
        agg["n_cov"] += 1
        if exist[:1] and exist[0] in Tc:
            agg["hit1"] += 1
        if any(t in Tc for t in exist[:3]):
            agg["hit3"] += 1
        for t in Tc:
            if len(clips_with_tag.get(t, ())) >= 2:  # tag has another carrier
                rec, tot = agg["per_tag"].get(t, (0, 0))
                agg["per_tag"][t] = (rec + (1 if t in exist[:TOPK] else 0), tot + 1)
    return agg


def main():
    sweep = "--sweep" in sys.argv
    conn = connect()
    SG.ensure_clip_embeddings(conn)
    clips, tags_by, clips_with_tag = _load(conn)
    n_clips = len(clips)
    n_tagged = sum(1 for c in clips if tags_by.get(c["id"]))
    print(f"DB: {os.environ['CLIPPER_DB']}")
    print(f"clips={n_clips}  tagged={n_tagged}  distinct tags={len(clips_with_tag)}")
    if n_tagged < 4:
        print("\n(too few tagged clips for a meaningful audit - add a corpus first)")
        conn.close(); return

    if sweep:
        orig = SG._EMBED_RESCUE_MARGIN
        print("\nRescue-margin sweep (recall vs quality):")
        print(f"  {'margin':>7} {'hit@1':>7} {'hit@3':>7} {'prec':>6} "
              f"{'clean%':>7} {'bleed/clip':>11}")
        for m in (0.0, 0.01, 0.02, 0.03, 0.04, 0.06):
            SG._EMBED_RESCUE_MARGIN = m
            a = score(conn, clips, tags_by, clips_with_tag)
            cov = a["n_cov"] or 1
            prec = a["prec_sum"] / (a["prec_n"] or 1)
            clean = 1 - a["bleed"] / (a["exist_total"] or 1)   # in-cluster share
            print(f"  {m:>7.2f} {a['hit1']/cov:>7.0%} {a['hit3']/cov:>7.0%} "
                  f"{prec:>6.0%} {clean:>7.0%} {a['bleed']/n_clips:>11.2f}")
        print("  prec = own-tag precision · clean% = suggestions inside the "
              "clip's tag-cluster (higher = less cross-topic bleed)")
        SG._EMBED_RESCUE_MARGIN = orig
        print(f"  (current _EMBED_RESCUE_MARGIN = {orig})")
        conn.close(); return

    if "--floor" in sys.argv:
        print("\nEmbedding-floor sweep (min_embed_sim - the eligibility cut):")
        print(f"  {'floor':>7} {'hit@1':>7} {'hit@3':>7} {'prec':>6} "
              f"{'clean%':>7} {'bleed/clip':>11} {'deserts':>8}")
        for f in (0.80, 0.82, 0.84, 0.86, 0.88):
            a = score(conn, clips, tags_by, clips_with_tag, min_embed_sim=f)
            cov = a["n_cov"] or 1
            prec = a["prec_sum"] / (a["prec_n"] or 1)
            clean = 1 - a["bleed"] / (a["exist_total"] or 1)
            print(f"  {f:>7.2f} {a['hit1']/cov:>7.0%} {a['hit3']/cov:>7.0%} "
                  f"{prec:>6.0%} {clean:>7.0%} {a['bleed']/n_clips:>11.2f} "
                  f"{len(a['deserts']):>8}")
        print("  Higher floor = stricter eligibility: fewer cross-type donors "
              "(less bleed) but more deserts. Mention signal is NOT gated by it.")
        conn.close(); return

    a = score(conn, clips, tags_by, clips_with_tag)
    cov = a["n_cov"] or 1
    print(f"\nCoverable clips (have a sibling donor): {a['n_cov']}  "
          f"| deserts: {len(a['deserts'])}")
    print("  hit@1     %5.0f%%   (top suggestion is one of the clip's own tags)" % (100*a["hit1"]/cov))
    print("  hit@3     %5.0f%%   (an own tag in the top 3)" % (100*a["hit3"]/cov))
    print("  precision %5.0f%%   (mean: own tags / existing suggestions)"
          % (100*a["prec_sum"]/(a["prec_n"] or 1)))
    print("  bleed     %5.2f    (mean out-of-neighbourhood tags per clip)"
          % (a["bleed"]/n_clips))

    print("\nPer-tag recovery (tags on >=2 clips; held-out carrier finds the tag in top-%d):" % TOPK)
    rows = sorted(a["per_tag"].items(), key=lambda kv: (kv[1][0]/kv[1][1], kv[1][1]))
    for tag, (rec, tot) in rows:
        bar = "#" * rec + "." * (tot - rec)
        print(f"  {rec}/{tot} {bar:<8} {tag}")

    if a["bleed_rows"]:
        print("\nWorst cross-topic bleed (clip -> leaked tags):")
        for n, cid, ti, off in sorted(a["bleed_rows"], reverse=True)[:8]:
            print(f"  {n:>2}  #{cid} {ti!r} -> {off}")

    if a["deserts"]:
        print("\nDeserts (no sibling donor - add a clip sharing one of these tags):")
        for cid, ti, ts in a["deserts"][:12]:
            print(f"  #{cid} {ti!r}  tags={ts}")
    conn.close()


if __name__ == "__main__":
    main()
