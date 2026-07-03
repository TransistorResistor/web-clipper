# Embedding ranker — roadmap & evaluation backlog

Current state (shipped): bundled **gte-small** (384-dim, ~67 MB, mean-pooling),
blended with FTS in `suggest.py`. On-/off-topic cosine floor `min_embed_sim =
0.82`, swept on the real library. Baseline to beat for any change below:

| Metric (eval_variants.py, n=15) | gte-small |
| --- | --- |
| correct variant in top-3 (+code) | 15/15 |
| correct variant #1 (+code+regex) | 14/15 |
| off-topic gate leak (aircraft vs chem lib) | 0 clips |
| on-topic donors admitted (chem held-out) | 7/7 |

**Whenever the model changes, re-run:** `eval_variants.py` + the gate-leak
control (chem-only library probed with aircraft/gene docs) and **re-measure
`min_embed_sim`** — it is calibrated to the active model's cosine distribution,
which a model swap *or quantization* shifts.

---

## 1. Quantized / larger models — earmarked, not yet adopted

The user has RAM headroom and wants "performance per MB". Two candidates to
evaluate when there's time to measure on real data:

### (a) int8 gte-small — low value, skip unless RAM is tight
Measured: dynamic int8 → **~45.5 MB** vs 66.8 fp32 (only ~32% smaller, because
the token-embedding tables aren't quantized in dynamic mode and dominate a model
this small). Speed gain on CPU is minor at this size. Not worth the quality risk
unless a specific deployment is RAM-starved (the ~0.9 GB box).

### (b) int8 base model — the actual per-MB play
`bge-base-en-v1.5` or `gte-base-en-v1.5` (109–137M, 768-dim) is ~+1.5 MTEB over
small. fp32 it's ~430–550 MB; **int8 ≈ ~220 MB** — i.e. base-model quality at
roughly small-model disk cost. This is the candidate worth a real bake-off.
Costs to weigh: 768-dim **doubles** `clip_embeddings` storage and per-query
cosine cost, and base inference is ~3× slower per `embed()` on CPU (felt at
ingest, not search). `bge`/`gte-base` use CLS / no-prefix conventions — verify
pooling matches `embed.py`'s mean-pool before trusting numbers.

### How to prepare a quantized bundle (offline target)
Quantize once on a machine with the fp32 weights, then ship the result so the
target never quantizes at load:
```python
import torch, torch.nn as nn
from transformers import AutoModel
m = AutoModel.from_pretrained("service/models/<dir>", local_files_only=True).eval()
# torch ≤2.9: torch.ao.quantization.quantize_dynamic(m, {nn.Linear}, dtype=torch.qint8)
# torch ≥2.10: deprecated -> use the torchao `quantize_` API instead.
```
`embed.py` would need a branch to load the quantized state_dict (dynamic-quant
modules don't round-trip through `from_pretrained`). Treat as a real (small)
code change, not a config swap.

### Decision gate
Adopt (b) only if, on `eval_variants.py` + the gate control, int8-base **beats**
gte-small fp32 by a margin that justifies 2× vector storage. Otherwise stay on
gte-small.

### Empirical bake-off — 2026-06-17 (`eval_models.py corpus.json`)
gte-small vs gte-base (fp16) vs gte-base int8 (weight-only fake-quant - see note),
same 5-fold kNN tag-suggestion eval, 443-clip broad corpus (the one with the most
headroom):

| model | weights | dim | recall@5 | prec@5 | R-prec | MRR |
| --- | --- | --- | --- | --- | --- | --- |
| **gte-small** | 67 MB | 384 | 0.921 | 0.367 | **0.714** | 0.832 |
| gte-base (fp16) | 219 MB | 768 | 0.921 | 0.368 | 0.711 | 0.845 |
| gte-base int8 | 133 MB | 768 | 0.918 | 0.367 | 0.712 | 0.842 |

int8-vs-fp32-base per-vector cosine: **mean 0.9994, min 0.940**.

**Findings.**
- **base ≈ small in quality.** recall@5 identical (0.921), prec@5 identical,
  R-prec marginally *worse* (0.711 vs 0.714), MRR marginally better (+1.3pts).
  A wash - base buys essentially nothing on tag suggestion, because (as the
  adapter study already showed) gte-small is near-ceiling on topical tags.
- **int8 is faithful.** Matches fp32-base to mean cosine 0.9994 and within
  ±0.003 on every metric, at ~40% smaller (133 vs 219 MB). So IF base were worth
  adopting, int8 is a near-free size cut. But base isn't, so int8-base is moot.
- **base is costly here.** 2× vector dim (768 - doubles `clip_embeddings` and
  query cost forever), 3.3× weights, ~3× embed compute, and it **spilled into
  the pagefile on this 8 GB/~1 GB-free box** (24 min/corpus vs gte-small) - on
  the ~0.9 GB deployment box it would be unusable. (Absolute embed seconds here
  are swap-corrupted and not reported as latency; the ~3× ratio is the signal.)
- **int8 caveat:** this `torch 2.10+cpu` build exposes only the `onednn`
  quant engine, whose dynamic kernel errors on our inputs, and torchao isn't
  installed - so real int8 *runtime* isn't achievable on this build at all. int8
  here is weight-only fake-quant (quality + size real; latency unmeasurable).

**Verdict.** base does **not** clear the decision gate: no quality gain over
gte-small, at 2× storage / 3× compute / unusable on the low-RAM target. **Stay
on gte-small.** Revisit only if a future need (e.g. much longer documents, or a
genuinely harder vocabulary) changes the quality picture; int8 is validated as
faithful if a base model is ever adopted. The `service/models/gte-base/` bundle
and the bake-off's `modelvec_*.npy` caches were **deleted after the eval** (not
adopted); re-download via the snippet in §1 to reproduce.

---

## 2. Tag-supervised adaptation — plan (option 2)

> **STATUS: SHELVED (2026-06-17).** Built and evaluated (see the empirical
> section below); upside is a bounded ~+2-3 R-prec points blended, not worth
> production integration. **Not wired into the app** — lives only as standalone
> research tooling: `tag_adapter.py` (module), `eval_adapter.py` (harness),
> `build_eval_corpus.py` / `build_crosscut_corpus.py` (corpus builders), and the
> `corpus*.json` data. No production module imports any of it. The plan below is
> retained as the original design; the verdict is at the end of this section.

**Motivation.** A generic embedding doesn't know that in *this* library
"transformer" means jet engines, not attention layers, or how to separate
F-15E from F-15EX. Every tagged clip is free supervised signal; adapt the metric
to the user's own vocabulary without retraining the model. Offline-friendly
(`scikit-learn` 1.7 is present).

### Three tiers (ship in this order; each is independently useful)

**Tier 0 — Whitening / PCA (unsupervised, no labels).**
Fit `mean μ` and a whitening matrix `W` on the population of clip vectors;
transform every vector `v → (v − μ) · W`. Removes the anisotropy that crushes
all gte cosines into [0.70, 0.96], widening separation for free. Cheapest, lowest
risk, needs no tags. ~30 lines + a cached `(μ, W)`.

**Tier 1 — Supervised linear projection (the core of option 2).**
Learn a projection `P` (e.g. 384→128) so clips sharing a tag are pulled together
and others pushed apart. Practical recipe without a training loop: build tag
*centroids* in whitened space, fit a multinomial/one-vs-rest logistic regression
(clip embedding → tags), and use either its decision function as a direct
tag-probability signal **or** its weight space as the projection. Apply `P` to
the query and the cached clip vectors inside
`similar_clips_by_embedding()` so the *existing* kNN/PMI machinery just operates
in the adapted space — minimal blast radius.

**Tier 2 — Direct tag classifier as a 4th signal.**
Instead of (or alongside) adapting kNN, train `P(tag | clip embedding)` (one-vs-
rest logistic regression over the tag vocabulary) and blend its top predictions
into `suggest_tags` next to content/predict/cooccur. Most powerful for
*existing*-tag suggestion; doesn't help new-tag discovery (keep RAKE for that).

### Integration
- New module `service/tag_adapter.py`: `fit(conn) -> artifact`, `transform(vecs)
  -> vecs`, `predict(vec) -> [(tag, p)]`, all no-op when unfit/unavailable.
- Store the artifact (`μ`, `W`/`P`, tag prototypes, model_id, trained-at,
  clip-count) as a blob — a `tag_adapter` table keyed by `model_id`, so it
  **auto-invalidates** when the embedding model changes (same pattern as
  `clip_embeddings`).
- `suggest.py` applies it behind a flag (`use_adapter`, default on when fit);
  raw-embedding path stays as fallback.

### Training trigger & lifecycle
- Fit lazily/on demand; **re-fit when the library grows by ~25% or N new clips**
  (cheap: seconds on CPU for thousands of clips). Never blocks a request — fit in
  a background pass, swap the artifact atomically.
- **Cold start:** require a minimum (~50 clips, ≥3–5 examples/tag); below that,
  skip the adapter and use raw embeddings. Rare tags (1–2 clips) get no prototype
  — fall through to kNN.

### Evaluation
Leave-one-out on the real library: hold out each clip, predict its tags from the
rest, measure recall@k / MRR — adapter ON vs OFF. Reuse `eval_variants.py` to
confirm variant discrimination doesn't regress. **Must beat raw gte-small** or
it doesn't ship.

### Risks
- Overfitting small/noisy libraries → keep the cold-start floor and the raw
  fallback; regularize the logistic regression hard.
- Concept drift as the library grows → the re-fit trigger handles it.
- Tag noise (inconsistent user tagging) propagates into the metric → Tier 0
  (unsupervised whitening) is immune and a safe default to ship first.

### Recommended sequencing
Ship **Tier 0 (whitening)** first — free, label-independent, de-risks the
pipeline. Then **Tier 1** once there's a library big enough to measure a real
LOO gain. Tier 2 only if Tier 1's projection underperforms a direct classifier
on the LOO eval.

### Empirical result — 2026-06-17 (`eval_adapter.py`, `build_eval_corpus.py`)
Built a 443-clip / 22-tag labelled corpus from OpenAlex abstracts (real
multi-label tags incl. an ML cluster, a bio cluster, and an aircraft/aerospace
cluster), and ran 5-fold CV comparing raw gte-small vs whiten vs supervised vs
blends. Leakage-safe (adapter fit on train only; held-out clips neither indexed
nor trained on). Implemented in `tag_adapter.py` (PCA-whiten → OvR L2 logistic
projection).

| mode | recall@5 | prec@5 | R-prec | MRR |
| --- | --- | --- | --- | --- |
| raw (gte-small) | 0.917 | 0.366 | 0.713 | 0.831 |
| whiten (Tier 0) | 0.894 | 0.281 | 0.693 | 0.835 |
| supervised (replace) | 0.873 | **0.498** | 0.708 | 0.831 |
| **blend 0.3 (0.7·raw + 0.3·sup)** | 0.906 | 0.446 | **0.740** | **0.845** |
| blend 0.5 | 0.901 | 0.476 | 0.735 | 0.847 |

**Findings.**
- **Replacement fails.** Pure supervised loses recall and is flat on the fair
  metrics (R-prec, MRR — which don't reward suggesting *fewer* tags); its prec@5
  jump is mostly an artifact of a sparse 22-dim logit space. It also *regressed*
  the confusable clusters it was meant to help (e.g. Reinforcement learning
  −0.14, Neuroscience −0.13).
- **Blend wins, modestly.** At 0.3, R-prec +2.7pts, MRR +1.6pts, prec@5 +8pts
  for ~1pt recall@5 — and it recovers most of the cluster regressions (clusters
  net neutral-to-positive vs raw, except Immunology/Neuroscience still ~−0.1).
  The adapter belongs as a *complement* (like `embed_blend`), never a swap.
- **Whitening alone doesn't help here** (slightly worse precision).
- **The corpus understates the value.** gte-small already separates these
  scientific fields very well (raw recall@5 0.92), so there's little headroom.
  The adapter's real value is on **idiosyncratic / near-synonym vocabularies**
  where generic embeddings fail — which is the actual Clipper use case but which
  this academic-field corpus doesn't stress. Treat +2.7 R-prec as a *floor*, not
  a ceiling.

**Data-floor confirmed.** Effects were stable and measurable at 443 clips with
all tags ≥10 support — consistent with the ~150–450-clip estimate above.

**Stress test — three corpora, three regimes (2026-06-17).** To find the
adapter's real ceiling I built two more corpora that should be *harder* for a
generic encoder, and re-ran the same eval (`eval_adapter.py <corpus>`):

| corpus | what it tests | raw R-prec | blend0.3 R-prec | Δ | axis acc Δ |
| --- | --- | --- | --- | --- | --- |
| `corpus.json` (broad fields) | easy topical | 0.713 | 0.740 | +2.7 | — |
| `corpus_hard.json` (NLP/CV/DL tasks) | fine intra-field near-synonyms | 0.842 | 0.858 | +1.6 | — |
| `corpus_crosscut.json` (Review vs Primary) | label orthogonal to topic | 0.828 | 0.851 | +2.3 | +1.3 |

**The surprise:** the "hard" fine-grained corpus was *easier* for raw gte-small
(R-prec 0.842 > 0.713), not harder. Fine task names ("object detection",
"machine translation") have **distinctive signature vocabulary**, so the encoder
separates them *better*. Topical near-synonyms are still topical. Generic
embeddings only struggle on labels **orthogonal to topic** (document function) -
but even there, review abstracts carry tells ("we review", "recent advances")
that gte already encodes, so raw scores 0.759 and the adapter lifts it only to
0.772.

**Root cause (firm).** gte-small is a strong frozen encoder and a *linear*
projection can only re-weight what is already linearly present in its vectors.
Where a tag is derivable from text, gte already organises it well (little to
add); where a tag is genuinely *not* in the text, nothing linear can recover it.
So Tier 1's realistic ceiling is **~+2-3 R-prec points** - consistent across all
three regimes, never large. Pure replacement consistently *hurts* recall and
regresses tags; only the blend (≈0.3, complement like `embed_blend`) is safe.
Label noise (OpenAlex review/article mislabels) likely caps the cross-cutting
number too.

**Verdict / recommendation.** Tier 1 works exactly as theory predicts and its
upside is now *bounded*: a small, safe, consistent gain - not a transformative
one. Recommendation: **do not prioritise production integration.** The juice
(~+2 R-prec, blended) is real but not worth the lifecycle machinery (refit
triggers, `tag_adapter` table, cold-start) for a single-user clipper whose base
ranker is already near-ceiling. If a bigger lever is wanted later, it is *not* a
linear adapter - it's a cross-encoder reranker (search) or encoder fine-tuning
(non-linear, not offline-cheap). The module + eval harness stay as validated,
shelved tooling; revisit only with real idiosyncratic user tags. Not shipped to
`suggest.py`.
