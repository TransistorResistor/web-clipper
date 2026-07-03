"""
File ingestion helpers for the drag-and-drop uploader.

Handles two upload types:
  - PDF  : text via PyMuPDF (reused from search.py), metadata from the PDF
           document info dict + a scan of the text for handling markings.
  - MHTML: MIME-multipart (.mht / .mhtml) - we locate the root HTML part,
           re-inline the embedded resource parts (images) as data URIs so
           the stored body is self-contained, then run HTML metadata
           extraction over it.

Plus server-side metadata extraction that mirrors the userscript's logic
(title / url / report_number / date_published / marking) but works on a
parsed HTML string or PDF rather than a live DOM.

Nothing here writes to the DB; the caller (app.py) stages results and
commits on confirmation.
"""

from __future__ import annotations

import base64
import email
import quopri
import re
from email import policy
from html import unescape
from typing import Optional
from urllib.parse import urljoin


# ---------------------------------------------------------------------------
# MHTML
# ---------------------------------------------------------------------------

def parse_mhtml(raw: bytes) -> dict:
    """Parse an MHTML archive into {html, base_url, images:[{cid/loc, data_uri}]}.

    Returns the root HTML with embedded resources re-inlined as data: URIs.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)

    # Collect every part keyed by both Content-Location and Content-ID so we
    # can resolve references from the HTML.
    parts_by_location: dict[str, tuple[str, bytes]] = {}
    parts_by_cid: dict[str, tuple[str, bytes]] = {}
    html_part = None
    html_location = None

    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        loc = part.get("Content-Location")
        cid = part.get("Content-ID")
        if cid:
            cid = cid.strip().strip("<>")
        if ctype == "text/html" and html_part is None:
            html_part = payload
            html_location = loc
            # Don't also treat the root HTML as an inlinable resource.
            continue
        if loc:
            parts_by_location[loc] = (ctype, payload)
        if cid:
            parts_by_cid[cid] = (ctype, payload)

    if html_part is None:
        # Some MHTML files label the body as text/plain or omit the type;
        # fall back to the first non-multipart text part.
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype.startswith("text/"):
                html_part = part.get_payload(decode=True)
                html_location = part.get("Content-Location")
                break
    if html_part is None:
        raise ValueError("No HTML part found in MHTML archive")

    # Decode the HTML bytes to text (charset from the part, default utf-8).
    charset = "utf-8"
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            charset = part.get_content_charset() or "utf-8"
            break
    try:
        html = html_part.decode(charset, errors="replace")
    except (LookupError, TypeError):
        html = html_part.decode("utf-8", errors="replace")

    base_url = html_location or ""

    # Re-inline resources: rewrite src/href that match a stored part's
    # Content-Location (absolute or resolved against base) or cid: reference.
    def to_data_uri(ctype: str, data: bytes) -> str:
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{ctype};base64,{b64}"

    inlined = 0

    def replace_attr(match: re.Match) -> str:
        nonlocal inlined
        attr, quote, url = match.group(1), match.group(2), match.group(3)
        if url.startswith("data:"):
            return match.group(0)
        resolved = None
        if url.startswith("cid:"):
            cid = url[4:].strip().strip("<>")
            resolved = parts_by_cid.get(cid)
        if resolved is None:
            # Direct Content-Location match.
            resolved = parts_by_location.get(url)
        if resolved is None and base_url:
            # Resolve relative URL against the base, then match.
            absu = urljoin(base_url, url)
            resolved = parts_by_location.get(absu)
        if resolved is None:
            return match.group(0)
        ctype, data = resolved
        inlined += 1
        return f'{attr}={quote}{to_data_uri(ctype, data)}{quote}'

    # Rewrite src="..." and src='...' (and the same for href on <img>-ish).
    html = re.sub(r'(src)=(["\'])([^"\']+)\2', replace_attr, html, flags=re.IGNORECASE)

    return {
        "html": html,
        "base_url": base_url,
        "inlined_resources": inlined,
    }


# ---------------------------------------------------------------------------
# Server-side metadata extraction (HTML)
# ---------------------------------------------------------------------------

def _meta_content(html: str, name: str) -> str:
    # <meta name="x" content="y"> or <meta property="x" content="y">,
    # in either attribute order.
    patterns = [
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\']{re.escape(name)}["\']',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return unescape(m.group(1).strip())
    return ""


def _first_text_by_selectorish(html: str, patterns: list[str]) -> str:
    """Crude class/id text grab without a DOM: find <tag ... class="...x...">
    or id="x" and return its inner text. patterns are (attr, value) pairs
    expressed as regexes already."""
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            inner = re.sub(r"<[^>]+>", " ", m.group(1))
            inner = re.sub(r"\s+", " ", unescape(inner)).strip()
            if inner:
                return inner
    return ""


def _title(html: str) -> str:
    og = _meta_content(html, "og:title")
    if og:
        return og
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"\s+", " ", unescape(m.group(1))).strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
    return ""


def _canonical_url(html: str, base_url: str = "") -> str:
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    og = _meta_content(html, "og:url")
    if og:
        return og
    return base_url


def _published(html: str) -> str:
    for name in ("article:published_time", "datePublished", "og:published_time",
                 "dc.date", "dc.date.issued"):
        v = _meta_content(html, name)
        if v:
            return v
    m = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


# Marking detection: visible banners + meta tags + ALL-CAPS heading + body scan.
_MARKING_TOKENS = [
    "TOP SECRET", "SECRET", "CONFIDENTIAL", "PROTECTED",
    "OFFICIAL: SENSITIVE", "OFFICIAL-SENSITIVE", "OFFICIAL",
    "UNCLASSIFIED", "UNOFFICIAL", "FOR OFFICIAL USE ONLY", "FOUO",
    "CUI", "RESTRICTED", "PUBLIC",
]


def _marking_candidates_html(html: str) -> dict:
    cands = {
        "metaClassification": _meta_content(html, "classification"),
        "metaSecurity": _meta_content(html, "security-classification") or _meta_content(html, "security"),
        "classBanner": _first_text_by_selectorish(html, [
            r'<[^>]+class=["\'][^"\']*(?:classification-banner|handling-marker|security-banner)[^"\']*["\'][^>]*>(.*?)</',
            r'<[^>]+id=["\']classification["\'][^>]*>(.*?)</',
            r'<[^>]+data-classification=["\'][^"\']*["\'][^>]*>(.*?)</',
        ]),
        "firstHeading": "",
        "bodyScan": "",
        "fallback": "",
    }
    # ALL-CAPS first heading
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if m:
        h = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
        if h and re.match(r"^[A-Z][A-Z\s:\-/]{3,}$", h.split("\n")[0]):
            cands["firstHeading"] = h
    # Body scan: look for a known marking token near the top of the visible text.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    head = text[:1500].upper()
    for tok in _MARKING_TOKENS:  # ordered most-sensitive first
        if tok in head:
            cands["bodyScan"] = tok
            break
    return cands


def select_marking(cands: dict) -> Optional[str]:
    """Mirror of the userscript's selectMarking hook. Customise to match
    your existing logic. Default: first non-empty in priority order."""
    priority = ["classBanner", "metaSecurity", "metaClassification",
                "firstHeading", "bodyScan", "fallback"]
    for key in priority:
        v = cands.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return None


def extract_html_metadata(html: str, base_url: str = "") -> dict:
    cands = _marking_candidates_html(html)
    return {
        "title": _title(html),
        "url": _canonical_url(html, base_url),
        "report_number": (_meta_content(html, "citation_technical_report_number")
                          or _meta_content(html, "citation_report_id")
                          or _meta_content(html, "dc.identifier") or ""),
        "date_published": _published(html),
        "marking_candidates": cands,
        "marking": select_marking(cands),
        "metadata_extractor": "server-html",
    }


# ---------------------------------------------------------------------------
# Server-side metadata extraction (PDF)
# ---------------------------------------------------------------------------

def _pdf_xmp_packet(doc) -> str:
    """Return a PDF's raw XMP metadata packet (the /Type /Metadata stream), or
    ''. fitz's `doc.metadata` exposes only the legacy /Info dictionary; modern
    producers (InDesign, Acrobat, most publishers) write Keywords/Subject into
    XMP, so it has to be read out of the stream separately."""
    try:
        for x in range(1, doc.xref_length()):
            if doc.xref_get_key(x, "Type")[1] == "/Metadata":
                return (doc.xref_stream(x) or b"").decode("utf-8", "replace")
    except Exception:
        pass
    return ""


_XMP_KEYWORDS_RE = re.compile(r"<pdf:Keywords>(.*?)</pdf:Keywords>", re.S | re.I)
_XMP_KEYWORDS_ATTR_RE = re.compile(r'pdf:Keywords\s*=\s*"([^"]*)"', re.I)
_XMP_SUBJECT_RE = re.compile(r"<dc:subject>(.*?)</dc:subject>", re.S | re.I)
_XMP_LI_RE = re.compile(r"<rdf:li[^>]*>(.*?)</rdf:li>", re.S | re.I)
_XMP_TAG_RE = re.compile(r"<[^>]+>")


def _xmp_keywords(xmp: str) -> str:
    """Extract a keywords string from an XMP packet, as a comma-joined value so
    `parse_keywords()` can split it the same way it splits an /Info /Keywords
    string. Reads `pdf:Keywords` (a delimited string, element or attribute form)
    and `dc:subject` (an rdf:Bag/Seq of rdf:li items). '' when none present."""
    if not xmp:
        return ""
    vals: list[str] = []
    m = _XMP_KEYWORDS_RE.search(xmp)
    if m:
        vals.append(_XMP_TAG_RE.sub("", m.group(1)))
    else:
        m = _XMP_KEYWORDS_ATTR_RE.search(xmp)
        if m:
            vals.append(m.group(1))
    m = _XMP_SUBJECT_RE.search(xmp)
    if m:
        lis = _XMP_LI_RE.findall(m.group(1))
        vals.extend(lis if lis else [_XMP_TAG_RE.sub("", m.group(1))])
    return ", ".join(unescape(v).strip() for v in vals if v and v.strip())


# Stock-image / template keyword boilerplate. Many PDFs are built around a stock
# cover graphic or template whose OWN keywords get written into the file's XMP
# dc:subject ("geometric", "polygonal", "backdrop", "futuristic", ...). They
# describe the artwork, never the document, so they make terrible tags. We detect
# the dump by a cluster of unmistakable visual-design markers - words that are
# essentially never a document's real topic - and, when found, drop the whole
# embedded keyword block: the rest of that same block ("technology", "network",
# ...) came from the same stock asset and is equally untrustworthy. A document
# that uses one such word in isolation is left alone (the cluster gate).
_STOCK_MARKERS = {
    "geometric", "polygon", "polygonal", "low-poly", "lowpoly", "wireframe",
    "halftone", "bokeh", "vignette", "backdrop", "copyspace", "copy space",
    "futuristic", "seamless", "tileable", "swirl", "swirls", "clipart",
    "clip art", "scrapbook", "hexagonal", "abstract background",
}
_STOCK_CLUSTER_MIN = 2


def _looks_like_stock_keywords(kws: list[str]) -> bool:
    """True if a parsed keyword list is a stock-image / template dump - a cluster
    of visual-design markers (`_STOCK_MARKERS`) no author uses as a topic tag."""
    hits = sum(1 for k in kws if k.strip().lower() in _STOCK_MARKERS)
    return hits >= _STOCK_CLUSTER_MIN


def extract_pdf_metadata(raw: bytes, pdf_text: str = "") -> dict:
    """Pull metadata from a PDF's info dict + a marking scan of its text."""
    info = {}
    xmp = ""
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        info = dict(doc.metadata or {})
        n_pages = doc.page_count
        xmp = _pdf_xmp_packet(doc)
        doc.close()
    except Exception:
        n_pages = None

    title = (info.get("title") or "").strip()
    # Marking: scan the first chunk of extracted text for known tokens.
    cands = {"pdfInfoSubject": (info.get("subject") or "").strip(),
             "bodyScan": "", "fallback": ""}
    if pdf_text:
        head = pdf_text[:1500].upper()
        for tok in _MARKING_TOKENS:
            if tok in head:
                cands["bodyScan"] = tok
                break
    # Report number: look for "Report No." style label in the text head.
    report_number = ""
    if pdf_text:
        m = re.search(r"(?:report|document|publication)\s*(?:no\.?|number|id)\s*[:#-]?\s*([\w./\-]+)",
                      pdf_text[:2000], re.IGNORECASE)
        if m:
            report_number = m.group(1)

    # Keywords: the legacy /Info /Keywords field first; if it's blank, fall back
    # to the XMP packet (pdf:Keywords / dc:subject), where modern producers
    # actually write them - otherwise XMP-only keywords are silently lost.
    kw_raw = (info.get("keywords") or "").strip() or _xmp_keywords(xmp)
    keywords = parse_keywords(kw_raw)
    # A stock-asset keyword dump describes the cover artwork, not the document;
    # drop it so it isn't auto-applied as tags.
    if _looks_like_stock_keywords(keywords):
        keywords = []

    return {
        "title": title,
        "url": "",
        "report_number": report_number,
        "date_published": (info.get("creationDate") or "").strip(),
        "page_count": n_pages,
        "marking_candidates": cands,
        "marking": select_marking({**cands, "metaSecurity": "", "classBanner": "",
                                    "metaClassification": "", "firstHeading": ""}),
        "metadata_extractor": "server-pdf",
        "pdf_author": (info.get("author") or "").strip(),
        # The PDF's authored abstract/description (Info /Subject). Not a tag in
        # itself, but high-signal topical prose the upload path folds into the
        # tag-suggestion context (see app._suggest_title / api_upload_preview).
        "subject": (info.get("subject") or "").strip(),
        # The PDF's own Keywords field, split into individual tags (stock-asset
        # dumps removed). These are authored tags, so the upload UI auto-applies
        # them (user can remove).
        "keywords": keywords,
    }


def parse_keywords(value: str) -> list:
    """Split a PDF/HTML keywords string into individual tag strings. Authors
    use commas, semicolons, or newlines as separators (occasionally a single
    keyword with internal spaces); we split on those, trim, drop blanks, and
    de-duplicate case-insensitively while preserving order."""
    if not value:
        return []
    parts = re.split(r"[;,\n]+", value)
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        k = p.lower()
        if p and k not in seen:
            seen.add(k)
            out.append(p)
    return out


# Heading-sized text that is structural boilerplate, not the document's topic:
# front-matter labels and journal section tags set in the same large font as a
# real title. Dropped from the heading context so they don't become title-
# weighted tag candidates ("Contents", "Executive Summary", "Viewpoint", ...).
_FRONTMATTER_HEADINGS = {
    "foreword", "preface", "contents", "table of contents", "executive summary",
    "summary", "abstract", "introduction", "background", "overview",
    "conclusion", "conclusions", "references", "bibliography", "appendix",
    "appendices", "index", "glossary", "notes", "endnotes", "acknowledgement",
    "acknowledgements", "acknowledgments", "disclaimer", "copyright",
    "article", "viewpoint", "editorial", "review", "commentary", "letter",
    "research article", "case study", "abbreviations", "acronyms",
}
# Leading enumeration ("1 ", "1. ", "1.2 ", "chapter 3 -", "part ii:").
_HEADING_NUM_RE = re.compile(
    r"^(?:(?:chapter|part|section|appendix)\s+)?[\divxlc]+[.):\-\s]+", re.IGNORECASE)
# Document-identifier lines that ride in the title font (arXiv ids, DOIs, ISBNs).
_HEADING_ID_RE = re.compile(r"^(arxiv:|doi:|isbn|issn)", re.IGNORECASE)


def _is_noise_heading(text: str) -> bool:
    """True if a heading-sized span is structural boilerplate rather than the
    document's actual title/topic (front-matter labels, 'List of ...' lines,
    bare enumerations, stray single characters, identifier lines)."""
    t = text.strip()
    if len(t) < 2:                       # stray single char (e.g. a dropped cap)
        return True
    if _HEADING_ID_RE.match(t):
        return True
    norm = _HEADING_NUM_RE.sub("", t).strip().lower().strip(".:-–— ")
    if not norm:                         # was pure enumeration ("1.", "II.")
        return True
    if norm.startswith("list of "):      # "List of Illustrations/Figures/Tables"
        return True
    return norm in _FRONTMATTER_HEADINGS


def extract_pdf_headings(raw: bytes, pdf_text: str = "", min_words: int = 100,
                         max_frac: float = 0.20, size_ratio: float = 1.3,
                         safety_cap: int = 3000, keep_words: int = 100) -> str:
    """Pull the visually-prominent (title/heading) text from a PDF's leading
    portion - useful as title-weighted tag context when the PDF has no usable
    metadata title (scanned/exported files often don't).

    The scan is bounded by WORDS, not pages: it reads the document in order
    and stops after `min(safety_cap, max(min_words, max_frac*total))` words.
    So it always covers at least ~`min_words` words, but up to `max_frac` of a
    longer document - the overflow deliberately reaches the executive-summary
    / table-of-contents region, whose headings are valuable. Within that
    window it keeps text set markedly larger than the body font (>=
    `size_ratio` x the modal body size); for single-font documents it falls
    back to bold and short ALL-CAPS lines. The kept heading text is truncated
    to `keep_words` words. Returns '' if PyMuPDF is unavailable, the PDF
    exposes no font info (e.g. a scan), or nothing qualifies."""
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:
        return ""

    total_words = len(pdf_text.split()) if pdf_text else 0
    budget = (min(safety_cap, max(min_words, round(max_frac * total_words)))
              if total_words else safety_cap)

    spans = []          # (size, text, bold, caps)
    seen_words = 0
    try:
        for page in doc:
            try:
                data = page.get_text("dict")
            except Exception:
                continue
            for block in data.get("blocks", []):
                if block.get("type", 0) != 0:        # 0 = text block
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = (span.get("text") or "").strip()
                        if not text:
                            continue
                        flags = int(span.get("flags") or 0)
                        spans.append((float(span.get("size") or 0), text,
                                      bool(flags & 16), text.isupper()))
                        seen_words += len(text.split())
            if seen_words >= budget:
                break
    finally:
        doc.close()
    if not spans:
        return ""

    # Modal body size, weighted by characters set in it.
    from collections import Counter
    sizes = Counter()
    for size, text, _b, _c in spans:
        sizes[round(size)] += len(text)
    body_size = sizes.most_common(1)[0][0] if sizes else 0

    head = [text for size, text, _b, _c in spans
            if body_size and size >= size_ratio * body_size]
    if sum(len(h.split()) for h in head) < 3:
        # Single-font document: size can't discriminate, so use bold / short
        # all-caps lines as the heading signal instead.
        head = [text for _s, text, bold, caps in spans
                if (bold or caps) and 0 < len(text.split()) <= 12]

    # Drop structural boilerplate (front-matter labels, section tags, stray
    # fragments) that shares the title font but isn't the document's topic.
    head = [h for h in head if not _is_noise_heading(h)]

    # Dedup (case-insensitive, order-preserving), then cap at keep_words words.
    seen, out, kept = set(), [], 0
    for h in head:
        k = h.lower()
        if k in seen:
            continue
        seen.add(k)
        words = h.split()
        if kept + len(words) > keep_words:
            out.append(" ".join(words[:keep_words - kept]))
            break
        out.append(h)
        kept += len(words)
    return "\n".join(x for x in out if x)
