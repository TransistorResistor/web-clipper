"""Customize how imported PDFs get their initial raw marking."""


def extract_pdf_marking(title: str, text_head: str, filename: str) -> str:
    """Return a raw marking string for this PDF, or "" to leave it blank.

    The title is taken from the PDF metadata, with the filename as a fallback.
    filename is the uploaded file name with any directory path removed.
    text_head contains up to 1,500 characters of extracted text in its
    original case. It may be empty for image-only PDFs. Use the delimiters
    configured in markings.json when returning multiple markings.
    """
    return ""
