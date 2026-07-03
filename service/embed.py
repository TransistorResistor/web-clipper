"""
Optional neural sentence-embedding backend for semantic clip ranking.

This is the concrete implementation of the "embedding upgrade seam" described
in suggest.py: it turns text into a dense vector so similarity is by *meaning*
(cosine distance) rather than shared words (FTS/BM25). suggest.py uses it when
available and silently falls back to the lexical ranker when it isn't.

DEPLOYMENT MODEL
---------------
The target environments have **no network access to the Hugging Face hub and
no alternate runtimes (no onnxruntime)** - only the `transformers` + `torch`
Python packages and a set of model weights *bundled with the program*. So this
module:

  * NEVER touches the network. It loads with `local_files_only=True` from a
    weights directory on disk - either `$CLIPPER_EMBED_MODEL_PATH` or the
    bundled default `service/models/<DEFAULT_MODEL_DIR>/`.
  * Treats every failure mode (torch/transformers not installed, weights
    directory absent or incomplete, a load/inference error) as "unavailable",
    so the caller falls back to FTS. Nothing here ever raises to the caller.

To prepare a bundle, save a sentence-transformer's weights into the directory,
e.g. once on a machine that does have access:

    from transformers import AutoTokenizer, AutoModel
    d = "service/models/gte-small"
    AutoTokenizer.from_pretrained("thenlper/gte-small").save_pretrained(d)
    AutoModel.from_pretrained("thenlper/gte-small").save_pretrained(d)

then ship that directory alongside the code.

The default bundle is gte-small (mean-pooling, 384-dim, ~67 MB) - a stronger
per-MB retriever than the original all-MiniLM-L6-v2 baseline, and a clean
drop-in: same mean-pooling, no instruction prefix, same 384-dim vectors. To A/B
another bundle, point $CLIPPER_EMBED_MODEL_PATH at its directory; the per-clip
vector cache self-invalidates on the model id (name:dim), so no migration. NOTE:
the on-/off-topic cosine floor (`min_embed_sim` in suggest.py) is calibrated to
the active model's distribution - swap the model and re-measure it.
"""
from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Default bundled weights directory name (under service/models/). gte-small: a
# mean-pooling sentence-transformer; small, CPU-friendly, 384-dim, and a
# stronger per-MB retriever than the original all-MiniLM-L6-v2 baseline (which
# is kept bundled alongside for A/B via $CLIPPER_EMBED_MODEL_PATH).
DEFAULT_MODEL_DIR = "gte-small"
# Token cap per text. Embeddings summarise topic, not every word - 256 tokens
# of title+lead is plenty and keeps CPU inference fast.
MAX_TOKENS = 256
_BATCH = 16


def _bundled_path() -> Path:
    return Path(__file__).resolve().parent / "models" / DEFAULT_MODEL_DIR


def model_dir() -> Path:
    """Resolved weights directory (env override or bundled default)."""
    env = os.environ.get("CLIPPER_EMBED_MODEL_PATH")
    return Path(env) if env else _bundled_path()


class _Backend:
    """Lazily-loaded singleton holding the tokenizer + model. All load/inference
    errors are captured into `reason`; `ok` stays False and callers fall back."""

    def __init__(self) -> None:
        self.loaded = False
        self.ok = False
        self.reason = ""
        self.dim = 0
        self.name = ""
        self._tok = None
        self._model = None
        self._torch = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self.loaded:
            return
        with self._lock:
            if self.loaded:
                return
            self.loaded = True
            path = model_dir()
            self.name = path.name
            if not path.is_dir():
                self.reason = f"weights directory not found: {path}"
                return
            try:
                import torch  # noqa: F401
                from transformers import AutoModel, AutoTokenizer
            except Exception as e:  # torch/transformers missing or broken
                self.reason = f"transformers/torch unavailable: {e!r}"
                return
            try:
                # local_files_only: never reach for the network, even if the
                # directory is incomplete - fail to fallback instead of hanging.
                tok = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
                model = AutoModel.from_pretrained(str(path), local_files_only=True)
                model.eval()
                self._torch = torch
                self._tok = tok
                self._model = model
                self.dim = int(getattr(model.config, "hidden_size", 0) or 0)
                self.ok = True
            except Exception as e:
                self.reason = f"failed to load weights: {e!r}"

    def embed(self, texts: list[str]):
        """Return an (n, dim) float32 numpy array of L2-normalised mean-pooled
        embeddings, or None if the backend is unavailable / errors."""
        self._load()
        if not self.ok:
            return None
        try:
            import numpy as np
            torch = self._torch
            out = []
            with torch.no_grad():
                for i in range(0, len(texts), _BATCH):
                    batch = [t or "" for t in texts[i:i + _BATCH]]
                    enc = self._tok(batch, padding=True, truncation=True,
                                    max_length=MAX_TOKENS, return_tensors="pt")
                    model_out = self._model(**enc)
                    # Mean-pool token embeddings using the attention mask.
                    tokens = model_out[0]                      # (b, seq, dim)
                    mask = enc["attention_mask"].unsqueeze(-1).type_as(tokens)
                    summed = (tokens * mask).sum(dim=1)
                    counts = mask.sum(dim=1).clamp(min=1e-9)
                    pooled = summed / counts
                    pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
                    out.append(pooled.cpu().numpy().astype("float32"))
            return np.vstack(out) if out else None
        except Exception as e:
            # An inference failure shouldn't poison future calls, but it does
            # mean this environment can't be trusted - mark unavailable.
            self.ok = False
            self.reason = f"inference error: {e!r}"
            return None


_BACKEND: Optional[_Backend] = None


def _backend() -> _Backend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _Backend()
    return _BACKEND


def available() -> bool:
    """True if the embedding model is loaded and usable."""
    b = _backend()
    b._load()
    return b.ok


def dim() -> int:
    return _backend().dim if available() else 0


def model_id() -> str:
    """Stable identifier for the active model, used to invalidate cached clip
    vectors when the bundled weights change."""
    b = _backend()
    return f"{b.name}:{b.dim}" if b.ok else ""


def embed(texts: list[str]):
    """Embed a list of texts -> (n, dim) float32 ndarray, or None if unavailable."""
    if not texts:
        return None
    return _backend().embed(texts)


@lru_cache(maxsize=64)
def _embed_one_cached(text: str):
    arr = embed([text])
    return None if arr is None else arr[0]


def embed_one(text: str):
    """Embed a single text -> (dim,) float32 ndarray, or None if unavailable.

    Memoized: the interactive tagging flow re-requests suggestions with the
    SAME document text every time a chip is added or removed (only `chosen`
    changes), and model inference is the dominant per-request cost - identical
    text must not re-run the model. Callers must treat the returned array as
    read-only. A None (backend unavailable) is cached too; unavailability is
    sticky for the process, so that never masks a recovery."""
    return _embed_one_cached(text)


def status() -> dict:
    """Diagnostics for the demo / health checks."""
    b = _backend()
    b._load()
    return {"available": b.ok, "model": b.name, "dim": b.dim,
            "path": str(model_dir()), "reason": "" if b.ok else b.reason}
