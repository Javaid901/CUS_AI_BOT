"""
backend/app/student/gate.py

SSE event builders for the Student Services auth gate and authenticated hub.

These providers keep the chat pipeline free of credential concepts: the engine
only ever emits an `auth_form` event (the prompt text lives here too) and the
frontend renders the sign-in widget. Payload values deliberately avoid the
literal strings ("password", "registration_number", capital service labels)
that tests prohibit leaking into unauthenticated renders.
"""

from __future__ import annotations

# Families that the Step-1 gate now owns. Students must sign in to reach them.
GATED_FAMILIES: frozenset[str] = frozenset({"results", "admit_card", "exam_form"})

# Display labels used only inside authenticated renders (never emitted to an
# unauthenticated chat — the family code is the payload, all-lowercase).
_FAMILY_LABELS: dict[str, str] = {
    "results": "Results",
    "admit_card": "Admit Card",
    "exam_form": "Exam Form",
}


def family_label(family: str | None) -> str:
    return _FAMILY_LABELS.get(family or "", "This service")


def auth_gate_message(family: str | None) -> str:
    """Bot text shown while asking the student to sign in."""
    if family and family in _FAMILY_LABELS:
        return (
            f"To access {_FAMILY_LABELS[family]} you must first verify that you are a "
            "registered student. Please sign in with your registration number."
        )
    return (
        "Student Services shows your own academic details, so we need to "
        "verify your identity first. Please sign in with your registration number."
    )


def auth_form_event(family: str | None) -> dict:
    """Instruct the frontend to render the sign-in widget."""
    return {"type": "auth_form", "payload": {"family": family or "hub"}}


def expired_gate_message(family: str | None) -> str:
    """Bot text shown when the fixed 10-minute session actually lapsed.

    Exact required wording; the same sign-in widget (auth_form_event) is
    rendered right after it.
    """
    return "Your Student Services session has expired. Please log in again."


def logged_out_gate_message(family: str | None) -> str:
    """Bot text shown when the session was ENDED by an explicit logout.

    Kept distinct from expired_gate_message so a freshly logged-out student is
    never falsely told their session newly "expired". The same sign-in widget
    (auth_form_event) is rendered right after it.
    """
    return "Your Student Services session has ended. Please log in again."


def hub_options(student: dict | None) -> dict:
    """Authenticated Student Services hub (chips shown only after sign-in)."""
    name = (student or {}).get("name")
    greeting = f"Hi {name}," if name else "Welcome,"
    return {
        "type": "options",
        "title": "Student Services",
        "message": f"{greeting} your identity is verified. Select a service below.",
        "options": [
            {"id": "student_results", "label": "Results"},
            {"id": "student_admit_card", "label": "Admit Card"},
            {"id": "student_exam_form", "label": "Exam Form"},
        ],
    }


def coming_soon_event(family: str) -> dict:
    """Placeholder detail for a gated family that Step 1 intentionally leaves unimplemented."""
    label = _FAMILY_LABELS.get(family, "This service")
    return {
        "type": "detail",
        "title": label,
        "message": f"{label} is reserved for registered students and will be enabled in an upcoming release.",
        "fields": [{"label": "Status", "value": "Coming soon"}],
    }


# --------------------------------------------------------------------------- #
# Student Results (Phase B) — authenticated renders only
# --------------------------------------------------------------------------- #
def results_form_event(semesters: list[dict], preselect: int | None = None, roll: str | None = None) -> dict:
    """Semester + examination-roll form for ONE authenticated attempt.

    The frontend renders a season `select` (populated from the student's OWN
    available semesters), a roll input, and a "View Result" button. Detail
    (marks) is only returned after the attempt is selected server-side via the
    POST /view lookup. `preselect` picks an initial semester (typed request or
    a legacy `results-sem-N` chip); `roll` prefills the input, but the roll is
    single-turn — it is never persisted in chat/session state so a later
    unrelated message cannot leak it.
    """
    opts = [
        {
            "semester": s["semester"],
            "academic_year": s.get("academic_year"),
            "exam_type": s.get("exam_type"),
            "subject_count": s.get("subject_count"),
        }
        for s in semesters
    ]
    selected = preselect if preselect in {s["semester"] for s in semesters} else None
    return {
        "type": "results_form",
        "title": "My Results",
        "message": (
            "Select the semester for which you want to view your result, "
            "then enter the examination roll number printed on your admit card."
        ),
        "semesters": opts,
        "semester": selected,
        "roll": roll or "",
        "placeholder": "Enter your examination roll number",
    }


# --------------------------------------------------------------------------- #
# Student Admit Card (Phase C) — authenticated renders only
# --------------------------------------------------------------------------- #
def admit_card_semesters_event(semesters: list[dict]) -> dict:
    """Semester picker for an authenticated student's own admit cards.

    Chip ids are `admit_card_sem-N`: the "admit_card" substring keeps the
    planner routing deterministically back to student_service/admit_card (Rule
    10a phrase 'admit_card'), and the engine parses N from the id.
    """
    return {
        "type": "options",
        "title": "My Admit Card",
        "message": "Which semester's admit card would you like to see?",
        "options": [
            {"id": f"admit_card_sem-{s['semester']}", "label": f"Semester {s['semester']}"}
            for s in semesters
        ],
    }


def admit_card_detail_event(semester: int, data: dict) -> dict:
    """A structured admit card (stored values only — no file/PDF concept)."""
    fields: list[dict[str, str]] = [
        {"label": "Examination", "value": f"{data.get('exam_type') or 'Regular'} · Semester {semester}"},
        {"label": "Exam Session", "value": data.get("exam_session") or "—"},
        {"label": "Academic Year", "value": data.get("academic_year") or "—"},
        {"label": "Centre", "value": f"{data.get('centre_name') or '—'} ({data.get('centre_code') or '—'})"},
        {"label": "Centre Address", "value": data.get("centre_address") or "—"},
        {"label": "Reporting Time", "value": data.get("reporting_time") or "—"},
        {"label": "Issued", "value": data.get("issued_date") or "—"},
    ]
    subjects = data.get("subjects") or []
    if subjects:
        fields.append({
            "label": "Subjects",
            "value": " · ".join(f"({i}) {s}" for i, s in enumerate(subjects, start=1)),
        })
    instructions = data.get("instructions") or []
    if instructions:
        fields.append({"label": "Instructions", "value": " · ".join(instructions)})
    return {
        "type": "detail",
        "title": f"Admit Card · Semester {semester}",
        "message": "Here is your admit card:",
        "fields": fields,
    }


# --------------------------------------------------------------------------- #
# Student Exam Form (Phase D) — authenticated renders only
# --------------------------------------------------------------------------- #
def exam_form_picker_event(forms: list[dict]) -> dict:
    """Entry picker for the student's own exam forms.

    Chip ids are `exam_form{exam_type}N`: the "exam_form" substring keeps the
    planner routing deterministically back to student_service/exam_form, and the
    engine parses type+semester back out. Each chip names the form identity so
    the student can fill/print the intended (semester, exam type) form.
    """
    if not forms:
        return {
            "type": "options",
            "title": "Exam Form",
            "message": "No Exam Form is available for your profile yet. It will appear here once it is provisioned.",
            "options": [],
        }
    options = []
    for f in forms:
        exam_type = f.get("exam_type") or "Regular"
        semester = f.get("semester")
        tid = exam_type.lower()
        options.append({
            "id": f"exam_form{tid}{semester}",
            "label": f"{exam_type} · Semester {semester}",
        })
    return {
        "type": "options",
        "title": "Exam Form",
        "message": "Which exam form would you like?",
        "options": options,
    }


def exam_form_detail_event(form: dict) -> dict:
    """Structured exam form view (print-friendly representation of OWN form).

    Fee status is shown (the student may need to know whether the exam fee is
    paid by the administration) but the transaction reference is intentionally
    withheld from the student payload — payment metadata stays admin-owned.
    """
    subjects = form.get("subjects") or []
    subj_value = " · ".join(f"({i}) {s}" for i, s in enumerate(subjects, start=1)) if subjects else "—"
    fields: list[dict[str, str]] = [
        {"label": "Examination", "value": f"{form.get('exam_type') or 'Regular'} · Semester {form.get('semester')}"},
        {"label": "Academic Year", "value": form.get("academic_year") or "—"},
        {"label": "Status", "value": form.get("form_status") or "Pending"},
        {"label": "Fee Status", "value": form.get("fee_status") or "Unpaid"},
        {"label": "Submitted On", "value": form.get("submission_date") or "—"},
        {"label": "Subjects", "value": subj_value},
    ]
    return {
        "type": "detail",
        "title": f"Exam Form · {form.get('exam_type') or 'Regular'} Semester {form.get('semester')}",
        "message": "Here is your exam form:",
        "fields": fields,
    }