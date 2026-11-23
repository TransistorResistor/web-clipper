"""Edit this file to add your own marking normalisation rules.

Called by normalize.normalize_marking for ingestion, metadata edits and
marking search queries, before the built-in parser.
No database access or schema changes are needed here.
"""


def normalize_marking_text(value: str) -> str:
    """Return the text the built-in marking parser should interpret.

    Stub: preserves the input unchanged. Replace this body with your rules.
    Always return a string; return "" for an absent marking. Prefer idempotent
    rules (running twice should give the same result). Do not call
    normalize.normalize_marking here: that would recurse into this hook.

    The original input remains in meta['marking'] and marking_norm['raw'];
    the returned text is saved as marking_norm['adjusted'] and used for UI
    display, marking search and filtering, even for custom vocabulary. It also
    feeds the legacy parser for structured components. The JSON schema then
    splits the text using its configured delimiters and canonicalises aliases
    into categories. A manual marking_adjusted override bypasses this hook
    but is still split, canonicalised and categorised. Existing rows are not rewritten automatically.
    Restart the service after changing this file, then re-save a clip's
    metadata to recompute its stored marking/filter columns with your new rules.
    """
    return value
