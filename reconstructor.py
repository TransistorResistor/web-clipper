 
import os
import re
import glob
import base64
import hashlib

from pypdf import PdfReader

PDF_PATTERN = "archive_part_*.pdf"

BASE64_RE = re.compile(r'^[A-Za-z0-9+/=]+$')


def extract_pdf_data(pdf_file):

    reader = PdfReader(pdf_file)

    metadata = {
        "file": None,
        "part": None,
        "total": None,
        "sha256": None
    }

    payload = []
    dropped = []

    for page_no, page in enumerate(reader.pages, start=1):

        text = page.extract_text()

        if not text:
            continue

        for line in text.splitlines():

            line = line.strip()

            if not line:
                continue

            if line.startswith("FILE:"):
                metadata["file"] = line[5:].strip()
                continue

            if line.startswith("PART:"):
                metadata["part"] = int(line[5:].strip())
                continue

            if line.startswith("TOTAL_PARTS:"):
                metadata["total"] = int(line[12:].strip())
                continue

            if line.startswith("SHA256:"):
                metadata["sha256"] = line[7:].strip()
                continue

            if BASE64_RE.match(line):
                payload.append(line)
            else:
                # A non-empty line that is neither a header nor valid
                # base64 means text extraction mangled the payload
                # (injected whitespace, split a token, etc.). Record it
                # so we can report exactly where rather than only failing
                # the final whole-file SHA256 with no location.
                dropped.append((page_no, line))

    return metadata, ''.join(payload), dropped


def main():

    files = sorted(glob.glob(PDF_PATTERN))

    if not files:
        raise Exception("No PDF files found")

    parts = []
    total_dropped = 0

    expected_sha = None
    expected_total = None
    original_name = None

    for pdf in files:

        print("Reading", pdf)

        metadata, payload, dropped = extract_pdf_data(pdf)

        if dropped:
            total_dropped += len(dropped)
            page_no, sample = dropped[0]
            print(
                f"  WARNING: {len(dropped)} unparseable line(s) in {pdf} "
                f"(first on page {page_no}): {sample[:60]!r}"
            )

        if metadata["part"] is None:
            raise Exception(
                f"Missing PART metadata in {pdf}"
            )

        if expected_sha is None:
            expected_sha = metadata["sha256"]
            expected_total = metadata["total"]
            original_name = metadata["file"]

        if metadata["sha256"] != expected_sha:
            raise Exception(
                f"SHA mismatch in metadata: {pdf}"
            )

        parts.append(
            (
                metadata["part"],
                payload
            )
        )

    parts.sort(key=lambda x: x[0])

    if len(parts) != expected_total:
        raise Exception(
            f"Expected {expected_total} parts "
            f"but found {len(parts)}"
        )

    for index, (part_no, _) in enumerate(parts, start=1):

        if part_no != index:
            raise Exception(
                f"Missing part {index}"
            )

    print("All parts verified")

    if total_dropped:
        print(
            f"NOTE: {total_dropped} line(s) could not be parsed as base64 "
            f"and were excluded; the SHA256 check below will reveal whether "
            f"payload data was lost."
        )

    base64_data = ''.join(
        payload for _, payload in parts
    )

    print("Decoding Base64")

    binary = base64.b64decode(base64_data)

    rebuilt_sha = hashlib.sha256(binary).hexdigest()

    print("Expected SHA :", expected_sha)
    print("Rebuilt SHA  :", rebuilt_sha)

    if rebuilt_sha != expected_sha:
        hint = (
            f" ({total_dropped} unparseable line(s) were dropped during "
            f"extraction — see WARNING(s) above)"
            if total_dropped else ""
        )
        raise Exception(
            "Integrity check FAILED" + hint
        )

    output_name = f"RESTORED_{original_name}"

    with open(output_name, "wb") as f:
        f.write(binary)

    print()
    print("SUCCESS")
    print("Output:", output_name)
    print("SHA256 verified")


if __name__ == "__main__":
    main()