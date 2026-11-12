"""Tests for normalize.py - date + marking canonicalisation.

Run: python test_normalize.py   (or: pytest test_normalize.py)
"""

import json

import normalize as N


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def test_iso_full():
    assert N.normalize_date("2026-03-14") == {"iso": "2026-03-14", "precision": "day"}


def test_iso_with_time_is_trimmed():
    assert N.normalize_date("2026-03-14T09:30:00Z")["iso"] == "2026-03-14"
    assert N.normalize_date("2026-03-14T09:30:00+01:00")["precision"] == "day"


def test_iso_month_and_year_precision():
    assert N.normalize_date("2026-03") == {"iso": "2026-03-01", "precision": "month"}
    assert N.normalize_date("2026") == {"iso": "2026-01-01", "precision": "year"}


def test_pdf_creation_date():
    assert N.normalize_date("D:20260314093000+00'00'") == {"iso": "2026-03-14", "precision": "day"}
    assert N.normalize_date("D:202603")["precision"] == "month"
    assert N.normalize_date("D:2026")["precision"] == "year"


def test_numeric_day_first_default():
    # 03/04/2026 -> 3 April (day-first).
    assert N.normalize_date("03/04/2026") == {"iso": "2026-04-03", "precision": "day"}


def test_numeric_month_first_opt_in():
    assert N.normalize_date("03/04/2026", day_first=False)["iso"] == "2026-03-04"


def test_numeric_swap_when_impossible():
    # 25/12/2026 can't be month-first, so day-first is forced regardless.
    assert N.normalize_date("25/12/2026", day_first=False)["iso"] == "2026-12-25"


def test_two_digit_year():
    assert N.normalize_date("14/03/26")["iso"] == "2026-03-14"


def test_year_first_numeric():
    assert N.normalize_date("2026/03/14")["iso"] == "2026-03-14"


def test_textual_dates():
    assert N.normalize_date("14 March 2026")["iso"] == "2026-03-14"
    assert N.normalize_date("14 Mar 2026")["iso"] == "2026-03-14"
    assert N.normalize_date("March 14, 2026")["iso"] == "2026-03-14"
    assert N.normalize_date("Mar 14 2026")["iso"] == "2026-03-14"
    assert N.normalize_date("4th July 2026")["iso"] == "2026-07-04"


def test_textual_month_year():
    assert N.normalize_date("March 2026") == {"iso": "2026-03-01", "precision": "month"}


def test_datetime_strings():
    # ISO with assorted time/zone tails.
    assert N.normalize_date("2026-03-14 09:30:00")["iso"] == "2026-03-14"
    assert N.normalize_date("2026-03-14T09:30:00.123456Z")["iso"] == "2026-03-14"
    # Numeric and textual dates carrying a time-of-day.
    assert N.normalize_date("03/04/2026 09:30")["iso"] == "2026-04-03"
    assert N.normalize_date("2026/03/14 23:59:59")["iso"] == "2026-03-14"
    assert N.normalize_date("14 March 2026 09:30")["iso"] == "2026-03-14"
    assert N.normalize_date("Mar 14 2026 9:30 AM")["iso"] == "2026-03-14"
    # RFC-2822 / email-header form, with and without the weekday.
    assert N.normalize_date("Mon, 14 Mar 2026 09:30:00 +0000")["iso"] == "2026-03-14"
    assert N.normalize_date("Monday, 14 March 2026 09:30:00 GMT")["iso"] == "2026-03-14"
    # A trailing time must not loosen the bare-year guard.
    assert N.normalize_date("2026 wombats") is None


def test_invalid_and_empty():
    assert N.normalize_date("") is None
    assert N.normalize_date(None) is None
    assert N.normalize_date("not a date") is None
    assert N.normalize_date("2026-02-31") is None      # 31 Feb -> rejected
    assert N.normalize_date("32/01/2026") is None


# ---------------------------------------------------------------------------
# Markings
# ---------------------------------------------------------------------------

def test_simple_level():
    m = N.normalize_marking("OFFICIAL")
    assert m["classification"] == "OFFICIAL"
    assert m["caveats"] == []
    assert m["normalized"] == "OFFICIAL"


def test_official_sensitive_not_split_on_colon():
    # The colon here is part of the marking, not a descriptor label.
    m = N.normalize_marking("OFFICIAL: SENSITIVE")
    assert m["classification"] == "OFFICIAL-SENSITIVE"
    assert m["label"] == ""


def test_label_is_stripped():
    m = N.normalize_marking("Privileged data: LEGAL SENSITIVE NO CONTRACTORS")
    assert m["label"] == "Privileged data"
    assert m["classification"] is None          # no level present
    assert m["caveats"] == ["LEGAL SENSITIVE", "NO CONTRACTORS"]
    assert m["normalized"] == "LEGAL SENSITIVE // NO CONTRACTORS"


def test_abbreviations_expand():
    m = N.normalize_marking("LS NC")
    assert m["caveats"] == ["LEGAL SENSITIVE", "NO CONTRACTORS"]


def test_mixed_long_and_abbrev_equivalent():
    a = N.normalize_marking("LEGAL SENSITIVE NC")
    b = N.normalize_marking("LS NO CONTRACTORS")
    assert a["caveats"] == b["caveats"] == ["LEGAL SENSITIVE", "NO CONTRACTORS"]


def test_level_plus_caveats():
    m = N.normalize_marking("SECRET // NOFORN")
    assert m["classification"] == "SECRET"
    assert m["caveats"] == ["NOFORN"]


def test_unknown_components_preserved():
    m = N.normalize_marking("OFFICIAL FROBNICATE WIDGET")
    assert m["classification"] == "OFFICIAL"
    assert m["unknown"] == ["FROBNICATE WIDGET"]


def test_caveats_deduped_and_ordered():
    m = N.normalize_marking("NO CONTRACTORS LEGAL SENSITIVE NC")
    assert m["caveats"] == ["NO CONTRACTORS", "LEGAL SENSITIVE"]


def test_most_sensitive_level_wins():
    m = N.normalize_marking("OFFICIAL SECRET")
    assert m["classification"] == "SECRET"


def test_empty_marking():
    m = N.normalize_marking("")
    assert m == {"raw": "", "label": "", "classification": None,
                 "caveats": [], "unknown": [], "normalized": ""}


def test_colon_inside_abbreviation_not_stripped():
    # The colon in "O:S" is part of the OFFICIAL-SENSITIVE abbreviation, not a
    # descriptor-label separator - it must not be eaten as a label.
    m = N.normalize_marking("O:S NC-LS")
    assert m["label"] == ""
    assert m["classification"] == "OFFICIAL-SENSITIVE"
    assert m["caveats"] == ["NO CONTRACTORS", "LEGAL SENSITIVE"]
    assert m["unknown"] == []


def test_separator_joined_abbreviations():
    for s in ("O-S/NC/LS", "OS NC LS", "O:S NC LS"):
        m = N.normalize_marking(s)
        assert m["classification"] == "OFFICIAL-SENSITIVE"
        assert set(m["caveats"]) == {"NO CONTRACTORS", "LEGAL SENSITIVE"}


import pytest


@pytest.fixture
def curated_rules(monkeypatch):
    # Test the legacy equivalence API without requiring bundled rule files.
    rules = [{"match": ["O:S NC-LS", "OS NCLS"],
              "classification": "OFFICIAL-SENSITIVE",
              "caveats": ["NO CONTRACTORS", "LEGAL SENSITIVE"]}]
    monkeypatch.setattr(N, "_EQUIVALENTS", N._build_equivalents(rules))


def test_equivalence_rule_overrides_parse(curated_rules):
    # "OS NCLS" can't be decomposed token-by-token (NCLS isn't an alias); it
    # resolves only via the explicit equivalence fixture.
    m = N.normalize_marking("OS NCLS")
    assert m["classification"] == "OFFICIAL-SENSITIVE"
    assert m["caveats"] == ["NO CONTRACTORS", "LEGAL SENSITIVE"]
    assert m["unknown"] == []


def test_equivalence_rule_is_case_and_punctuation_insensitive(curated_rules):
    # The same rule matches regardless of case / separator spelling.
    for s in ("O:S NC-LS", "o:s  nc-ls", "OS NCLS", "os/ncls"):
        m = N.normalize_marking(s)
        assert m["classification"] == "OFFICIAL-SENSITIVE", s
        assert m["caveats"] == ["NO CONTRACTORS", "LEGAL SENSITIVE"], s


def test_equivalence_after_label_strip(curated_rules):
    # A descriptor label is split off and the remainder hits the rule.
    m = N.normalize_marking("Privileged data: OS NCLS")
    assert m["label"] == "Privileged data"
    assert m["classification"] == "OFFICIAL-SENSITIVE"
    assert m["caveats"] == ["NO CONTRACTORS", "LEGAL SENSITIVE"]


def test_unknown_glued_string_is_not_force_split():
    # Without an equivalents rule, an unseparated run that isn't an alias stays
    # unknown (no speculative segmentation).
    m = N.normalize_marking("NCLS")
    assert m["caveats"] == [] and m["classification"] is None
    assert m["unknown"] == ["NCLS"]


def test_reload_from_custom_file():
    import json, os, tempfile
    data = {
        "caveats": {"PROJECT ZEBRA": ["PROJECT ZEBRA", "PZ"]},
        "equivalents": [{"match": ["FOO BAR", "FB"], "classification": "SECRET",
                         "caveats": ["PROJECT ZEBRA"]}],
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        N.load_markings(path)
        m = N.normalize_marking("FB")                 # equivalence rule
        assert m["classification"] == "SECRET"
        assert m["caveats"] == ["PROJECT ZEBRA"]
        assert N.normalize_marking("PZ")["caveats"] == ["PROJECT ZEBRA"]  # custom alias
    finally:
        os.remove(path)
        N.load_markings()                             # restore built-in defaults


def test_non_label_colon_not_stripped():
    # "Note:" precedes no recognised marking, so it isn't treated as a label.
    m = N.normalize_marking("Note: see appendix")
    assert m["label"] == ""
    assert m["classification"] is None
    assert m["caveats"] == []


# ---------------------------------------------------------------------------
# Integration: apply_normalization + columns_from_meta
# ---------------------------------------------------------------------------

def test_apply_and_columns():
    meta = {"date_published": "14 March 2026",
            "marking": "Privileged data: LS NC"}
    N.apply_normalization(meta)
    assert meta["date_norm"]["iso"] == "2026-03-14"
    assert meta["marking_norm"]["caveats"] == ["LEGAL SENSITIVE", "NO CONTRACTORS"]

    cols = N.columns_from_meta(meta)
    assert cols["date_published_norm"] == "2026-03-14"
    assert cols["date_precision"] == "day"
    assert cols["marking_class"] is None
    assert json.loads(cols["marking_caveats"]) == ["LEGAL SENSITIVE", "NO CONTRACTORS"]


def test_columns_from_pdf_creation_date():
    meta = {"date_published": "D:20260314093000+00'00'", "marking": "OFFICIAL"}
    cols = N.columns_from_meta(meta)          # normalises lazily
    assert cols["date_published_norm"] == "2026-03-14"
    assert cols["marking_class"] == "OFFICIAL"
    assert cols["marking_caveats"] is None     # empty -> NULL


def test_columns_empty_when_nothing():
    cols = N.columns_from_meta({"title": "x"})
    assert cols == {"date_published_norm": None, "date_precision": None,
                    "marking_class": None, "marking_caveats": None}


if __name__ == "__main__":
    import traceback
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print("  ok  -", name)
            except Exception:
                failed += 1
                print("  FAIL-", name)
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


def test_custom_marking_rules_preserve_source(monkeypatch):
    import marking_rules
    monkeypatch.setattr(marking_rules, "normalize_marking_text",
                        lambda value: "OFFICIAL" if value == "MY LOCAL MARKING" else value)
    meta = {"marking": "MY LOCAL MARKING"}
    N.apply_normalization(meta)
    assert meta["marking"] == "MY LOCAL MARKING"
    assert meta["marking_norm"]["raw"] == "MY LOCAL MARKING"
    assert meta["marking_norm"]["classification"] == "OFFICIAL"
    assert N.columns_from_meta(meta)["marking_class"] == "OFFICIAL"
    assert N.normalize_marking("MY LOCAL MARKING")["normalized"] == "OFFICIAL"
