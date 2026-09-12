"""
backend/app/student_admit_card/render.py

Admit-card DOCUMENT renderers (presentation only).

Two renderers produce the same formal Cluster University examination
admit-card document from the SAME assembled data dict (see
service.student_card_document):

  render_admit_card_document_html(data)  -> standalone HTML document
                                            (in-chat "View" + preview)
  render_admit_card_pdf(data)            -> one-page A4 portrait PDF bytes
                                            (Download / Print)

Both renderers work from allow-listed display values only. DOB, passwords,
hashes, session tokens and cookies are never rendered. The university brand
is fixed to "CLUSTER UNIVERSITY SRINAGAR" — this renderer is the sole source
for the admit-card document branding.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_UNIVERSITY = "CLUSTER UNIVERSITY SRINAGAR"


def _now() -> str:
    """Local print timestamp, e.g. 09-09-2026 11:05."""
    return datetime.now().strftime("%d-%m-%Y %H:%M")


def _candidate_detail(value: str | None) -> str:
    return (value or "").strip() or ""


def _render_html_value(value: str) -> str:
    return escape(value)


def render_admit_card_document_html(data: dict[str, Any]) -> str:
    """Standalone HTML document mirroring the admit-card reference layout.

    Values are HTML-escaped; this is a printable/embeddable document, not a
    styled chat card. No URLs, ids, tokens or credentials are embedded.
    """
    e = escape

    def _td(label: str, value: str) -> str:
        return f"<tr><td class='lbl'>{e(label)}</td><td class='val'>{e(value or '—')}</td></tr>"

    semester = str(data.get("semester") or "")
    exam_form_no = _candidate_detail(data.get("reg_no"))
    exam_roll_no = _candidate_detail(data.get("exam_roll_no"))
    subjects = data.get("subjects") or []
    instructions = data.get("instructions") or []
    centre_name = _candidate_detail(data.get("centre_name"))
    centre_address = _candidate_detail(data.get("centre_address"))
    centre_code = _candidate_detail(data.get("centre_code"))
    exam_type = _candidate_detail(data.get("exam_type")) or "Regular"
    academic_year = _candidate_detail(data.get("academic_year"))
    exam_session = _candidate_detail(data.get("exam_session"))
    name = _candidate_detail(data.get("name"))
    gender = _candidate_detail(data.get("gender"))
    father_name = _candidate_detail(data.get("father_name"))
    mobile = _candidate_detail(data.get("mobile"))
    batch = _candidate_detail(data.get("batch"))
    programme = _candidate_detail(data.get("programme"))
    printed_on = _now()

    subject_rows = "".join(
        f"<tr><td class='num'>{i}</td><td class='val'>{e(str(s))}</td></tr>"
        for i, s in enumerate(subjects, start=1)
    )
    instruction_rows = "".join(
        f"<tr><td class='num'>{i}</td><td class='val'>{e(str(x))}</td></tr>"
        for i, x in enumerate(instructions, start=1)
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Admit Card - Semester {e(semester)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  @page {{ size: A4 portrait; margin: 12mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; color: #17202a; background: #ffffff; margin: 0; }}
  .doc {{ max-width: 760px; margin: 0 auto; padding: 14px 18px; }}
  .org {{ text-align: center; font-size: 17px; letter-spacing: 2px; color: #143a5c; font-weight: 700; margin: 2px 0 0; }}
  .exam {{ text-align: center; font-size: 13px; letter-spacing: 2px; color: #17202a; margin: 6px 0 2px; }}
  .title {{ text-align: center; font-size: 20px; font-weight: 700; color: #0f2c49; margin: 0 0 4px; }}
  .demo {{ text-align: center; font-size: 9px; color: #7a8494; letter-spacing: 1px; margin: 0 0 6px; }}
  .rule {{ border-bottom: 3px double #143a5c; margin: 4px 0 10px; }}
  h2 {{ font-size: 12px; color: #0f2c49; background: #f3f6fa; border: 1px solid #d5dbe3; padding: 5px 10px; margin: 12px 0 6px; letter-spacing: 1px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  td, th {{ padding: 5px 8px; border: 1px solid #c7ced7; vertical-align: top; }}
  .meta td {{ padding: 6px 10px; }}
  .meta td.lbl {{ width: 34%; font-weight: 600; background: #f3f6fa; }}
  .meta td.num {{ width: 8%; text-align: center; font-weight: 600; background: #f3f6fa; }}
  .meta .lbl.subj {{ width: 14%; }}
  .center-line {{ border: 1px solid #c7ced7; padding: 6px 10px; font-size: 12px; }}
  .center-line b {{ color: #0f2c49; }}
  .foot {{ margin-top: 22px; font-size: 11px; color: #33404f; }}
  .foot-row {{ display: flex; justify-content: space-between; margin-top: 4px; }}
  .sig {{ margin-top: 34px; border-top: 1px dotted #5a6b7c; width: 220px; padding-top: 2px; font-size: 10px; text-align: center; }}
  .cell-detail {{ width: 100%; }}
</style></head><body>
<div class="doc">
  <p class="org">{_render_html_value(_UNIVERSITY)}</p>
  <p class="exam">SEMESTER {e(semester)} EXAMINATION</p>
  <p class="title">ADMIT CARD</p>
  <p class="demo">DEMO</p>
  <div class="rule"></div>

  <h2>IDENTIFICATION</h2>
  <table class="meta">
    <tr><td class="lbl">Exam Form No.</td><td class="val">{e(exam_form_no)}</td></tr>
    <tr><td class="lbl">Exam Roll No.</td><td class="val">{e(exam_roll_no)}</td></tr>
    <tr><td class="lbl">Printed On</td><td class="val">{e(printed_on)}</td></tr>
  </table>

  <h2>CANDIDATE DETAILS</h2>
  <table class="meta">
    <tr><td class="lbl">CUS Registration No.</td><td class="val">{e(data.get('reg_no') or '—')}</td></tr>
    <tr><td class="lbl">Name</td><td class="val">{e(name)}</td></tr>
    <tr><td class="lbl">Gender</td><td class="val">{e(gender)}</td></tr>
    <tr><td class="lbl">Father's Name</td><td class="val">{e(father_name or '')}</td></tr>
    <tr><td class="lbl">Mobile</td><td class="val">{e(mobile or '')}</td></tr>
    <tr><td class="lbl">Batch</td><td class="val">{e(batch)}</td></tr>
    <tr><td class="lbl">Course</td><td class="val">{e(programme)}</td></tr>
    <tr><td class="lbl">Combination Code</td><td class="val">—</td></tr>
  </table>

  <h2>SUBJECTS / PAPER TITLES</h2>
  <table class="meta">
    <tr><th class="num">#</th><th>Paper Title</th></tr>
    {subject_rows}
  </table>

  <h2>EXAMINATION CENTER</h2>
  <div class="center-line">
    <div><b>Center Name &amp; Address:</b> {e(centre_name)}{(' — ' + e(centre_address)) if centre_address else ''}</div>
    <div style="margin-top:6px"><b>Center Number:</b> {e(centre_code)}</div>
  </div>

  <h2>EXAMINATION INFORMATION</h2>
  <table class="meta">
    <tr><td class="lbl">Semester</td><td class="val">{e(semester)}</td></tr>
    <tr><td class="lbl">Exam Type</td><td class="val">{e(exam_type)}</td></tr>
    <tr><td class="lbl">Academic Year</td><td class="val">{e(academic_year)}</td></tr>
    <tr><td class="lbl">Exam Session</td><td class="val">{e(exam_session)}</td></tr>
  </table>

  <h2>IMPORTANT</h2>
  <table class="meta">
    {instruction_rows}
  </table>

  <div class="foot">
    <div class="foot-row">
      <span>Printed On: {e(printed_on)}</span>
      <span>Developed by I.T Cell</span>
    </div>
    <div class="foot-row" style="justify-content:flex-end; margin-top:2px;">
      <div>
        <div class="sig">Signature of the Applicant</div>
      </div>
    </div>
  </div>
</div></body></html>"""  # noqa: E501


# --------------------------------------------------------------------------- #
# ReportLab PDF — one-page A4 portrait university document
# --------------------------------------------------------------------------- #
_A4_W = A4[0]
_A4_H = A4[1]

_PT_M = 11 * mm


def render_admit_card_pdf(data: dict[str, Any]) -> bytes:
    """One-page A4 portrait admit-card PDF (Download / Print).

    Built with the project's existing PDF framework (reportlab/platypus).
    Compact spacing keeps the whole document on exactly one page; single-page
    is asserted by callers/tests via the emitted byte length/PDF reader.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=_PT_M,
        rightMargin=_PT_M,
        topMargin=_PT_M,
        bottomMargin=_PT_M,
    )

    styles = {
        "org": ParagraphStyle("org", fontName="Helvetica-Bold", fontSize=16, leading=18, alignment=TA_CENTER, textColor=colors.HexColor("#143A5C")),
        "exam": ParagraphStyle("exam", fontName="Helvetica-Bold", fontSize=12, leading=14, alignment=TA_CENTER, textColor=colors.HexColor("#17202A"), spaceBefore=2),
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=18, leading=20, alignment=TA_CENTER, textColor=colors.HexColor("#0F2C49"), spaceBefore=2),
        "demo": ParagraphStyle("demo", fontName="Helvetica", fontSize=8, leading=10, alignment=TA_CENTER, textColor=colors.HexColor("#7A8494"), spaceBefore=1),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=colors.HexColor("#0F2C49"), spaceBefore=8, spaceAfter=3),
        "lbl": ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=9, leading=11),
        "val": ParagraphStyle("val", fontName="Helvetica", fontSize=9, leading=11),
        "num": ParagraphStyle("num", fontName="Helvetica", fontSize=9, leading=11, alignment=TA_CENTER),
        "foot": ParagraphStyle("foot", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#33404F")),
    }

    def p(text: str, style: str) -> Paragraph:
        return Paragraph(str(text or ""), styles[style])

    def field_row(label: str, value: str) -> list[Paragraph]:
        return [p(label, "lbl"), p((value or "").strip() or "—", "val")]

    def box(rows: list[list], widths: tuple | None = None, style_ops: list | None = None, col_widths: list | None = None) -> Table:
        t = Table(rows, colWidths=col_widths)
        merged = [
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C7CED7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]
        if style_ops:
            merged.extend(style_ops)
        t.setStyle(TableStyle(merged))
        return t

    story: list = []
    story.append(p(_UNIVERSITY, "org"))
    story.append(p(f"SEMESTER {data.get('semester') or ''} EXAMINATION", "exam"))
    story.append(p("ADMIT CARD", "title"))
    story.append(p("DEMO", "demo"))

    # Identification
    story.append(p("IDENTIFICATION", "h2"))
    ident_rows = [field_row("Exam Form No.", str(data.get("reg_no") or "")),
                  field_row("Exam Roll No.", str(data.get("exam_roll_no") or "")),
                  field_row("Printed On", _now())]
    story.append(box(ident_rows, col_widths=[120, _A4_W - 2 * _PT_M - 120]))

    # Candidate details
    story.append(p("CANDIDATE DETAILS", "h2"))
    cand_rows = [
        field_row("CUS Registration No.", str(data.get("reg_no") or "")),
        field_row("Name", str(data.get("name") or "")),
        field_row("Gender", str(data.get("gender") or "")),
        [p("Father's Name", "lbl"), p(str(data.get("father_name") or "").strip() or "", "val")],
        [p("Mobile", "lbl"), p(str(data.get("mobile") or "").strip() or "", "val")],
        field_row("Batch", str(data.get("batch") or "")),
        field_row("Course", str(data.get("programme") or "")),
        [p("Combination Code", "lbl"), p("", "val")],
    ]
    col_w = [0.30 * (_A4_W - 2 * _PT_M), 0.70 * (_A4_W - 2 * _PT_M)]
    story.append(box(cand_rows, col_widths=col_w))

    # Subjects
    story.append(p("SUBJECTS / PAPER TITLES", "h2"))
    subj_rows = [[p("Sr. No.", "lbl"), p("Paper Title", "lbl")]]
    for i, s in enumerate(list(data.get("subjects") or []), start=1):
        subj_rows.append([p(str(i), "num"), p(str(s), "val")])
    story.append(box(subj_rows, col_widths=[0.10 * (_A4_W - 2 * _PT_M), 0.90 * (_A4_W - 2 * _PT_M)], style_ops=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F6FA"))]))

    # Examination Center (bordered block, not a grid)
    story.append(p("EXAMINATION CENTER", "h2"))
    centre_text = str(data.get("centre_name") or "")
    if str(data.get("centre_address") or "").strip():
        centre_text = f"{centre_text} — {data.get('centre_address')}"
    full = _A4_W - 2 * _PT_M
    story.append(box([
        [p(f"Center Name & Address: {centre_text}", "val")],
        [p(f"Center Number: {data.get('centre_code') or ''}", "val")],
    ], col_widths=[full]))

    # Examination information
    story.append(p("EXAMINATION INFORMATION", "h2"))
    info_rows = [
        field_row("Semester", str(data.get("semester") or "")),
        field_row("Exam Type", str(data.get("exam_type") or "Regular")),
        field_row("Academic Year", str(data.get("academic_year") or "")),
        field_row("Exam Session", str(data.get("exam_session") or "")),
    ]
    story.append(box(info_rows, col_widths=[120, _A4_W - 2 * _PT_M - 120]))

    # Important
    story.append(p("IMPORTANT", "h2"))
    inst_rows = [[p("Sr. No.", "lbl"), p("Instruction", "lbl")]]
    for i, ins in enumerate(list(data.get("instructions") or []), start=1):
        inst_rows.append([p(str(i), "num"), p(str(ins), "val")])
    story.append(box(inst_rows, col_widths=[0.10 * (_A4_W - 2 * _PT_M), 0.90 * (_A4_W - 2 * _PT_M)], style_ops=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F6FA"))]))

    # Footer
    story.append(Spacer(1, 10))
    story.append(p(f"Printed On: {_now()}                       Developed by I.T Cell", "foot"))
    story.append(Spacer(1, 26))
    story.append(box([[p("Signature of the Applicant", "val")]], col_widths=[0.55 * full], style_ops=[
        ("LINEABOVE", (0, 0), (0, 0), 0.5, colors.HexColor("#5A6B7C")),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))

    doc.build(story)
    return buf.getvalue()