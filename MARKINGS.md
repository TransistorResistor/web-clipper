# Configuring markings

The active configuration is `service/markings.json`, loaded automatically at
process startup. Set `CLIPPER_MARKINGS` to an alternative JSON file if needed.
The filename is **markings.json** (with an r). The shipped file is an example;
replace the sample projects, cost centres and clients with your own values.

## JSON format

```json
{
  "delimiters": ["|", ";"],
  "categories": {
    "projects": {
      "label": "Projects",
      "values": {
        "Project A": [],
        "Project B": [],
        "Project C": []
      }
    },
    "cost_centres": {
      "label": "Cost centres",
      "values": {"123": [], "456": [], "789": []}
    },
    "clients": {
      "label": "Clients",
      "values": {
        "Client 1": [],
        "Client 2": [],
        "Client 3": ["Client C"]
      }
    }
  }
}
```

- `delimiters` contains literal strings, not regular expressions. At least one
  nonempty string is required. Multiple-character delimiters work; longer ones
  match first. The first delimiter also joins the canonical adjusted text.
  Spaces are not delimiters unless you explicitly include one.
- Category keys are stable lowercase identifiers: letters, digits and
  underscores, starting with a letter. Labels are the human-readable UI names.
  Existing metadata/search field names are reserved; `_unmapped` is built in.
- `values` maps each canonical output string to an array of aliases. Empty
  arrays are valid. Cost centre codes must be quoted strings, preserving zeros.
- Matching uses whole tokens, ignoring case and repeated/outer whitespace.
  `Client C` maps to `Client 3`; it does not match arbitrary text containing it.
- A spelling cannot belong to two different canonical values or categories.
  Ambiguous aliases and invalid configurations fail startup with an error.
- Canonical values and aliases cannot contain configured delimiters. Canonical
  values cannot begin with `@`, which is reserved for API presence controls.
- Empty tokens are ignored. Repeated canonical values are deduplicated while
  preserving order. Unmatched tokens retain their spelling and appear under
  **Unmapped**, remaining visible and filterable.

For `project a|client C;456|unfamiliar`, the output is
`Project A|Client 3|456|unfamiliar`, with Project A in Projects, Client 3 in
Clients, 456 in Cost centres, and unfamiliar in Unmapped.

## PDF marking extraction

Edit `service/pdf_marking_rules.py` to set the initial raw marking for single
uploads, bulk uploads and PDF attachments imported from Outlook.
`extract_pdf_marking(title, text_head, filename)` receives the embedded PDF
title (or the filename if blank), the first 1,500 characters of extracted text
with case preserved, and the filename without its directory path. Scanned PDFs may provide no text.

The default hook returns an empty string. Your hook can return a value such
as `Project A|Client C` using the delimiters in your JSON file. The single-file
upload preview lets you edit the result before saving. At save time,
`marking_rules.py` and `markings.json` process the raw marking as usual.
Restart the service after editing the hook. It runs when a PDF is imported.

## Processing and manual edits

Automatic mode runs raw text through
`marking_rules.normalize_marking_text(raw)`, splits the result using the JSON
list, resolves aliases, bins tokens, and builds the adjusted display string.
The hook is optional and currently returns the input unchanged. Keep it
idempotent; do not call the normaliser recursively from inside it.

Both metadata editors expose **Raw marking** and **Adjusted marking**.
Automatic mode shows the saved adjusted result. Uncheck automatic to enter a
manual override; it bypasses the Python hook but still goes through JSON
splitting, alias resolution and category assignment. Thus the display and
filters always agree. A blank manual override intentionally means no effective
marking, even if raw text exists. Changing raw text while an override is active
does not discard the override. Recheck automatic and save to regenerate from
raw. Capture forms show the server-computed automatic result after saving.

The original raw text is stored as `meta.marking`; manual input, when present,
is stored as `meta.marking_adjusted`. Derived data lives in
`meta.marking_norm` (`adjusted`, `categories`, `tokens`, `unmapped`, `manual`).
The original database schema and legacy structured fields remain compatible.
No existing metadata keys need to be removed. Generic categories use the new
JSON structure and do not need to exist in the old built-in vocabulary.

## Applying changed rules to existing clips

Stop the app first. From `service`, use the same `CLIPPER_DB` and, if set,
`CLIPPER_MARKINGS` environment settings as the app:

```powershell
python backfill_norm.py --dry-run
python backfill_norm.py
python app.py
```

Backfill preserves raw text and manual overrides, reinterpreting both using
the current schema. It is safe to repeat. Restart all processes after changing
JSON or Python rules; there is no hot reload. Merely editing the configuration
does not update stored categories. Old clips without categories appear as
missing until backfilled or re-saved. Review a backup before large rule changes.

## Search and filters

Both the library and advanced-search pages generate category controls from the
JSON. Choose Selected values and use Ctrl/Cmd to select several values. Choices
within a category are OR; different categories are AND. Present and Missing
work for each category, including Unmapped. Filters run before the result limit
and are also applied to downloaded advanced-search reports.

Advanced search supports `marking:"Client C"`, `has:projects` and
`missing:cost_centres`. The ordinary library text box remains a text search over
titles, bodies, tags and notes; combine it with category filters for metadata.

API clients can pass a URL-encoded `marking_filters` JSON object to `/api/clips`
or `/api/search`, for example:

```json
{"projects": ["Project A", "Project B"], "clients": ["Client C"]}
```

Use `["@present"]` or `["@missing"]` for presence filters.
`GET /api/marking-schema` supplies the configured labels, canonical choices,
delimiters and observed Unmapped values. To set/reset an override, PATCH a clip
with `{"meta":{"marking_adjusted":"project a|client c"}}` or
`{"meta":{"marking_adjusted":null}}`, respectively.
