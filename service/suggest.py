"""
Offline tag suggestion.

Three complementary, dependency-free signals, blended, plus a fourth path
that proposes brand-new tags from the document itself:

  1. CONTENT SIMILARITY (FTS): build a query from the document's most
     distinctive terms (TF-IDF against the FTS index, dropping terms too
     common to discriminate), run it against the existing FTS5 index to
     find similar clips, keep only those close to the best match, and
     borrow their tags weighted by similarity rank. Answers "documents
     like this one were tagged X".

  2. CO-OCCURRENCE: for any tags the user has already chosen, boost tags
     that frequently appear alongside them across the library. Answers
     "things tagged X are often also tagged Y".

  3. TERM->TAG PREDICTION (Naive Bayes / PMI): score each tag directly by
     the positive pointwise mutual information between the document's
     salient terms and that tag across the library. Answers "documents
     containing these words tend to be tagged X" - more direct than #1's
     borrow-from-neighbours, and PMI makes ubiquitous tags self-cancel.

  4. KEYPHRASES (RAKE): extract candidate phrases from the document and
     propose the ones the library has never seen as NEW tags. This is the
     only signal that can surface a tag that doesn't already exist; it fills
     leftover slots so it never displaces a confident existing tag.

The three library signals damp each candidate tag by its global usage count
(score / log(2 + count)), so bulk tags that sit on half the library
(migration tags, folder tags) don't drown out specific ones.

The blended list is cut off below a fraction of the top score - an empty
chip row means "nothing confident", not "here are eight weak guesses".

------------------------------------------------------------------------
SEMANTIC (EMBEDDING) RANKER
------------------------------------------------------------------------
`similar_clips_by_content()` (FTS5/BM25) and `similar_clips_by_embedding()`
(sentence embeddings, see embed.py) share one signature and return shape. When
the embedding weights load, `suggest_tags()` BLENDS them (`embed_blend`): the
embedding cosine gates eligibility (off-topic uploads borrow nothing) while a
strong lexical match can rescue a borderline-semantic clip, and the two
normalised similarities are summed - so exact-token jargon / codes ("NaOH",
"BRCA1", "F-35") keep their precision while embeddings add meaning-based recall.
When the weights are absent it falls back to the lexical leg alone (its gate is
the salient-term overlap anchor). Everything downstream (PMI, co-occurrence,
keyphrases, the endpoint, the UI) is identical regardless of which ran.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Optional

from db import blocked_keyword_keys, tag_key

# Very common English words we never want to use as FTS query terms - they
# match everything and add noise. Kept short on purpose; the IDF weighting
# and FTS ranking handle the rest.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "as", "at", "by", "from", "is", "are", "was", "were", "be",
    "been", "being", "this", "that", "these", "those", "it", "its", "we",
    "you", "they", "he", "she", "i", "not", "no", "yes", "if", "then",
    "than", "so", "such", "can", "will", "would", "could", "should", "may",
    "might", "have", "has", "had", "do", "does", "did", "about", "into",
    "over", "under", "out", "up", "down", "more", "most", "some", "any",
    "all", "each", "their", "there", "here", "what", "which", "who", "when",
    "where", "how", "also", "only", "just", "very", "much", "many",
    # Non-topical filler that recurs across academic/web text and otherwise
    # surfaces as bogus salient terms ("our model", email/URL fragments).
    "our", "your", "com", "www", "http", "https", "org", "via", "etc",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]{2,}")

# A term that appears in more than this fraction of the library can't
# discriminate between clips - it would retrieve a big, unrelated slice - so
# it's dropped from the FTS query no matter how often it shows up in the
# document. Only enforced once the library is large enough for the fraction
# to be meaningful (on a 3-clip library every term is "common").
_MAX_DF_FRAC = 0.5
_MIN_LIB_FOR_DF_CAP = 8

# Fraction of a document's salient terms a clip must share before its tags may
# be borrowed (see the topical-overlap anchor in `suggest_tags`). Tuned so a
# clip that merely shares a generic word or two with an off-topic document
# can't drag its tags in, while a clip on the same subject - which overlaps on
# several salient terms - still qualifies.
_OVERLAP_FRAC = 0.34

# In the blended ranker, a clip with only a lukewarm embedding cosine could be
# "rescued" into the donor set IF it was also a strong lexical (FTS) match - the
# idea being to recover exact jargon / formula overlap the embedding under-
# weights. This opens a band `[floor - margin, floor)`: a clip in it is admitted
# when it shares salient words. An ABSOLUTE margin (not a fraction of the floor)
# because gte-small packs everything into ~[0.70, 0.96], so a fraction-of-floor
# band would sit under the global minimum and admit every lexical match.
#
# DEFAULT 0.0 (rescue OFF). Calibrated on the live corpus by leave-one-out
# (`eval_subtopics --sweep`): the band is inherently low-precision - it admits
# ANY lexically-overlapping clip, not just exact-jargon ones - and it became
# redundant once the body-mention signal (`_mention_scores`) landed. Mention
# recovers a document's own named tags doc-intrinsically and with far higher
# precision, and `code_boost` covers designations, so the rescue now adds mostly
# cross-topic bleed. The sweep showed 0.0 DOMINATES: highest hit@3 (82% vs 77%
# at 0.02), highest precision (50% vs 42%), lowest bleed - and eval_codes' exact-
# token cases (747 vs 737, F-35 vs F-22) still resolve via the blend's exact-
# token leg + mention/code, no rescue needed. Raise this only if you disable the
# mention signal (mention_weight=0); re-sweep on any model/corpus change.
_EMBED_RESCUE_MARGIN = 0.0

# Base similarity granted to a clip that an exact code-token match pulls in but
# the similarity ranker didn't otherwise score (e.g. a length-outlier excluded
# from FTS, or a clip just under the embedding floor). Modest, so the exact-code
# boost decides its rank rather than letting an unscored clip dominate outright.
_CODE_BASE_SIM = 0.4


def _doc_frequency(conn: sqlite3.Connection, term: str) -> int:
    """How many indexed clips contain `term`? Uses FTS MATCH so the count
    goes through the same tokenizer/stemmer as retrieval will."""
    safe = term.replace('"', "")
    if not safe:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM clips_fts WHERE clips_fts MATCH ?",
            (f'"{safe}"',),
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _salient_terms(conn: sqlite3.Connection, text: str, limit: int = 10) -> list[str]:
    """Pick distinctive terms from the document for an FTS query.

    Term frequency in the document x inverse document frequency in the
    library (via FTS MATCH counts, so stemming is consistent). Two filters
    keep the query tight so retrieval stays "similar clips" and doesn't
    degrade into "most of the library":

      - terms matching nothing in the index are dropped (retrieve nothing);
      - terms appearing in more than `_MAX_DF_FRAC` of clips are dropped -
        generic words like "system"/"data"/"results" have high in-document
        frequency but can't discriminate, and ORing them in is what dragged
        unrelated clips (and their tags) into the suggestions.

    Only the top `limit` survivors go into the query. Falls back to plain
    frequency ranking on an empty/new library.
    """
    counts: Counter[str] = Counter()
    for m in _WORD_RE.finditer(text.lower()):
        w = m.group(0)
        if w in _STOPWORDS:
            continue
        counts[w] += 1
    if not counts:
        return []
    # Score only the top candidates by raw frequency to bound the number
    # of df lookups per call.
    candidates = counts.most_common(60)
    try:
        total = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    except sqlite3.OperationalError:
        total = 0
    scored: list[tuple[str, float]] = []
    if total > 0:
        # Above this many clips a term is too common to be distinctive. On a
        # small library the cap is disabled (set above the max possible df).
        df_cap = (total * _MAX_DF_FRAC if total >= _MIN_LIB_FOR_DF_CAP
                  else total + 1)
        for term, tf in candidates:
            df = _doc_frequency(conn, term)
            if df == 0:
                continue  # not in the index; retrieves nothing
            if df > df_cap:
                continue  # too common to discriminate; would broaden the query
            idf = math.log((total + 1) / (df + 0.5))
            scored.append((term, tf * max(idf, 0.1)))
        scored.sort(key=lambda x: -x[1])
    if scored:
        return [t for t, _ in scored[:limit]]
    # Tiny or empty library: plain frequency is the best we can do.
    return [w for w, _ in candidates[:limit]]


def _build_fts_query(terms: list[str]) -> str:
    """OR the terms into a safe FTS5 MATCH string.

    Each term is wrapped in double quotes so FTS5 treats it as a literal
    (defuses any stray operator characters). Empty -> ''.
    """
    safe = []
    for t in terms:
        t = t.replace('"', "").strip()
        if t:
            safe.append(f'"{t}"')
    return " OR ".join(safe)


def similar_clips_by_content(conn: sqlite3.Connection, text: str,
                             exclude_clip_id: Optional[int] = None,
                             limit: int = 40,
                             min_sim_frac: float = 0.25) -> list[tuple[int, float]]:
    """Return [(clip_id, similarity_score), ...] for clips most similar to
    `text`, using FTS5/BM25. Score is normalised so higher = more similar.

    Clips scoring below `min_sim_frac` of the best match are dropped: an OR
    query of several terms always retrieves a tail of weak matches (a clip
    that shares one generic word), and borrowing tags from those is what
    produced piles of unrelated suggestions. The cut keeps borrowing to
    clips that are actually close.

    This is the swap point for the future embedding ranker (#4): a
    `similar_clips_by_embedding(conn, text, ...)` with the same return shape
    could replace or blend with this.
    """
    terms = _salient_terms(conn, text)
    query = _build_fts_query(terms)
    if not query:
        return []
    try:
        rows = conn.execute(
            """
            SELECT clips_fts.rowid AS clip_id, bm25(clips_fts) AS rank
            FROM clips_fts
            WHERE clips_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # Malformed query for any reason - fail soft with no suggestions.
        return []

    out: list[tuple[int, float]] = []
    # SQLite's bm25() returns NEGATIVE values where more negative = better
    # (so ORDER BY rank ascending puts the best first). Goodness is -rank.
    # NB: the old 1/(1+abs(rank)) transform inverted the ranking - it gave
    # the WEAKEST matches the highest similarity. Callers normalise, so the
    # unbounded scale is fine.
    for r in rows:
        cid = r["clip_id"]
        if exclude_clip_id is not None and cid == exclude_clip_id:
            continue
        rank = r["rank"] if r["rank"] is not None else 0.0
        score = max(0.0, -rank)
        out.append((cid, score))
    # Drop the weak tail. Threshold off the best surviving score (the
    # excluded clip may have been the rank-1 row, so don't assume out[0]).
    if out:
        top = max(s for _, s in out)
        if top > 0:
            cutoff = top * min_sim_frac
            out = [(cid, s) for cid, s in out if s >= cutoff]
    return out


# ---------------------------------------------------------------------------
# Embedding ranker (optional) - the semantic counterpart of the FTS ranker.
#
# `similar_clips_by_embedding()` has the SAME shape as
# `similar_clips_by_content()` ([(clip_id, score), ...], higher = closer) but
# ranks by cosine similarity of sentence embeddings, so it matches on meaning
# (e.g. "neural translation" ~ "sequence-to-sequence model") rather than shared
# words. It returns None when the embedding backend is unavailable, which is the
# signal for `suggest_tags()` to fall back to the lexical ranker. See embed.py.
# ---------------------------------------------------------------------------

_EMBED_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_embeddings (
    clip_id      INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    content_hash TEXT,     -- invalidate when the clip's text changes
    model        TEXT,     -- invalidate when the bundled weights change
    dim          INTEGER,
    vector       BLOB,     -- float32 bytes, L2-normalised
    updated_at   INTEGER
);
"""
# How much clip text to embed. The model only reads ~256 tokens anyway; a few
# thousand chars of title+lead covers the topic without big DB blobs.
_EMBED_TEXT_CHARS = 4000


def _embed_clip_text(row: sqlite3.Row) -> str:
    """Text used to represent a clip in embedding space: its title (it leads,
    and carries the most aboutness) followed by its body text."""
    title = (row["title"] or "").strip()
    body = (row["content_text"] or "").strip()
    return (title + "\n" + body)[:_EMBED_TEXT_CHARS]


def ensure_clip_embeddings(conn: sqlite3.Connection) -> bool:
    """Compute and cache an embedding for every clip that lacks a current one
    (missing, or stale because the clip text or the model changed). Returns
    False if the backend is unavailable (nothing cached). Cheap on repeat calls
    once the cache is warm."""
    import embed as _embed
    if not _embed.available():
        return False
    import numpy as np
    conn.execute(_EMBED_CACHE_SCHEMA)
    model = _embed.model_id()
    rows = conn.execute(
        """SELECT c.id, c.title, c.content_text, c.content_hash
           FROM clips c
           LEFT JOIN clip_embeddings e ON e.clip_id = c.id
           WHERE e.clip_id IS NULL
              OR e.model != ?
              OR e.content_hash IS NOT c.content_hash""", (model,)).fetchall()
    if rows:
        vecs = _embed.embed([_embed_clip_text(r) for r in rows])
        if vecs is None:
            return False
        now = int(time.time())
        conn.executemany(
            """INSERT INTO clip_embeddings
                   (clip_id, content_hash, model, dim, vector, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(clip_id) DO UPDATE SET
                   content_hash=excluded.content_hash, model=excluded.model,
                   dim=excluded.dim, vector=excluded.vector,
                   updated_at=excluded.updated_at""",
            [(r["id"], r["content_hash"], model, vecs.shape[1],
              vecs[i].astype("float32").tobytes(), now)
             for i, r in enumerate(rows)])
        conn.commit()
    return True


def similar_clips_by_embedding(conn: sqlite3.Connection, text: str,
                               exclude_clip_id: Optional[int] = None,
                               limit: int = 40,
                               min_sim_frac: float = 0.25,
                               min_abs_sim: float = 0.0
                               ) -> Optional[list[tuple[int, float]]]:
    """Semantic sibling of `similar_clips_by_content`. Returns
    [(clip_id, cosine), ...] sorted closest-first, or None if the embedding
    backend isn't available (caller falls back to FTS).

    `min_abs_sim` is an ABSOLUTE cosine floor: unlike the relative tail-cut,
    it lets the result be empty when nothing in the library is genuinely on
    topic - the semantic equivalent of the lexical overlap gate, and what stops
    an off-topic upload borrowing the library's popular tags."""
    import embed as _embed
    if not (text or "").strip() or not _embed.available():
        return None
    try:
        import numpy as np
        if not ensure_clip_embeddings(conn):
            return None
        rows = conn.execute(
            "SELECT clip_id, vector FROM clip_embeddings WHERE dim > 0").fetchall()
        if not rows:
            return []
        qv = _embed.embed_one(text)
        if qv is None:
            return None
        mat = np.vstack([np.frombuffer(r["vector"], dtype="float32") for r in rows])
        ids = [r["clip_id"] for r in rows]
        sims = mat @ qv.astype("float32")           # both L2-normalised -> cosine
        out = [(cid, float(s)) for cid, s in zip(ids, sims)
               if not (exclude_clip_id is not None and cid == exclude_clip_id)]
        out = [(cid, s) for cid, s in out if s >= min_abs_sim]
        if not out:
            return []
        top = max(s for _, s in out)
        cutoff = max(min_abs_sim, top * min_sim_frac) if top > 0 else min_abs_sim
        out = [(cid, s) for cid, s in out if s >= cutoff]
        out.sort(key=lambda x: -x[1])
        return out[:limit]
    except Exception:
        return None


def _tags_for_clips(conn: sqlite3.Connection,
                    clip_ids: list[int]) -> dict[int, list[str]]:
    if not clip_ids:
        return {}
    placeholders = ",".join("?" * len(clip_ids))
    rows = conn.execute(
        f"""
        SELECT ct.clip_id, t.name
        FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id
        WHERE ct.clip_id IN ({placeholders})
        """,
        clip_ids,
    ).fetchall()
    by_clip: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        by_clip[r["clip_id"]].append(r["name"])
    return by_clip


def _tag_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Global usage count per tag name, for popularity damping."""
    return {
        r["name"]: r["n"] for r in conn.execute(
            "SELECT t.name, COUNT(ct.clip_id) AS n FROM tags t "
            "LEFT JOIN clip_tags ct ON ct.tag_id = t.id GROUP BY t.id")
    }


def _length_outlier_clips(conn: sqlite3.Connection, mult: float = 6.0,
                          min_abs: int = 20000, min_clips: int = 6) -> set[int]:
    """Clip ids that are length outliers - many times longer than the typical
    clip in the library (e.g. a full encyclopedia article among ordinary
    bookmarks).

    Such clips are excluded as tag DONORS for the content/predict signals: an
    80k-word reference page contains so much vocabulary that it 'matches'
    almost any upload on a few incidental words, and the broad tags it carries
    ('Mammals', 'biology') then ride in on every suggestion. Their own tags are
    still reachable through co-occurrence and keyphrases; they're just not
    treated as evidence that an unrelated document is about their topic.

    Guarded so it never fires on a small or uniformly-long library: it needs at
    least `min_clips` clips, and only flags clips that are BOTH more than
    `mult`x the median length AND over `min_abs` characters in absolute terms.
    """
    rows = conn.execute(
        "SELECT id, LENGTH(COALESCE(content_text, '')) AS n FROM clips").fetchall()
    if len(rows) < min_clips:
        return set()
    lengths = sorted(r["n"] for r in rows)
    mid = len(lengths) // 2
    median = (lengths[mid] if len(lengths) % 2
              else (lengths[mid - 1] + lengths[mid]) / 2)
    if median <= 0:
        return set()
    threshold = max(min_abs, mult * median)
    return {r["id"] for r in rows if r["n"] > threshold}


def _damp(count: int) -> float:
    """Popularity damping factor: bigger global count -> smaller weight.

    log(2+count) keeps it gentle - a tag on 3 clips is damped ~1.6x, a bulk
    tag on 200 clips ~5.3x - enough that ubiquitous migration/folder tags
    stop outranking specific ones without being banned outright."""
    return math.log(2 + max(count, 0))


def _cooccurring_tags(conn: sqlite3.Connection,
                      chosen: list[str]) -> Counter:
    """Count tags that co-occur with the chosen tags across all clips.

    For each clip that carries at least one chosen tag, every *other* tag on
    that clip gets a vote. The result is a Counter of candidate -> votes.
    """
    votes: Counter[str] = Counter()
    if not chosen:
        return votes
    lowered = [c.lower() for c in chosen]
    placeholders = ",".join("?" * len(lowered))
    # Clips that have any chosen tag.
    clip_rows = conn.execute(
        f"""
        SELECT DISTINCT ct.clip_id
        FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id
        WHERE t.name COLLATE NOCASE IN ({placeholders})
        """,
        lowered,
    ).fetchall()
    clip_ids = [r["clip_id"] for r in clip_rows]
    if not clip_ids:
        return votes
    by_clip = _tags_for_clips(conn, clip_ids)
    chosen_set = set(lowered)
    for tags in by_clip.values():
        for tag in tags:
            if tag.lower() not in chosen_set:
                votes[tag] += 1
    return votes


def _clips_matching_term(conn: sqlite3.Connection, term: str,
                         limit: int = 300) -> list[int]:
    """Clip ids whose indexed text contains `term` (via FTS, so stemming is
    consistent with retrieval). Capped so a very common term can't blow up
    the per-request work."""
    safe = term.replace('"', "")
    if not safe:
        return []
    try:
        rows = conn.execute(
            "SELECT rowid FROM clips_fts WHERE clips_fts MATCH ? LIMIT ?",
            (f'"{safe}"', limit),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def _code_clip_strength(conn: sqlite3.Connection, code: str,
                        limit: int = 50) -> dict[int, float]:
    """How PROMINENTLY each clip features the code token `code`, as a 0..1
    weight (the most-prominent clip = 1.0). Uses BM25 over the FTS index, so a
    clip *about* the F-15E outweighs one that merely cross-references it - the
    discrimination that bare membership can't make between near-identical
    variants. Empty if the term isn't indexed."""
    safe = code.replace('"', "")
    if not safe:
        return {}
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(clips_fts) AS r FROM clips_fts "
            "WHERE clips_fts MATCH ? ORDER BY r LIMIT ?",
            (f'"{safe}"', limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    # bm25() is negative, more negative = better; goodness = -r.
    goods = {r[0]: max(0.0, -(r[1] if r[1] is not None else 0.0)) for r in rows}
    mx = max(goods.values(), default=0.0)
    return {cid: g / mx for cid, g in goods.items()} if mx > 0 else {}


def predicted_tag_scores(conn: sqlite3.Connection, terms: list[str],
                         tag_counts: dict[str, int], total: int,
                         chosen_lower: set[str],
                         eligible_clips: Optional[set[int]] = None) -> Counter:
    """Signal 4 - term->tag association ("documents containing these words
    tend to be tagged X"), a Naive-Bayes-flavoured model.

    For each salient term we look at the tags on the clips that contain it
    and score each tag by its POSITIVE pointwise mutual information with the
    term - log(P(tag|term) / P(tag)) - weighted by how informative the term
    is (IDF) and shrunk when the co-occurrence is thin. PMI is the key: a
    bulk tag that sits on half the library has P(tag|term) ~ P(tag), so its
    PMI is ~0 and it contributes nothing, no ad-hoc damping needed. Unlike
    the kNN content signal (which borrows whole clips' tag sets), this scores
    each tag directly from the words that predict it.

    `eligible_clips`, when given, is the topical-overlap anchor (see
    `suggest_tags`): only clips that share enough of the document's salient
    vocabulary contribute tags, so a single coincidental term-match in an
    otherwise-unrelated clip can't manufacture a confident suggestion. The IDF
    and n_term denominators still use the full term frequency, so the PMI of a
    genuinely predictive term is unchanged.
    """
    scores: Counter[str] = Counter()
    if total <= 0 or not terms:
        return scores
    for term in terms:
        ids = _clips_matching_term(conn, term)
        n_term = len(ids)
        if n_term == 0:
            continue
        if eligible_clips is not None:
            ids = [i for i in ids if i in eligible_clips]
            if not ids:
                continue
        idf = max(math.log((total + 1) / (n_term + 0.5)), 0.1)
        tally: Counter[str] = Counter()
        for tags in _tags_for_clips(conn, ids).values():
            for tag in tags:
                tally[tag] += 1
        for tag, c in tally.items():
            if tag.lower() in chosen_lower:
                continue
            p_tag_given_term = c / n_term
            p_tag = max(tag_counts.get(tag, 1), 1) / total
            lift = p_tag_given_term / p_tag
            if lift > 1.0:  # positive association only
                # log(lift) is the PMI; (c/(c+1)) shrinks one-off coincidences.
                scores[tag] += math.log(lift) * idf * (c / (c + 1))
    return scores


_KEYPHRASE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*|[^\sa-z0-9]")

# Words that survive the (short) stopword filter but make poor tag *edges* -
# verbs, gerunds, and stray prepositions/adverbs that read as noise at the
# start or end of a phrase ("understanding async" -> "async", "fresh beans
# matter" -> "fresh beans"). Trimmed from phrase edges only, so they can
# still sit mid-phrase ("rate of return"). This is a deliberately cheap,
# dependency-free stand-in for real part-of-speech filtering; a POS tagger
# would catch the mid-phrase cases too but pulls in a model dependency.
_EDGE_STOPWORDS = {
    # verbs / gerunds / participles
    "understanding", "understand", "using", "use", "used", "based",
    "including", "include", "includes", "provide", "provides", "providing",
    "getting", "get", "gets", "making", "make", "makes", "made", "doing",
    "does", "done", "matter", "matters", "want", "wants", "need", "needs",
    "see", "seeing", "look", "looking", "know", "knowing", "given", "give",
    "gives", "take", "takes", "taking", "going", "goes", "become", "becomes",
    "comes", "coming", "let", "lets", "say", "says", "said", "show", "shows",
    "shown", "found", "find", "finds", "help", "helps", "build", "builds",
    "building", "added", "adding", "improve", "improves", "boost", "boosts",
    # prepositions / adverbs not in the main stopword list
    "inside", "outside", "within", "without", "across", "around", "through",
    "between", "among", "toward", "towards", "onto", "off", "near", "via",
    "per", "upon", "along", "beyond", "above", "below", "beside", "despite",
    "round", "year",
}


def _trim_edges(words: list[str]) -> list[str]:
    """Drop _EDGE_STOPWORDS from both ends of a candidate phrase."""
    lo, hi = 0, len(words)
    while lo < hi and words[lo] in _EDGE_STOPWORDS:
        lo += 1
    while hi > lo and words[hi - 1] in _EDGE_STOPWORDS:
        hi -= 1
    return words[lo:hi]


def _content_words(text: str) -> set[str]:
    """The keyphrase-eligible words in `text` (same filter as candidates)."""
    return {
        t for t in _KEYPHRASE_TOKEN_RE.findall((text or "").lower())
        if (t.isalnum() or "'" in t or "-" in t)
        and len(t) > 2 and not t.isdigit() and t not in _STOPWORDS
    }


def _is_alnum_mix(word: str) -> bool:
    """True if `word` mixes letters and digits - the shape of chemical
    formulae, model numbers and designations ("Al2O3", "R2-D2", "C-3-PO",
    "COVID-19"). Hyphens/apostrophes inside the token are ignored; only the
    presence of at least one letter AND one digit matters. These are
    distinctive, high-signal tag candidates that as lone words would
    otherwise score the RAKE floor."""
    has_alpha = has_digit = False
    for ch in word:
        if ch.isalpha():
            has_alpha = True
        elif ch.isdigit():
            has_digit = True
        if has_alpha and has_digit:
            return True
    return False


# A "code" token: a run of letters/digits with internal hyphens or slashes,
# e.g. "F-15E", "H2O", "BRCA1", "Ti-6Al-4V". We keep only the alphanumeric-mix
# ones (letters AND digits) - those are the distinctive designations that
# separate near-identical variants ("F-15" vs "F-15E"); a bare word or a bare
# number is not discriminating enough.
_CODE_SCAN_RE = re.compile(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*")


def _compile_user_regex(pattern: Optional[str]) -> Optional["re.Pattern[str]"]:
    """Safely compile a user-supplied code regex; None on empty/invalid/over-long
    so a bad setting degrades to 'no extra codes' rather than erroring."""
    if not pattern or len(pattern) > 300:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _code_tokens(text: str, user_pat: Optional["re.Pattern[str]"] = None) -> set[str]:
    """Distinctive code tokens in `text` (lowercased, for matching): the
    alphanumeric-mix designations, plus any spans matching the user's code
    regex (so designations the library has never seen still count)."""
    out: set[str] = set()
    for m in _CODE_SCAN_RE.finditer(text or ""):
        tok = m.group(0)
        if len(tok) >= 2 and _is_alnum_mix(tok):
            out.add(tok.lower())
    if user_pat is not None:
        for m in user_pat.finditer(text or ""):
            s = m.group(0).strip()
            if len(s) >= 2:
                out.add(s.lower())
    return out


def _regex_new_tags(title: str, text: str, user_pat: Optional["re.Pattern[str]"],
                    known: set[str], limit: int = 5) -> list[tuple[str, bool]]:
    """Verbatim code designations matching the user regex that the library does
    NOT already tag - e.g. a future aircraft type. Returned as (code, in_title)
    preserving original case, title hits first, deduped against `known`."""
    if user_pat is None:
        return []
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for src, in_title in ((title or "", True), (text or "", False)):
        for m in user_pat.finditer(src):
            s = m.group(0).strip()
            k = s.lower()
            if len(s) < 2 or k in known or k in seen:
                continue
            seen.add(k)
            out.append((s, in_title))
            if len(out) >= limit:
                return out
    return out


# A UK-style postcode splits into an "outward" code (1-2 letters, a digit,
# maybe a trailing letter/digit: "SN6", "EC1A") immediately followed by an
# "inward" code (a digit then two letters: "8LA", "1AA"). The adjacent pair is
# the distinctive signature of a postal address in a document footer. Matching
# the *pair* - not a lone token - keeps legitimate alphanumeric tags ("G7",
# "G20", "3PL", "Al2O3") safe; those never appear as an outward+inward run.
_PC_OUTWARD_RE = re.compile(r"^[a-z]{1,2}\d[a-z\d]?$")
_PC_INWARD_RE = re.compile(r"^\d[a-z]{2}$")


def _looks_like_address(words: list[str]) -> bool:
    """True if a candidate phrase contains an adjacent postcode pair (e.g.
    'wiltshire sn6 8la') - a street/footer address, never a useful topic tag."""
    for a, b in zip(words, words[1:]):
        if _PC_OUTWARD_RE.match(a) and _PC_INWARD_RE.match(b):
            return True
    return False


def extract_keyphrases(text: str, max_phrases: int = 5, max_words: int = 3,
                       title: str = "", title_boost: float = 1.8,
                       alnum_boost: float = 2.0, recur_boost: float = 1.6
                       ) -> list[tuple[str, float]]:
    """RAKE-style keyphrase extraction for proposing NEW tags the library
    has never seen (the kNN/PMI signals can only ever surface existing tags).

    `text` is the document body; `title` (optional) is folded in as its own
    line so its words are eligible candidates even when absent from the body.

    Candidate phrases are runs of content words bounded by stopwords and
    punctuation; verb/preposition noise is trimmed from each phrase's edges
    (see `_EDGE_STOPWORDS`). Each word then scores degree/frequency (so words
    in longer phrases score higher) and a phrase's score is the sum of its
    words'. Returns [(phrase, score), ...] sorted best-first, scores raw.

    Three boosts ride on top, composing multiplicatively because they apply
    at independent scopes:

      - `alnum_boost` (word): a token mixing letters and digits
        (`_is_alnum_mix`: "Al2O3", "R2-D2") is scaled up - distinctive codes
        rank well even as lone words.
      - `recur_boost` (word): a content word present in BOTH the title and
        the body is scaled up. RAKE is otherwise frequency-blind, so an
        unusual term recurring across title and body - the strongest
        "aboutness" cue for a new tag - would not otherwise stand out.
      - `title_boost` (phrase): a phrase's score is scaled by how much it
        overlaps the title, peaking at `title_boost` for a wholly-title
        phrase.

    So an unusual alphanumeric term that appears in the title and recurs in
    the body is lifted by all three at once.
    """
    title_words = _content_words(title)
    body_words = _content_words(text)
    # Draw candidates from the title AND the body. The title leads on its own
    # line so its words stay eligible even if absent from the body, and so a
    # phrase never spans the title/body seam.
    source = (title + "\n" + text) if title else text

    candidates: list[list[str]] = []
    # Process line by line so a phrase never spans a line break: titles, list
    # items and paragraphs are separate phrases.
    for line in source.split("\n"):
        cur: list[str] = []
        for tok in _KEYPHRASE_TOKEN_RE.findall(line.lower()):
            if (tok.isalnum() or "'" in tok or "-" in tok) and \
                    len(tok) > 2 and not tok.isdigit() and tok not in _STOPWORDS:
                cur.append(tok)
            else:
                if cur:
                    candidates.append(cur)
                    cur = []
        if cur:
            candidates.append(cur)
    # Trim verb/preposition noise off the ends, then keep 1..max_words phrases.
    candidates = [t for c in candidates if 1 <= len(t := _trim_edges(c)) <= max_words]
    # Drop street/footer addresses (a postcode pair is unmistakable boilerplate).
    candidates = [c for c in candidates if not _looks_like_address(c)]
    if not candidates:
        return []
    freq: Counter[str] = Counter()
    degree: Counter[str] = Counter()
    for c in candidates:
        for w in c:
            freq[w] += 1
            degree[w] += len(c)  # degree includes the word itself
    word_score = {w: degree[w] / freq[w] for w in freq}
    # Word-level boosts: alphanumeric-mix tokens, and words recurring across
    # both the title and the body (a strong, otherwise-ignored topical signal).
    cross = title_words & body_words
    for w in word_score:
        if alnum_boost != 1.0 and _is_alnum_mix(w):
            word_score[w] *= alnum_boost
        if recur_boost != 1.0 and w in cross:
            word_score[w] *= recur_boost
    scored: dict[str, float] = {}
    for c in candidates:
        phrase = " ".join(c)
        s = sum(word_score[w] for w in c)
        if s > scored.get(phrase, 0.0):
            scored[phrase] = s
    # Phrase-level title boost: lift phrases by their overlap with the title.
    if title_words and title_boost != 1.0:
        for phrase in list(scored):
            pw = phrase.split()
            overlap = sum(1 for w in pw if w in title_words) / len(pw)
            if overlap:
                scored[phrase] *= 1 + (title_boost - 1) * overlap
    ranked = sorted(scored.items(), key=lambda x: (-x[1], x[0]))
    return ranked[:max_phrases]


def rank_keyphrases_semantic(candidates: list[tuple[str, float]],
                             text: str, title: str = "",
                             rake_blend: float = 0.5,
                             mmr_lambda: float = 0.6,
                             code_user: Optional["re.Pattern[str]"] = None,
                             ) -> list[tuple[str, float]]:
    """KeyBERT-style re-rank of RAKE keyphrase candidates by SEMANTIC centrality
    to the document, reusing the bundled sentence embedder (embed.py).

    RAKE (`extract_keyphrases`) scores a phrase by the degree/frequency of its
    words WITHIN the document - it is blind to whether a phrase captures what the
    document is ABOUT, so a locally-repeated but peripheral phrase can outrank the
    one that names the topic. This embeds the whole document and each candidate
    phrase with the SAME model the similarity ranker uses, scores each candidate
    by its cosine to the document vector (its "aboutness"), blends that with the
    RAKE score, and spreads the picks with MMR so the top phrases are distinct
    facets rather than paraphrases of one concept.

    Returns [(phrase, score), ...] best-first, scores ~0..1. FAIL-SOFT: if the
    embedding backend is unavailable or errors, the input candidates are returned
    unchanged (RAKE order) - so this is a safe, opt-in overlay.

      rake_blend  - the RAKE score's share of the blend (0 = pure semantic,
                    1 = pure RAKE). 0.5 balances topical centrality against
                    RAKE's domain priors (its alnum/code/title boosts).
      mmr_lambda  - MMR relevance/diversity knob (1 = relevance only, 0 = max
                    diversity); ~0.6 keeps picks on-topic but distinct.
      code_user   - optional compiled user code-regex. Phrases carrying a code /
                    alnum designation are floored to at least their RAKE score:
                    gte-small embeds short designations ("F-15E", "BRCA1") poorly,
                    and a pure-semantic rank would otherwise bury exactly the
                    high-precision codes RAKE worked to surface.
    """
    if not candidates:
        return []
    import embed as _embed
    if not _embed.available():
        return candidates
    try:
        import numpy as np
        phrases = [p for p, _ in candidates]
        rake = np.array([s for _, s in candidates], dtype="float32")
        rmax = float(rake.max()) if rake.size else 0.0
        rake = rake / rmax if rmax > 0 else np.zeros_like(rake)
        # Represent the document the same way clip vectors are built (title leads,
        # then body), capped to the model's useful window. NB this embeds the doc
        # afresh; a later optimisation could thread the similarity leg's query
        # vector through instead of paying a second forward pass.
        doc_rep = (((title + "\n") if title else "") + (text or ""))[:_EMBED_TEXT_CHARS]
        dv = _embed.embed_one(doc_rep)
        cv = _embed.embed(phrases)
        if dv is None or cv is None:
            return candidates
        rel = (cv @ dv.astype("float32") + 1.0) / 2.0       # cosine -> [0, 1]
        score = rake_blend * rake + (1.0 - rake_blend) * rel
        # Don't let the semantic leg bury exact designations the embedder can't
        # represent well; floor those to their (normalised) RAKE score.
        for i, p in enumerate(phrases):
            if _code_tokens(p, code_user) or any(_is_alnum_mix(w) for w in p.split()):
                score[i] = max(float(score[i]), float(rake[i]))
        # MMR: greedily pick relevance minus redundancy against the picks so far,
        # so the head of the list covers distinct facets, not synonyms of one.
        selected: list[int] = []
        remaining = list(range(len(phrases)))
        while remaining:
            if not selected:
                i = max(remaining, key=lambda k: float(score[k]))
            else:
                i = max(remaining, key=lambda k: mmr_lambda * float(score[k])
                        - (1.0 - mmr_lambda) * max(float(cv[k] @ cv[j]) for j in selected))
            selected.append(i)
            remaining.remove(i)
        return [(phrases[i], round(float(score[i]), 4)) for i in selected]
    except Exception:
        return candidates


# --- Keyphrase candidate cleaning (opt-in, see `clean_keyphrase_candidates`) ---
# The semantic re-rank is only as good as the pool it re-orders: it happily
# promotes phrases that are topically central but useless as tags (a photo
# credit, a generic phrase, a sentence fragment). These denylists/heuristics
# strip the obvious junk BEFORE ranking. Kept off the default RAKE path so
# production output is unchanged until `keyphrase_clean` is set.

# Attribution / credit boilerplate (substring match, lowercased). Distinct from
# ingest._STOCK_MARKERS, which catches visual-design stock-asset *keyword dumps*;
# these are the photo-credit lines that leak into body text ("alamy stock photo").
_CREDIT_MARKERS = {
    "alamy", "getty", "shutterstock", "istock", "reuters", "associated press",
    "stock photo", "stock image", "image credit", "photo by", "photograph by",
    "courtesy of", "all rights reserved", "copyright",
}
# Filename / format cruft - tokens that ride in when a title is a filename
# ("…_web.pdf") or a download artefact, never a topic.
_FORMAT_JUNK = {
    "pdf", "web", "doc", "docx", "html", "htm", "png", "jpg", "jpeg", "gif",
    "scan", "final", "draft", "copy", "version", "vol", "www", "http", "https",
}
# Verb / adverb / clause words that, at a phrase EDGE, mark a sentence fragment
# rather than a noun-phrase tag ("proceed remains unanswered", "finally
# terminated after"). Trimmed-then-dropped only in the clean path, so the global
# RAKE edge-trimming (`_EDGE_STOPWORDS`) and the default output are untouched.
_CLAUSE_WORDS = {
    "remains", "remain", "remained", "appears", "appear", "appeared", "proceed",
    "proceeds", "terminated", "terminate", "unanswered", "willing", "finally",
    "significantly", "since", "while", "pursuing", "pursued", "piled", "fallen",
    "fall", "falls", "considering", "became", "posed", "compromising", "willing",
}


def _too_generic(conn: sqlite3.Connection, phrase: str, total: int,
                 frac: float = _MAX_DF_FRAC) -> bool:
    """True if EVERY content word of `phrase` is common across the library (high
    document frequency) - i.e. the phrase carries no distinctive term. This is the
    cheap proxy for "semantically central but useless": a generic phrase sits
    close to the document AND to everything else. Reuses the FTS df machinery the
    salient-term selector already uses. Disabled on tiny libraries."""
    if total < _MIN_LIB_FOR_DF_CAP:
        return False
    words = [w for w in re.findall(r"[A-Za-z0-9'\-]+", phrase.lower())
             if len(w) > 2 and w not in _STOPWORDS]
    if not words:
        return True
    cap = total * frac
    return all(_doc_frequency(conn, w) > cap for w in words)


def _keyphrase_title(title: str) -> str:
    """Normalise a filename-ish title before keyphrase extraction: strip the
    extension and a leading date, split separators to spaces, drop format tokens.
    '20160201_whp_maximum_value_from_the_f-35_web.pdf' -> 'whp maximum value from
    the f-35'. Plain prose titles pass through unchanged."""
    t = (title or "").strip()
    if re.search(r"\.[A-Za-z0-9]{2,4}$", t) or ("_" in t and " " not in t):
        t = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", t)        # extension
        t = re.sub(r"^\d{6,8}[_\-. ]+", "", t)           # leading date stamp
        t = re.sub(r"[_\-]+", " ", t).strip()            # separators -> spaces
        t = " ".join(w for w in t.split() if w.lower() not in _FORMAT_JUNK)
    return t


def clean_keyphrase_candidates(conn: sqlite3.Connection,
                               candidates: list[tuple[str, float]], total: int,
                               generic_penalty: float = 0.3
                               ) -> list[tuple[str, float]]:
    """Strip junk from a RAKE candidate pool before ranking. Hard-drops credit
    boilerplate, format/filename cruft, and edge sentence-fragments; soft-
    penalises generic (non-distinctive) phrases so a strongly-supported one can
    still survive. Returns the filtered [(phrase, score), ...]."""
    out: list[tuple[str, float]] = []
    for phrase, score in candidates:
        low = phrase.lower()
        if any(m in low for m in _CREDIT_MARKERS):                 # credit line
            continue
        toks = low.split()
        if toks and all(w in _FORMAT_JUNK or w in _STOPWORDS for w in toks):
            continue                                               # format cruft
        if toks and (toks[0] in _CLAUSE_WORDS or toks[-1] in _CLAUSE_WORDS):
            continue                                               # clause fragment
        if _too_generic(conn, phrase, total):
            score *= generic_penalty
        out.append((phrase, score))
    return out


def _normalise(scores: dict[int, float]) -> dict[int, float]:
    """Scale a {clip_id: score} map to 0..1 by its own max (empty -> empty)."""
    mx = max(scores.values(), default=0.0)
    return {k: v / mx for k, v in scores.items()} if mx > 0 else {}


def _fts_similarity(conn: sqlite3.Connection, effective_text: str,
                    salient: list[str], exclude_clip_id: Optional[int],
                    min_overlap: int) -> tuple[dict[int, float], set[int]]:
    """Lexical (FTS/BM25) leg of the ranker: returns
    ({clip_id: bm25_goodness}, eligible_clips). Eligibility is the salient-term
    OVERLAP anchor minus encyclopedic length-outliers (see the long note in
    suggest_tags). Factored out so the blend can combine it with embeddings."""
    clip_overlap: Counter = Counter()
    for term in salient:
        for cid in _clips_matching_term(conn, term):
            clip_overlap[cid] += 1
    needed = max(1, round(_OVERLAP_FRAC * len(salient)))
    if len(salient) >= 2 * min_overlap:
        needed = max(needed, min_overlap)
    eligible = {cid for cid, ov in clip_overlap.items()
                if ov >= needed} - _length_outlier_clips(conn)
    sims = dict(similar_clips_by_content(
        conn, effective_text, exclude_clip_id=exclude_clip_id) or [])
    return sims, eligible


# --- Body-mention signal ---------------------------------------------------
# A tag whose NAME literally recurs in the document is almost always relevant -
# the most direct "this doc is about X" cue there is, and one the borrow-from-
# neighbours signals miss when the library has no close donor. We count whole-
# word/phrase occurrences (title weighted x3), damp by popularity, blend the
# result in (`mention_weight`), and exempt the strongest such tags from the
# confidence floor so at least some in-text tags always surface when present.
_MENTION_PAT_CACHE: dict = {}
# Guarantee a FEW in-text tags past the floor, not just one: within a tight
# cluster (e.g. defense - F-35 / Hornet / HQ-9) the embedding can't tell the
# variants apart, but the document names the ones it's actually about, so several
# of them are exactly the discriminating tags the user wants.
_MENTION_KEEP_FRAC = 0.3     # keep in-text tags scoring >= this (normalised)...
_MENTION_KEEP_MAX = 4        # ...up to this many, so a keyword-stuffed page
                             # still can't flood the row past the floor.


def _mention_pattern(tag: str):
    r"""Cached whole-word/phrase matcher for a tag NAME. Internal boundaries are
    generic (`\W+`) so multi-word / punctuated tags ("air defence", "F/A-18")
    match; `(?<!\w) ... (?!\w)` stops a tag firing inside a longer word ("NATO"
    must not match "senator")."""
    if tag not in _MENTION_PAT_CACHE:
        parts = [re.escape(w) for w in tag.split() if w]
        _MENTION_PAT_CACHE[tag] = (
            re.compile(r"(?<!\w)" + r"\W+".join(parts) + r"(?!\w)", re.I)
            if parts else None)
    return _MENTION_PAT_CACHE[tag]


def _mention_scores(text: str, title: str, tag_counts: dict,
                    chosen_lower: set) -> dict:
    """Per-tag body-mention score in 0..1: how often each existing tag's name
    occurs in the document (title counted x3), damped by global popularity and
    normalised by the top scorer. Doc-intrinsic - NOT gated by donor eligibility,
    so a literally-named tag surfaces even with no similar clip in the library."""
    title_s, body_s = title or "", text or ""
    raw: dict[str, float] = {}
    for tag in tag_counts:
        if tag.lower() in chosen_lower:
            continue
        pat = _mention_pattern(tag)
        if pat is None:
            continue
        c = 3 * len(pat.findall(title_s)) + len(pat.findall(body_s))
        if c > 0:
            raw[tag] = c / _damp(tag_counts.get(tag, 1))
    mx = max(raw.values(), default=0.0)
    return {k: v / mx for k, v in raw.items()} if mx > 0 else {}


# --- Embedding-floor autotune ----------------------------------------------
# Bounds for the floor "slider" - the model's useful cosine band. Below ~0.80
# everything is admitted; above ~0.99 only near-duplicates survive.
EMBED_FLOOR_MIN = 0.80
EMBED_FLOOR_MAX = 0.99
EMBED_FLOOR_DEFAULT = 0.86


def autotune_embed_floor(conn: sqlite3.Connection,
                         candidates: Optional[list[float]] = None,
                         prec_tolerance: float = 0.02,
                         min_pairs: int = 5) -> dict:
    """Recommend a `min_embed_sim` floor from the live corpus, cheaply.

    Lightweight by design: it does NOT run the full suggestion pipeline. It uses
    the already-cached clip embeddings to build the clip-clip cosine matrix once,
    then for each candidate floor measures, over clips that have a tag-sibling:
      - precision = of the neighbours at cosine >= floor, the share that share a
        tag with the clip (same-cluster vs cross-type bleed) - what the floor is
        actually FOR;
      - recall    = share of those clips that still have >=1 same-tag neighbour
        above the floor (shown for transparency only - see the note below).

    CONSERVATIVE: it targets the PRECISION KNEE - the lowest candidate whose
    donor-precision is within `prec_tolerance` of the best - and never loosens
    below the current default (the result is clamped up to it). Climbing past the
    knee buys no extra precision; and it would only shrink recall, which this
    proxy deliberately does NOT optimise on - once the body-mention signal
    carries recall doc-intrinsically, same-tag embedding neighbours are sparse
    (often just near-duplicates), so neighbour-recall looks flat and is a poor
    objective. So the knee climbs only when denser data shows a higher floor
    genuinely cleaner, and otherwise sits at the default. For a definitive
    recall-aware tune, `eval_subtopics.py --floor` runs the full LOO. Falls back
    to the default when the corpus is too small (`min_pairs` same-tag pairs)."""
    if candidates is None:
        candidates = [0.84, 0.86, 0.88, 0.90, 0.92]
    candidates = sorted(c for c in candidates
                        if EMBED_FLOOR_MIN <= c <= EMBED_FLOOR_MAX)
    try:
        import numpy as np
        if not ensure_clip_embeddings(conn):
            return {"recommended": EMBED_FLOOR_DEFAULT, "reason": "no embedding backend",
                    "table": [], "candidates": candidates}
        rows = conn.execute(
            "SELECT clip_id, vector FROM clip_embeddings WHERE dim > 0").fetchall()
        tags_by: dict[int, set] = defaultdict(set)
        for r in conn.execute(
                "SELECT ct.clip_id, t.name FROM clip_tags ct "
                "JOIN tags t ON t.id = ct.tag_id"):
            tags_by[r["clip_id"]].add(r["name"])
    except Exception:
        return {"recommended": EMBED_FLOOR_DEFAULT, "reason": "unavailable",
                "table": [], "candidates": candidates}

    ids = [r["clip_id"] for r in rows]
    # Clips that have at least one tag-sibling anywhere - the only ones whose
    # recall is even measurable (others are deserts, floor-independent).
    sib = {i: {j for j in ids if j != i and tags_by[i] & tags_by[j]} for i in ids}
    coverable = [i for i in ids if sib[i]]
    if len(coverable) < min_pairs:
        return {"recommended": EMBED_FLOOR_DEFAULT, "reason": "too few tag-siblings",
                "table": [], "candidates": candidates}

    import numpy as np
    mat = np.vstack([np.frombuffer(r["vector"], dtype="float32") for r in rows])
    sims = mat @ mat.T                                  # cosine (vectors L2-normed)
    pos = {i: k for k, i in enumerate(ids)}

    table = []
    for f in candidates:
        prec_num = prec_den = rec_hits = 0
        for i in coverable:
            row = sims[pos[i]]
            nbrs = [j for j in ids if j != i and row[pos[j]] >= f]
            if nbrs:
                same = sum(1 for j in nbrs if tags_by[i] & tags_by[j])
                prec_num += same
                prec_den += len(nbrs)
            if any(j in sib[i] and row[pos[j]] >= f for j in ids):
                rec_hits += 1
        table.append({"floor": round(f, 2),
                      "precision": round(prec_num / prec_den, 3) if prec_den else 0.0,
                      "recall": round(rec_hits / len(coverable), 3)})

    best_prec = max((r["precision"] for r in table), default=0.0)
    # Knee: the lowest floor whose donor-precision is within tol of the best.
    knee = min((r["floor"] for r in table if r["precision"] >= best_prec - prec_tolerance),
               default=EMBED_FLOOR_DEFAULT)
    # Conservative: never loosen below the validated default.
    recommended = round(clamp_embed_floor(max(knee, EMBED_FLOOR_DEFAULT)), 2)
    return {"recommended": recommended, "reason": "autotuned",
            "table": table, "candidates": candidates,
            "coverable": len(coverable)}


def clamp_embed_floor(value) -> float:
    """Coerce a floor value into the slider's [MIN, MAX] band; non-numeric ->
    the default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return EMBED_FLOOR_DEFAULT
    return max(EMBED_FLOOR_MIN, min(EMBED_FLOOR_MAX, v))


def suggest_tags(conn: sqlite3.Connection,
                 text: str = "",
                 title: str = "",
                 note: str = "",
                 chosen_tags: Optional[list[str]] = None,
                 exclude_clip_id: Optional[int] = None,
                 limit: int = 12,
                 content_weight: float = 1.0,
                 cooccur_weight: float = 0.6,
                 predict_weight: float = 0.9,
                 mention_weight: float = 0.9,
                 keyphrase_weight: float = 0.35,
                 keyphrase_semantic: bool = False,
                 keyphrase_clean: bool = False,
                 keyphrase_pool: int = 25,
                 keyphrase_rake_blend: float = 0.5,
                 keyphrase_mmr_lambda: float = 0.6,
                 suggest_new: bool = True,
                 max_new: int = 3,
                 title_new_reserve: int = 1,
                 min_score_frac: float = 0.25,
                 min_overlap: int = 2,
                 use_embeddings: bool = True,
                 min_embed_sim: float = 0.86,
                 embed_blend: float = 0.5,
                 code_boost: float = 2.5,
                 code_frac_cap: float = 0.5,
                 code_regex: Optional[str] = None,
                 _debug: Optional[dict] = None) -> list[dict]:
    """Return ranked tag suggestions: [{tag, score, why}, ...].

    `text`     - document body. Drives the content-similarity and
                 term->tag-prediction signals.
    `title`    - document title; internally repeated so title terms weigh
                 more than body terms (titles are high-signal).
    `note`     - the user's own comment on the clip. Folded into `title` and
                 thus treated identically to it (tripled into the similarity /
                 PMI text, title-weighted in the body-mention and keyphrase
                 signals, its codes counted as title codes) - the user telling
                 us what a document is about is as high-signal as its title.
    `chosen_tags` - tags already entered; excluded from results and used to
                 drive the co-occurrence signal.
    `suggest_new` - when set, propose keyphrases from the document itself as
                 brand-new tags (flagged `new: True`) to fill any remaining
                 slots; this is the only signal that can surface a tag the
                 library doesn't already have.
    `keyphrase_semantic` - opt-in overlay: re-rank the RAKE keyphrase candidates
                 by embedding centrality to the document (KeyBERT-style, see
                 `rank_keyphrases_semantic`) instead of by RAKE score alone. Pulls
                 a larger candidate pool (`keyphrase_pool`) and blends the RAKE and
                 semantic scores (`keyphrase_rake_blend`), diversifying with MMR
                 (`keyphrase_mmr_lambda`). Needs the embedding backend (else it is
                 a no-op and RAKE order stands). Default OFF, so production output
                 is unchanged until toggled - the seam for A/B (eval_keyphrases.py).
    `why`      - 'similar' | 'predicted' | 'co-occurring' |
                 'mention' (tag name recurs in the document body/title) |
                 'both' (multiple signals) | 'keyword' (a proposed new tag) |
                 'code' (a matched alphanumeric designation), for UI transparency.
    `mention_weight` - weight of the body-mention signal: existing tags whose
                 NAME literally recurs in the document are up-weighted, and the
                 top few are guaranteed past the confidence floor. Strong within
                 a tight cluster the embedding can't separate (F-35 vs Hornet);
                 0 disables. See `_mention_scores`.
    Candidates scoring below `min_score_frac` of the best score are dropped
    so the chip row only shows confident suggestions.
    `min_overlap` - topical-overlap floor: a clip must share this many distinct
                 salient terms with the document before its tags can be
                 borrowed by the content/predict signals (relaxed to 1 for
                 documents with very little extractable text). Guards against an
                 off-topic library surfacing its popular tags on every upload.
                 Only used on the lexical (FTS) ranker.
    `use_embeddings` - blend the semantic embedding leg (embed.py) with the
                 lexical FTS leg when the bundled weights load; falls back to FTS
                 alone otherwise. Set False to force the lexical leg only.
    `min_embed_sim` - absolute cosine floor: clips below it are off-topic and
                 contribute nothing, so an unrelated document yields keyword
                 suggestions only. Calibrated to the active model's cosine
                 distribution: gte-small's range is ~[0.70, 0.96]. Default 0.86,
                 raised from 0.82 once the body-mention signal carried recall: a
                 `min_embed_sim` floor sweep (`eval_subtopics --floor`) showed
                 0.82->0.86 lifts precision 50->76% and in-cluster share 53->89%
                 (bleed/clip 3.2->0.5) at NO recall cost (hit@1 actually rises,
                 because mention is not gated by this floor) - it cuts the
                 cross-TYPE bleed (an F-15 doc borrowing F-35/Hornet tags) that
                 sits at ~0.82-0.85. 0.88 starts starving genuine donors. NB this
                 separates topics/types, NOT facets of one program (F-35 variants
                 vs acquisition vs history all sit ~0.90+): those need tag
                 granularity + the mention signal, not the floor.
    `embed_blend` - the embedding leg's share of the blended similarity score in
                 [0,1]; the FTS leg takes the rest. 0.5 balances semantic recall
                 with lexical (exact-token/jargon) precision; 0 = lexical only,
                 1 = semantic-led (lexical still gates/rescues).
    `code_boost` - how much to lift a clip that shares one of the document's
                 alphanumeric designations ("F-15E", "BRCA1"); a title code
                 counts double a body code (compounds with title weighting).
                 1.0 disables. Helps the right variant win among look-alikes.
    `code_frac_cap` - ceiling on how much of the row code-boosted tags may take,
                 as a fraction of `limit`. The boost is meant to reorder a
                 designation AMONG its look-alike siblings, not to let a fistful
                 of model numbers crowd out the document's topical tags. So at
                 most round(code_frac_cap * limit) code-boosted tags are kept,
                 and only when there are non-code tags waiting to fill the freed
                 slots (a doc whose only relevant tags are codes still shows them
                 all). 1.0 disables the cap. Independent of the floor split below.
    `code_regex` - optional user regex; spans matching it are treated as codes
                 too (for the boost above) AND, when not already a tag, surfaced
                 verbatim as new-tag suggestions - so designations the library
                 has never seen (a future aircraft type) are still recognised.
    `_debug` - optional dict; if passed, filled with {'ranker', 'eligible_clips'}
                 for diagnostics/demos. Does not affect results.
    """
    chosen = [c.strip() for c in (chosen_tags or []) if c.strip()]
    chosen_lower = {c.lower() for c in chosen}
    tag_counts = _tag_counts(conn)
    # Keywords the user has banned from suggestions (matched by the same
    # normalised key as near-duplicate tags, so 'web-scraping' covers
    # 'Web Scraping'). Drops both existing-tag candidates and proposed new
    # keyphrases below; it never affects tags already on a clip.
    blocked_keys = blocked_keyword_keys(conn)

    def _blocked(name: str) -> bool:
        return bool(blocked_keys) and tag_key(name) in blocked_keys
    try:
        total_clips = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
    except sqlite3.OperationalError:
        total_clips = 0

    # Title terms are the strongest "aboutness" signal; weight them by
    # repetition rather than asking every caller to remember to. The user's
    # note is folded in here so everything title-derived below (the tripled
    # similarity/PMI text, the title x3 in body-mention, the keyphrase title
    # boost, title-code detection) treats it exactly like the title.
    title = (title or "").strip()
    note = (note or "").strip()
    if note:
        title = (title + "\n" + note) if title else note
    effective_text = ((title + "\n") * 3 + (text or "")) if title else (text or "")

    # Salient terms feed both the content (kNN) and prediction (PMI) signals.
    salient = _salient_terms(conn, effective_text) if effective_text.strip() else []

    # --- Pick the similarity ranker, then derive `similar` + `eligible_clips` ---
    # Both text-derived signals (content kNN and PMI) borrow tags from clips
    # that match the document, gated by `eligible_clips` - the clips on-topic
    # enough to trust. Two rankers can fill these roles, with identical output
    # shape so the rest of the function doesn't care which ran:
    #
    #   * EMBEDDING (semantic): cosine similarity of sentence embeddings, so it
    #     matches on meaning even across different words. Eligibility is an
    #     ABSOLUTE cosine floor (`min_embed_sim`): a document unrelated to the
    #     whole library clears it for nobody, so the existing-tag signals fall
    #     silent and the row is left to keyword suggestions. Used when embed.py's
    #     bundled weights are loadable.
    #   * FTS (lexical fallback): BM25 over salient terms. Because a clip can
    #     "match" on one generic shared word, eligibility here is a salient-term
    #     OVERLAP anchor - a clip must share a FRACTION (`_OVERLAP_FRAC`, but at
    #     least `min_overlap` for text-rich docs) of the document's salient
    #     terms, relaxed to one term for near-empty docs. Used whenever the
    #     embedding backend isn't available.
    #
    # The lexical (FTS) leg is always computed; the semantic (embedding) leg is
    # added when the bundled weights load. They are BLENDED rather than swapped:
    #
    #   * Eligibility (who may donate tags) is gated by SEMANTIC relevance - the
    #     embedding cosine floor - so an off-topic upload borrows nothing, even
    #     if it shares generic words with a clip. A strong LEXICAL match can
    #     RESCUE a clip whose cosine is only lukewarm (exact jargon / formula
    #     overlap the embedding under-weights, e.g. "NaOH", "BRCA1", "F-35"),
    #     but only above a soft fraction of the floor, so a pure lexical
    #     coincidence the embedding judges off-topic stays out.
    #   * Score (how strongly) blends the two normalised similarities. So an
    #     exact-token match and a semantic match reinforce each other, and a
    #     semantic-only near-miss (a same-shaped formula with no real word
    #     overlap) is tempered by the lexical leg contributing zero.
    #
    # When embeddings are unavailable we fall back to the FTS leg alone, whose
    # own gate is the salient-term overlap anchor. Length-outlier clips (a 90k-
    # char Wikipedia article among ordinary bookmarks) are excluded as donors on
    # BOTH legs: BM25 lets an encyclopedic clip match anything on a few shared
    # words, and - despite the earlier assumption otherwise - the embedding leg
    # is not immune either, because a clip is embedded from its first passage
    # only, so a broad intro ("Rabbit", "Bamboo") embeds to a broad region that
    # an adjacent or sparse query can reach, dragging its broad tags ("Mammals",
    # "Botany") onto unrelated documents. Such clips' tags stay reachable via
    # co-occurrence / keyphrases; they're just not treated as evidence that an
    # unrelated doc is about their topic.
    ranker = "fts"
    similar: list[tuple[int, float]] = []
    eligible_clips: set[int] = set()
    if effective_text.strip():
        fts_sim, fts_eligible = _fts_similarity(
            conn, effective_text, salient, exclude_clip_id, min_overlap)
        emb = (similar_clips_by_embedding(
                   conn, effective_text, exclude_clip_id=exclude_clip_id,
                   min_sim_frac=0.0, min_abs_sim=0.0)
               if use_embeddings else None)
        if emb is not None and embed_blend > 0:
            ranker = "blend"
            emb_sim = {cid: s for cid, s in emb}
            emb_eligible = {cid for cid, s in emb_sim.items() if s >= min_embed_sim}
            rescue = min_embed_sim - _EMBED_RESCUE_MARGIN
            # fts_eligible already drops length outliers; subtract them from the
            # union so the embedding leg can't re-admit one (see note above).
            eligible_clips = (emb_eligible | {
                cid for cid in fts_eligible if emb_sim.get(cid, 0.0) >= rescue}
                ) - _length_outlier_clips(conn)
            fn, en = _normalise(fts_sim), _normalise(emb_sim)
            similar = [(cid, (1 - embed_blend) * fn.get(cid, 0.0)
                             + embed_blend * en.get(cid, 0.0))
                       for cid in eligible_clips]
            similar = [(c, s) for c, s in similar if s > 0]
        else:
            eligible_clips = fts_eligible
            similar = list(fts_sim.items())
    # --- Exact code-token boost ---
    # Near-identical variants ("F-15" vs "F-15E", "737" vs "737 MAX") differ by
    # a tiny alphanumeric suffix that is a sliver of the document and easily lost
    # in similarity. So we give clips that contain the document's own code
    # designations a direct lift. A code in the TITLE counts double a body code
    # (compounding with the title weighting, which already triples title terms
    # into `effective_text`). A user-supplied `code_regex` extends recognition to
    # designations the library has never indexed (e.g. a future "F-47"). A
    # title-code match is distinctive enough to also grant eligibility, so the
    # right variant surfaces even when its siblings cluster around it.
    # The discriminator between near-identical variants is the designation
    # itself, so the boost works at the TAG level: a tag whose NAME is one of
    # the document's code tokens is lifted ("F-15E" matches the query's "f-15e";
    # the look-alike "F-15EX" does not, so it isn't lifted). A title code counts
    # double a body code, compounding with the title weighting. To ensure the
    # right tag is even in the running, a clip strongly featuring a TITLE code is
    # also admitted as eligible (BM25 prominence, so the clip ABOUT the F-15E
    # beats one that only cross-references it). A user `code_regex` feeds the
    # same machinery for multi-word / digit-only designations ("737 MAX") and
    # ones the library has never seen.
    code_user = _compile_user_regex(code_regex)
    title_codes = _code_tokens(title, code_user)
    body_codes = _code_tokens(text or "", code_user) - title_codes
    code_mult: dict[str, float] = {}
    code_eligible: set[int] = set()
    if code_boost > 1.0 and (title_codes or body_codes):
        for codes, w in ((title_codes, 1.0), (body_codes, 0.5)):
            for code in codes:
                code_mult[code] = max(code_mult.get(code, 1.0),
                                      1 + (code_boost - 1) * w)
                if w == 1.0:                 # title code: admit the clip about it
                    strength = _code_clip_strength(conn, code)
                    if len(strength) <= 25:
                        code_eligible |= {c for c, s in strength.items() if s >= 0.5}
        eligible_clips |= code_eligible
    if _debug is not None:
        _debug["ranker"] = ranker
        _debug["eligible_clips"] = sorted(eligible_clips)
        _debug["code_tokens"] = sorted(title_codes | body_codes)

    # --- Signal 1: content similarity ---
    content_scores: Counter = Counter()
    sim_by_clip = dict(similar)
    # A clip admitted purely by a code match still needs a base similarity so
    # its tag can be scored (an exact designation match is its own evidence).
    for cid in code_eligible:
        sim_by_clip.setdefault(cid, _CODE_BASE_SIM)
    if sim_by_clip:
        by_clip = _tags_for_clips(conn, list(sim_by_clip))
        for cid, sim in sim_by_clip.items():
            if cid not in eligible_clips:
                continue  # not on-topic enough to borrow from
            tags_here = by_clip.get(cid, [])
            if not tags_here:
                continue
            # A clip's similarity is evidence spread across ALL its tags. A
            # focused 1-tag clip backs its tag fully; a grab-bag clip bundling
            # unrelated topics (e.g. "Bamboo | Chickens | Goats") backs each
            # only fractionally, so its off-topic tags stop riding in whenever
            # one of them happens to match.
            spread = sim / len(tags_here)
            for tag in tags_here:
                if tag.lower() in chosen_lower:
                    continue
                content_scores[tag] += spread / _damp(tag_counts.get(tag, 1))

    # Normalise content scores to 0..1 for stable blending.
    if content_scores:
        mx = max(content_scores.values())
        if mx > 0:
            for k in list(content_scores):
                content_scores[k] /= mx

    # --- Signal 4: term->tag prediction (Naive Bayes / PMI) ---
    predict_scores: Counter = Counter()
    raw_predict = predicted_tag_scores(conn, salient, tag_counts, total_clips,
                                       chosen_lower, eligible_clips)
    if raw_predict:
        mx = max(raw_predict.values())
        if mx > 0:
            for tag, v in raw_predict.items():
                predict_scores[tag] = v / mx

    # --- Signal 2: co-occurrence ---
    cooccur_scores: Counter = Counter()
    votes = _cooccurring_tags(conn, chosen)
    if votes:
        damped = {tag: v / _damp(tag_counts.get(tag, 1))
                  for tag, v in votes.items()
                  if tag.lower() not in chosen_lower}
        if damped:
            mx = max(damped.values())
            if mx > 0:
                for tag, v in damped.items():
                    cooccur_scores[tag] = v / mx

    # --- Signal 5: body mention (tag name literally recurs in the document) ---
    # Doc-intrinsic, so it works even where every borrow-from-neighbours signal
    # is silent (a topic desert) and where the embedding can't separate a tight
    # cluster's variants. The top few in-text tags are guaranteed past the floor.
    mention_scores = _mention_scores(text or "", title, tag_counts, chosen_lower)
    must_keep = {t for t in sorted(mention_scores, key=lambda k: -mention_scores[k])
                 [:_MENTION_KEEP_MAX]
                 if mention_scores[t] >= _MENTION_KEEP_FRAC and not _blocked(t)}

    # --- Blend ---
    all_tags = (set(content_scores) | set(predict_scores)
                | set(cooccur_scores) | set(mention_scores))
    results = []
    for tag in all_tags:
        if _blocked(tag):
            continue
        cs = content_scores.get(tag, 0.0)
        ps = predict_scores.get(tag, 0.0)
        os_ = cooccur_scores.get(tag, 0.0)
        mn = mention_scores.get(tag, 0.0)
        score = (content_weight * cs + predict_weight * ps
                 + cooccur_weight * os_
                 + mention_weight * mn)
        if score <= 0:
            continue
        # Lift a tag that IS one of the document's code designations, so the
        # exact variant outranks its look-alike siblings.
        boost = code_mult.get(tag.lower(), 1.0)
        score *= boost
        contributing = [name for name, v in
                        (("similar", cs), ("predicted", ps),
                         ("co-occurring", os_), ("mention", mn))
                        if v > 0]
        why = ("code" if boost > 1.0 else
               contributing[0] if len(contributing) == 1 else "both")
        results.append({"tag": tag, "score": round(score, 4), "why": why})

    results.sort(key=lambda r: (-r["score"], r["tag"]))
    # Confidence floor, applied PER GROUP (code-boosted vs the rest). The code
    # boost is a large multiplier (code_boost), so a single boosted designation
    # would set a floor that wipes out every topical tag - the model numbers and
    # the subject tags answer different questions and shouldn't gate each other.
    # Each group instead keeps entries within `min_score_frac` of that group's
    # own top score; codes and topical tags then coexist, re-merged by score.
    if results:
        code_res = [r for r in results if r["why"] == "code"]
        plain_res = [r for r in results if r["why"] != "code"]

        def _floored(group: list[dict]) -> list[dict]:
            if not group:
                return []
            top = group[0]["score"]          # group is already sorted desc
            return [r for r in group if r["score"] >= top * min_score_frac]

        code_res, plain_res = _floored(code_res), _floored(plain_res)
        # Cap the code-boosted share of the row, but only when topical tags are
        # there to take the freed slots (else a codes-only doc keeps them all).
        if code_res and plain_res and code_frac_cap < 1.0:
            code_res = code_res[:max(1, round(code_frac_cap * limit))]
        # Guarantee the document's own strongly-named tags a slot: re-admit any
        # top in-text tag the floor dropped (the body-mention guarantee). Code
        # tags are excluded - their share of the row is governed by the cap
        # above, so the guarantee never resurrects a designation the cap dropped.
        if must_keep:
            present = {r["tag"] for r in code_res} | {r["tag"] for r in plain_res}
            plain_res += [r for r in results
                          if r["tag"] in must_keep and r["tag"] not in present
                          and r["why"] != "code"]
        results = code_res + plain_res
        results.sort(key=lambda r: (-r["score"], r["tag"]))

    # --- Proposed new tags from document keyphrases (the only signal that can
    # surface a tag the library lacks). They normally just fill leftover
    # slots after existing tags, but we reserve up to `title_new_reserve`
    # slot(s) for a title-derived phrase so a strong title keyword can appear
    # even when existing-tag signals would otherwise fill the whole row. New
    # tags always sort after existing ones. ---
    new_tags: list[dict] = []
    if suggest_new and (text or title):
        title_words = _content_words(title)
        known = ({t.lower() for t in tag_counts}
                 | {r["tag"].lower() for r in results} | chosen_lower)
        # User-regex designations the library doesn't yet tag (e.g. a future
        # aircraft type) are surfaced first, verbatim, and a title hit reserves
        # a slot like a title keyphrase - so a brand-new "F-47" still appears.
        for code, in_title in _regex_new_tags(title, text or "", code_user, known):
            if len(new_tags) >= max_new:
                break
            if _blocked(code):
                continue
            known.add(code.lower())
            new_tags.append({"tag": code, "why": "code", "new": True,
                             "score": round(keyphrase_weight * (1.2 if in_title else 1.0), 4),
                             "_title": in_title})
        # Pass the body and title separately (not the conflated effective_text)
        # so the cross-field recurrence boost can tell them apart. With the
        # semantic overlay on, pull a larger candidate pool and re-rank it by
        # embedding centrality (KeyBERT-style); otherwise the RAKE order stands
        # byte-for-byte (default path unchanged).
        if keyphrase_semantic or keyphrase_clean:
            kp_title = _keyphrase_title(title) if keyphrase_clean else title
            cands = extract_keyphrases(text or "", title=kp_title,
                                       max_phrases=keyphrase_pool)
            if keyphrase_clean:
                cands = clean_keyphrase_candidates(conn, cands, total_clips)
            if keyphrase_semantic and use_embeddings:
                cands = rank_keyphrases_semantic(
                    cands, text or "", title, rake_blend=keyphrase_rake_blend,
                    mmr_lambda=keyphrase_mmr_lambda, code_user=code_user)
            phrases = cands
        else:
            phrases = extract_keyphrases(text or "", title=title)
        top = phrases[0][1] if phrases else 1.0
        for phrase, raw in phrases:
            if len(new_tags) >= max_new:
                break
            if phrase in known or _blocked(phrase):
                continue
            known.add(phrase)
            new_tags.append({"tag": phrase, "why": "keyword", "new": True,
                             "score": round(keyphrase_weight * raw / (top or 1.0), 4),
                             "_title": bool(title_words & set(phrase.split()))})

    if not new_tags:
        return results[:limit]

    # Reserve room for title-derived new tags (drop the weakest existing tags
    # if the row is full), then fill any remaining slots - title phrases
    # first, then the rest.
    reserve = min(title_new_reserve, sum(1 for n in new_tags if n["_title"]))
    out = results[:max(0, limit - reserve)]
    ordered_new = ([n for n in new_tags if n["_title"]]
                   + [n for n in new_tags if not n["_title"]])
    for n in ordered_new:
        if len(out) >= limit:
            break
        out.append(n)
    for n in out:
        n.pop("_title", None)
    return out[:limit]
