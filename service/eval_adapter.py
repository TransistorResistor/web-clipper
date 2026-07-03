"""
Does the Tier-1 tag-supervised adapter actually improve tag suggestion, or just
look good on a toy set? This is the go/no-go: it must beat raw gte-small on a
leakage-safe, real-data eval or it doesn't ship.

Setup: the OpenAlex corpus (build_eval_corpus.py) - real abstracts, real
multi-label tags. 5-fold cross-validation: in each fold the train clips are the
"existing library" AND the adapter's training data; the held-out clips are fresh
"uploads". For each held-out clip we run the core kNN tag-suggestion step - vote
tags from its nearest train clips, weighted by cosine - in three embedding
spaces:

    raw         gte-small as shipped
    whiten      Tier 0: PCA-whitened (unsupervised, no tags)
    supervised  Tier 1: whitened + tag-supervised linear projection

and score the suggested tags against the clip's true tags. The adapter is fit on
train only and the held-out clip is neither in the library nor in training, so
there's no leakage. Reports recall@5, precision@5, R-precision, MRR, plus
per-tag recall for the confusable clusters (the case the adapter should help most).

Run:  python eval_adapter.py
"""
import os, sys, json, hashlib
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
import embed
from tag_adapter import TagAdapter

K_NN = 10           # neighbours that vote
TOPK = 5            # suggestions evaluated
FOLDS = 5
SEED = 13


CORPUS_FILE = sys.argv[1] if len(sys.argv) > 1 else "corpus.json"


def load_corpus():
    c = json.load(open(CORPUS_FILE, encoding="utf-8"))
    return c


def get_embeddings(corpus):
    """Embed title+abstract with gte-small, cached on corpus content hash."""
    h = hashlib.sha1(json.dumps([c["id"] for c in corpus]).encode()).hexdigest()[:12]
    cache = f"corpus_emb_{embed.model_id().replace(':','_')}_{h}.npy"
    if os.path.exists(cache):
        return np.load(cache)
    texts = [((c["title"] or "") + "\n" + c["text"]) for c in corpus]
    V = embed.embed(texts)
    np.save(cache, V)
    return V


def knn_suggest_sims(sims, tags_tr, k=K_NN):
    """Vote tags from the k highest-similarity train clips. `sims` is a
    precomputed (n_train,) similarity vector. Returns ranked [(tag, score)]."""
    idx = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
    scores = {}
    for j in idx:
        for t in tags_tr[j]:
            scores[t] = scores.get(t, 0.0) + float(sims[j])
    return sorted(scores.items(), key=lambda kv: -kv[1])


def score_one(ranked, truth, topk=TOPK):
    pred = [t for t, _ in ranked[:topk]]
    tset = set(truth)
    hit = sum(1 for p in pred if p in tset)
    recall = hit / max(1, len(tset))
    prec = hit / max(1, len(pred))
    # R-precision: precision at k = #true tags
    r = len(tset)
    rp = sum(1 for t, _ in ranked[:r] if t in tset) / max(1, r)
    # MRR of first correct
    mrr = 0.0
    for i, (t, _) in enumerate(ranked):
        if t in tset:
            mrr = 1.0 / (i + 1)
            break
    return recall, prec, rp, mrr


def run():
    corpus = load_corpus()
    if not embed.available():
        print("embedding backend unavailable:", embed.status()["reason"]); return
    V = get_embeddings(corpus).astype(np.float64)
    tags = [c["tags"] for c in corpus]
    n = len(corpus)
    rng = np.random.default_rng(SEED)
    order = rng.permutation(n)
    folds = np.array_split(order, FOLDS)

    # raw / whiten / supervised, plus BLENDS of raw+supervised similarity
    # (how the adapter would actually deploy - complement, not replacement).
    modes = ["raw", "whiten", "supervised", "blend0.3", "blend0.5"]
    agg = {m: {"recall": [], "prec": [], "rp": [], "mrr": []} for m in modes}
    # per-tag recall@5 for confusable clusters (broad + hard families); keep
    # only the families whose tags actually appear in this corpus.
    present = {t for ts in tags for t in ts}
    all_clusters = {
        "ML": ["Machine learning", "Deep learning", "Computer vision",
               "Natural language processing", "Reinforcement learning"],
        "BIO": ["Genetics", "Genomics", "Immunology", "Neuroscience"],
        "AERO": ["Aerospace engineering", "Avionics", "Jet engine",
                 "Spin (aerodynamics)"],
        "NLP-tasks": ["Named-entity recognition", "Machine translation",
                      "Question answering", "Sentiment analysis",
                      "Automatic summarization", "Coreference"],
        "CV-tasks": ["Object detection", "Image segmentation", "Pose",
                     "Facial recognition system",
                     "Optical character recognition", "Image retrieval"],
        "DL-arch": ["Convolutional neural network", "Recurrent neural network",
                    "Generative adversarial network", "Transformer",
                    "Autoencoder", "Graph (abstract data type)"],
    }
    clusters = {cl: [t for t in ts if t in present]
                for cl, ts in all_clusters.items()
                if any(t in present for t in ts)}
    flat_cluster = {t: cl for cl, ts in clusters.items() for t in ts}
    tagrec = {m: {} for m in modes}   # mode -> tag -> [hits]
    # Cross-cutting binary axes: pairs where recall@k is insensitive but the
    # *ordering* of the two is the real test (does the model rank the correct
    # one above the wrong one?). Auto-enabled when both tags are present.
    axes = [("Review", "Primary research")]
    axes = [(a, b) for a, b in axes if a in present and b in present]
    axisacc = {m: [] for m in modes}

    for fi in range(FOLDS):
        te = folds[fi]
        tr = np.concatenate([folds[j] for j in range(FOLDS) if j != fi])
        Xtr_raw, Xte_raw = V[tr], V[te]
        tags_tr = [tags[i] for i in tr]
        # Fit the two adapter spaces once per fold; raw is identity.
        ad_raw = TagAdapter().fit(Xtr_raw, tags_tr, mode="identity")
        ad_wht = TagAdapter().fit(Xtr_raw, tags_tr, mode="whiten")
        ad_sup = TagAdapter().fit(Xtr_raw, tags_tr, mode="supervised")
        Xtr_raw_n = ad_raw.transform(Xtr_raw)
        Xtr_wht = ad_wht.transform(Xtr_raw)
        Xtr_sup = ad_sup.transform(Xtr_raw)
        for r, i in enumerate(te):
            qr = ad_raw.transform(Xte_raw[r:r + 1])[0]
            qw = ad_wht.transform(Xte_raw[r:r + 1])[0]
            qs = ad_sup.transform(Xte_raw[r:r + 1])[0]
            sims = {
                "raw": Xtr_raw_n @ qr,
                "whiten": Xtr_wht @ qw,
                "supervised": Xtr_sup @ qs,
            }
            sims["blend0.3"] = 0.7 * sims["raw"] + 0.3 * sims["supervised"]
            sims["blend0.5"] = 0.5 * sims["raw"] + 0.5 * sims["supervised"]
            for m in modes:
                ranked = knn_suggest_sims(sims[m], tags_tr)
                rec, prec, rp, mrr = score_one(ranked, tags[i])
                agg[m]["recall"].append(rec); agg[m]["prec"].append(prec)
                agg[m]["rp"].append(rp); agg[m]["mrr"].append(mrr)
                pred5 = {t for t, _ in ranked[:TOPK]}
                for t in tags[i]:
                    if t in flat_cluster:
                        tagrec[m].setdefault(t, []).append(1.0 if t in pred5 else 0.0)
                # cross-cutting axis: is the correct side ranked above the wrong?
                sc = dict(ranked)
                tset = set(tags[i])
                for a, b in axes:
                    if (a in tset) ^ (b in tset):
                        truth = a if a in tset else b
                        other = b if truth == a else a
                        axisacc[m].append(
                            1.0 if sc.get(truth, 0.0) > sc.get(other, 0.0) else 0.0)

    print(f"\ncorpus: {n} clips, {len({t for ts in tags for t in ts})} tags | "
          f"kNN k={K_NN}, top-{TOPK}, {FOLDS}-fold\n")
    print(f"  {'mode':12}{'recall@5':>10}{'prec@5':>9}{'R-prec':>9}{'MRR':>8}")
    print("  " + "-" * 46)
    for m in modes:
        a = agg[m]
        print(f"  {m:12}{np.mean(a['recall']):>10.3f}{np.mean(a['prec']):>9.3f}"
              f"{np.mean(a['rp']):>9.3f}{np.mean(a['mrr']):>8.3f}")

    if any(axisacc.values()):
        print(f"\n  cross-cutting axis accuracy (correct side ranked above wrong):")
        for m in modes:
            v = axisacc[m]
            print(f"    {m:12}{np.mean(v):>8.3f}   (n={len(v)})")

    show = ["raw", "supervised", "blend0.3"]
    print(f"\n  per-tag recall@5 on confusable clusters ({' -> '.join(show)}):")
    for cl, ts in clusters.items():
        print(f"   [{cl}]")
        for t in ts:
            vals = [np.mean(tagrec[m].get(t, [0])) for m in show]
            d = vals[-1] - vals[0]   # blend vs raw
            print(f"     {t:30} {vals[0]:.2f} -> {vals[1]:.2f} -> {vals[2]:.2f}"
                  f"   (blend vs raw {'+' if d>=0 else ''}{d:.2f})")


if __name__ == "__main__":
    run()
