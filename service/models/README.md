# Bundled embedding weights

This directory holds the sentence-embedding model weights used by the **optional
semantic tag ranker** (`embed.py`). They are loaded **offline** with
`local_files_only=True` — the program never contacts the Hugging Face hub at
runtime, which is why the weights are bundled here rather than downloaded.

If this directory is missing/incomplete, or `torch`/`transformers` aren't
installed, tag suggestion silently falls back to the lexical FTS ranker. Nothing
breaks; suggestions are just word-based instead of meaning-based.

## What's here

```
gte-small/               # 384-dim GTE sentence encoder (~67 MB), CPU-friendly — DEFAULT
all-MiniLM-L6-v2/        # 384-dim MiniLM sentence encoder (~90 MB) — prior baseline, kept for A/B
```

`gte-small` is the default: a stronger per-MB retriever than the original
MiniLM baseline, and a clean drop-in (same mean-pooling, no instruction prefix,
same 384-dim vectors). The MiniLM bundle is retained so you can compare via
`CLIPPER_EMBED_MODEL_PATH`.

## Selecting / overriding the model

- Default: `service/models/gte-small/` (constant `DEFAULT_MODEL_DIR` in
  `embed.py`).
- Override the directory at runtime with `CLIPPER_EMBED_MODEL_PATH=/path/to/dir`
  (e.g. point it at `all-MiniLM-L6-v2/` to A/B the old model).

> **Calibration note:** the on-/off-topic cosine floor `min_embed_sim` in
> `suggest.py` is tuned to the **active model's** cosine distribution. gte-small
> packs everything into ~[0.70, 0.96] with on-/off-topic separating near 0.80
> (hence the 0.78 default); MiniLM's range is much wider (~0.30 floor). If you
> swap models, re-measure this floor or gating will be wrong.

The directory must contain a `transformers`-loadable model: `config.json`, the
weights (`model.safetensors` or `pytorch_model.bin`), and the tokenizer files
(`tokenizer.json`, `vocab.txt`, `tokenizer_config.json`, `special_tokens_map.json`).

## Preparing a bundle (on a machine with network access)

```python
from transformers import AutoTokenizer, AutoModel
name = "thenlper/gte-small"
dest = "service/models/gte-small"
AutoTokenizer.from_pretrained(name).save_pretrained(dest)
AutoModel.from_pretrained(name).save_pretrained(dest)
```

Then ship the directory alongside the code. Any mean-pooling sentence encoder
works; swap in a smaller/larger one by replacing the directory and pointing
`CLIPPER_EMBED_MODEL_PATH` (or `DEFAULT_MODEL_DIR`) at it. Cached clip vectors
auto-invalidate when the model identifier (`name:dim`) changes.

## Checking which ranker is live

```
GET /api/embed/status        -> {"available": true, "model": "...", "dim": 384, ...}
python demo_embed.py         # semantic vs lexical, side by side
```
