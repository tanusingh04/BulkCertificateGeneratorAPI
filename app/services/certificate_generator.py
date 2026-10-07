from io import BytesIO
import os
from pathlib import Path
import uuid

import qrcode
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from templates.certificate_template import (
    BORDER_INNER_MARGIN,
    BORDER_INNER_WIDTH,
    BORDER_OUTER_MARGIN,
    BORDER_OUTER_WIDTH,
    COLOR_BORDER_BG,
    COLOR_PRIMARY,
    COLOR_SECONDARY,
    COLOR_TEXT_MAIN,
    COLOR_TEXT_MUTED,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    QR_SIZE,
    QR_X,
    QR_Y,
)


def _fit_font_size(c: canvas.Canvas, text: str, font_name: str, initial_size: int, max_width: float, min_size: int = 10) -> int:
    """Reduce font size until text fits within max_width."""
    size = initial_size
    while size > min_size and c.stringWidth(text, font_name, size) > max_width:
        size -= 1
    return size


def generate_certificate_pdf(
    recipient_name: str,
    certificate_title: str,
    course_name: str,
    issuer: str,
    issue_date: str,
    verification_code: str,
    verification_url: str,
    output_path: str | Path,
) -> str:
    """Generate a single landscape A4 certificate PDF with an authenticating QR code.

    Pure function: Runs in isolated child worker processes.
    Does NOT import SQLAlchemy, database sessions, or application settings.
    Writes to a temporary file and atomically renames it to guarantee crash safety.
    """
    dest_path = Path(output_path).resolve()
    dest_dir = dest_path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file in the same directory to allow atomic replacement
    temp_file = dest_dir / f".tmp_{uuid.uuid4().hex}.pdf"

    try:
        c = canvas.Canvas(str(temp_file), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
        c.setTitle(f"{certificate_title} - {recipient_name}")

        # 1. Subtle background fill
        c.setFillColor(COLOR_BORDER_BG)
        c.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)

        # 2. Outer and Inner Decorative Borders
        c.setStrokeColor(COLOR_PRIMARY)
        c.setLineWidth(BORDER_OUTER_WIDTH)
        c.rect(
            BORDER_OUTER_MARGIN,
            BORDER_OUTER_MARGIN,
            PAGE_WIDTH - 2 * BORDER_OUTER_MARGIN,
            PAGE_HEIGHT - 2 * BORDER_OUTER_MARGIN,
            fill=0,
            stroke=1,
        )

        c.setStrokeColor(COLOR_SECONDARY)
        c.setLineWidth(BORDER_INNER_WIDTH)
        c.rect(
            BORDER_INNER_MARGIN,
            BORDER_INNER_MARGIN,
            PAGE_WIDTH - 2 * BORDER_INNER_MARGIN,
            PAGE_HEIGHT - 2 * BORDER_INNER_MARGIN,
            fill=0,
            stroke=1,
        )

        center_x = PAGE_WIDTH / 2.0
        max_text_width = PAGE_WIDTH - (2 * BORDER_INNER_MARGIN) - 40

        # 3. Certificate Title
        title_text = certificate_title.upper()
        title_size = _fit_font_size(c, title_text, "Helvetica-Bold", 28, max_text_width, min_size=14)
        c.setFont("Helvetica-Bold", title_size)
        c.setFillColor(COLOR_PRIMARY)
        c.drawCentredString(center_x, PAGE_HEIGHT - 100, title_text)

        # 4. Attestation Line
        c.setFont("Helvetica", 15)
        c.setFillColor(COLOR_TEXT_MUTED)
        c.drawCentredString(center_x, PAGE_HEIGHT - 150, "This is to certify that")

        # 5. Recipient Name (Prominent & Centered)
        name_size = _fit_font_size(c, recipient_name, "Helvetica-Bold", 32, max_text_width, min_size=14)
        c.setFont("Helvetica-Bold", name_size)
        c.setFillColor(COLOR_TEXT_MAIN)
        c.drawCentredString(center_x, PAGE_HEIGHT - 215, recipient_name)

        # Name underline accent
        c.setStrokeColor(COLOR_SECONDARY)
        c.setLineWidth(1.5)
        name_width = min(c.stringWidth(recipient_name, "Helvetica-Bold", name_size) + 40, max_text_width)
        c.line(
            center_x - (name_width / 2.0),
            PAGE_HEIGHT - 228,
            center_x + (name_width / 2.0),
            PAGE_HEIGHT - 228,
        )

        # 6. Fulfillment Line
        c.setFont("Helvetica", 14)
        c.setFillColor(COLOR_TEXT_MUTED)
        c.drawCentredString(center_x, PAGE_HEIGHT - 275, "has successfully completed the program")

        # 7. Course / Event Name
        course_size = _fit_font_size(c, course_name, "Helvetica-Bold", 20, max_text_width, min_size=10)
        c.setFont("Helvetica-Bold", course_size)
        c.setFillColor(COLOR_PRIMARY)
        c.drawCentredString(center_x, PAGE_HEIGHT - 315, course_name)

        # 8. Left Metadata (Issuer & Date)
        meta_left_x = BORDER_INNER_MARGIN + 45
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(COLOR_TEXT_MAIN)
        c.drawString(meta_left_x, BORDER_OUTER_MARGIN + 80, f"Issued by: {issuer}")

        c.setFont("Helvetica", 11)
        c.setFillColor(COLOR_TEXT_MUTED)
        c.drawString(meta_left_x, BORDER_OUTER_MARGIN + 55, f"Date: {issue_date}")

        # 9. Right Metadata (QR Code & Verification Code)
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1,
        )
        qr.add_data(verification_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        qr_buffer = BytesIO()
        qr_img.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)
        qr_reader = ImageReader(qr_buffer)

        # Draw QR Code image
        c.drawImage(
            qr_reader,
            QR_X,
            QR_Y,
            width=QR_SIZE,
            height=QR_SIZE,
            mask="auto",
        )

        # Verification code text under QR code
        c.setFont("Courier-Bold", 8)
        c.setFillColor(COLOR_TEXT_MAIN)
        code_text = f"ID: {verification_code}"
        c.drawCentredString(QR_X + (QR_SIZE / 2.0), QR_Y - 14, code_text)

        # Finalize page and write file
        c.showPage()
        c.save()

        # Atomic rename guarantees no partially-written files are seen
        temp_file.replace(dest_path)
        return str(dest_path)

    except Exception:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise
