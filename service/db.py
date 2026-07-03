"""
SQLite schema for the local clipper.

Design notes:
- One database file. Default location <repo root>/clipper.db (CLIPPER_DB
  overrides), anchored to this file rather than the CWD.
- Assets (images, PDFs) stored as BLOBs keyed by sha256 - dedup is free.
- Annotations reference clips by id and store an anchor (text-quote per the
  W3C Web Annotation spec) so they survive minor content edits.
- FTS5 virtual table indexes title, text content, and tag names.
- A revisions table keeps every version of a clip's content so edits are
  non-destructive.
- An `embedding` BLOB column on clips is reserved but unused; populate later
  with sqlite-vec for semantic search.
"""

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = r"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY,
    url             TEXT,
    canonical_url   TEXT,           -- from <link rel="canonical"> if present
    title           TEXT NOT NULL,
    captured_at     INTEGER NOT NULL,  -- unix epoch seconds
    source          TEXT,           -- 'userscript-article', 'userscript-full', 'migrator-onenote', etc.
    content_html    TEXT,           -- the rendered HTML for the clip
    content_text    TEXT,           -- plain text extracted from html, for FTS
    content_hash    TEXT,           -- sha256 of content_text, for change detection
    word_count      INTEGER,
    reading_minutes INTEGER,
    language        TEXT,
    -- Free-form JSON metadata. Things like OG tags, OneNote source paths,
    -- author/published date if we could parse them.
    meta_json       TEXT,
    -- Free-form user comment attached at clip time (the "user note"). Fed to
    -- tag suggestion like the title, and surfaced atop search-report exports.
    user_note       TEXT,
    -- Normalised projections of the messy meta_json fields, for fast filtering
    -- (see normalize.py). date_published_norm is a full sortable ISO date even
    -- for partial inputs; date_precision (day|month|year) says how much to
    -- trust. marking_class is the single classification level; marking_caveats
    -- is a JSON array of canonical caveats. All NULL when not detected.
    date_published_norm TEXT,
    date_precision      TEXT,
    marking_class       TEXT,
    marking_caveats     TEXT,
    -- Reserved for future use with sqlite-vec / sqlite-vss.
    embedding       BLOB
);

CREATE INDEX IF NOT EXISTS idx_clips_captured ON clips(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_clips_url      ON clips(url);
CREATE INDEX IF NOT EXISTS idx_clips_canon    ON clips(canonical_url);

-- Assets: images and PDFs, deduped by content hash.
CREATE TABLE IF NOT EXISTS assets (
    sha256       TEXT PRIMARY KEY,
    mime         TEXT NOT NULL,
    bytes        BLOB NOT NULL,
    byte_size    INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);

-- Link assets to clips. An asset can belong to many clips and vice versa.
CREATE TABLE IF NOT EXISTS clip_assets (
    clip_id      INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    asset_sha    TEXT NOT NULL REFERENCES assets(sha256),
    role         TEXT NOT NULL,   -- 'inline-image', 'attachment', 'pdf-primary'
    filename     TEXT,            -- original filename for attachments
    PRIMARY KEY (clip_id, asset_sha, role, filename)
);
CREATE INDEX IF NOT EXISTS idx_clip_assets_clip ON clip_assets(clip_id);

-- Tags and the join table.
CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS clip_tags (
    clip_id  INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (clip_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_clip_tags_tag ON clip_tags(tag_id);

-- Annotations (highlights + notes), anchored by text quote.
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    -- Anchor follows W3C Web Annotation TextQuoteSelector: the exact text
    -- being annotated plus a little context before/after to disambiguate.
    quote       TEXT,
    prefix      TEXT,
    suffix      TEXT,
    -- For PDFs we use page + bounding box instead.
    pdf_page    INTEGER,
    pdf_rect    TEXT,           -- "x,y,w,h" in PDF points, JSON-encoded
    note        TEXT,           -- the user's annotation body, markdown
    color       TEXT            -- highlight color, hex
);
CREATE INDEX IF NOT EXISTS idx_anno_clip ON annotations(clip_id);

-- Edit history for clips. Every modification of content_html/title writes
-- a row here first.
CREATE TABLE IF NOT EXISTS revisions (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    saved_at    INTEGER NOT NULL,
    title       TEXT,
    content_html TEXT,
    note        TEXT             -- e.g. "trimmed nav junk", "manual fix"
);
CREATE INDEX IF NOT EXISTS idx_rev_clip ON revisions(clip_id, saved_at DESC);

-- Remembered metadata corrections. When the user edits the auto-extracted
-- metadata in the upload preview, we store the override keyed by a scope
-- (host for HTML/MHTML, or 'ext:pdf' etc.) so future ingests of similar
-- files can pre-fill the user's prior fixes. field/value pairs.
CREATE TABLE IF NOT EXISTS metadata_corrections (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL,    -- e.g. 'host:example.com' or 'ext:pdf'
    field       TEXT NOT NULL,    -- e.g. 'marking', 'report_number'
    value       TEXT,             -- the corrected value
    updated_at  INTEGER NOT NULL,
    UNIQUE(scope, field)
);
CREATE INDEX IF NOT EXISTS idx_corr_scope ON metadata_corrections(scope);

-- Suggestion blocklist. Keywords the user never wants proposed as tag
-- suggestions again (e.g. a generic term the ranker keeps surfacing). Matched
-- by the same normalised key as near-duplicate tags (see tag_key), so blocking
-- 'Web Scraping' also suppresses 'web-scraping'/'webscraping'. This only
-- filters what the suggester PROPOSES; it never touches tags already on clips,
-- so a user can still type a blocked word by hand if they want it.
CREATE TABLE IF NOT EXISTS suggestion_blocklist (
    id          INTEGER PRIMARY KEY,
    keyword     TEXT NOT NULL,           -- original spelling, for display
    norm_key    TEXT UNIQUE NOT NULL,    -- tag_key() match key
    created_at  INTEGER NOT NULL
);

-- Small key/value store for app-wide settings (e.g. whether Outlook imports
-- open a tagging review window). String values; callers coerce.
CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

"""

# FTS5 schema kept in its own string so the migration can rebuild JUST the
# index. FTS5 virtual tables can't be ALTERed to add a column, so adding `note`
# to an existing DB means dropping and recreating the table + triggers, then
# repopulating - see _migrate(). Standalone (not external content) because we
# index `tags`, which isn't a real column on clips; we pay a small storage cost
# (text duplicated) for a much simpler model. `note` is the user's clip comment.
FTS_SCHEMA = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
    title, content_text, tags, note,
    tokenize='porter unicode61'
);

-- Triggers to keep FTS5 in sync with clip inserts/deletes/updates. The tags
-- column is filled from clip_tags by subquery, so an UPDATE of any clips
-- column (meta_json, the norm projections, ...) preserves tag searchability
-- - the old '' placeholder silently wiped it. refresh_fts_tags() is still
-- required when clip_tags ITSELF changes (no trigger fires on clips then).
CREATE TRIGGER IF NOT EXISTS clips_fts_ai AFTER INSERT ON clips BEGIN
    INSERT INTO clips_fts(rowid, title, content_text, tags, note)
    VALUES (new.id, new.title, COALESCE(new.content_text, ''),
            COALESCE((SELECT GROUP_CONCAT(t.name, ' ')
                      FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id
                      WHERE ct.clip_id = new.id), ''),
            COALESCE(new.user_note, ''));
END;
CREATE TRIGGER IF NOT EXISTS clips_fts_ad AFTER DELETE ON clips BEGIN
    DELETE FROM clips_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS clips_fts_au AFTER UPDATE ON clips BEGIN
    DELETE FROM clips_fts WHERE rowid = old.id;
    INSERT INTO clips_fts(rowid, title, content_text, tags, note)
    VALUES (new.id, new.title, COALESCE(new.content_text, ''),
            COALESCE((SELECT GROUP_CONCAT(t.name, ' ')
                      FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id
                      WHERE ct.clip_id = new.id), ''),
            COALESCE(new.user_note, ''));
END;
"""

SCHEMA += FTS_SCHEMA


def get_db_path() -> str:
    # Anchored to the repo root (the parent of service/), NOT the CWD: a
    # CWD-relative default silently opens/creates a different library
    # depending on where the service was launched from.
    default = Path(__file__).resolve().parent.parent / "clipper.db"
    return os.environ.get("CLIPPER_DB", str(default))


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or get_db_path())
    conn.row_factory = sqlite3.Row
    # Foreign keys must be enabled per-connection.
    conn.execute("PRAGMA foreign_keys = ON")
    # The dev server is threaded and some read paths commit cache rows, so
    # writers can collide: wait for the lock instead of failing instantly.
    conn.execute("PRAGMA busy_timeout = 5000")
    # Safe under WAL (the journal survives a crash); much cheaper commits.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _fts_columns(conn: sqlite3.Connection) -> set[str]:
    """Column names of the clips_fts virtual table (empty set if absent)."""
    try:
        return {r["name"] for r in conn.execute("PRAGMA table_info(clips_fts)")}
    except sqlite3.OperationalError:
        return set()


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Repopulate clips_fts from scratch for every clip (title/text/tags/note)."""
    conn.execute("DELETE FROM clips_fts")
    for r in conn.execute("SELECT id FROM clips"):
        refresh_fts_tags(conn, r["id"])


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema changes for databases created before a column existed.

    `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a new
    column on `clips` would be invisible to an older clipper.db. Apply such
    additions here, idempotently, by inspecting the live schema."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
    if "user_note" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN user_note TEXT")

    # Normalised date/marking projection columns (see normalize.py). Added for
    # filtering; older clips keep them NULL until a backfill is run.
    for col in ("date_published_norm", "date_precision",
                "marking_class", "marking_caveats"):
        if col not in cols:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {col} TEXT")
    # Indexes live here (not SCHEMA) because SCHEMA runs before these ALTERs, so
    # on an older DB a CREATE INDEX in SCHEMA would reference a missing column.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_marking_class "
                 "ON clips(marking_class)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_date_norm "
                 "ON clips(date_published_norm)")

    # The FTS index gained a `note` column. FTS5 can't ALTER columns, so on an
    # older index drop the table + triggers, recreate them from FTS_SCHEMA, and
    # repopulate (must run AFTER the user_note ALTER above, which it reads).
    fts_cols = _fts_columns(conn)
    if fts_cols and "note" not in fts_cols:
        conn.execute("DROP TRIGGER IF EXISTS clips_fts_ai")
        conn.execute("DROP TRIGGER IF EXISTS clips_fts_ad")
        conn.execute("DROP TRIGGER IF EXISTS clips_fts_au")
        conn.execute("DROP TABLE IF EXISTS clips_fts")
        conn.executescript(FTS_SCHEMA)
        _rebuild_fts(conn)

    # Older triggers wrote tags='' into clips_fts on every clips UPDATE, so a
    # meta-only update (review flag, norm backfill) silently dropped the clip's
    # tags from the index. Detect them by the marker the fixed trigger carries
    # (the GROUP_CONCAT subquery), swap the triggers, and rebuild the index -
    # existing rows may already have blanked tags. CREATE TRIGGER IF NOT EXISTS
    # never replaces, hence the explicit drop.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'clips_fts_au'").fetchone()
    if row and "GROUP_CONCAT" not in (row["sql"] or ""):
        conn.execute("DROP TRIGGER IF EXISTS clips_fts_ai")
        conn.execute("DROP TRIGGER IF EXISTS clips_fts_ad")
        conn.execute("DROP TRIGGER IF EXISTS clips_fts_au")
        conn.executescript(FTS_SCHEMA)
        _rebuild_fts(conn)


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager that commits on success, rolls back on exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def now_ts() -> int:
    return int(time.time())


def write_norm_columns(conn: sqlite3.Connection, clip_id: int, cols: dict,
                       meta_json: str | None = None) -> None:
    """Write a clip's normalised date/marking projection columns (see
    normalize.columns_from_meta). The single owner of this UPDATE so the ingest
    path and the backfill stay in lock-step. Pass meta_json to rewrite it in the
    same statement (the backfill does; ingest leaves it None)."""
    sets = ["date_published_norm = ?", "date_precision = ?",
            "marking_class = ?", "marking_caveats = ?"]
    vals = [cols["date_published_norm"], cols["date_precision"],
            cols["marking_class"], cols["marking_caveats"]]
    if meta_json is not None:
        sets.insert(0, "meta_json = ?")
        vals.insert(0, meta_json)
    vals.append(clip_id)
    conn.execute(f"UPDATE clips SET {', '.join(sets)} WHERE id = ?", vals)


def refresh_fts_tags(conn: sqlite3.Connection, clip_id: int) -> None:
    """Rebuild the FTS row for one clip after its tags change.

    The AFTER INSERT/UPDATE triggers on clips populate FTS with an empty
    tags string. Whenever clip_tags changes we must re-write the FTS row
    with the latest concatenated tag names.
    """
    row = conn.execute(
        """
        SELECT GROUP_CONCAT(t.name, ' ') AS names
        FROM clip_tags ct JOIN tags t ON t.id = ct.tag_id
        WHERE ct.clip_id = ?
        """,
        (clip_id,),
    ).fetchone()
    tag_str = (row["names"] or "") if row else ""
    clip = conn.execute(
        "SELECT id, title, content_text, user_note FROM clips WHERE id = ?", (clip_id,)
    ).fetchone()
    if not clip:
        return
    conn.execute("DELETE FROM clips_fts WHERE rowid = ?", (clip["id"],))
    conn.execute(
        "INSERT INTO clips_fts(rowid, title, content_text, tags, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (clip["id"], clip["title"], clip["content_text"] or "", tag_str,
         clip["user_note"] or ""),
    )


_TAG_KEY_RE = re.compile(r"[^a-z0-9]+")


def tag_key(name: str) -> str:
    """Normalised comparison key for near-duplicate tag detection:
    lowercase, every non-alphanumeric run removed. 'Web Scraping',
    'web-scraping' and 'webscraping' all share the key 'webscraping'."""
    return _TAG_KEY_RE.sub("", (name or "").lower())


def gc_orphan_tags(conn: sqlite3.Connection) -> None:
    """Delete tag rows that no clip references any more, so removed tags
    stop haunting autocomplete, the sidebar, and co-occurrence stats."""
    conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM clip_tags)"
    )


def upsert_tags(conn: sqlite3.Connection, clip_id: int, tag_names: Iterable[str]) -> None:
    """Replace the full tag set for a clip.

    New tag names that normalise to the same key as an existing tag attach
    the existing tag instead of creating a near-duplicate ('webscraping'
    typed when 'web-scraping' exists reuses 'web-scraping'). Exact
    (case-insensitive) matches always win over key matches.
    """
    conn.execute("DELETE FROM clip_tags WHERE clip_id = ?", (clip_id,))
    by_key: dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        by_key.setdefault(tag_key(r["name"]), r["id"])
    for raw in tag_names:
        name = (raw or "").strip()
        if not name:
            continue
        row = conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row:
            tag_id = row["id"]
        else:
            key = tag_key(name)
            if key and key in by_key:
                tag_id = by_key[key]
            else:
                cur = conn.execute("INSERT INTO tags(name) VALUES (?)", (name,))
                tag_id = cur.lastrowid
                if key:
                    by_key[key] = tag_id
        conn.execute(
            "INSERT OR IGNORE INTO clip_tags(clip_id, tag_id) VALUES (?, ?)",
            (clip_id, tag_id),
        )
    # Tags this clip just dropped may now be unreferenced.
    gc_orphan_tags(conn)
    refresh_fts_tags(conn, clip_id)


def _resolve_tag(conn: sqlite3.Connection, name: str):
    """Find an existing tag matching `name` - exact (case-insensitive) first,
    then by normalised key (so 'web scraping' resolves to 'web-scraping').
    Returns the sqlite3.Row, or None."""
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        return row
    key = tag_key(name)
    if not key:
        return None
    for r in conn.execute("SELECT id, name FROM tags"):
        if tag_key(r["name"]) == key:
            return r
    return None


def _clips_with_tags(conn: sqlite3.Connection, tag_ids: list[int]) -> list[int]:
    if not tag_ids:
        return []
    ph = ",".join("?" * len(tag_ids))
    return [r["clip_id"] for r in conn.execute(
        f"SELECT DISTINCT clip_id FROM clip_tags WHERE tag_id IN ({ph})", tag_ids)]


def delete_tags(conn: sqlite3.Connection, names: Iterable[str]) -> dict:
    """Remove tags entirely - unlink them from every clip and drop the tag
    rows. Returns {tags_deleted, clips_affected}."""
    ids, resolved = [], []
    for n in names:
        row = conn.execute(
            "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE", ((n or "").strip(),)
        ).fetchone()
        if row and row["id"] not in ids:
            ids.append(row["id"]); resolved.append(row["name"])
    if not ids:
        return {"tags_deleted": 0, "clips_affected": 0}
    affected = _clips_with_tags(conn, ids)
    ph = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM clip_tags WHERE tag_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM tags WHERE id IN ({ph})", ids)
    for cid in affected:
        refresh_fts_tags(conn, cid)
    return {"tags_deleted": len(ids), "clips_affected": len(affected),
            "deleted": resolved}


def merge_tags(conn: sqlite3.Connection, source_names: Iterable[str],
               target_name: str) -> dict:
    """Fold every source tag into `target_name` across all clips. The target
    is matched by exact name, else normalised key, else created. Clips that
    carried a source tag end up with the target instead (deduped). Returns
    {target, clips_affected, sources_merged}."""
    target_name = (target_name or "").strip()
    if not target_name:
        raise ValueError("target tag name required")
    target = _resolve_tag(conn, target_name)
    if target:
        target_id, target_name = target["id"], target["name"]
    else:
        target_id = conn.execute(
            "INSERT INTO tags(name) VALUES (?)", (target_name,)).lastrowid

    src_ids = []
    for n in source_names:
        row = conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", ((n or "").strip(),)
        ).fetchone()
        if row and row["id"] != target_id and row["id"] not in src_ids:
            src_ids.append(row["id"])
    if not src_ids:
        return {"target": target_name, "clips_affected": 0, "sources_merged": 0}

    affected = _clips_with_tags(conn, src_ids)
    ph = ",".join("?" * len(src_ids))
    # Re-point links to the target (OR IGNORE dedupes clips that had both).
    conn.execute(
        f"INSERT OR IGNORE INTO clip_tags(clip_id, tag_id) "
        f"SELECT clip_id, ? FROM clip_tags WHERE tag_id IN ({ph})",
        [target_id] + src_ids)
    conn.execute(f"DELETE FROM clip_tags WHERE tag_id IN ({ph})", src_ids)
    conn.execute(f"DELETE FROM tags WHERE id IN ({ph})", src_ids)
    for cid in affected:
        refresh_fts_tags(conn, cid)
    return {"target": target_name, "clips_affected": len(affected),
            "sources_merged": len(src_ids)}


def rename_tag(conn: sqlite3.Connection, old_name: str, new_name: str) -> dict:
    """Rename a tag across all clips. If the new name resolves to a different
    existing tag, this becomes a merge into it. A new name that only differs
    cosmetically (case/punctuation) from the source is applied in place.
    Returns {status: 'renamed'|'merged'|'source_missing', clips_affected, ...}."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("new tag name required")
    src = conn.execute(
        "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE", ((old_name or "").strip(),)
    ).fetchone()
    if not src:
        return {"status": "source_missing", "clips_affected": 0}
    target = _resolve_tag(conn, new_name)
    if target and target["id"] != src["id"]:
        res = merge_tags(conn, [src["name"]], target["name"])
        res["status"] = "merged"
        return res
    # In-place rename: a brand-new literal name, or a cosmetic variant of the
    # source itself (the UNIQUE constraint can't trip - no other tag matches).
    affected = _clips_with_tags(conn, [src["id"]])
    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, src["id"]))
    for cid in affected:
        refresh_fts_tags(conn, cid)
    return {"status": "renamed", "clips_affected": len(affected), "target": new_name}


def find_duplicate(conn: sqlite3.Connection, content_hash: str | None = None,
                   primary_sha: str | None = None) -> dict | None:
    """Return an existing clip that duplicates the incoming one, or None.

    A duplicate is: same content_hash, OR a clip already carrying the same
    primary-PDF asset (by sha256). Returns a dict with id/title/captured_at
    and which signal matched, so callers can report it to the user.
    Shared by the HTTP ingest paths (app.py) and the migrator.
    """
    if content_hash:
        row = conn.execute(
            "SELECT id, title, captured_at FROM clips WHERE content_hash = ? "
            "ORDER BY id LIMIT 1", (content_hash,)
        ).fetchone()
        if row:
            return {"id": row["id"], "title": row["title"],
                    "captured_at": row["captured_at"], "matched_on": "content_hash"}
    if primary_sha:
        row = conn.execute(
            "SELECT c.id, c.title, c.captured_at FROM clips c "
            "JOIN clip_assets ca ON ca.clip_id = c.id "
            "WHERE ca.asset_sha = ? AND ca.role = 'pdf-primary' "
            "ORDER BY c.id LIMIT 1", (primary_sha,)
        ).fetchone()
        if row:
            return {"id": row["id"], "title": row["title"],
                    "captured_at": row["captured_at"], "matched_on": "pdf-primary"}
    return None


# ---------------------------------------------------------------------------
# Suggestion blocklist - keywords the user never wants proposed as tags.
# ---------------------------------------------------------------------------

def blocked_keyword_keys(conn: sqlite3.Connection) -> set[str]:
    """The set of normalised keys currently on the suggestion blocklist.

    Used by the suggester to drop matching candidates. Returns an empty set if
    the table is absent (an older DB not yet migrated) so suggestions degrade
    to 'nothing blocked' rather than erroring."""
    try:
        return {r["norm_key"] for r in
                conn.execute("SELECT norm_key FROM suggestion_blocklist")}
    except sqlite3.OperationalError:
        return set()


def list_blocked_keywords(conn: sqlite3.Connection) -> list[dict]:
    """All blocklist entries, newest first, as {keyword, created_at} dicts."""
    try:
        return [{"keyword": r["keyword"], "created_at": r["created_at"]}
                for r in conn.execute(
                    "SELECT keyword, created_at FROM suggestion_blocklist "
                    "ORDER BY created_at DESC, keyword COLLATE NOCASE")]
    except sqlite3.OperationalError:
        return []


def add_blocked_keyword(conn: sqlite3.Connection, keyword: str) -> Optional[str]:
    """Add a keyword to the suggestion blocklist (idempotent by normalised
    key). Returns the stored keyword, or None if it was blank / unkeyable.
    A later spelling does not overwrite the first one kept for display."""
    keyword = (keyword or "").strip()
    key = tag_key(keyword)
    if not keyword or not key:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO suggestion_blocklist(keyword, norm_key, created_at) "
        "VALUES (?, ?, ?)", (keyword, key, now_ts()))
    return keyword


def remove_blocked_keyword(conn: sqlite3.Connection, keyword: str) -> int:
    """Remove a keyword from the blocklist by its normalised key. Returns the
    number of rows deleted (0 if it wasn't blocked)."""
    key = tag_key(keyword)
    if not key:
        return 0
    return conn.execute(
        "DELETE FROM suggestion_blocklist WHERE norm_key = ?", (key,)).rowcount


# ---------------------------------------------------------------------------
# App settings (key/value).
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str,
                default: Optional[str] = None) -> Optional[str]:
    """Read a setting value, or `default` if unset / table absent."""
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a setting value (stored as text)."""
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)))


def meta_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return json.loads(row["meta_json"] or "{}").get(key, default)
    except (json.JSONDecodeError, TypeError):
        return default


if __name__ == "__main__":
    # Run this file directly to (re)create the schema.
    init_db()
    print(f"Initialized {get_db_path()}")
