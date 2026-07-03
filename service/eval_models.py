"""
Embedding-model bake-off: gte-small vs gte-base (fp32) vs gte-base int8.

Decision gate (EMBEDDING_ROADMAP.md §1): adopt a base model only if it beats
gte-small on tag-suggestion quality by a margin that justifies ~2x vector
storage (384 -> 768 dim) and ~3x embed latency; and int8 should keep base's
quality while cutting size/RAM.

Method: for each model, embed a labelled corpus (mean-pool, 256 tokens, L2-norm -
identical to embed.py), then run the SAME leakage-safe 5-fold kNN tag-suggestion
eval as eval_adapter.py, in RAW space (no adapter), so the only variable is the
encoder. Also reports weight size, embed latency, dim, and int8-vs-fp32 cosine
fidelity. Quality metric is gate-independent (pure ranking), so no per-model
min_embed_sim recalibration is needed just to compare encoders.

Run:  python eval_models.py [corpus.json]
"""
import os, sys, json, io, time
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
MAX_TOKENS = 256
_BATCH = 4   # small batch: this box has ~1GB free RAM; base spills to pagefile
K_NN, TOPK, FOLDS, SEED = 10, 5, 5, 13

# tag -> (weights dir, quantize?)
MODELS = {
    "gte-small":     (os.path.join(MODELS_DIR, "gte-small"), False),
    "gte-base":      (os.path.join(MODELS_DIR, "gte-base"), False),
    "gte-base-int8": (os.path.join(MODELS_DIR, "gte-base"), True),
}


def _fake_quant_int8_(model):
    """Simulate int8 dynamic quantisation by quantising every Linear WEIGHT to
    per-output-channel symmetric int8 and writing back the dequantised values,
    so normal float inference reflects int8-weight error exactly. Returns the
    realistic int8 footprint (Linear weights at 1 byte, everything else at its
    native size).

    Why this and not torch.quantization.quantize_dynamic: this torch-2.10+cpu
    build exposes only the `onednn` engine, whose dynamic qlinear kernel errors
    on our inputs, and torchao isn't installed - so no working int8 CPU kernel.
    Weight-only fake-quant captures the dominant (weight) quantisation error;
    it omits activation quantisation, so it's a mild UPPER bound on int8 quality
    and does NOT measure int8 runtime speed (unobtainable on this build)."""
    import torch, torch.nn as nn
    lin_weight_ids = set()
    with torch.no_grad():
        for m in model.modules():
            if isinstance(m, nn.Linear):
                W = m.weight.data
                scale = W.abs().amax(dim=1, keepdim=True) / 127.0
                scale = scale.clamp(min=1e-12)
                m.weight.data = (torch.round(W / scale).clamp(-127, 127)) * scale
                lin_weight_ids.add(id(m.weight))
    nbytes = sum(p.numel() * (1 if id(p) in lin_weight_ids else p.element_size())
                 for p in model.parameters())
    return nbytes


def load_model(path, quantize):
    import torch
    from transformers import AutoModel, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(path, local_files_only=True)
    model = AutoModel.from_pretrained(path, local_files_only=True).eval()
    int8_bytes = _fake_quant_int8_(model) if quantize else None
    return tok, model, int8_bytes


def embed_texts(tok, model, texts):
    import torch
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), _BATCH):
            batch = [t or "" for t in texts[i:i + _BATCH]]
            enc = tok(batch, padding=True, truncation=True,
                      max_length=MAX_TOKENS, return_tensors="pt")
            mo = model(**enc)
            tokens = mo[0]
            mask = enc["attention_mask"].unsqueeze(-1).type_as(tokens)
            pooled = (tokens * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            out.append(pooled.cpu().numpy().astype("float32"))
    return np.vstack(out)


# --- kNN tag-suggestion scoring (same as eval_adapter, inlined) ---
def knn_vote(sims, tags_tr, k=K_NN):
    idx = np.argpartition(-sims, min(k, len(sims) - 1))[:k]
    sc = {}
    for j in idx:
        for t in tags_tr[j]:
            sc[t] = sc.get(t, 0.0) + float(sims[j])
    return sorted(sc.items(), key=lambda kv: -kv[1])


def score_one(ranked, truth):
    pred = [t for t, _ in ranked[:TOPK]]; tset = set(truth)
    hit = sum(1 for p in pred if p in tset)
    rec = hit / max(1, len(tset)); prec = hit / max(1, len(pred))
    r = len(tset)
    rp = sum(1 for t, _ in ranked[:r] if t in tset) / max(1, r)
    mrr = 0.0
    for i, (t, _) in enumerate(ranked):
        if t in tset:
            mrr = 1.0 / (i + 1); break
    return rec, prec, rp, mrr


def kfold_eval(V, tags):
    V = V.astype(np.float64)
    n = len(tags)
    rng = np.random.default_rng(SEED)
    folds = np.array_split(rng.permutation(n), FOLDS)
    acc = {"recall": [], "prec": [], "rp": [], "mrr": []}
    for fi in range(FOLDS):
        te = folds[fi]; tr = np.concatenate([folds[j] for j in range(FOLDS) if j != fi])
        Xtr = V[tr]; tags_tr = [tags[i] for i in tr]
        for r, i in enumerate(te):
            sims = Xtr @ V[i]
            rec, prec, rp, mrr = score_one(knn_vote(sims, tags_tr), tags[i])
            acc["recall"].append(rec); acc["prec"].append(prec)
            acc["rp"].append(rp); acc["mrr"].append(mrr)
    return {k: float(np.mean(v)) for k, v in acc.items()}


def main():
    corpus_file = sys.argv[1] if len(sys.argv) > 1 else "corpus.json"
    corpus = json.load(open(corpus_file, encoding="utf-8"))
    texts = [((c["title"] or "") + "\n" + c["text"]) for c in corpus]
    tags = [c["tags"] for c in corpus]
    print(f"corpus: {corpus_file}  ({len(corpus)} clips, "
          f"{len({t for ts in tags for t in ts})} tags)\n")

    import hashlib
    chash = hashlib.sha1("".join(c["id"] for c in corpus).encode()).hexdigest()[:10]
    rows = []
    vecs = {}
    for tag, (path, quant) in MODELS.items():
        if not os.path.isdir(path):
            print(f"  [skip] {tag}: weights dir missing ({path})"); continue
        wsize = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)
                    if f.endswith((".safetensors", ".bin")))
        cache = f"modelvec_{tag}_{chash}.npy"
        meta = cache + ".meta"
        # int8 footprint is cheap to recompute only if we load; cache stores it.
        if os.path.exists(cache):
            V = np.load(cache)
            dt, int8_bytes = json.load(open(meta)) if os.path.exists(meta) else (float("nan"), None)
            print(f"  [cached] {tag}: dim={V.shape[1]}")
        else:
            tok, model, int8_bytes = load_model(path, quant)
            t0 = time.time(); V = embed_texts(tok, model, texts); dt = time.time() - t0
            np.save(cache, V); json.dump([dt, int8_bytes], open(meta, "w"))
            del tok, model
            print(f"  embedded with {tag}: {dt:.1f}s, dim={V.shape[1]}")
        vecs[tag] = V
        disk = (int8_bytes if int8_bytes else wsize) / 1e6
        m = kfold_eval(V, tags)
        rows.append((tag, disk, V.shape[1], dt, m))

    print(f"\n  {'model':16}{'MB':>7}{'dim':>6}{'embed_s':>9}"
          f"{'recall@5':>10}{'prec@5':>9}{'R-prec':>9}{'MRR':>8}")
    print("  " + "-" * 74)
    for tag, disk, dim, dt, m in rows:
        print(f"  {tag:16}{disk:>7.0f}{dim:>6}{dt:>9.1f}"
              f"{m['recall']:>10.3f}{m['prec']:>9.3f}{m['rp']:>9.3f}{m['mrr']:>8.3f}")

    # int8 fidelity vs fp32 base
    if "gte-base" in vecs and "gte-base-int8" in vecs:
        cos = np.sum(vecs["gte-base"] * vecs["gte-base-int8"], axis=1)
        print(f"\n  int8 vs fp32-base per-vector cosine: "
              f"mean={cos.mean():.4f} min={cos.min():.4f}")
    print("\n  NB: 'int8' = weight-only fake-quant (quality + size are real; a")
    print("      mild upper bound as it omits activation quant). embed_s for it")
    print("      runs in float - this torch-cpu build has no working int8 kernel,")
    print("      so int8 RUNTIME speed is not measurable here (would need torchao).")


if __name__ == "__main__":
    main()
