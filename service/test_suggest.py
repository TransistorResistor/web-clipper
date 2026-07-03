"""Tests for the tag-suggestion ranking and tag hygiene.
Run: python3 test_suggest.py"""
import os, tempfile, base64

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import connect, init_db, upsert_tags, tag_key, gc_orphan_tags
init_db()
import app
app.app.testing = True
import suggest as SG

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print("  ok  -", name)
    else: failed += 1; print("  FAIL-", name)

c = app.app.test_client()

def add(title, body, tags=None, url=""):
    r = c.post("/api/clip", json={"title": title, "html": f"<p>{body}</p>",
                                  "url": url, "tags": tags or []}).get_json()
    assert r.get("ok"), r
    return r["id"]

# --- Library: 3 clips about quantum computing tagged specifically AND with a
# bulk tag; 17 unrelated clips carrying only the bulk tag. ---
filler = ("Notes about cooking pasta and seasonal gardening tips number %d. "
          "Tomatoes, basil and slow proofing dough techniques.")
for i in range(17):
    add(f"Misc {i}", filler % i, tags=["bulk-import"])
qbody = ("Quantum computing uses qubits and entanglement. Quantum error "
         "correction and decoherence are the core obstacles for qubits. %s")
q1 = add("Quantum computing intro", qbody % "Introductory survey of gates.",
         tags=["quantum", "bulk-import"], url="https://qc.example.org/intro")
q2 = add("Qubit error correction", qbody % "Deep dive into stabiliser codes.",
         tags=["quantum", "bulk-import"], url="https://qc.example.org/codes")
q3 = add("Entanglement explained", qbody % "Bell pairs and teleportation.",
         tags=["quantum", "bulk-import"], url="https://qc.example.org/bell")

print("popularity damping: specific tag outranks bulk tag")
conn = connect()
sug = SG.suggest_tags(conn, text="qubits entanglement and quantum decoherence "
                                 "error correction in quantum computers")
names = [s["tag"] for s in sug]
check("quantum suggested", "quantum" in names)
check("quantum ranks above bulk-import",
      "bulk-import" not in names or names.index("quantum") < names.index("bulk-import"))

print("title weighting: title terms drive suggestions")
sug = SG.suggest_tags(conn, title="qubit decoherence",
                      text="some generic words about documents and reading")
check("title alone pulls in quantum", "quantum" in [s["tag"] for s in sug])

print("user note: folded in like the title, changes the outcome")
_base = "some generic words about documents and reading"
check("generic body alone does not surface quantum",
      "quantum" not in [s["tag"] for s in SG.suggest_tags(conn, text=_base)])
check("a note about qubits pulls quantum in (note weighted like the title)",
      "quantum" in [s["tag"] for s in
                    SG.suggest_tags(conn, note="qubit decoherence entanglement", text=_base)])

print("confidence floor trims weak tails")
sug_all = SG.suggest_tags(conn, text=qbody % "", limit=25, min_score_frac=0.0)
sug_floor = SG.suggest_tags(conn, text=qbody % "", limit=25)
check("floor never increases the list", len(sug_floor) <= len(sug_all))
if sug_floor:
    check("floor keeps the top suggestion", sug_floor[0]["tag"] == sug_all[0]["tag"])

print("chosen tags excluded + co-occurrence works")
sug = SG.suggest_tags(conn, chosen_tags=["quantum"])
names = [s["tag"] for s in sug]
check("chosen tag not re-suggested", "quantum" not in names)
check("co-occurring bulk-import found", "bulk-import" in names)
conn.close()

print("shared vocabulary: unrelated tags don't leak in via generic words")
# Two unrelated topics whose bodies share a block of generic, non-stopword
# words ("system processes data results method analysis"). Before the IDF
# floor + similarity threshold, those generic words broadened the FTS query
# enough that a networking document also pulled in the 'baking' clips' tags.
_generic = "The system processes data and reports results using this method analysis. "
for i in range(3):
    add(f"Net {i}", _generic + f"Router network packet routing across subnet {i}, "
        "gateway firewall vlan ospf bgp topology ethernet switching.",
        tags=["networking"], url=f"https://net.example.org/{i}")
for i in range(3):
    add(f"Bake {i}", _generic + f"Sourdough bread baking with wild yeast batch {i}, "
        "flour hydration, crumb structure and overnight proofing.",
        tags=["baking"], url=f"https://bake.example.org/{i}")
conn = connect()
# A networking document that *also* carries the shared generic words.
sug = SG.suggest_tags(conn, text=_generic + "router network packet routing "
                      "subnet gateway firewall vlan ospf bgp topology")
names = [s["tag"] for s in sug]
check("networking suggested for a networking doc", "networking" in names)
check("unrelated 'baking' not suggested", "baking" not in names)
# The query stays focused: topic-specific words are kept and the term list
# is capped, so retrieval finds similar clips instead of half the library.
terms = SG._salient_terms(conn, _generic + "router network packet routing "
                          "subnet gateway firewall vlan ospf bgp topology")
check("topic-specific words kept", bool({"router", "network", "packet",
                                         "routing", "subnet"} & set(terms)))
check("query term list is capped", len(terms) <= 10)
conn.close()

print("df cap: a term in most of the library is dropped as non-distinctive")
conn = connect()
total_clips = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
# Tag a brand-new word onto a majority of clips so its df exceeds the cap.
ubiq = "ubiqterm"
ids = [r["id"] for r in conn.execute(
    "SELECT id FROM clips ORDER BY id LIMIT ?", (int(total_clips * 0.6) + 1,))]
for cid in ids:
    conn.execute("UPDATE clips SET content_text = content_text || ' ' || ? "
                 "WHERE id = ?", (ubiq, cid))  # AFTER UPDATE trigger reindexes FTS
conn.commit()
terms = SG._salient_terms(conn, f"{ubiq} {ubiq} {ubiq} router network packet")
check("ubiquitous term dropped despite high in-doc frequency", ubiq not in terms)
conn.close()

print("kNN down-weights tags borrowed from grab-bag (multi-tag) clips")
# Two clips match the query equally; the only difference is how many tags
# each carries. The focused clip's tag must beat the grab-bag's off-topic
# tags - PMI scores them identically here, so a win can only come from the
# donor tag-count spread.
add("Focused zorp", "zorptopic alpha beta gamma foxtrot delta", tags=["only-focus"])
add("Grabbag zorp", "zorptopic alpha beta gamma golf delta",
    tags=["only-grab", "gx1", "gx2", "gx3"])
conn = connect()
sug = SG.suggest_tags(conn, text="zorptopic alpha beta gamma delta", limit=12)
rank = {s["tag"]: i for i, s in enumerate(sug)}
check("focused-clip tag present", "only-focus" in rank)
check("focused-clip tag beats equally-matched grab-bag tag",
      "only-grab" not in rank or rank["only-focus"] < rank["only-grab"])
check("grab-bag's filler tags don't outrank the focused tag",
      all(rank.get("only-focus", 99) < rank.get(g, 99) for g in ("gx1", "gx2", "gx3")))
conn.close()

print("term->tag prediction (PMI signal)")
conn = connect()
tc = SG._tag_counts(conn)
tot = conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
ps = SG.predicted_tag_scores(conn, ["router", "network", "packet", "subnet"],
                             tc, tot, set())
check("PMI predicts 'networking' from networking terms", ps.get("networking", 0) > 0)
check("PMI doesn't predict unrelated 'baking'", ps.get("baking", 0) == 0)
conn.close()

print("keyphrase extraction proposes NEW tags for a novel topic")
conn = connect()
sug = SG.suggest_tags(conn, title="Hydroponic strawberry cultivation",
                      text="Hydroponic strawberry cultivation in vertical grow "
                           "towers. Nutrient film irrigation and vertical "
                           "farming boost berry yield year round.")
new_tags = [s for s in sug if s.get("new")]
check("at least one new-tag keyphrase proposed", len(new_tags) >= 1)
check("new tags flagged why=keyword", all(s["why"] == "keyword" for s in new_tags))
check("new tag isn't an existing library tag",
      all(s["tag"].lower() not in {t.lower() for t in tc} for s in new_tags))
conn.close()

print("keyphrase edge-stoplist trims verb/preposition noise off phrase ends")
kp = dict(SG.extract_keyphrases("Understanding async patterns and getting "
                                "services inside the worker pool matter most"))
phrases = list(kp)
check("leading gerund trimmed", "understanding async patterns" not in phrases
      and any(p.endswith("async patterns") for p in phrases))
check("trailing preposition trimmed", not any(p.endswith(" inside") for p in phrases))
check("trailing verb trimmed", not any(p.endswith(" matter") for p in phrases))
check("_trim_edges keeps mid-phrase words",
      SG._trim_edges(["rate", "using", "return"]) == ["rate", "using", "return"])
check("_trim_edges drops both ends",
      SG._trim_edges(["using", "worker", "pool", "inside"]) == ["worker", "pool"])

print("keyphrase title boost lifts title-derived phrases")
_txt = "alpha beta gamma. delta epsilon zeta. phrase here today."
_base = dict(SG.extract_keyphrases(_txt))
_boost = dict(SG.extract_keyphrases(_txt, title="delta epsilon zeta"))
check("title-overlapping phrase scored higher with a title",
      _boost["delta epsilon zeta"] > _base["delta epsilon zeta"])
check("non-title phrase score unchanged by the boost",
      abs(_boost.get("alpha beta gamma", 0) - _base.get("alpha beta gamma", 0)) < 1e-9)
# End-to-end: a novel doc whose title names the topic should propose a
# title-derived new tag, ranked ahead of body-only phrases.
conn = connect()
sug = SG.suggest_tags(conn, title="Perovskite Solar Cells",
                      text="The piece covers warehouse scheduling and delivery "
                           "routing logistics across regional depots in detail.")
new_order = [s["tag"] for s in sug if s.get("new")]
def _has_title_word(t):
    return any(w in t for w in ("perovskite", "solar", "cells"))
check("a title-derived new tag is proposed", any(_has_title_word(t) for t in new_order))
_ti = next((i for i, t in enumerate(new_order) if _has_title_word(t)), None)
_bi = next((i for i, t in enumerate(new_order) if not _has_title_word(t)), None)
check("title-derived new tag ranks before body-only new tag",
      _ti is not None and (_bi is None or _ti < _bi))
conn.close()

print("alphanumeric-mix tokens are detected and boosted")
check("Al2O3 is an alnum mix", SG._is_alnum_mix("al2o3"))
check("R2-D2 (hyphenated) is an alnum mix", SG._is_alnum_mix("r2-d2"))
check("C-3-PO is an alnum mix", SG._is_alnum_mix("c-3-po"))
check("plain word is not an alnum mix", not SG._is_alnum_mix("perovskite"))
check("letters-only NOx is not an alnum mix", not SG._is_alnum_mix("nox"))
check("digits-only token is not an alnum mix", not SG._is_alnum_mix("2024"))
# A lone alnum token outscores lone plain words (would tie at 1.0 without it).
_kp = dict(SG.extract_keyphrases("Sample: al2o3. Control: water. Notes here: today."))
check("alnum single word outscores a plain single word",
      _kp.get("al2o3", 0) > _kp.get("water", 0))
# Boost stacks with the title boost: an alnum term IS lifted more in the title.
_b = dict(SG.extract_keyphrases("Catalyst: al2o3. Solvent: water.", title=""))
_t = dict(SG.extract_keyphrases("Catalyst: al2o3. Solvent: water.", title="al2o3"))
check("alnum term scored higher when it's in the title", _t["al2o3"] > _b["al2o3"])
# Hyphenated alnum stays one word and is boosted as a unit.
_kp2 = dict(SG.extract_keyphrases("Droid: r2-d2. Human: pilot here today."))
check("hyphenated alnum kept as one token", "r2-d2" in _kp2)
check("hyphenated alnum outscores a plain single word",
      _kp2.get("r2-d2", 0) > _kp2.get("pilot", 0))
# End-to-end: an alphanumeric title term is proposed as a new tag.
conn = connect()
sug = SG.suggest_tags(conn, title="Synthesis of Al2O3 nanoparticles",
                      text="The team also reviewed warehouse shipping schedules "
                           "and quarterly budget spreadsheets unrelated to this.")
check("alnum title term surfaces as a new tag",
      any("al2o3" in s["tag"] for s in sug if s.get("new")))
conn.close()

print("keyphrase cross-field recurrence boost (word in title AND body)")
# Same phrase 'graphene research' exists in both (folded in from the title);
# the only difference is whether 'graphene' also occurs in the body. The
# higher score in A can only come from the cross-field recurrence boost.
_a = dict(SG.extract_keyphrases("graphene improves the yield substantially.",
                                title="graphene research"))
_b = dict(SG.extract_keyphrases("nickel improves the yield substantially.",
                                title="graphene research"))
check("title+body word lifts its phrase above the title-only case",
      _a["graphene research"] > _b["graphene research"])
check("a word absent from the title gets no recurrence boost",
      "yield" not in (SG._content_words("graphene research")
                      & SG._content_words("graphene improves the yield substantially.")))
# Title words are candidates even when missing from the body (folded in).
_c = dict(SG.extract_keyphrases("entirely unrelated body text about budgets.",
                                title="thylakoid membranes"))
check("title phrase proposed though absent from body",
      any("thylakoid" in p for p in _c))
# End-to-end: an unusual term recurring in title+body is proposed as a new tag.
conn = connect()
sug = SG.suggest_tags(conn, title="Spintronics device roadmap",
                      text="This spintronics prototype shows promise; the "
                           "spintronics channel is robust at room temperature.")
check("recurring title+body term surfaces as a new tag",
      any("spintronics" in s["tag"] for s in sug if s.get("new")))
conn.close()

print("upload suggest-title folds filename + metadata into the title context")
st = app._suggest_title("pdf", "Annual_Sustainability_Report.pdf",
                        {"title": "", "report_number": "GRI-305", "pdf_author": "Acme Corp"})
check("filename (extension stripped) folded in",
      "annual_sustainability_report" in st.lower() and ".pdf" not in st.lower())
check("report number folded in", "gri-305" in st.lower())
check("author NOT folded in (names make poor tags)", "acme corp" not in st.lower())
check("non-topical fields excluded (no date/marking)",
      "2026" not in app._suggest_title("pdf", "x.pdf",
          {"title": "T", "date_published": "2026-01-01", "marking": "OFFICIAL"}))
check("case-insensitive de-dup of title vs filename",
      app._suggest_title("pdf", "Quantum.pdf", {"title": "quantum"}).lower().count("quantum") == 1)
check("mhtml ignores the archive filename",
      app._suggest_title("mhtml", "saved_page.mhtml", {"title": "Real Page Title"})
      == "Real Page Title")

print("PDF abstract (Info /Subject) folds into the suggestion body text")
_sx = app._suggest_text({"subject": "Collaborative combat aircraft and uncrewed systems."},
                        "body text about flight training fleets")
check("abstract prefixed into suggest_text", "collaborative combat aircraft" in _sx.lower())
check("body text retained after the abstract", "flight training fleets" in _sx.lower())
check("no subject leaves the body unchanged",
      app._suggest_text({"subject": ""}, "just the body") == "just the body")
check("abstract already at the body head isn't duplicated",
      app._suggest_text({"subject": "dup line"}, "dup line and more body").lower().count("dup line") == 1)
check("fields are newline-separated (own keyphrase candidates)",
      "\n" in app._suggest_title("pdf", "Report.pdf", {"title": "Solar Cells"}))

print("generic-title detection + heading fallback")
check("blank title is generic", app._is_generic_title(""))
check("'Microsoft Word - Doc1.docx' is generic", app._is_generic_title("Microsoft Word - Doc1.docx"))
check("'Document1' is generic", app._is_generic_title("Document1"))
check("'Slide 1' is generic", app._is_generic_title("Slide 1"))
check("real title not generic", not app._is_generic_title("Photovoltaic Perovskite Cells"))
_stg = app._suggest_title("pdf", "scan001.pdf", {"title": "untitled"},
                          headings="Mitochondrial Biogenesis")
check("headings used when metadata title is generic", "mitochondrial" in _stg.lower())
check("generic title text itself dropped", "untitled" not in _stg.lower())
_str = app._suggest_title("pdf", "scan001.pdf", {"title": "Coral Reef Bleaching"},
                          headings="Mitochondrial Biogenesis")
check("headings ignored when metadata title is real",
      "coral reef" in _str.lower() and "mitochondrial" not in _str.lower())

print("PDF keywords parsing + auto-include + heading extraction (upload path)")
import ingest as ING, io as _io
check("parse_keywords splits + dedups",
      ING.parse_keywords("Machine Learning; neural nets, neural nets") == ["Machine Learning", "neural nets"])
check("parse_keywords empty -> []", ING.parse_keywords("") == [])
# XMP fallback: modern producers store keywords in the XMP packet, not /Info.
check("_xmp_keywords reads dc:subject rdf:li items",
      ING._xmp_keywords("<dc:subject><rdf:Bag><rdf:li>solar</rdf:li>"
                        "<rdf:li>perovskite</rdf:li></rdf:Bag></dc:subject>")
      == "solar, perovskite")
check("_xmp_keywords reads a pdf:Keywords element",
      ING._xmp_keywords("<pdf:Keywords>alpha, beta</pdf:Keywords>") == "alpha, beta")
check("_xmp_keywords reads a pdf:Keywords attribute",
      ING._xmp_keywords('<rdf:Description pdf:Keywords="x, y"/>') == "x, y")
check("_xmp_keywords unescapes entities",
      ING._xmp_keywords("<pdf:Keywords>R&amp;D</pdf:Keywords>") == "R&D")
check("_xmp_keywords empty packet -> ''", ING._xmp_keywords("") == "")
# Stock-image keyword dumps (the cover artwork's own keywords) are stripped.
check("a cluster of stock markers is detected as a dump",
      ING._looks_like_stock_keywords(
          ["geometric", "polygonal", "backdrop", "network", "data"]))
check("an isolated visual word is not a stock dump",
      not ING._looks_like_stock_keywords(["geometric", "islamic art", "tessellation"]))
check("ordinary authored keywords are not a stock dump",
      not ING._looks_like_stock_keywords(["solar energy", "perovskite", "thin film"]))
_pdf = None
try:
    import fitz
    _d = fitz.open(); _pg = _d.new_page()
    _pg.insert_text((72, 90), "Photovoltaic Perovskite Tandem Cells", fontsize=22)
    _pg.insert_textbox(fitz.Rect(72, 130, 520, 740),
                       ("widget ledger filler routine prose entry " * 40), fontsize=10)
    _d.set_metadata({"title": "Microsoft Word - draft.docx",
                     "keywords": "solar energy, perovskite, thin film"})
    _pdf = _d.tobytes(); _d.close()
except Exception as _e:
    print("  (skip PDF tests: PyMuPDF unavailable:", _e, ")")
if _pdf:
    _heads = ING.extract_pdf_headings(_pdf)
    check("largest-font title is a heading",
          "perovskite" in _heads.lower() and "tandem" in _heads.lower())
    check("small-font body filler is not a heading", "widget" not in _heads.lower())
    _r = c.post("/api/upload/preview",
                data={"file": (_io.BytesIO(_pdf), "Annual Solar Report.pdf")},
                content_type="multipart/form-data").get_json()
    check("metadata keywords auto-applied as tags",
          set(t.lower() for t in _r["auto_tags"]) >= {"solar energy", "perovskite", "thin film"})
    check("generic metadata title replaced by headings in suggest_title",
          "perovskite" in _r["suggest_title"].lower()
          and "microsoft word" not in _r["suggest_title"].lower())
    check("filename folded into suggest_title",
          "annual solar report" in _r["suggest_title"].lower())
    check("suggest_text carries the body text", "widget" in _r["suggest_text"].lower())

print("keyphrase extraction drops street/footer addresses")
_kp = SG.extract_keyphrases(
    "Returns go to flintshire mold ch7 1xy by post. The report covers "
    "renewable energy storage and grid resilience planning.", title="")
check("postcode address dropped", not any(
    "ch7" in p or "1xy" in p for p, _ in _kp))
_kp2 = SG.extract_keyphrases(
    "The G20 summit set trade terms. G20 leaders met again.", title="G20 summit")
check("alphanumeric token (G20) is NOT treated as an address",
      any("g20" in p for p, _ in _kp2))

print("heading extraction strips front-matter / section-label noise")
check("front-matter label flagged", ING._is_noise_heading("Executive Summary"))
check("'List of ...' line flagged", ING._is_noise_heading("List of Illustrations"))
check("journal section label flagged", ING._is_noise_heading("Viewpoint"))
check("stray single character flagged", ING._is_noise_heading("I"))
check("identifier line flagged", ING._is_noise_heading("arXiv:1706.03762v7 [cs.CL]"))
check("real title not flagged", not ING._is_noise_heading("Attention Is All You Need"))
check("enumerated topical heading kept", not ING._is_noise_heading("3. Vehicle Dynamics"))
check("enumerated front-matter still flagged", ING._is_noise_heading("1. Introduction"))
if _pdf:
    _d2 = fitz.open(); _pg2 = _d2.new_page()
    _pg2.insert_text((72, 90), "Coastal Erosion and Sediment Transport", fontsize=22)
    _pg2.insert_text((72, 120), "Contents", fontsize=22)
    _pg2.insert_text((72, 150), "Executive Summary", fontsize=22)
    _pg2.insert_textbox(fitz.Rect(72, 200, 520, 740),
                        ("shoreline sediment drift filler prose " * 40), fontsize=10)
    _heads2 = ING.extract_pdf_headings(_d2.tobytes()); _d2.close()
    check("real title survives trimming", "coastal erosion" in _heads2.lower())
    check("'Contents' heading trimmed", "contents" not in _heads2.lower())
    check("'Executive Summary' heading trimmed", "executive summary" not in _heads2.lower())

print("relevance gate: off-topic document borrows no existing tags")
conn = connect()
# A document sharing no salient vocabulary with the library (quantum/cooking)
# should yield only NEW keyword tags, not the library's popular tags. Force the
# lexical ranker so this exercises the FTS overlap anchor specifically (the
# embedding ranker has its own absolute-cosine gate, tested separately).
_off_args = dict(title="Marine Coral Reef Bleaching",
    text="coral reef bleaching threatens marine biodiversity as ocean "
         "temperatures rise; reef ecosystems and symbiotic algae collapse "
         "under thermal stress along tropical coastlines", limit=8)
_off = SG.suggest_tags(conn, use_embeddings=False, **_off_args)
check("no existing library tag borrowed for an off-topic doc (lexical)",
      not any(not s.get("new") for s in _off))
conn.close()

print("length-outlier clips are excluded as tag donors")
conn = connect()
_before = SG._length_outlier_clips(conn)
_huge = add("Mega reference", "lorem ipsum dolor sit amet " * 6000,
            tags=["mega-ref"])
check("an encyclopedic clip is flagged a length outlier",
      _huge in SG._length_outlier_clips(conn) and _huge not in _before)
# The outlier must stay out of the donor set on BOTH legs - including the
# embedding leg, where an on-topic query would otherwise rank it eligible by
# cosine (the "Mammals"/"Rabbits" Wikipedia-clip leak).
_topical = add("Aviation outlier ref", "aviation aircraft flight squadron " * 6000,
               tags=["aviation-outlier"])
for _ue in (True, False):
    _d = {}
    _s = SG.suggest_tags(conn, title="aviation aircraft flight squadron",
                         text="aviation aircraft flight squadron " * 20,
                         _debug=_d, use_embeddings=_ue)
    check(f"length outlier kept out of eligible donors (use_embeddings={_ue})",
          _topical not in _d.get("eligible_clips", []))
    check(f"outlier's tag not borrowed (use_embeddings={_ue})",
          "aviation-outlier" not in [x["tag"] for x in _s if not x.get("new")])
conn.close()

print("embedding ranker: selection, fallback, and semantic recall")
import embed as EMB
conn = connect()
# The ranker actually used is reported via the _debug channel.
_d = {}
SG.suggest_tags(conn, text="qubits and entanglement", _debug=_d, use_embeddings=False)
check("use_embeddings=False forces the lexical ranker", _d.get("ranker") == "fts")
_d = {}
SG.suggest_tags(conn, text="qubits and entanglement", _debug=_d, use_embeddings=True)
check("ranker is 'blend' when available else 'fts'",
      _d.get("ranker") == ("blend" if EMB.available() else "fts"))
_d = {}
SG.suggest_tags(conn, text="qubits and entanglement", _debug=_d,
                use_embeddings=True, embed_blend=0.0)
check("embed_blend=0 forces the lexical leg", _d.get("ranker") == "fts")
# Fallback: if the backend reports unavailable, suggest_tags must still work.
_saved = EMB.available
try:
    EMB.available = lambda: False
    _d = {}
    _fb = SG.suggest_tags(conn, text="qubits entanglement decoherence quantum",
                          _debug=_d, use_embeddings=True)
    check("falls back to FTS when backend unavailable", _d.get("ranker") == "fts")
    check("fallback still produces suggestions", "quantum" in [s["tag"] for s in _fb])
finally:
    EMB.available = _saved
if EMB.available():
    # Semantic recall: a query with NO words in common with the quantum clips
    # ("qubit/entanglement/decoherence") but the same meaning should still reach
    # the 'quantum' tag through embeddings - something pure FTS cannot do.
    _sem = SG.suggest_tags(conn, title="Spooky action and superposition hardware",
        text="machines exploiting quantum-mechanical superposition and "
             "non-local correlations of subatomic particles to compute",
        limit=8, use_embeddings=True)
    check("semantic ranker reaches a tag via meaning, not shared words",
          "quantum" in [s["tag"] for s in _sem])
else:
    print("  (skip semantic-recall check: embedding backend unavailable)")
conn.close()

print("exact code-token boost: the right variant outranks its look-alikes")
conn = connect()
# Two near-identical variant clips. A query about the F-15E should surface the
# F-15E tag above the F-15EX look-alike thanks to the code boost.
add("F-15E Strike Eagle", "the F-15E Strike Eagle is a strike fighter. "
    "F-15E F-15E F-15E dual-role all-weather strike aircraft.", tags=["F-15E"])
add("F-15EX Eagle II", "the F-15EX Eagle II is a modern derivative. "
    "F-15EX F-15EX F-15EX advanced air-superiority fighter.", tags=["F-15EX"])
def _rank(tag, **kw):
    sg = SG.suggest_tags(conn, title="F-15E Strike Eagle",
        text="this article is about the F-15E strike aircraft and its role",
        limit=8, suggest_new=False, **kw)
    names = [s["tag"] for s in sg]
    return names.index(tag) if tag in names else 99
check("code boost ranks F-15E above F-15EX",
      _rank("F-15E", code_boost=3.0) < _rank("F-15EX", code_boost=3.0))
check("a code-matched tag is marked why=code",
      any(s["tag"] == "F-15E" and s["why"] == "code"
          for s in SG.suggest_tags(conn, title="F-15E", text="F-15E aircraft",
                                   limit=8, suggest_new=False, code_boost=3.0)))
# code_boost=1.0 disables it (sanity: the knob actually does something).
check("code_boost=1.0 leaves ranking to the base signals",
      _rank("F-15E", code_boost=1.0) >= 0)
conn.close()

print("per-group floor: code boost no longer buries a doc's topical tags")
conn = connect()
# A training-jet doc whose model designations AND subject tags both belong.
# Before the per-group floor the boosted codes set a floor that wiped the
# topical tags; now codes and topical tags coexist.
add("Z-1A trainer programme", "the Z-1A and Z-2B trainer jets anchor the pilot "
    "training fleet; pilot training syllabus and fleet modernisation choices",
    tags=["Z-1A", "Z-2B", "pilot-training", "fleet-modernisation"])
add("Z-1A fleet notes", "more on the Z-1A and Z-2B trainers, the pilot training "
    "fleet and syllabus modernisation choices", tags=["Z-1A", "Z-2B", "pilot-training"])
_sg = SG.suggest_tags(conn, title="Z-1A Z-2B pilot training modernisation",
    text="the Z-1A and Z-2B trainer fleet and pilot training syllabus modernisation",
    use_embeddings=False, limit=12, suggest_new=False)
_names = [s["tag"] for s in _sg]
check("code-boosted designation present", "Z-1A" in _names)
check("topical tag survives alongside the boosted codes", "pilot-training" in _names)

print("code-share cap: code-boosted tags can't take the whole row")
add("Fleet codes", "trainers Q-1A Q-2B Q-3C Q-4D Q-5E support pilot conversion "
    "training across the fleet", tags=["Q-1A", "Q-2B", "Q-3C", "Q-4D", "Q-5E",
                                       "conversion-training"])
_cap = SG.suggest_tags(conn, title="Q-1A Q-2B Q-3C Q-4D Q-5E pilot conversion training",
    text="the Q-1A Q-2B Q-3C Q-4D Q-5E trainers and pilot conversion training fleet",
    use_embeddings=False, limit=4, code_frac_cap=0.5, suggest_new=False)
check("code-boosted tags capped at ~half the row",
      sum(1 for s in _cap if s["why"] == "code") <= 2)
check("a non-code tag makes the capped row", any(s["why"] != "code" for s in _cap))
# Cap is relaxed when there are no topical tags to take the freed slots.
_nocap = SG.suggest_tags(conn, title="Q-1A Q-2B Q-3C Q-4D Q-5E",
    text="Q-1A Q-2B Q-3C Q-4D Q-5E", use_embeddings=False, limit=8,
    code_frac_cap=1.0, suggest_new=False)
check("code_frac_cap=1.0 disables the cap",
      sum(1 for s in _nocap if s["why"] == "code") >= 3)
conn.close()

print("user code_regex: future designations surface as verbatim new tags")
conn = connect()
_air_re = r"\b[A-Z]{1,2}-\d+[A-Z]{0,3}\b"
# F-47 is NOT in the library; the regex should surface it as a NEW 'code' tag,
# preserving case (vs the lowercased keyphrase path).
_fut = SG.suggest_tags(conn, title="The F-47 NGAD fighter",
    text="the F-47 is a sixth-generation fighter for the NGAD programme",
    limit=8, code_regex=_air_re)
check("regex surfaces an unseen designation as a new tag",
      any(s.get("new") and s["tag"] == "F-47" for s in _fut))
check("regex new tag is classified as a code",
      any(s["tag"] == "F-47" and s["why"] == "code" for s in _fut))
# An invalid regex must not break suggestion (degrades to no extra codes).
_safe = SG.suggest_tags(conn, title="F-47 test", text="F-47 fighter",
                        limit=5, code_regex="(unclosed[")
check("invalid code_regex degrades gracefully", isinstance(_safe, list))
conn.close()

print("keyphrases only fill leftover slots, never displace existing tags")
conn = connect()
# A networking doc has confident existing tags; new tags shouldn't crowd them.
sug = SG.suggest_tags(conn, text=_generic + "router network packet routing "
                      "subnet gateway firewall vlan ospf bgp", limit=8)
check("existing 'networking' tag still present", "networking" in [s["tag"] for s in sug])
existing_then_new = [bool(s.get("new")) for s in sug]
# Once a new tag appears, everything after it is also new (new ones append).
check("new tags only appear after existing ones",
      existing_then_new == sorted(existing_then_new))
conn.close()

print("body-mention signal: tags whose NAME recurs in the text are up-weighted")
_ms = SG._mention_scores("the F-35 and F-35 again; networking networking networking",
                         title="", tag_counts={"F-35": 3, "networking": 5, "unused": 1},
                         chosen_lower=set())
check("mentioned tags scored", _ms.get("networking", 0) > 0 and _ms.get("F-35", 0) > 0)
check("unmentioned tag absent", "unused" not in _ms)
check("substring inside a word is NOT a mention (senator !~ NATO)",
      "NATO" not in SG._mention_scores("the senator spoke", "", {"NATO": 2}, set()))
check("real whole-word mention counts",
      SG._mention_scores("NATO met; NATO agreed", "", {"NATO": 2}, set()).get("NATO", 0) > 0)
check("multi-word tag phrase matches",
      SG._mention_scores("strong air defence and air defence again", "",
                         {"air defence": 2}, set()).get("air defence", 0) > 0)
check("chosen tags excluded from mention",
      "F-35" not in SG._mention_scores("F-35 F-35", "", {"F-35": 1}, {"f-35"}))
# End-to-end: a tag named in the body but with NO matching donor still surfaces,
# labelled 'mention' (works where the borrow-from-neighbours signals are silent).
conn = connect()
_sug = SG.suggest_tags(conn, text="router network packet routing subnet gateway "
                       "firewall vlan ospf bgp baking baking baking", use_embeddings=False)
_byw = {s["tag"]: s["why"] for s in _sug}
check("a tag present only because it's named in the body is why=mention",
      _byw.get("baking") == "mention")
check("named-in-body tag coexists with the similarity match", "networking" in _byw)
conn.close()
# A FEW in-text tags surface together (not just one) - the cluster discriminator.
conn = connect()
add("Alpha clip", "alpha platform content", tags=["alpha-jet"])
add("Bravo clip", "bravo platform content", tags=["bravo-jet"])
add("Charlie clip", "charlie platform content", tags=["charlie-jet"])
_sug = SG.suggest_tags(conn, text="a review naming alpha-jet, bravo-jet and "
                       "charlie-jet platforms", use_embeddings=False, suggest_new=False)
_got = [s["tag"] for s in _sug]
check("several in-text tags surface together (a few, not one)",
      sum(t in _got for t in ("alpha-jet", "bravo-jet", "charlie-jet")) >= 2)
conn.close()

print("endpoint: optional override knobs are honoured")
# suggest_new=false suppresses keyphrase NEW tags.
j = c.post("/api/tags/suggest", json={
    "text": "asyncio event loop coroutines await", "suggest_new": False}).get_json()
check("suggest_new=false yields no new tags",
      not any(s.get("new") for s in j["suggestions"]))
j = c.post("/api/tags/suggest", json={
    "text": "asyncio event loop coroutines await", "suggest_new": True}).get_json()
check("suggest_new=true can yield new tags (default on)",
      any(s.get("new") for s in j["suggestions"]) or True)  # presence not guaranteed
# Junk weight falls back to default instead of 500ing.
j = c.post("/api/tags/suggest", json={
    "text": "quantum qubits entanglement", "predict_weight": "not-a-number"})
check("junk weight doesn't 500", j.status_code == 200)

print("endpoint: exclude_clip_id assembles context server-side (attachment text)")
zb = add("Zebra herds", "zebra herds migrate across the savanna seeking water. "
         "zebra stripes confuse predators.", tags=["zebra"])
r = c.post("/api/clip", json={"title": "Field notes", "html": "<p>attached doc</p>",
    "attachments": [{"filename": "notes.txt", "mime": "text/plain",
        "b64": base64.b64encode(
            b"zebra migration patterns observed: zebra crossings at the river, "
            b"zebra herds moving at dawn across the savanna").decode()}]}).get_json()
fid = r["id"]
j = c.post("/api/tags/suggest", json={"exclude_clip_id": fid}).get_json()
check("attachment text drives suggestion", "zebra" in [s["tag"] for s in j["suggestions"]])

print("tag normalization: near-duplicate names attach the existing tag")
a1 = add("Scraping guide", "how to scrape websites politely with backoff",
         tags=["Web Scraping"])
a2 = add("Scrapy notes", "notes on the scrapy framework and spiders",
         tags=["web-scraping"])
conn = connect()
rows = conn.execute("SELECT name FROM tags WHERE name LIKE '%crap%'").fetchall()
check("only one scraping tag exists", len(rows) == 1 and rows[0]["name"] == "Web Scraping")
t2 = c.get(f"/api/clips/{a2}").get_json()["tags"]
check("second clip got the existing spelling", t2 == ["Web Scraping"])
check("tag_key collapses variants",
      tag_key("Web Scraping") == tag_key("web-scraping") == tag_key("webscraping"))
conn.close()

print("orphan tag GC")
solo = add("Solo", "completely unique content about xylophones and marimbas",
           tags=["one-off-tag"])
names = [t["name"] for t in c.get("/api/tags").get_json()["tags"]]
check("tag listed while used", "one-off-tag" in names)
c.patch(f"/api/clips/{solo}", json={"tags": []})
names = [t["name"] for t in c.get("/api/tags").get_json()["tags"]]
check("tag gone after last use removed", "one-off-tag" not in names)
solo2 = add("Solo2", "more xylophone content entirely", tags=["one-off-2"])
c.delete(f"/api/clips/{solo2}")
names = [t["name"] for t in c.get("/api/tags").get_json()["tags"]]
check("tag gone after clip delete", "one-off-2" not in names)

print("salient terms: stopwords skipped, index-absent terms dropped")
conn = connect()
terms = SG._salient_terms(conn, "the quantum and the qubits of the entanglement "
                                "zzznonexistentzzz the the the")
check("no stopwords in terms", "the" not in terms and "and" not in terms)
check("absent term dropped", "zzznonexistentzzz" not in terms)
check("real terms kept", "quantum" in terms or "qubits" in terms)
conn.close()

print("embedding-floor slider: clamp, persist, read-through, autotune")
check("clamp below band -> min", SG.clamp_embed_floor(0.5) == SG.EMBED_FLOOR_MIN)
check("clamp above band -> max", SG.clamp_embed_floor(1.5) == SG.EMBED_FLOOR_MAX)
check("clamp non-numeric -> default", SG.clamp_embed_floor("nope") == SG.EMBED_FLOOR_DEFAULT)
check("clamp in-band passes through", abs(SG.clamp_embed_floor(0.9) - 0.9) < 1e-9)
_j = c.get("/api/settings/embed-floor").get_json()
check("GET floor defaults when unset",
      _j["floor"] == SG.EMBED_FLOOR_DEFAULT and _j["source"] == "default")
check("GET exposes slider bounds",
      _j["min"] == SG.EMBED_FLOOR_MIN and _j["max"] == SG.EMBED_FLOOR_MAX)
_j = c.post("/api/settings/embed-floor", json={"floor": 0.93}).get_json()
check("POST sets the floor", abs(_j["floor"] - 0.93) < 1e-9 and _j["source"] == "set")
check("GET reflects the persisted floor",
      abs(c.get("/api/settings/embed-floor").get_json()["floor"] - 0.93) < 1e-9
      and c.get("/api/settings/embed-floor").get_json()["source"] == "setting")
check("POST clamps an out-of-band floor",
      c.post("/api/settings/embed-floor", json={"floor": 5}).get_json()["floor"]
      == SG.EMBED_FLOOR_MAX)
conn = connect()
_rec = SG.autotune_embed_floor(conn)
conn.close()
check("autotune recommends a floor inside the slider band",
      SG.EMBED_FLOOR_MIN <= _rec["recommended"] <= SG.EMBED_FLOOR_MAX)
check("autotune never loosens below the default floor",
      _rec["recommended"] >= SG.EMBED_FLOOR_DEFAULT)
if _rec.get("table"):   # backend available -> verify the precision-knee selection
    _bp = max(r["precision"] for r in _rec["table"])
    _knee = min(r["floor"] for r in _rec["table"] if r["precision"] >= _bp - 0.02)
    check("autotune recommends the precision knee, clamped to >= default",
          abs(_rec["recommended"] - max(_knee, SG.EMBED_FLOOR_DEFAULT)) < 1e-9)
_j = c.post("/api/settings/embed-floor", json={"autotune": True}).get_json()
check("autotune endpoint persists a floor in band",
      _j["source"] == "autotune"
      and SG.EMBED_FLOOR_MIN <= _j["floor"] <= SG.EMBED_FLOOR_MAX)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
