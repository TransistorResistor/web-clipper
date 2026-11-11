# Local Clipper (SQLite edition)

Personal knowledge-base / web clipper. Browser userscript captures pages,
local Flask service stores them in SQLite with full-text search, and a
built-in viewer lets you browse, search, annotate, and edit.

A migrator imports your existing OneNote notebooks.

## Layout

```
service/           Flask app, schema, viewer templates
  app.py             HTTP API + viewer routes
  db.py              Schema, connection, FTS5 helpers
  search.py          Advanced search engine (FTS, windows, dedup, report)
  query.py           Search query-language parser
  ingest.py          PDF / MHTML parsing + server-side metadata extraction
  suggest.py         Offline tag suggestion (FTS similarity + co-occurrence)
  embed.py           Optional semantic ranker (bundled embedding weights)
  models/            Bundled embedding model weights (offline; see models/README.md)
  templates/
    index.html       List/search page
    detail.html      Single-clip page: annotations, metadata, tag widget
    search.html      Advanced search page
    upload.html      Drag-and-drop PDF/MHTML upload with preview
  requirements.txt
  test_query.py      Query-parser unit tests
  test_search.py     Search-engine tests
  test_dedupe_bulk.py Dedupe-on-ingest + bulk-operation tests
  test_fixes.py      Regression tests (cache persistence, sanitization, FTS fallback, …)
  test_suggest.py    Tag-suggestion ranking + tag hygiene tests
  test_tags_manage.py Tag rename/merge/delete (vocabulary management) tests
  static/tags.js     Shared chip-style tag input widget (autocomplete + suggestions)
userscript/
  clipper.user.js    Tampermonkey/Violentmonkey script
migrator/
  migrate.py         OneNote → SQLite import (com or folder mode)
  test_migrate.py    End-to-end self-test
tools/
  b64_docx.py        Pack/unpack a file as base64 inside a .docx (encode/decode/verify)
```

## Quick start

```bash
cd service
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python app.py                                       # listens on http://127.0.0.1:5577
```

Then install `userscript/clipper.user.js` in Tampermonkey/Violentmonkey.
A small 📎 button appears on every page; click it, optionally enter tags,
hit Save. The "library" link (or `http://127.0.0.1:5577/`) opens the
viewer.

## What's stored

One SQLite file (`clipper.db` next to the service by default; override
with `CLIPPER_DB`). The schema:

- `clips` – id, url, canonical_url, title, captured_at, source,
  content_html, content_text (for FTS), content_hash, word_count,
  reading_minutes, language, meta_json (open-ended), user_note (a
  free-form comment you attach when clipping — see below), embedding
  (reserved for future vector search via sqlite-vec)
- `assets` – images & PDFs as BLOBs, keyed by sha256 (dedup is automatic;
  clip the same site twice, you don't store the logo twice)
- `clip_assets` – many-to-many join with role (inline-image, attachment,
  pdf-primary)
- `tags`, `clip_tags` – the usual
- `annotations` – text-quote anchored, with prefix/suffix context, plus
  pdf_page+rect for future PDF annotation
- `revisions` – every edit to title/content_html writes the old version
  here first; nothing is destructive
- `clips_fts` – FTS5 virtual table indexing title, text, tag names, and the
  user note (so notes are searchable; added by an automatic migration that
  rebuilds the index on first run against an older DB)

## Retrieval

- Free-text search uses SQLite's FTS5 with BM25 ranking; queries return
  snippets with `<mark>` highlighted matches. The clip's user note is indexed
  too, so you can find a clip by what you wrote about it
- Tag filter (single tag), `has_pdf=1` filter, more easily added as SQL
  `WHERE` clauses
- Sort: by relevance when searching, by capture date otherwise

## Tag suggestion ranker

Tag suggestions blend several signals (content similarity, term→tag PMI,
co-occurrence, same-host) plus RAKE keyphrases for brand-new tags. The
"content similarity" step has two interchangeable rankers:

- **Lexical (default, always available):** FTS5/BM25 over the document's
  salient terms, gated by a salient-term overlap anchor so an off-topic upload
  doesn't borrow the library's popular tags.
- **Semantic (optional):** sentence embeddings (`embed.py`, bundled
  **gte-small** — 384-dim, ~67 MB, mean-pooling, chosen for best retrieval
  quality per MB), so similarity is by meaning rather than shared words — e.g. a
  paper on *neural translation* matches clips about *sequence-to-sequence
  models*. The on-/off-topic cosine floor (`min_embed_sim`, **0.86**) is
  calibrated to this model's distribution; swapping models means re-measuring it
  (`eval_subtopics.py --floor`). It was raised from 0.82 once the body-mention
  signal carried recall — a higher floor cuts cross-*type* bleed (an F-15 doc
  borrowing F-35/Hornet tags) at no recall cost, lifting suggestion precision
  sharply. It separates topics/types, not facets of one program (F-35 variants
  vs acquisition vs history all sit ~0.90+) — those need tag granularity. The
  floor is an adjustable **slider** (0.80–0.99) in the library *manage* panel;
  an **Auto** button (`POST /api/settings/embed-floor {autotune:true}`) picks a
  value from your library — a lightweight cosine-matrix proxy targets the
  precision *knee* (where cross-type donor bleed disappears), stays conservative
  (never loosens below the default, won't overshoot into recall it can't
  measure). The persisted value, or a per-request `min_embed_sim`, overrides the
  built-in default.

When the bundled weights (`models/`) and `torch`/`transformers` are present, the
two are **blended** (`embed_blend`, default 0.5): the embedding cosine gates
eligibility (so off-topic uploads borrow nothing) while the lexical leg adds
exact-token precision for jargon and alphanumeric codes (`NaOH`, `BRCA1`,
`F-35`). The blend is robust in both directions — embeddings rescue a lexical
mix-up (a sparse *"Boeing 747"* query that FTS confuses with *737*), and the
exact-token leg rescues an embedding conflation (a bare *"BRCA1"* that
embeddings alone pull toward the look-alike *BRCA2*). If the weights or
libraries are absent it **falls back to the lexical leg** alone. Weights load
offline only — see [service/models/README.md](service/models/README.md). Check
the live ranker via `GET /api/embed/status` or run `python demo_embed.py`.

**Variant / code disambiguation.** In a library full of look-alike tags
(`F-15` … `F-15EX`, the dozen 737 sub-variants), a tag whose *name* is one of
the document's alphanumeric designations is boosted (`code_boost`), with a
designation in the **title** counting double a body one — so the specific
variant outranks its siblings rather than being lost among them. A designation
that's digit-only or multi-word (`737 MAX`), or one the library has never seen,
isn't caught automatically; set a **`code_regex`** (per-request, or globally via
the `CLIPPER_CODE_REGEX` env var) to recognise those — matches both feed the
boost and, when not yet a tag, surface verbatim as new-tag suggestions (so a
future `F-47` is proposed as soon as it appears). See `eval_variants.py` for the
benchmark.

The boost is meant to reorder a designation *among its look-alike siblings*, not
to let a fistful of model numbers crowd out a document's topical tags. Two guards
keep it in its lane: the confidence floor is applied **per group** (code-boosted
tags and the rest each keep entries within `min_score_frac` of *their own* top),
so a large boost can't set a floor that wipes the subject tags; and the
code-boosted share of the row is **capped** (`code_frac_cap`, default 0.5 of
`limit`) when there are non-code tags waiting for the freed slots — a doc whose
only relevant tags are codes still shows them all. So a fast-jet paper surfaces
both its airframe designations *and* tags like `pilot-training`, rather than
only the model numbers.

**User note as a suggestion signal.** A comment you add when clipping (the
`note` field / `user_note` column) is folded into the suggester's `title`
input, so it's treated exactly like the page title: tripled into the
similarity/PMI text, title-weighted in the body-mention signal, and its
designations counted as title codes. The user saying *what a document is about*
is as high-signal as its title — so a one-line note steers the suggestions
toward the angle you care about. Stored notes feed re-suggestion in the viewer
too (the endpoint reads them server-side); the userscript also sends the
live-edited note as you type. You can view and edit a clip's note in the viewer
detail page — it persists with **Save changes** like tags (no revision row),
and editing it re-runs suggestions live. Notes are also full-text searchable
(see *Retrieval*) and lead the clip's entry in search exports (below).

## Advanced search + report export

A second, deeper search (web UI at `/search`, API at `/api/search`) goes
beyond the clip text to search the **contents of attached PDFs, Word docs,
and text files**, and dumps matches to a single Markdown or plain-text file.

Query language:

- Boolean `AND` `OR` `NOT` (also `&` `|` `-`), grouping with `( )`
- Phrases: `"exact phrase"`
- Unordered proximity: `a %20 b` matches when `a` is within 20 words of `b`
  in either order
- Metadata fields: `title:` `tag:` `source:` `url:`, plus date fields
  `added:` (date saved) and `published:` (from `meta.article:published_time`)
- Date specs: `>2024-01-01`, `<2025-01-01`, `2024-01-01..2025-01-01`, or an
  exact `2024-01-01`
- Bare adjacent terms combine by a configurable default (OR in the simple
  box, switchable to AND)

Example: `("machine learning" %20 model) AND tag:research NOT deprecated
published:2024-01-01..2025-12-31`

How the report is built:

- Around each keyword hit it includes a window of N words (default 30,
  configurable), clamped to the start/end of the field or document.
- Multiple hits in one field are merged into one longer block with no
  duplicated text (overlapping/adjacent windows are unioned).
- Across documents, near-duplicate blocks are suppressed: blocks are
  compared on normalized word-shingles, so minor formatting changes or
  small errata at the edges don't cause a re-include — but a materially
  longer block that genuinely extends a previous one *is* kept, so extra
  information at the start/end is never lost.
- Tables (from HTML and Word docs) are rendered as Markdown tables and
  preserved; window boundaries snap outward to keep table rows intact.
- The **user note** is searched alongside the clip text (a clip can match on
  its note alone), and when a matched clip has one it leads that clip's entry,
  prefixed `User comment:` (a blockquote in Markdown), so your own framing
  sits above the extracted context as a reminder of why you kept the clip.

Extracted attachment text is cached in an `asset_text` table so PyMuPDF
only runs once per PDF. PDF extraction needs `pymupdf` (in
requirements.txt); if it's absent, clip/HTML/docx/text search still works
and PDFs are simply skipped.

API: `GET /api/search?q=...&context=30&default_op=OR&added_from=...&
added_to=...&published_from=...&published_to=...&format=md`. Add
`&download=1` to stream the report as a file instead of JSON.

## Article extraction (userscript)

"Article" mode runs a four-step cascade to find the main content, stopping
at the first step that produces a result:

1. **Site-specific extractor** — matched by hostname. Built in for
   Wikipedia, GitHub READMEs, Stack Overflow / Stack Exchange, Medium, and
   MDN. Each can clean site clutter (e.g. Wikipedia edit links, infoboxes,
   nav boxes) and override title detection.
2. **Tier-1 selectors** — high-confidence content markers (`article`,
   `[itemprop="articleBody"]`, `.entry-content`, …). First match with
   meaningful text (≥200 chars) wins.
3. **Tier-2 selectors** — broader, fuzzier markers (`main`, `#content`,
   `.prose`, `.markdown-body`, …), tried only if no tier-1 selector matched.
4. **Generic heuristic** — the text-density scorer, as a final fallback.

Selectors in each tier are plain CSS, so you can target any kind of
marking — tag (`article`), id (`#article-body`), class (`.post-content`),
ARIA role (`[role="main"]`), or arbitrary attribute
(`[data-component="article-body"]`, `[itemprop="articleBody"]`). Edit the
`TIER1_SELECTORS` / `TIER2_SELECTORS` arrays or add entries to
`SITE_EXTRACTORS` near the top of `clipper.user.js` to tune for sites you
clip often.

Which step fired is recorded in the clip's `meta.extractor` (e.g.
`site:Wikipedia`, `tier1:.entry-content`, `tier2:#content`, `generic`) and
shown in the save toast, so you can see what matched and refine selectors.

**Hyperlinks are stripped** from every captured clip before storage: each
`<a>` is unwrapped (its text and any nested images are kept, the link
itself removed), and stray `href`/`xlink:href` attributes are dropped. The
count is reported as `meta.links_removed`.

## Blocklist (userscript)

Pages matching any block rule get **no clipper button** and refuse to clip
even via the menu command. The `BLOCK_RULES` array at the top of
`clipper.user.js` is where you customise this. The default rules cover
login pages, generic auth forms, account/auth URLs, checkout/payment
flows, and a host-block example.

A rule is an object of conditions. **All conditions in a rule must hold**
(AND within a rule). **Any rule firing blocks the page** (OR across rules).
Supported keys:

| Key | What it matches |
|---|---|
| `text` | string \| string[] \| RegExp – string(s) must appear in page HTML (AND) |
| `anyText` | string[] – at least one must appear (OR) |
| `selector` | CSS selector \| string[] – every selector must match (AND) |
| `anySelector` | string[] – at least one must match (OR) |
| `urlIncludes` | string \| string[] – substring(s) in `location.href` |
| `urlMatches` | RegExp on `location.href` |
| `host` | string (exact) \| RegExp on `location.hostname` |
| `title` | string \| RegExp on `document.title` |
| `not` | a nested rule that must NOT hold (negation) |
| `label` | optional human name shown in the console when it fires |

Example — block only when *both* the phrase "login page" appears *and* a
form with id `user-login` exists:

```js
{ label: 'Login page', text: 'login page', anySelector: ['form#user-login'] }
```

Selectors handle every marking kind: id (`#user-login`), class
(`.login-form`), tag (`form`), attribute (`[name="password"]`,
`[autocomplete="cc-number"]`), or any compound CSS. Invalid selectors are
ignored silently rather than crashing the page.

## Metadata extraction (userscript)

A separate metadata pipeline runs on every clip, independent of content
extraction. It captures structured fields and merges them into
`meta_json` on the clip.

Standard fields populated when available:

- `title` — from `og:title` or `<title>`
- `url` — canonical URL (`<link rel="canonical">`) or `og:url`, falling
  back to `location.href`
- `report_number` — typical sources include `citation_technical_report_number`,
  `citation_report_id`, `dc.identifier`, or a per-site selector
- `date_published` — `article:published_time`, `datePublished`,
  `og:published_time`, `dc.date`, or `<time datetime="…">`
- `marking` — auto-extracted marking (see below)
- `metadata_extractor` — which extractor produced the result
  (`site:arXiv`, `site:Wikipedia`, or `generic`)

The full extractor output is preserved in `meta_json`, and the viewer
detail page surfaces marking as a banner plus the key fields in a compact
table, with the raw JSON in an expandable section.

**Per-site extractors.** `SITE_METADATA_EXTRACTORS` in `clipper.user.js`
holds a list of `{ name, match(host), extract() }` entries. Each
`extract()` returns a partial object; any fields it omits are filled in
from the generic extractor. Included examples cover arXiv (parsing the
arXiv ID from the URL) and a synthetic "gov report site" (pulling a
report ID from `.report-id` and a marking banner). Edit or add
your own — the existing logic you said you'd port can drop straight in
as new entries.

**Marking selection.** The `selectMarking(candidates)` function is the
single hook your existing logic plugs into. Each extractor produces a
flat object of candidates from visible marking banners, metadata, headings,
footers and URL hints. `selectMarking` collapses them to one string (or null).
The default implementation picks the first non-empty candidate in priority
order; replace its body with your own selection logic. See the extractor
source for supported selectors and candidate keys.

For server-side normalisation, edit `normalize_marking_text(value)` in
`service/marking_rules.py`. This pass-through stub runs before the built-in
parser for ingestion, metadata edits and marking queries. Keep the return value
a string. Original markings and the existing database format are preserved.
Restart the service after changing rules; re-save existing clip metadata to
recompute its stored marking/filter values.

Marking is searchable via the advanced search language:

```
marking:internal AND report:example
```

`marking:` matches substrings of `meta.marking`; `report:` (or
`report_number:`) matches substrings of `meta.report_number`. Both are
case-insensitive.

## Clipping a stub article with its linked PDF

When you're on an HTML article that links to the underlying PDF (a press
release linking to the full report, a publication listing linking to the
paper, etc.), right-click the PDF link and a small "📎 Clip this PDF with
article context" chip appears near the cursor. Click it and the userscript:

1. Runs the regular article extractor on the current page (the "stub")
2. Fetches the linked PDF via `GM_xmlhttpRequest` — cookies for that origin
   come along, so authenticated documents work
3. Posts both as one combined clip: the article HTML as the body, the PDF
   as an attached asset

The PDF is stored as a deduped asset (sha256), attached to the clip with
role `pdf-primary` (so the viewer iframes it), and its text is indexed by
the search engine — searching for a phrase that only appears inside the
PDF body will find the combined clip. Metadata extraction runs against
the live article page, not the PDF.

The PDF link is auto-detected by:

- `.pdf` extension on the href (with or without query/fragment)
- Query params like `?format=pdf`, `?type=pdf`, `?filetype=pdf`
- `type="application/pdf"` attribute on the anchor
- `aria-label` containing "download PDF", "view PDF", or "(PDF)"

Right-clicking nested elements (an icon inside the link) works — the
detector walks up from the click target to find the enclosing `<a>`. The
chip is added in addition to your browser's normal context menu, not
instead of it.

The API endpoint `POST /api/clip` now also accepts an optional
`attachments` array of `{filename, mime, b64}` entries that get stored
the same way; the first PDF in the list becomes `pdf-primary`, the rest
are tagged as `attachment`.

## Drag-and-drop upload (PDF / MHTML)

The `/upload` page (linked from the library header) accepts a PDF or MHTML
(`.mht` / `.mhtml`) file by drag-and-drop or file picker, extracts metadata
server-side, and shows an **editable preview before ingestion**.

Flow:

1. Drop a file → `POST /api/upload/preview` parses it, extracts metadata,
   applies remembered corrections for MHTML only, and stages the bytes in memory under
   a token (30-minute TTL, lost on restart by design).
2. The preview shows editable fields (title, url, report_number,
   date_published, marking), the marking *candidates* (so you can see where
   a detected marking came from), a short text preview, and a tags box.
   For MHTML, fields pre-filled from a remembered correction are flagged.
3. Confirm → `POST /api/upload/commit` creates the clip. Any field you
   changed from the auto-extracted value is saved as a correction for MHTML only.

**MHTML handling.** The MIME-multipart archive is parsed, the root HTML
part is kept as the clip body, and embedded resource parts (images) are
re-inlined as `data:` URIs, then pulled into the deduped asset store on
commit (same pipeline as a normal clip). Metadata comes from the embedded
HTML (`og:title`/`<title>`, canonical link, `article:published_time`,
marking metadata, banners and headings, body-scan for handling markers).

**PDF handling.** Text is extracted with PyMuPDF; metadata comes from the
PDF info dict (title, author, creation date, page count) plus a scan of
the document head for a handling marker and a "Report No." style label.
The PDF is stored as a `pdf-primary` attachment, so it renders in the
viewer and its text is searchable.

**Authored keywords & abstract.** A PDF's own `Keywords` are read and pre-applied
as tags (removable before commit). The legacy `/Info` `/Keywords` field is read
first, falling back to the **XMP packet** (`pdf:Keywords` / `dc:subject`) where
modern producers (InDesign, Acrobat, most publishers) actually write them — so
XMP-only keywords aren't silently lost. Separately, the PDF's `Subject` (its
authored abstract, when present) is **folded into the tag-suggestion context**:
it's dense, on-topic prose that often lives only in the metadata or deep in the
body, past the suggestion window, and gives the keyphrase / term→tag signals the
document's own summary of what it's about.

**Remembered corrections.** MHTML corrections are stored in
`metadata_corrections` by source host. PDF metadata edits apply only to that
PDF: PDF uploads neither read nor write remembered corrections. Existing table
rows remain compatible with older database files.

## Tag autocomplete & suggestions (offline)

Tag inputs in the upload preview, the viewer detail page, and the
userscript clip panel share one chip-style widget (templates load
`/static/tags.js`; the userscript carries a mirrored copy since it can't
inject service scripts into arbitrary pages). Everything is fully offline
and dependency-free.

**Chip input.** Accepted tags render as removable chips; Enter or comma
commits the text being typed, Backspace on an empty input removes the last
chip, pasting `a, b, c` adds all three, and leftover typed text is
committed on blur so clicking Save never silently drops it.

**Autocomplete.** A dropdown filters the existing tags (from `/api/tags`,
fetched once) — prefix matches rank first, then usage count — with
arrow-key navigation, Tab/Enter/click to accept. The same as-you-type
autocomplete is also wired to the library page's tag-management inputs (the
"merge selected into…" box and the bulk add/remove-tags field) via
`ClipperTags.attachAutocomplete()`, so completing existing tag names works
everywhere you type one, not just in the chip widget.

**Near-duplicate prevention.** On save, a new tag name that normalises to
the same key as an existing tag (lowercase, non-alphanumerics stripped:
`webscraping` ≈ `web-scraping` ≈ `Web Scraping`) attaches the existing tag
instead of creating a duplicate. Tags that lose their last clip are
garbage-collected, so removed tags don't haunt autocomplete.

**Suggestions.** Clickable chips above the box propose likely-relevant
existing tags, from three blended signals computed server-side by
`suggest.py`:

- *Content similarity* — the document's distinctive terms (TF-IDF against
  the FTS index, so common words don't dominate; title terms weighted
  extra) retrieve similar clips via FTS5/BM25, whose tags are borrowed
  weighted by similarity. Answers "documents like this were tagged X".
- *Co-occurrence* — for any tags already chosen, tags that frequently
  appear alongside them across the library are boosted.
- *Body mention* — existing tags whose **name literally recurs** in the
  document (title counted extra) are up-weighted, and the top few are
  guaranteed past the confidence floor. This is doc-intrinsic, so it works
  where the borrow-from-neighbours signals are silent (a topic desert) and,
  crucially, **inside a tight cluster the embedding can't separate** — a
  paper that names `F-35`, `Hornet` and `HQ-9` gets exactly those, not a
  blur of the whole defense neighbourhood. Whole-word/phrase matched, so
  `NATO` doesn't fire on "senator". Tuned by `mention_weight`.

Every candidate is damped by its global usage count, so bulk tags
(migration tags, folder tags) stop drowning out specific ones, and a
confidence floor trims the weak tail — an empty chip row means "nothing
confident", not eight noisy guesses. Each chip shows why it was suggested:
`≈` similar, `◎` predicted, `↔` co-occurring, `¶` in-text
(body mention), `#` designation, `★` multiple signals, `＋` new keyword. The
userscript panel shows this key as a legend under the chips.
Suggestion fetches are debounced and skipped when nothing changed.

API: `POST /api/tags/suggest` with `{ text, title, chosen,
exclude_clip_id, limit }` → `{ suggestions: [{tag, score, why}, ...] }`.
All inputs optional. When `exclude_clip_id` is given (the detail page),
the document context is assembled server-side from the stored clip —
including cached attachment text, so PDF clips get real suggestions. Query
construction quotes terms so arbitrary document text (including FTS operator
characters) is safe.

The similarity ranker is isolated in `suggest.similar_clips_by_content()`
so a future semantic-embedding ranker can be swapped or blended in without
touching the endpoint or UI (see "What's deliberately not built yet").

In the userscript, the clip panel starts with an **empty** tag input —
the previous clip's tags are offered as one-click "Recent" chips rather
than pre-filled, so they can't silently bleed onto unrelated pages (the
right-click PDF flow attaches no automatic tags for the same reason). The
suggestion context is the page title plus a sample of the visible body
text. All calls go to the local
service over `127.0.0.1`, so this works offline like everything else.

## Duplicate detection & bulk operations

**Dedupe on ingest.** Every ingest path checks for an existing duplicate
before creating a clip. A duplicate is: the same `content_hash` (the
sha256 of the extracted text — or the file bytes for a PDF), OR a clip
that already carries the same primary-PDF asset by sha256. The check is
shared across all paths via `find_duplicate()`:

- `/api/clip` — checks content hash and, if an attachment is a PDF, that
  PDF's sha. Returns `{ok:false, duplicate:{id,title,captured_at,matched_on}}`.
- `/api/attach-pdf` — checks the PDF sha.
- `/api/upload/preview` — reports a `duplicate` field so the UI can flag it.
- `/api/upload/commit` — skips with `{ok:false, skipped:true, duplicate}`.

In every case the duplicate is *skipped with feedback* (which existing
clip it matched, and on what). Passing `force:true` (JSON) or `force=1`
(form) overrides and ingests anyway. The userscript shows a confirm
dialog naming the existing clip and offers to clip again; the upload page
shows a per-file banner with an "ingest anyway" checkbox.

**Bulk upload.** The upload page accepts multiple files at once (drag a
selection, or pick several). Each gets its own preview card with its own
editable metadata, duplicate banner, and Ingest button. A batch bar adds
"Ingest all non-duplicates" (duplicates are left for you to opt in via
their checkbox) and "Clear".

**Bulk library operations.** The library viewer has a checkbox on each
clip. Selecting any reveals a bar with select-all/clear, add-tags,
remove-tags, and delete, all operating on the selection via
`POST /api/clips/bulk` with `{ids, op, tags}` where `op` is one of
`delete | add_tags | remove_tags | set_tags`. `add_tags` is idempotent
(won't duplicate an existing tag); delete cleans up orphaned assets.

**Tag-vocabulary management.** Separate from clip membership, you can
rename, merge, and delete tags across the whole library. In the library
sidebar, click **manage** next to "Tags": each tag gets a ✎ (rename) and ✕
(delete-from-all-clips) control, and ticking several tags reveals a "merge
selected into…" box. All three go through `POST /api/tags/bulk`:

- `{ op: "rename", from: "old", to: "new" }` — rename everywhere. If the
  new name resolves to a *different* existing tag (by exact name or
  normalised key), it becomes a merge into that tag; a name that only
  differs cosmetically (case/punctuation) from the source is applied in
  place. Returns `status: renamed | merged | source_missing`.
- `{ op: "merge", names: ["a","b"], into: "target" }` — fold several tags
  into one. The target is matched by name, else key, else created; clips
  that carried a source tag end up with the target (deduplicated).
- `{ op: "delete", names: ["a","b"] }` — unlink the tags from every clip
  and drop the tag rows.

Every operation refreshes the FTS index for affected clips (so tag-name
searches stay correct) and leaves no orphan tag rows behind. Because
near-duplicate names already collapse on ingest, merge/rename mostly exist
to tidy vocabulary that predates that rule or to consolidate genuinely
different spellings (`ml` → `machine-learning`).

**Tag cleanup suggestions.** The manage panel also surfaces a short, *high-
precision* list of cleanup candidates (`GET /api/tags/cleanup`, logic in
`tagcleanup.py`). Two conservative signals only — deliberately, because naive
ones are useless on a sparse, code-heavy library (pure clip-subset flags
`Chickens`⊆`Bamboo`; edit-distance flags `F-35`~`F-15`; most tags are
singletons):

- **Merge candidates** — a tag name that is a leading token-prefix of another
  (`F-35` → `F-35 Program`, `ea-18g` → `ea-18g growler`), i.e. one reads as a
  sub-tag of the other; clip-set containment (every child clip also carries the
  parent) upgrades the confidence. One-click **Merge** runs the bulk merge above.
- **Format outliers** — a tag whose designation token is *all lowercase*
  (`f15d`, `ea-18g`): unambiguously malformed. Mixed-case (`MiG-29`,
  `F/A-18E/F`) is left alone — often the correct spelling. One-click **Rename**
  applies the uppercased form (a cosmetic in-place rename).

Each candidate has a **Dismiss** (persisted, so it won't reappear). Acting on a
candidate via merge/rename makes it drop out of the list on its own. Nothing is
ever auto-applied; merges rewrite `clip_tags`, so they always need a click.

## Packing the project as base64-in-a-docx

`tools/b64_docx.py` packs any file into a Word document as a base64 text
block (and unpacks it again). This is how `clipper.zip` gets shipped inside
a `.docx`. It only needs `python-docx`.

```bash
# Pack
python3 tools/b64_docx.py encode clipper.zip clipper-zip-b64.docx

# Unpack back to the original bytes
python3 tools/b64_docx.py decode clipper-zip-b64.docx recovered.zip

# Confirm a docx round-trips against the original (no output written)
python3 tools/b64_docx.py verify clipper.zip clipper-zip-b64.docx
```

The base64 is written in 8pt Consolas, wrapped to 100 chars per line,
between `----- BEGIN BASE64 -----` / `----- END BASE64 -----` markers, with
a header that records the original size and sha256. `decode` checks the
recovered bytes against that embedded sha256 automatically. `--width` and
`--size` customise the wrapping and font size; `decode --from-text` recovers
from a pasted plain-text block instead of a `.docx`. To recover by hand,
copy the lines between the markers into `pasted.txt` and run the one-liner
printed at the top of the document.

## OneNote migration

Two modes:

```bash
# Folder mode: works anywhere. You manually export OneNote pages as .docx
# first (in OneNote: File → Export → Word Document, for each section), then:
python migrator/migrate.py folder /path/to/exported_pages --tag from-onenote

# COM mode: Windows + OneNote 2016 only. Drives OneNote to export every
# open notebook to a temp dir, then ingests.
python migrator/migrate.py com --tag from-onenote
```

Re-running the migrator is safe: files whose content (or PDF bytes) match an
existing clip are skipped and reported. Pass `--force` to ingest them anyway.

What survives the round-trip:

- Page titles (from Title/Heading1 styles)
- Body text with paragraph and heading structure
- Inline images (deduped via sha256, served from /asset/<sha>)
- Embedded OLE attachments (stored as `attachment` role)
- Folder structure becomes tags automatically (so OneNote sections are
  searchable as tag filters)

What's lost or imperfect:

- OneNote's freeform 2D layout (text boxes positioned absolutely) becomes
  linear paragraphs. This is the cost of having ingestible content.
- Drawing/ink strokes don't translate.
- Audio recordings would need a separate pass; we ignore them currently.

## Annotations

In the viewer, select any text within a clip's content area and click
"Annotate selection". Anchors use the W3C Web Annotation TextQuoteSelector
pattern (quote + prefix + suffix), so annotations survive minor edits to
the source HTML. PDFs render in an iframe; PDF-specific annotations would
need PDF.js integration as a follow-up.

## Edit history

Editing a clip's title or HTML writes the previous version into
`revisions`. The PATCH endpoint optionally takes a `revision_note`.

## What's deliberately not built yet

- Semantic **search**. Semantic tag *suggestions* are built (the blended
  embedding ranker above, `embed.py` + `suggest.similar_clips_by_embedding()`,
  with per-clip vectors cached in `clip_embeddings`). Wiring the same backend
  into the search path is the remaining piece — a hybrid that fuses BM25 with
  embedding recall (e.g. Reciprocal Rank Fusion) so the query DSL, snippets, and
  exact-match precision survive while semantic recall is added on top. Likely
  also wants chunked (per-passage) embeddings for deep-content recall and, for a
  larger library, an ANN index instead of brute-force NumPy cosine.
- A live-editing UI for `content_html`. The PATCH endpoint accepts it but
  the viewer doesn't surface a rich editor yet.
- PDF annotation UI (PDF.js integration).
- Multi-user / auth. This is single-user, bound to localhost.

## Security posture

Single-user, localhost-only, but hardened against drive-by abuse from other
pages open in the same browser:

- **No CORS.** The userscript talks to the service through
  `GM_xmlhttpRequest` (exempt from CORS) and the viewer is same-origin, so
  cross-origin requests from arbitrary websites stay blocked by the
  browser's default policy.
- **HTML sanitization on ingest.** `<script>`-bearing tags, inline `on*`
  event handlers, and `javascript:` URLs are stripped from clip HTML before
  storage (defense in depth — the viewer renders stored HTML directly).
- **CSP on viewer pages** restricts loads to same-origin (+ `data:` images),
  so stored clips can't phone home to the original site.
- **Asset serving allowlist.** Only images, PDFs, and plain text are served
  inline from `/asset/<sha>`; anything else downloads as an opaque blob, and
  SVG gets a no-script CSP.

## Endpoints (cheat sheet)

| Method | Path | Purpose |
|--------|------|---------|
| POST   | /api/clip                          | Create clip from JSON (optional `attachments`, `note`) |
| POST   | /api/attach-pdf                    | Create clip from multipart PDF (optional `note`) |
| GET    | /api/clips?q=&tag=&has_pdf=&limit= | List/search clips |
| GET    | /api/clips/<id>                    | Clip detail (incl. `user_note`) |
| PATCH  | /api/clips/<id>                    | Update title/content/tags/`note` (content edits write a revision; tag/note edits don't) |
| DELETE | /api/clips/<id>                    | Delete clip + orphaned assets |
| POST   | /api/clips/bulk                    | Bulk delete / add_tags / remove_tags / set_tags |
| GET    | /api/tags                          | All tags with counts |
| POST   | /api/tags/bulk                     | Rename / merge / delete a tag across all clips |
| GET/POST | /api/tags/cleanup                | Cleanup candidates (merge sub-tags, fix lowercase codes); `{dismiss:sig}` to hide one |
| POST   | /api/tags/suggest                  | Ranked tag suggestions (offline) |
| GET/POST | /api/settings/embed-floor        | Get/set the suggestion topic-strictness floor (`min_embed_sim`); `{autotune:true}` to auto-pick |
| GET    | /api/search?q=&context=&format=    | Advanced search; `&download=1` for a file |
| POST   | /api/upload/preview                | Parse PDF/MHTML, return editable metadata preview |
| POST   | /api/upload/commit                 | Commit a previewed upload; records corrections |
| GET    | /api/clips/<id>/annotations        | List annotations |
| POST   | /api/clips/<id>/annotations        | Add annotation |
| DELETE | /api/annotations/<id>              | Remove annotation |
| GET    | /asset/<sha>                       | Stream an asset |
| GET    | /                                  | Viewer list page |
| GET    | /clip/<id>                         | Viewer detail page |
| GET    | /search                            | Advanced search page |
| GET    | /upload                            | Drag-and-drop upload page |


## Licence

Web Clipper's original source code is free and open source software under the
[MIT License](LICENSE). Third-party components retain their own licences;
see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
