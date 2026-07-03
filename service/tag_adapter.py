"""
Tier-1 tag-supervised embedding adapter.

STATUS: SHELVED RESEARCH - NOT wired into the app. Evaluated 2026-06-17 across
three corpora; the blended gain is a bounded ~+2-3 R-prec points and pure
replacement hurts, so it was judged not worth production integration. Nothing in
the running service (app.py / suggest.py / embed.py / ingest.py) imports this;
it exists only for the eval harness (eval_adapter.py). Full verdict in
EMBEDDING_ROADMAP.md. Kept as validated tooling to re-run against a real
idiosyncratic-tag library later.

A generic sentence embedding (gte-small) knows English, not *this* library's tag
vocabulary - so it can't tell that here "transformer" means jet engines, or pull
F-15E away from F-15EX. Every tagged clip is free supervision; this module learns
a LINEAR projection of the frozen embeddings so that cosine similarity reflects
the user's own tags. It plugs in inside `similar_clips_by_embedding`: transform
the query and the cached clip vectors, and the existing kNN/PMI machinery just
runs in the adapted space.

Pipeline (all closed-form / convex, no training loop, CPU-only, offline):

  1. WHITEN  - PCA-whiten the raw embeddings (centre, decorrelate, unit-variance,
               keep the top components). Undoes the anisotropy that crushes all
               gte cosines into a narrow band, and shrinks the dimensionality so
               the supervised step is data-efficient. This alone is "Tier 0".
  2. PROJECT - one-vs-rest L2-regularised logistic regression (whitened -> tags).
               The stacked weight matrix is a supervised linear map into a
               tag-aligned space; a clip is represented by its per-tag logits, so
               two clips about the same tags sit close regardless of wording.

The whole thing composes to a single affine map `x -> ((x-mu) @ Wwhiten) @ C.T
+ b` followed by L2-normalisation, i.e. a plain supervised linear projection.

`fit` needs enough per-tag support to mean anything; tags with too few positive
examples are dropped from the adapter (the raw/kNN path still serves them). When
unfit or below the data floor, `transform` is the identity (graceful fallback).
"""
from __future__ import annotations

import numpy as np

# Data floors below which supervision is noise - fall back to raw embeddings.
MIN_CLIPS = 60
MIN_TAG_SUPPORT = 4          # a tag needs this many positives to be learned
_MAX_WHITEN_DIMS = 128       # PCA cap: keep params << samples
_LR_C = 0.5                  # inverse L2 strength; small = strong regularisation


class TagAdapter:
    def __init__(self) -> None:
        self.fitted = False
        self.mode = "identity"        # "identity" | "whiten" | "supervised"
        self.mu = None                # (d,)
        self.components = None         # (k, d) PCA directions
        self.std = None                # (k,) per-component std (for whitening)
        self.coef = None               # (T, k) logistic-regression weights
        self.intercept = None          # (T,)
        self.tags: list[str] = []      # learned tag order (length T)

    # --- fitting -----------------------------------------------------------
    def _whiten_fit(self, X: np.ndarray) -> np.ndarray:
        self.mu = X.mean(axis=0)
        Xc = X - self.mu
        # SVD-based PCA (works when n < d). Components are rows of Vt.
        _, s, Vt = np.linalg.svd(Xc, full_matrices=False)
        k = int(min(_MAX_WHITEN_DIMS, Vt.shape[0], max(1, X.shape[0] - 1)))
        self.components = Vt[:k]                       # (k, d)
        n = X.shape[0]
        self.std = (s[:k] / np.sqrt(max(1, n - 1)))    # per-PC std
        self.std[self.std < 1e-8] = 1e-8
        return (Xc @ self.components.T) / self.std     # (n, k)

    def _whiten_apply(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mu) @ self.components.T) / self.std

    def fit(self, X: np.ndarray, tag_lists: list[list[str]],
            mode: str = "supervised") -> "TagAdapter":
        """X: (n, d) raw embeddings. tag_lists: per-clip list of tag names.
        mode: 'supervised' (whiten+project) | 'whiten' (Tier 0) | 'identity'."""
        X = np.asarray(X, dtype=np.float64)
        n = X.shape[0]
        if mode == "identity" or n < MIN_CLIPS:
            self.fitted, self.mode = True, "identity"
            return self
        Xw = self._whiten_fit(X)
        if mode == "whiten":
            self.fitted, self.mode = True, "whiten"
            return self
        # supervised: keep only tags with enough support
        from collections import Counter
        supp = Counter(t for ts in tag_lists for t in ts)
        self.tags = sorted([t for t, c in supp.items() if c >= MIN_TAG_SUPPORT])
        if len(self.tags) < 2:
            self.fitted, self.mode = True, "whiten"   # not enough labels
            return self
        idx = {t: i for i, t in enumerate(self.tags)}
        Y = np.zeros((n, len(self.tags)), dtype=np.int8)
        for r, ts in enumerate(tag_lists):
            for t in ts:
                if t in idx:
                    Y[r, idx[t]] = 1
        from sklearn.linear_model import LogisticRegression
        from sklearn.multiclass import OneVsRestClassifier
        clf = OneVsRestClassifier(
            LogisticRegression(C=_LR_C, max_iter=2000, class_weight="balanced"))
        clf.fit(Xw, Y)
        # Stack per-tag linear weights into one projection matrix.
        self.coef = np.vstack([e.coef_[0] for e in clf.estimators_])      # (T,k)
        self.intercept = np.array([e.intercept_[0] for e in clf.estimators_])
        self.fitted, self.mode = True, "supervised"
        return self

    # --- transform ---------------------------------------------------------
    def transform(self, X: np.ndarray) -> np.ndarray:
        """Map raw embeddings into the adapted space and L2-normalise. Identity
        (just renormalise) when unfit / below the data floor."""
        X = np.asarray(X, dtype=np.float64)
        if not self.fitted or self.mode == "identity":
            out = X
        elif self.mode == "whiten":
            out = self._whiten_apply(X)
        else:  # supervised: per-tag logits
            Xw = self._whiten_apply(X)
            out = Xw @ self.coef.T + self.intercept
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        norm[norm < 1e-12] = 1e-12
        return (out / norm).astype(np.float32)

    # --- persistence -------------------------------------------------------
    def state(self) -> dict:
        return {"mode": self.mode, "mu": self.mu, "components": self.components,
                "std": self.std, "coef": self.coef, "intercept": self.intercept,
                "tags": np.array(self.tags, dtype=object)}
