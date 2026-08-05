"""CSV + PDF rendering for report and invoice exports.

Deliberately dumb: it takes already-computed headers and rows and turns them
into bytes. All the querying stays in the report/enrollment routers so there
is exactly one source of truth for what a "sales report" means — an export
can never drift from what the chart on screen shows.

PDF uses reportlab (pure-Python wheels, no cairo/pango system libraries), so
it works unchanged on a serverless host.
"""
import csv
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BRAND = colors.HexColor("#376EF4")
INK = colors.HexColor("#071123")
MUTED = colors.HexColor("#596475")
LINE = colors.HexColor("#E0E5EB")
ZEBRA = colors.HexColor("#F7FBFD")


def to_csv(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    # utf-8-sig: Excel on Windows misreads plain UTF-8 CSV as ANSI and
    # mangles any non-ASCII name, so the BOM is what makes it open correctly.
    return buf.getvalue().encode("utf-8-sig")


def to_pdf(title: str, subtitle: str, headers: list[str], rows: list[list]) -> bytes:
    buf = io.BytesIO()
    # Landscape: these tables are wide (invoices run to 7 columns) and would
    # otherwise wrap into unreadable slivers.
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=title, author="Fresh Breath Therapy",
    )

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontSize=17, leading=21,
                        textColor=INK, spaceAfter=2, alignment=TA_LEFT)
    sub = ParagraphStyle("sub", parent=base["Normal"], fontSize=9, leading=12, textColor=MUTED)
    cell = ParagraphStyle("cell", parent=base["Normal"], fontSize=8.5, leading=11, textColor=INK)

    story = [
        Paragraph(title, h1),
        Paragraph(subtitle, sub),
        Spacer(1, 8),
    ]

    if not rows:
        story.append(Paragraph("No data for the selected filters.", sub))
    else:
        # Wrap every cell in a Paragraph so long client/package names wrap
        # inside the column instead of overflowing the page.
        data = [[Paragraph(f"<b>{h}</b>", cell) for h in headers]]
        data += [[Paragraph("" if v is None else str(v), cell) for v in row] for row in rows]

        table = Table(data, repeatRows=1, hAlign="LEFT")
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]
        for i in range(1, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
        table.setStyle(TableStyle(style))
        story.append(table)

    story += [
        Spacer(1, 10),
        Paragraph(
            f"Generated {date.today().isoformat()} — Fresh Breath Therapy dashboard", sub
        ),
    ]

    # repeatRows=1 above makes the header row reprint on every page.
    doc.build(story)
    return buf.getvalue()


def filename_for(slug: str, fmt: str) -> str:
    return f"{slug}-{date.today().isoformat()}.{fmt}"


MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "pdf": "application/pdf"}
