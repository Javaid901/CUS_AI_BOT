"""
backend/tests/test_document_classifier.py — Phase 1 classification core.

Covers the deterministic official-document classifier for crawled resources:
  * HTML pages keep the legacy knowledge classification (admissions, ...)
  * binary documents -> date-sheet / model-paper / official-notification /
    other-official-document / ambiguous with confidence band + signals
  * model-paper HARD exclusions (previous year / PYQ / syllabus / BoS ...
    an excluded document is NEVER labelled model-paper)
  * conflicting close-margin signals -> ambiguous (never a wrong guess)
  * EVSSem1 / EnglishSem-2 style documents stay ambiguous

Run:  python tests/test_document_classifier.py   (or pytest tests/test_document_classifier.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_sync.document_classifier import classify_document

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


PDF = b"%PDF-1.4 fake document bytes"


def _doc(**kw) -> dict:
    base = {"content_type": "document", "raw": PDF}
    base.update(kw)
    return base


def test_html_keeps_knowledge_classification() -> None:
    print("-- HTML pages keep knowledge classification --")
    r = classify_document(title="Admission Notification 2026", url="https://www.cusrinagar.edu.in/admissions", text="PG admission notification", content_type="html")
    check("admissions is knowledge", r["doc_type"] == "knowledge" and r["category"] == "admissions", str(r))

    r2 = classify_document(title="Examination date sheet 2026", text="", content_type="html")
    check("html date sheet is knowledge/examinations", r2["doc_type"] == "knowledge" and r2["category"] == "examinations", str(r2))

    r3 = classify_document(title="zzzz qqqq", text="aaaa", content_type="html")
    check("unknown html -> ambiguous", r3["doc_type"] == "ambiguous" and r3["category"] == "ambiguous", str(r3))


def test_official_date_sheet() -> None:
    print("-- date sheets -> official/date-sheet, high confidence --")
    r = classify_document(title="End Semester UG Date Sheet 2026", url="https://x/notices/UG_Date_Sheet_2026.pdf", **_doc())
    check("date sheet doc_type", r["doc_type"] == "official", str(r))
    check("date sheet category", r["category"] == "date-sheet", str(r))
    check("date sheet high confidence", r["confidence"]["band"] == "high", str(r["confidence"]))
    check("date sheet signals present", any("date-sheet" in s for s in r["signals"]), str(r["signals"]))


def test_model_paper_positive() -> None:
    print("-- model paper: labelled + held for review --")
    r = classify_document(title="Model Paper Environmental Science", url="https://x/exams/Model_Paper_EVS.pdf", **_doc())
    check("model paper doc_type", r["doc_type"] == "official", str(r))
    check("model paper category", r["category"] == "model-paper", str(r))
    check("model paper label+hold signal", any("hold for review" in s for s in r["signals"]), str(r["signals"]))


def test_model_paper_hard_exclusions() -> None:
    print("-- model-paper exclusions win over markers --")
    cases = {
        # A previous-year question paper living under a /notices/ URL still
        # reads as an official notification (same shape as acceptance case F),
        # but never as model-paper.
        "Model Paper Previous Year Question Paper": "official-notification",
        "MCA Previous Year Question Paper": "official-notification",
        "Model Paper Syllabus BoS": "other-official-document",
        "Model Paper and Date Sheet": "date-sheet",
    }
    for title, expected in cases.items():
        url = "https://x/notices/" + title.replace(" ", "_") + ".pdf"
        r = classify_document(title=title, url=url, **_doc())
        check(f"exclusion {title!r} != model-paper", r["category"] != "model-paper", str(r))
        check(f"exclusion {title!r} -> {expected}", r["category"] == expected, str(r))


def test_mca_previous_year_qp_not_model_paper() -> None:
    print("-- MCA Previous Year Question Paper must NOT become model-paper --")
    r = classify_document(
        title="MCA Previous Year Question Paper",
        url="https://www.cusrinagar.edu.in/notices/MCA_Previous_Year_Question_Paper.pdf",
        **_doc(),
    )
    check("not model-paper", r["category"] != "model-paper", str(r))
    check("official-notification (notices url)", r["category"] == "official-notification", str(r))
    check("low confidence", r["confidence"]["band"] == "low", str(r["confidence"]))


def test_syllabus_other_official() -> None:
    print("-- syllabus -> other-official-document --")
    r = classify_document(title="B.Tech Syllabus Revised 2026", url="https://x/curriculum/bt_syllabus.pdf", **_doc())
    check("syllabus is official", r["doc_type"] == "official", str(r))
    check("syllabus -> other-official-document", r["category"] == "other-official-document", str(r))


def test_ambiguous_and_conflicts() -> None:
    print("-- ambiguous + conflicting-signal safety --")
    for title in ("EVSSem1.pdf", "EnglishSem-2.pdf", "Course Brochure 2026"):
        r = classify_document(title=title, url="https://x/downloads/" + title, **_doc())
        check(f"{title} stays ambiguous", r["doc_type"] == "ambiguous" and r["category"] == "ambiguous", str(r))

    r = classify_document(title="Circular and Syllabus", url="https://x/d/x.pdf", **_doc())
    check("close conflicting signals -> ambiguous", r["category"] == "ambiguous", str(r))
    check("conflict signal recorded", any("conflicting" in s for s in r["signals"]), str(r["signals"]))


def test_confidence_bands() -> None:
    print("-- confidence bands --")
    high = classify_document(title="End Semester UG Date Sheet 2026", url="https://x/notices/UG_Date_Sheet_2026.pdf", **_doc())
    medium = classify_document(title="B.Tech Syllabus", url="https://x/d/2026.pdf", **_doc())
    low = classify_document(title="MCA Previous Year Question Paper", url="https://x/notices/MCA_Previous_Year_Question_Paper.pdf", **_doc())
    check("date sheet -> high", high["confidence"]["band"] == "high", str(high["confidence"]))
    check("syllabus -> medium", medium["confidence"]["band"] == "medium", str(medium["confidence"]))
    check("banner-less notification -> low", low["confidence"]["band"] == "low", str(low["confidence"]))
    for r in (high, medium, low):
        score = r["confidence"]["score"]
        check("score bounded 0..100", 0 <= score <= 100, str(score))


def test_acceptance_categories() -> None:
    print("-- acceptance category mapping (A-G) --")

    a = classify_document(
        title="MCA 3rd Semester Date Sheet",
        url="https://www.cusrinagar.edu.in/notices/MCA_3rd_Semester_Date_Sheet.pdf",
        **_doc(),
    )
    check("A: date sheet official", a["doc_type"] == "official", str(a))
    check("A: date sheet category", a["category"] == "date-sheet", str(a))

    b = classify_document(
        title="MCA Model Question Paper",
        url="https://www.cusrinagar.edu.in/exams/MCA_Model_Question_Paper.pdf",
        **_doc(),
    )
    check("B: true model paper official", b["doc_type"] == "official", str(b))
    check("B: true model paper category", b["category"] == "model-paper", str(b))

    c = classify_document(
        title="Notice for Submission of Examination Forms",
        url="https://www.cusrinagar.edu.in/notices/Notice_for_Submission_of_Examination_Forms.pdf",
        **_doc(),
    )
    check("C: notice official", c["doc_type"] == "official", str(c))
    check("C: notice category", c["category"] == "official-notification", str(c))

    d = classify_document(
        title="University Regulations 2026",
        url="https://www.cusrinagar.edu.in/documents/University_Regulations_2026.pdf",
        **_doc(),
    )
    check("D: regulations official", d["doc_type"] == "official", str(d))
    check("D: regulations category", d["category"] == "other-official-document", str(d))

    e = classify_document(
        title="Faculty Profile", url="https://www.cusrinagar.edu.in/about/faculty", text="Faculty members",
        content_type="html",
    )
    check("E: faculty stays knowledge", e["doc_type"] == "knowledge", str(e))
    check("E: knowledge stays faculty bucket", e["category"] == "faculty", str(e))

    f = classify_document(
        title="MCA Previous Year Question Paper",
        url="https://www.cusrinagar.edu.in/notices/MCA_Previous_Year_Question_Paper.pdf",
        **_doc(),
    )
    check("F: previous year NOT model-paper", f["category"] != "model-paper", str(f))

    g = classify_document(
        title="EVSSem1.pdf", url="https://www.cusrinagar.edu.in/downloads/EVSSem1.pdf", **_doc()
    )
    check("G: unclear doc ambiguous", g["doc_type"] == "ambiguous" and g["category"] == "ambiguous", str(g))


def test_defect_text_driven_date_sheets() -> None:
    print("-- real CUS date sheets carry 'Date Sheet for...' only in the body --")
    cases = [
        (
            "b_ed_2ndsemsterbatch2025supp.pdf",
            "OFFICE OF THE CONTROLLER OF EXAMINATIONS\nCLUSTER UNIVERSITY SRINAGAR\n"
            "Gogji-Bagh Campus, Srinagar-190008\n"
            "Date Sheet for B.Ed. (Supplementary) Semester 2nd Regular Batch 2025\n"
            "Session: Sept.2026 Examination Time: 10:30 AM\n"
            "Date Sheet for B.Ed. (Supplementary) Semester 2nd Regular Batch 2025 (contd.)\n",
        ),
        (
            "b_ed_3rdsemster2025aug2026.pdf",
            "OFFICE OF THE CONTROLLER OF EXAMINATIONS\nCLUSTER UNIVERSITY SRINAGAR\n"
            "Date Sheet for B.Ed. Semester 3rd Regular Batch 2025 & Previous Backlog Batches\n"
            "Session: - 2026 Examination Time: 01:00 PM\n"
            "Date Sheet for B.Ed. Semester 3rd Regular Batch 2025 & Previous Backlog Batches (contd.)\n",
        ),
        (
            "pg1stsemregularbatch2025.pdf",
            "OFFICE OF THE CONTROLLER OF EXAMINATIONS\nCLUSTER UNIVERSITY SRINAGAR\n"
            "Date Sheet for P.G. Semester 1st Regular Batch 2025\nSession-2025 Examination Timing: 10:30 AM\n"
            "Date Sheet for P.G. Semester 1st Regular Batch 2025 (contd.)\n",
        ),
        (
            "dspg2ndsembacklogbatch202224.pdf",
            "OFFICE OF THE CONTROLLER OF EXAMINATIONS\nCLUSTER UNIVERSITY SRINAGAR\n"
            "Date Sheet for PG Diploma Semester 2nd Backlog Batch 2022-2024\n"
            "Examination Time: 01:00 PM\n"
            "Date Sheet for PG Diploma Semester 2nd Backlog Batch 2022-2024 (contd.)\n",
        ),
    ]
    for filename, pdf_text in cases:
        r = classify_document(
            title=filename,
            url="https://www.cusrinagar.edu.in/FolderManager/Downloads/" + filename,
            text=pdf_text,
            **_doc(),
        )
        check(f"text date sheet {filename} official", r["doc_type"] == "official", str(r))
        check(f"text date sheet {filename} -> date-sheet", r["category"] == "date-sheet", str(r))
        check(f"text date sheet {filename} signal", any("date-sheet" in s for s in r["signals"]), str(r["signals"]))
        check(f"text date sheet {filename} band low/medium", r["confidence"]["band"] in ("low", "medium"), str(r["confidence"]))

    # A stray single mention must NOT promote an unrelated doc to date-sheet.
    stray = classify_document(
        title="CVMobin.pdf",
        url="https://www.cusrinagar.edu.in/FolderManager/Downloads/CVMobin.pdf",
        text="Employment history 2015..2024. He handed over the date sheet to the office.",
        **_doc(),
    )
    check("stray date-sheet mention stays ambiguous", stray["category"] == "ambiguous", str(stray))


def test_defect_notification_examples() -> None:
    print("-- official notification strong title signals --")
    for title, filename in [
        ("Notice for submission of examination forms", "Notice_for_submission_of_examination_forms.pdf"),
        ("Notification regarding examination", "Notification_regarding_examination.pdf"),
        ("Official notification", "Official_notification.pdf"),
        ("Examination form notice", "Examination_form_notice.pdf"),
    ]:
        r = classify_document(
            title=title,
            url="https://www.cusrinagar.edu.in/notices/" + filename,
            **_doc(),
        )
        check(f"notification {title!r} official", r["doc_type"] == "official", str(r))
        check(f"notification {title!r} -> official-notification", r["category"] == "official-notification", str(r))

    # A real date sheet must stay a date-sheet even when its webpage/path
    # and body mention "notification".
    ds = classify_document(
        title="MCA 3rd Semester Date Sheet",
        url="https://www.cusrinagar.edu.in/notices/MCA_3rd_Semester_Date_Sheet.pdf",
        text="examination notification notification notice",
        **_doc(),
    )
    check("date sheet not swallowed by notification noise", ds["category"] == "date-sheet", str(ds))


def test_required_category_cases() -> None:
    print("-- exact requested category cases --")
    from app.knowledge_sync.document_classifier import classification_state_for

    cases = [
        ("MCA Date Sheet.pdf", "date-sheet", "official", None),
        ("Revised Date Sheet UG 6th Semester.pdf", "date-sheet", "official", None),
        ("Notice for submission of examination forms.pdf", "official-notification", "official", None),
        ("MCA Model Question Paper.pdf", "model-paper", "official", "pending_review"),
        ("MCA Previous Year Question Paper.pdf", None, None, None),  # NOT model-paper
        ("CUS Regulations 2026.pdf", "other-official-document", "official", None),
        ("zqwv document without signals.pdf", "ambiguous", "ambiguous", None),
    ]
    for title, exp_cat, exp_dt, exp_state in cases:
        url = "https://www.cusrinagar.edu.in/FolderManager/Downloads/" + title.replace(" ", "_")
        r = classify_document(title=title, url=url, **_doc())
        if exp_cat is None:
            check(f"case {title!r} NOT model-paper", r["category"] != "model-paper", str(r))
            continue
        check(f"case {title!r} doc_type={exp_dt}", r["doc_type"] == exp_dt, str(r))
        check(f"case {title!r} category={exp_cat}", r["category"] == exp_cat, str(r))
        if exp_state is not None:
            state = classification_state_for(r, is_document=True)
            check(f"case {title!r} state={exp_state}", state == exp_state, str(state))


def test_real_cus_date_sheet_corpus() -> None:
    print("-- real corpus date sheets in backend/app/data/notices (skip if absent) --")
    import os

    base = Path(__file__).resolve().parents[1] / "app" / "data" / "notices"
    wanted = [
        "b_ed_2ndsemsterbatch2025supp.pdf",
        "b_ed_3rdsemster2025aug2026.pdf",
        "pg1stsemregularbatch2025.pdf",
        "dspg2ndsembacklogbatch202224.pdf",
        "ugdatesheet2ndsem2025cbcs.pdf",
        "ug4thsemesternepbatch2024backlog.pdf",
    ]
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:  # pragma: no cover - env without pypdf
        print("  SKIP  pypdf not available")
        return
    checked = 0
    for fname in sorted(os.listdir(base)):
        if "_" not in fname:
            continue
        stem = fname.split("_", 1)[1]
        if stem not in wanted:
            continue
        try:
            raw = (base / fname).read_bytes()
            text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(base / fname)).pages)
        except Exception:  # pragma: no cover
            continue
        r = classify_document(
            title=stem,
            url="https://www.cusrinagar.edu.in/FolderManager/Downloads/" + stem,
            text=text.strip(),
            content_type="document",
            raw=raw,
        )
        checked += 1
        check(f"corpus {stem} -> date-sheet", r["doc_type"] == "official" and r["category"] == "date-sheet", str(r))
    if checked == 0:
        print("  SKIP  no corpus files found")


def test_contract_shape() -> None:
    print("-- classification contract shape --")
    r = classify_document(title="Admissions 2026", text="body", content_type="html")
    check("contract keys", set(r) == {"doc_type", "category", "confidence", "signals"}, str(sorted(r)))
    check("confidence keys", set(r["confidence"]) == {"band", "score"}, str(r["confidence"]))
    check("signals is list of str", isinstance(r["signals"], list) and all(isinstance(s, str) for s in r["signals"]))


def main() -> None:
    test_html_keeps_knowledge_classification()
    test_official_date_sheet()
    test_model_paper_positive()
    test_model_paper_hard_exclusions()
    test_mca_previous_year_qp_not_model_paper()
    test_syllabus_other_official()
    test_ambiguous_and_conflicts()
    test_confidence_bands()
    test_acceptance_categories()
    test_defect_text_driven_date_sheets()
    test_defect_notification_examples()
    test_required_category_cases()
    test_real_cus_date_sheet_corpus()
    test_contract_shape()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()