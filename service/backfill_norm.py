"""One-shot backfill of the normalised date/marking projections for clips that
were ingested before normalisation existed (see normalize.py).

New ingests are normalised automatically; this re-parses the ``date_published``
/ ``marking`` already sitting in each clip's meta_json and writes both the
structured meta_json sub-objects (``date_norm`` / ``marking_norm``) and the four
dedicated columns. Idempotent - safe to re-run.

    python backfill_norm.py             # default DB ($CLIPPER_DB or clipper.db)
    python backfill_norm.py --dry-run   # report what would change, write nothing
    python backfill_norm.py --db other.db
"""

from __future__ import annotations

import argparse
import json

from db import connect, init_db, write_norm_columns
import normalize


def backfill(conn, dry_run: bool = False) -> tuple[int, int]:
    """Re-normalise every clip. Returns (total_clips, clips_with_any_projection)."""
    rows = conn.execute("SELECT id, meta_json FROM clips").fetchall()
    with_proj = 0
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except Exception:
            meta = {}
        normalize.apply_normalization(meta)
        cols = normalize.columns_from_meta(meta)
        if any(cols.values()):
            with_proj += 1
        if dry_run:
            continue
        write_norm_columns(conn, r["id"], cols, meta_json=json.dumps(meta))
    if not dry_run:
        conn.commit()
    return len(rows), with_proj


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="path to clipper.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db(args.db)
    conn = connect(args.db)
    try:
        total, proj = backfill(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "would set" if args.dry_run else "set"
    print(f"{total} clips scanned; {verb} date/marking projections on {proj}.")
