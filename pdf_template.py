"""
TEN Capital — Pitch Deck Analysis Report (PDF)
===============================================

A PDF rendering of the same `ReportData` contract that `report_template.py`
turns into a .docx — same content framework (title page, section-by-section
assessment, design recommendations, revised outline), same brand palette, but
built natively with reportlab so the two formats can be generated
independently and downloaded side by side.

Usage
-----
    from pdf_template import build_report_pdf
    build_report_pdf(Path("out.pdf"), data)
"""

from __future__ import annotations

import functools
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from report_template import (
    ACCENT_STRIP,
    FOOTER_LOGO,
    GREY,
    NAVY,
    TEAL,
    ZEBRA_FILL,
)

PAGE_MARGIN = 0.75 * inch
ACCENT_HEIGHT = 5
FOOTER_Y = 0.4 * inch


def _hex(value: str) -> colors.Color:
    return colors.HexColor(f"#{value}")


def _find_logo() -> Path | None:
    here = Path(__file__).parent
    for candidate in (here / FOOTER_LOGO, here.parent / FOOTER_LOGO):
        if candidate.exists():
            return candidate
    return None


LOGO_PATH = _find_logo()

# --- styles ----------------------------------------------------------------

TITLE_STYLE = ParagraphStyle(
    "Title", fontName="Helvetica-Bold", fontSize=20, leading=24,
    textColor=_hex(NAVY), alignment=TA_CENTER, spaceAfter=8,
)
SUBTITLE_STYLE = ParagraphStyle(
    "Subtitle", fontName="Helvetica", fontSize=12.5, leading=16,
    textColor=_hex(TEAL), alignment=TA_CENTER, spaceAfter=6,
)
META_STYLE = ParagraphStyle(
    "Meta", fontName="Helvetica", fontSize=9.5, leading=13,
    textColor=_hex(GREY), alignment=TA_CENTER, spaceAfter=4,
)
SECTION_STYLE = ParagraphStyle(
    "Section", fontName="Helvetica-Bold", fontSize=14, leading=17,
    textColor=_hex(NAVY), spaceBefore=14, spaceAfter=8,
)
SUBSECTION_STYLE = ParagraphStyle(
    "Subsection", fontName="Helvetica-Bold", fontSize=11, leading=14,
    textColor=_hex(TEAL), spaceBefore=12, spaceAfter=6,
)
BODY_STYLE = ParagraphStyle(
    "Body", fontName="Helvetica", fontSize=9.5, leading=13,
    textColor=colors.HexColor("#1A1A1A"), spaceAfter=6,
)
CELL_STYLE = ParagraphStyle(
    "Cell", fontName="Helvetica", fontSize=8.5, leading=11,
    textColor=colors.HexColor("#1A1A1A"),
)
CELL_BOLD_STYLE = ParagraphStyle("CellBold", parent=CELL_STYLE, fontName="Helvetica-Bold")
HEADER_CELL_STYLE = ParagraphStyle(
    "HeaderCell", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white,
)


# --- table helpers -----------------------------------------------------------


def _cell(value, *, bold: bool = False) -> Paragraph:
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(text, CELL_BOLD_STYLE if bold else CELL_STYLE)


def _table(headers, rows, col_widths, *, bold_first_col: bool = False) -> Table:
    data = [[Paragraph(h, HEADER_CELL_STYLE) for h in headers]]
    for row in rows:
        data.append([_cell(value, bold=(bold_first_col and i == 0)) for i, value in enumerate(row)])

    table = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _hex(NAVY)),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C7CFDA")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data)):
        if (i - 1) % 2 == 1:
            style.append(("BACKGROUND", (0, i), (-1, i), _hex(ZEBRA_FILL)))
    table.setStyle(TableStyle(style))
    return table


# --- page furniture: accent strip, footer, page numbering --------------------


class _BrandedCanvas(pdfcanvas.Canvas):
    """Buffers every page so the footer can show 'Page N of TOTAL', and draws
    the coral/amber/teal accent strip plus branded footer on each one."""

    def __init__(self, *args, compiled_on: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._compiled_on = compiled_on
        self._saved_page_states: list[dict] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_furniture(total)
            super().showPage()
        super().save()

    def _draw_furniture(self, total_pages: int) -> None:
        width, _ = self._pagesize

        segment = width / len(ACCENT_STRIP)
        for i, color_hex in enumerate(ACCENT_STRIP):
            self.setFillColor(_hex(color_hex))
            self.rect(i * segment, self._pagesize[1] - ACCENT_HEIGHT, segment, ACCENT_HEIGHT, stroke=0, fill=1)

        self.setFont("Helvetica", 7.5)
        self.setFillColor(_hex(GREY))
        self.drawCentredString(width / 2, FOOTER_Y, f"Page {self.getPageNumber()} of {total_pages}")
        self.drawRightString(width - PAGE_MARGIN, FOOTER_Y, f"Compiled {self._compiled_on}")

        if LOGO_PATH is not None:
            logo_width = 0.62 * inch
            self.drawImage(
                str(LOGO_PATH),
                PAGE_MARGIN,
                FOOTER_Y - 0.06 * inch,
                width=logo_width,
                height=0.24 * inch,
                mask="auto",
                preserveAspectRatio=True,
            )


# --- section builders ---------------------------------------------------------


def _header_block(data: dict) -> list:
    """Compact header — company, document title, source, date — sitting directly
    atop Section 1 rather than on its own mostly-blank cover page."""
    return [
        Spacer(1, 0.15 * inch),
        Paragraph(data["company_name"].upper(), TITLE_STYLE),
        Paragraph(data["document_title"], SUBTITLE_STYLE),
        Paragraph(f"Source: {data['source']}", META_STYLE),
        Paragraph(f"{data['date']}  |  Prepared by TEN Capital Network", META_STYLE),
        Spacer(1, 6),
    ]


def _section_1_deck_analysis(data: dict, usable_width: float) -> list:
    flow: list = [
        Paragraph("SECTION 1 — PITCH DECK ANALYSIS: STRENGTHS, WEAKNESSES &amp; RECOMMENDATIONS", SECTION_STYLE),
        Paragraph(
            "The following analysis applies the deck review framework: a section-by-section "
            "assessment of what is working, what is hurting investor perception, and specific "
            "actionable improvements.",
            BODY_STYLE,
        ),
        Paragraph("1.1  Section-by-Section Assessment", SUBSECTION_STYLE),
        _table(
            ["Section", "Strengths", "Weaknesses / Gaps", "Recommendations"],
            [
                (row["section"], row["strengths"], row["weaknesses"], row["recommendations"])
                for row in data["section_assessment"]
            ],
            [w * usable_width for w in (0.16, 0.28, 0.28, 0.28)],
            bold_first_col=True,
        ),
        Spacer(1, 10),
        Paragraph("1.2  Formatting, Storytelling &amp; Design Recommendations", SUBSECTION_STYLE),
    ]
    for item in data["design_recommendations"]:
        flow.append(
            Paragraph(f"<b>{item['label']}:</b> {item['text']}", BODY_STYLE)
        )

    flow.append(Paragraph("1.3  Proposed Revised Slide Outline", SUBSECTION_STYLE))
    flow.append(
        _table(
            ["Slide", "Content"],
            [(row["slide"], row["content"]) for row in data["revised_outline"]],
            [0.1 * usable_width, 0.9 * usable_width],
        )
    )
    return flow


# --- entry point ---------------------------------------------------------------


def build_report_pdf(output_path: Path | str, data: dict) -> Path:
    """Render `data` into the branded TEN Capital pitch deck analysis PDF."""
    output_path = Path(output_path)
    usable_width = LETTER[0] - 2 * PAGE_MARGIN

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        title=f"{data['company_name']} — {data['document_title']}",
    )

    story = _header_block(data) + _section_1_deck_analysis(data, usable_width)

    canvasmaker = functools.partial(_BrandedCanvas, compiled_on=data["date"])
    doc.build(story, canvasmaker=canvasmaker)
    return output_path


if __name__ == "__main__":
    # Emit the empty skeleton so the layout can be inspected without an API call.
    from report_template import blank_report_data

    out = build_report_pdf(Path(__file__).with_name("_template_preview.pdf"), blank_report_data())
    print(f"Wrote template preview -> {out}")
