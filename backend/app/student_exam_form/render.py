"""
backend/app/student_exam_form/render.py

Exam-form DOCUMENT renderers (presentation only).

Two renderers produce the same formal Cluster University examination form
document from the SAME allow-listed display dict (see service.student_form_document):

  render_exam_form_document_html(data)  -> standalone HTML document
                                           (in-chat "View" + preview)
  render_exam_form_pdf(data)            -> one-page A4 portrait PDF bytes
                                           (Download / Print)

Only display values are rendered (name, reg no, roll no, programme, college,
semester, exam type, academic year, session, subjects, fee, transaction ref,
payment date). DOB, credentials, session tokens and cookies are never rendered.
The university brand is fixed to "CLUSTER UNIVERSITY SRINAGAR" — this renderer
is the sole source for the exam-form document branding.
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


def _payment_datetime(value: Any) -> str:
    """Display the server-recorded payment date/time readably (e.g. 10-Sep-2026 19:32)."""
    raw = _val(value)
    if not raw:
        return ""
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return raw
    return dt.strftime("%d-%b-%Y %H:%M")


def _val(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    return str(value).strip()


def _render_html_value(value: str) -> str:
    return escape(value)


def render_exam_form_document_html(data: dict[str, Any]) -> str:
    """Standalone HTML document mirroring the exam-form reference layout.

    Values are HTML-escaped; this is a printable/embeddable document, never a
    styled chat card. No URLs, ids, tokens or credentials are embedded.
    """
    e = escape
    semester = str(data.get("semester") or "")
    form_no = _val(data.get("form_no"))
    exam_roll_no = _val(data.get("exam_roll_no"))
    subjects = data.get("subjects") or []
    name = _val(data.get("name"))
    reg_no = _val(data.get("reg_no"))
    college = _val(data.get("college"))
    programme = _val(data.get("programme"))
    batch = _val(data.get("batch"))
    exam_type = _val(data.get("exam_type")) or "Regular"
    academic_year = _val(data.get("academic_year"))
    session_code = _val(data.get("session_code"))
    session_name = _val(data.get("session_name"))
    fee_normal = _val(data.get("fee_normal"))
    fee_late = _val(data.get("fee_late"))
    fee_total = _val(data.get("fee_total"))
    txn_ref = _val(data.get("transaction_id"))
    payment_date = _val(data.get("payment_date"))
    fee_status = _val(data.get("fee_status")) or "Unpaid"
    photo_path = _val(data.get("photo_path"))
    status = _val(data.get("form_status")) or "Pending"
    submission_date = _val(data.get("submission_date"))
    printed_on = _now()

    subject_rows = "".join(
        f"<tr><td class='num'>{i}</td><td class='val'>{e(str(s))}</td></tr>"
        for i, s in enumerate(subjects, start=1)
    )

    fee_rows = (
        f"<tr><td class='lbl'>Base Fee</td><td class='val'>{e(fee_normal or '—')}</td></tr>"
        f"<tr><td class='lbl'>Late Fee</td><td class='val'>{e(fee_late or '—')}</td></tr>"
        f"<tr><td class='lbl'>Total Payable</td><td class='val'>{e(fee_total or '—')}</td></tr>"
        f"<tr><td class='lbl'>Payment Status</td><td class='val'>{e(fee_status)}</td></tr>"
        f"<tr><td class='lbl'>Transaction Reference</td><td class='val'>{e(txn_ref or '—')}</td></tr>"
        f"<tr><td class='lbl'>Payment Date</td><td class='val'>{e(_payment_datetime(payment_date) or '—')}</td></tr>"
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Exam Form - Semester {e(semester)}</title>
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
  .photo {{ border: 1px dashed #c7ced7; padding: 8px 12px; font-size: 11px; color: #5a6b7c; margin: 0 0 6px; }}
  .foot {{ margin-top: 22px; font-size: 11px; color: #33404f; }}
  .foot-row {{ display: flex; justify-content: space-between; margin-top: 4px; }}
  .sig {{ margin-top: 34px; border-top: 1px dotted #5a6b7c; width: 220px; padding-top: 2px; font-size: 10px; text-align: center; }}
</style></head><body>
<div class="doc">
  <p class="org">{_render_html_value(_UNIVERSITY)}</p>
  <p class="exam">SEMESTER {e(semester)} EXAMINATION</p>
  <p class="title">EXAMINATION FORM</p>
  <p class="demo">DEMO</p>
  <div class="rule"></div>

  <h2>IDENTIFICATION</h2>
  <table class="meta">
    <tr><td class="lbl">Exam Form No.</td><td class="val">{e(form_no or '—')}</td></tr>
    <tr><td class="lbl">Exam Roll No.</td><td class="val">{e(exam_roll_no or '—')}</td></tr>
    <tr><td class="lbl">Form Status</td><td class="val">{e(status or '—')}</td></tr>
    <tr><td class="lbl">Submitted On</td><td class="val">{e(submission_date or '—')}</td></tr>
    <tr><td class="lbl">Printed On</td><td class="val">{e(printed_on)}</td></tr>
  </table>

  <h2>CANDIDATE DETAILS</h2>
  <div class="photo">Photo: {e(photo_path or 'Not provided')} &nbsp;&nbsp;|&nbsp;&nbsp; College will affix a recent photograph of the candidate.</div>
  <table class="meta">
    <tr><td class="lbl">CUS Registration No.</td><td class="val">{e(reg_no)}</td></tr>
    <tr><td class="lbl">Name</td><td class="val">{e(name)}</td></tr>
    <tr><td class="lbl">College</td><td class="val">{e(college)}</td></tr>
    <tr><td class="lbl">Course / Programme</td><td class="val">{e(programme)}</td></tr>
    <tr><td class="lbl">Batch</td><td class="val">{e(batch or '—')}</td></tr>
  </table>

  <h2>EXAMINATION DETAILS</h2>
  <table class="meta">
    <tr><td class="lbl">Semester</td><td class="val">{e(semester)}</td></tr>
    <tr><td class="lbl">Exam Type</td><td class="val">{e(exam_type)}</td></tr>
    <tr><td class="lbl">Academic Year</td><td class="val">{e(academic_year)}</td></tr>
    <tr><td class="lbl">Exam Session</td><td class="val">{e(session_name or session_code or '—')}{(' (' + e(session_code) + ')') if session_code and session_code != session_name else ''}</td></tr>
  </table>

  <h2>SUBJECTS / PAPERS</h2>
  <table class="meta">
    <tr><th class="num">#</th><th>Paper Title</th></tr>
    {subject_rows}
  </table>

  <h2>FEE DETAILS</h2>
  <table class="meta">
    {fee_rows}
  </table>

  <div class="foot">
    <div class="foot-row">
      <span>Printed On: {e(printed_on)}</span>
      <span>Developed by I.T Cell</span>
    </div>
    <div class="foot-row">
      <span>Signature of the Applicant</span>
      <span>Authorized Signatory</span>
    </div>
  </div>
</div></body></html>"""  # noqa: E501


# --------------------------------------------------------------------------- #
# ReportLab PDF — one-page A4 portrait university document
# --------------------------------------------------------------------------- #
_A4_W = A4[0]
_A4_H = A4[1]

_PT_M = 11 * mm


def render_exam_form_pdf(data: dict[str, Any]) -> bytes:
    """One-page A4 portrait exam-form PDF (Download / Print).

    Built with the project's existing PDF framework (reportlab/platypus).
    Compact spacing keeps the whole document on exactly one page.
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

    full = _A4_W - 2 * _PT_M
    story: list = []
    story.append(p(_UNIVERSITY, "org"))
    story.append(p(f"SEMESTER {data.get('semester') or ''} EXAMINATION", "exam"))
    story.append(p("EXAMINATION FORM", "title"))
    story.append(p("DEMO", "demo"))

    # Identification
    story.append(p("IDENTIFICATION", "h2"))
    ident_rows = [field_row("Exam Form No.", str(data.get("form_no") or "")),
                  field_row("Exam Roll No.", str(data.get("exam_roll_no") or "")),
                  field_row("Form Status", str(data.get("form_status") or "Pending")),
                  field_row("Submitted On", str(data.get("submission_date") or "")),
                  field_row("Printed On", _now())]
    story.append(box(ident_rows, col_widths=[120, full - 120]))

    # Candidate details + photo note
    story.append(p("CANDIDATE DETAILS", "h2"))
    story.append(box([[
        p(f"Photo: {str(data.get('photo_path') or 'Not provided')}  |  College will affix a recent photograph of the candidate.", "val")
    ]], col_widths=[full], style_ops=[("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#C7CED7"))]))
    cand_rows = [
        field_row("CUS Registration No.", str(data.get("reg_no") or "")),
        field_row("Name", str(data.get("name") or "")),
        field_row("College", str(data.get("college") or "")),
        field_row("Course / Programme", str(data.get("programme") or "")),
        field_row("Batch", str(data.get("batch") or "")),
    ]
    col_w = [0.34 * full, 0.66 * full]
    story.append(box(cand_rows, col_widths=col_w))

    # Examination details
    story.append(p("EXAMINATION DETAILS", "h2"))
    session_display = str(data.get("session_name") or data.get("session_code") or "")
    if str(data.get("session_code") or "") and str(data.get("session_name") or "") != str(data.get("session_code") or ""):
        session_display = f"{session_display} ({data.get('session_code')})"
    info_rows = [
        field_row("Semester", str(data.get("semester") or "")),
        field_row("Exam Type", str(data.get("exam_type") or "Regular")),
        field_row("Academic Year", str(data.get("academic_year") or "")),
        field_row("Exam Session", session_display),
    ]
    story.append(box(info_rows, col_widths=[120, full - 120]))

    # Subjects
    story.append(p("SUBJECTS / PAPERS", "h2"))
    subj_rows = [[p("Sr. No.", "lbl"), p("Paper Title", "lbl")]]
    for i, s in enumerate(list(data.get("subjects") or []), start=1):
        subj_rows.append([p(str(i), "num"), p(str(s), "val")])
    story.append(box(subj_rows, col_widths=[0.10 * full, 0.90 * full], style_ops=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F6FA"))]))

    # Fee details
    story.append(p("FEE DETAILS", "h2"))
    fee_rows = [
        field_row("Base Fee", str(data.get("fee_normal") or "")),
        field_row("Late Fee", str(data.get("fee_late") or "")),
        field_row("Total Payable", str(data.get("fee_total") or "")),
        field_row("Payment Status", str(data.get("fee_status") or "Unpaid")),
        field_row("Transaction Reference", str(data.get("transaction_id") or "")),
        field_row("Payment Date", _payment_datetime(data.get("payment_date"))),
    ]
    story.append(box(fee_rows, col_widths=[120, full - 120]))

    # Footer
    story.append(Spacer(1, 20))
    story.append(p("Printed On: %-60s Developed by I.T Cell" % _now(), "foot"))
    story.append(Spacer(1, 18))
    story.append(box([
        [p("Signature of the Applicant", "val")],
        [p("Authorized Signatory", "val")],
    ], col_widths=[0.5 * full], style_ops=[
        ("LINEABOVE", (0, 0), (0, 0), 0.5, colors.HexColor("#5A6B7C")),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))

    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# FEE RECEIPT — presented right after a successful payment (view / download).
# Built from the same allow-listed document dict; payment-only values come from
# the server-owned payment snapshot attached by service.student_payment_receipt.
# --------------------------------------------------------------------------- #
def render_fee_receipt_html(data: dict[str, Any]) -> str:
    """Standalone HTML fee receipt (payment success only)."""

    e = escape
    form_no = _val(data.get("form_no"))
    payment_id = _val(data.get("payment_id"))
    payment_status = _val(data.get("payment_status")) or "success"
    payment_date = _payment_datetime(data.get("payment_date_iso") or data.get("payment_date"))
    txn_ref = _val(data.get("transaction_id"))
    gateway = _val(data.get("gateway")) or "mock"
    name = _val(data.get("name"))
    reg_no = _val(data.get("reg_no"))
    roll_no = _val(data.get("roll_no"))
    college = _val(data.get("college"))
    programme = _val(data.get("programme"))
    batch = _val(data.get("batch"))
    semester = _val(data.get("semester"))
    exam_type = _val(data.get("exam_type")) or "Regular"
    academic_year = _val(data.get("academic_year"))
    session_code = _val(data.get("session_code"))
    session_name = _val(data.get("session_name"))
    fee_normal = _val(data.get("fee_normal"))
    fee_late = _val(data.get("fee_late"))
    fee_total = _val(data.get("fee_total"))
    printed_on = _now()

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Fee Receipt</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  @page {{ size: A4 portrait; margin: 12mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; color: #17202a; background: #ffffff; margin: 0; }}
  .doc {{ max-width: 720px; margin: 0 auto; padding: 14px 18px; }}
  .org {{ text-align: center; font-size: 17px; letter-spacing: 2px; color: #143a5c; font-weight: 700; margin: 2px 0 0; }}
  .title {{ text-align: center; font-size: 19px; font-weight: 700; color: #0f2c49; margin: 8px 0 4px; }}
  .demo {{ text-align: center; font-size: 9px; color: #7a8494; letter-spacing: 1px; margin: 0 0 6px; }}
  .rule {{ border-bottom: 3px double #143a5c; margin: 4px 0 6px; }}
  .paid {{ display: inline-block; border: 2px solid #1e7e46; color: #1e7e46; font-weight: 700; letter-spacing: 3px; padding: 3px 16px; border-radius: 6px; margin: 6px 0; }}
  h2 {{ font-size: 12px; color: #0f2c49; background: #f3f6fa; border: 1px solid #d5dbe3; padding: 5px 10px; margin: 10px 0 6px; letter-spacing: 1px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  td, th {{ padding: 5px 8px; border: 1px solid #c7ced7; vertical-align: top; }}
  td.lbl {{ width: 38%; font-weight: 600; background: #f3f6fa; }}
  .note {{ margin-top: 12px; font-size: 10px; color: #5a6b7c; }}
  .foot {{ margin-top: 18px; font-size: 11px; color: #33404f; }}
  .foot-row {{ display: flex; justify-content: space-between; margin-top: 4px; }}
</style></head><body><div class="doc">
  <p class="org">{_render_html_value(_UNIVERSITY)}</p>
  <p class="title">OFFICIAL PAYMENT RECEIPT</p>
  <p class="demo">DEMO</p>
  <p style="text-align:center;"><span class="paid">PAID</span></p>
  <div class="rule"></div>

  <h2>RECEIPT DETAILS</h2>
  <table>
    <tr><td class="lbl">Receipt No.</td><td>{e(form_no or '—')}</td></tr>
    <tr><td class="lbl">Payment ID</td><td>{e(payment_id or '—')}</td></tr>
    <tr><td class="lbl">Payment Status</td><td>{e(payment_status.upper())}</td></tr>
    <tr><td class="lbl">Payment Date</td><td>{e(payment_date or '—')}</td></tr>
    <tr><td class="lbl">Transaction Reference</td><td>{e(txn_ref or '—')}</td></tr>
    <tr><td class="lbl">Payment Gateway</td><td>{e(gateway)}</td></tr>
  </table>

  <h2>BILLED TO</h2>
  <table>
    <tr><td class="lbl">Name</td><td>{e(name)}</td></tr>
    <tr><td class="lbl">CUS Registration No.</td><td>{e(reg_no)}</td></tr>
    <tr><td class="lbl">Exam Roll No.</td><td>{e(roll_no or '—')}</td></tr>
    <tr><td class="lbl">College</td><td>{e(college)}</td></tr>
    <tr><td class="lbl">Course / Programme</td><td>{e(programme)}</td></tr>
    <tr><td class="lbl">Batch</td><td>{e(batch or '—')}</td></tr>
  </table>

  <h2>EXAMINATION DETAILS</h2>
  <table>
    <tr><td class="lbl">Semester</td><td>{e(semester)}</td></tr>
    <tr><td class="lbl">Exam Type</td><td>{e(exam_type)}</td></tr>
    <tr><td class="lbl">Academic Year</td><td>{e(academic_year)}</td></tr>
    <tr><td class="lbl">Exam Session</td><td>{e(session_name or session_code or '—')}{(' (' + e(session_code) + ')') if session_code and session_code != session_name else ''}</td></tr>
  </table>

  <h2>FEE BREAK-UP</h2>
  <table>
    <tr><td class="lbl">Base Fee</td><td>{e(fee_normal or '—')}</td></tr>
    <tr><td class="lbl">Late Fee</td><td>{e(fee_late or '—')}</td></tr>
    <tr><td class="lbl">Total Paid</td><td><strong>{e(fee_total or '—')}</strong></td></tr>
  </table>

  <p class="note">This is a system-generated receipt for the demonstration. No real money was charged.</p>

  <div class="foot">
    <div class="foot-row">
      <span>Generated On: {e(printed_on)}</span>
      <span>Developed by I.T Cell</span>
    </div>
  </div>
</div></body></html>"""  # noqa: E501


def render_fee_receipt_pdf(data: dict[str, Any]) -> bytes:
    """One-page A4 portrait fee receipt PDF (Download / View)."""
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
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=18, leading=20, alignment=TA_CENTER, textColor=colors.HexColor("#0F2C49"), spaceBefore=2),
        "demo": ParagraphStyle("demo", fontName="Helvetica", fontSize=8, leading=10, alignment=TA_CENTER, textColor=colors.HexColor("#7A8494"), spaceBefore=1),
        "paid": ParagraphStyle("paid", fontName="Helvetica-Bold", fontSize=13, leading=15, alignment=TA_CENTER, textColor=colors.HexColor("#1E7E46"), spaceBefore=4),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=colors.HexColor("#0F2C49"), spaceBefore=8, spaceAfter=3),
        "lbl": ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=9, leading=11),
        "val": ParagraphStyle("val", fontName="Helvetica", fontSize=9, leading=11),
        "foot": ParagraphStyle("foot", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#33404F")),
    }

    def p(text: str, style: str) -> Paragraph:
        return Paragraph(str(text or ""), styles[style])

    def field_row(label: str, value: str) -> list[Paragraph]:
        return [p(label, "lbl"), p((value or "").strip() or "—", "val")]

    def box(rows: list[list], col_widths: list | None = None, style_ops: list | None = None) -> Table:
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

    full = _A4_W - 2 * _PT_M
    col = [130, full - 130]
    session_display = str(data.get("session_name") or data.get("session_code") or "")
    if str(data.get("session_code") or "") and str(data.get("session_name") or "") != str(data.get("session_code") or ""):
        session_display = f"{session_display} ({data.get('session_code')})"

    story: list = []
    story.append(p(_UNIVERSITY, "org"))
    story.append(p("OFFICIAL PAYMENT RECEIPT", "title"))
    story.append(p("DEMO", "demo"))
    story.append(p("PAID", "paid"))

    story.append(p("RECEIPT DETAILS", "h2"))
    story.append(box([
        field_row("Receipt No.", str(data.get("form_no") or "")),
        field_row("Payment ID", str(data.get("payment_id") or "")),
        field_row("Payment Status", str(data.get("payment_status") or "success").upper()),
        field_row("Payment Date", _payment_datetime(data.get("payment_date_iso") or data.get("payment_date"))),
        field_row("Transaction Reference", str(data.get("transaction_id") or "")),
        field_row("Payment Gateway", str(data.get("gateway") or "mock")),
    ], col_widths=col))

    story.append(p("BILLED TO", "h2"))
    story.append(box([
        field_row("Name", str(data.get("name") or "")),
        field_row("CUS Registration No.", str(data.get("reg_no") or "")),
        field_row("Exam Roll No.", str(data.get("roll_no") or "")),
        field_row("College", str(data.get("college") or "")),
        field_row("Course / Programme", str(data.get("programme") or "")),
        field_row("Batch", str(data.get("batch") or "")),
    ], col_widths=col))

    story.append(p("EXAMINATION DETAILS", "h2"))
    story.append(box([
        field_row("Semester", str(data.get("semester") or "")),
        field_row("Exam Type", str(data.get("exam_type") or "Regular")),
        field_row("Academic Year", str(data.get("academic_year") or "")),
        field_row("Exam Session", session_display),
    ], col_widths=[120, full - 120]))

    story.append(p("FEE BREAK-UP", "h2"))
    story.append(box([
        field_row("Base Fee", str(data.get("fee_normal") or "")),
        field_row("Late Fee", str(data.get("fee_late") or "")),
        field_row("Total Paid", str(data.get("fee_total") or "")),
    ], col_widths=col, style_ops=[
        ("BACKGROUND", (1, 2), (1, 2), colors.HexColor("#EAF6EE")),
        ("TEXTCOLOR", (0, 2), (1, 2), colors.HexColor("#1E7E46")),
    ]))

    story.append(p("This is a system-generated receipt for the demonstration. No real money was charged.", "foot"))
    story.append(Spacer(1, 14))
    story.append(p(f"Generated On: {_now()}        Developed by I.T Cell", "foot"))

    doc.build(story)
    return buf.getvalue()