
import os
import sys
import math
import base64
import hashlib

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

INPUT_ZIP = "backup.zip"
OUTPUT_PREFIX = "archive_part"

FONT_NAME = "Courier"
FONT_SIZE = 8

LINE_LENGTH = 80

TARGET_LINES_PER_PDF = 40000


def sha256_file(filename):
    h = hashlib.sha256()

    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def create_pdf(
        filename,
        file_name,
        part_num,
        total_parts,
        sha256_hash,
        lines):

    c = canvas.Canvas(filename, pagesize=A4)

    width, height = A4

    left = 30
    top = height - 30

    c.setFont(FONT_NAME, FONT_SIZE)

    y = top

    headers = [
        f"FILE: {file_name}",
        f"PART: {part_num}",
        f"TOTAL_PARTS: {total_parts}",
        f"SHA256: {sha256_hash}",
        ""
    ]

    for line in headers:
        c.drawString(left, y, line)
        y -= FONT_SIZE + 2

    for line in lines:

        if y < 30:
            c.showPage()
            c.setFont(FONT_NAME, FONT_SIZE)
            y = top

        c.drawString(left, y, line)
        y -= FONT_SIZE + 2

    c.save()


def main():

    input_zip = sys.argv[1] if len(sys.argv) > 1 else INPUT_ZIP

    with open(input_zip, "rb") as f:
        binary = f.read()

    sha256_hash = hashlib.sha256(binary).hexdigest()

    encoded = base64.b64encode(binary).decode("ascii")

    lines = [
        encoded[i:i + LINE_LENGTH]
        for i in range(0, len(encoded), LINE_LENGTH)
    ]

    total_parts = math.ceil(
        len(lines) / TARGET_LINES_PER_PDF
    )

    print("Source ZIP:", input_zip)
    print("SHA256:", sha256_hash)
    print("Parts:", total_parts)

    for i in range(total_parts):

        start = i * TARGET_LINES_PER_PDF
        end = start + TARGET_LINES_PER_PDF

        pdf_lines = lines[start:end]

        pdf_name = f"{OUTPUT_PREFIX}_{i+1:03d}.pdf"

        print("Creating", pdf_name)

        create_pdf(
            pdf_name,
            os.path.basename(input_zip),
            i + 1,
            total_parts,
            sha256_hash,
            pdf_lines
        )

    print("Complete")


if __name__ == "__main__":
    main()