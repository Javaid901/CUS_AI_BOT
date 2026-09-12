"""
backend/app/student_exam_form/payment.py

Payment backend adapter for exam-form fees.

The exam form becomes submittable / approved only after a `success` payment
row exists. Browsers NEVER tell the server a payment succeeded; the payment
backend adapter is the only component that can transition initiated -> success.
A confirmed success ALSO auto-transitions the student's form Pending →
Approved (and stamps submission_date), so a paid form needs no separate admin
approval. With the built-in mock gateway every initiated payment succeeds
deterministically, which drives a realistic end-to-end demo while keeping the
security invariant that `fee_status`/`amount`/`transaction_id` are server-owned.

Lifecycle:  initiated -> success | failed  (reconciliation marks refunded).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExamPayment


class PaymentBackend(Protocol):
    """Interface every exam-form payment gateway must implement."""

    name: str

    def process(self, payment: ExamPayment) -> dict[str, Any]:
        """Resolve an initiated payment to success or failure (server-side)."""
        ...


class MockPaymentBackend:
    """Deterministic demo gateway: any valid initiated payment succeeds."""

    name = "mock"

    def process(self, payment: ExamPayment) -> dict[str, Any]:
        return {
            "status": "success",
            "gateway_ref": f"MOCK-{payment.id.hex[:12].upper()}-{payment.amount}",
            "message": "Payment approved by the mock gateway.",
        }


_backends: dict[str, PaymentBackend] = {
    "mock": MockPaymentBackend(),
}


def _current_backend() -> PaymentBackend:
    mode = getattr(settings, "STUDENT_EXAM_PAYMENT_MODE", "mock") or "mock"
    return _backends.get(mode, _backends["mock"])


class PaymentError(ValueError):
    """Structured payment failure (routes map to HTTP statuses)."""


def _payment_dto(p: ExamPayment, include_ref: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(p.id),
        "form_id": str(p.form_id),
        "amount": p.amount,
        "head": p.head or "Exam Form Fee",
        "status": p.status or "initiated",
        "gateway": p.gateway or "mock",
        "recorded_at": p.recorded_at.isoformat() if p.recorded_at else None,
    }
    if include_ref:
        out["gateway_ref"] = p.gateway_ref or ""
    return out


def _payment_or_404(db: Session, payment_id: str) -> ExamPayment:
    try:
        uid = uuid.UUID(str(payment_id))
    except (ValueError, AttributeError):
        raise PaymentError("Payment not found")
    payment = db.get(ExamPayment, uid)
    if payment is None:
        raise PaymentError("Payment not found")
    return payment


def initiate_payment(db: Session, form, student_id: str) -> dict[str, Any]:
    """Open an `initiated` payment for the student's OWN form.

    The amount is ALWAYS server-derived (form fee) — a client never submits an
    amount. The form must belong to the student, be Pending and carry a fee.
    """
    if str(form.student_id) != student_id:
        raise PaymentError("You are not authorized to pay for this form.")
    if form.form_status != "Pending":
        raise PaymentError("Your Exam Form has already been submitted.")
    amount = int(form.fee_amount or 0)
    if amount < 0:
        raise PaymentError("This exam form has no payable fee.")
    existing = (
        db.query(ExamPayment)
        .filter(
            ExamPayment.form_id == form.id,
            ExamPayment.status == "initiated",
        )
        .count()
    )
    backend = _current_backend()
    payment = ExamPayment(
        id=uuid.uuid4(),
        form_id=form.id,
        session_id=form.exam_session_id,
        amount=amount,
        head="Exam Form Fee",
        status="initiated",
        gateway=backend.name,
        recorded_by="mock",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return _payment_dto(payment)


def confirm_payment(db: Session, payment_id: str, student_id: str) -> dict[str, Any]:
    """Run the backend adapter on an initiated payment and record success.

    `recorded_at` is the payment date the document prints. On success the form's
    fee fields are stamped too (fee_status Paid, fee_amount, transaction_id) and
    the form auto-transitions Pending → Approved with submission_date stamped
    (the paid form needs no further student submission or admin approval).
    Repeated confirms after success are idempotent: the recorded state is
    returned untouched.
    """
    payment = _payment_or_404(db, payment_id)
    form = payment.form
    if form is None or str(form.student_id) != student_id:
        raise PaymentError("You are not authorized to confirm this payment.")
    if payment.status == "success":
        return _payment_dto(payment, include_ref=True)

    backend = _current_backend()
    result = backend.process(payment)
    now = datetime.now(timezone.utc)
    payment.status = result.get("status") or "failed"
    payment.gateway_ref = result.get("gateway_ref") or payment.gateway_ref
    payment.recorded_at = now
    if payment.status == "success":
        payment.reconciled_at = now
        form.fee_status = "Paid"
        form.fee_amount = payment.amount
        form.transaction_id = payment.gateway_ref
        if form.form_status == "Pending":
            form.form_status = "Approved"
            if not form.submission_date:
                form.submission_date = datetime.now().strftime("%d-%b-%Y")
    else:
        payment.failure_reason = str(result.get("message") or "Payment failed")
    db.commit()
    db.refresh(payment)
    return _payment_dto(payment, include_ref=True)


def mark_refunded(db: Session, payment_id: str, actor: str) -> dict[str, Any]:
    """Super-Admin reconciliation: mark a success payment refunded."""
    payment = _payment_or_404(db, payment_id)
    if payment.status != "success":
        raise PaymentError("Only a successful payment can be refunded.")
    payment.status = "refunded"
    payment.recorded_by = actor
    db.commit()
    db.refresh(payment)
    return _payment_dto(payment, include_ref=True)


def list_form_payments(db: Session, form_id: str) -> list[dict[str, Any]]:
    try:
        uid = uuid.UUID(str(form_id))
    except (ValueError, AttributeError):
        raise PaymentError("Exam form not found")
    rows = (
        db.query(ExamPayment)
        .filter(ExamPayment.form_id == uid)
        .order_by(ExamPayment.created_at)
        .all()
    )
    return [_payment_dto(p, include_ref=True) for p in rows]


def payment_snapshot(db: Session, form_id: str) -> dict[str, Any] | None:
    """Best success payment for the form (used on the printed document)."""
    try:
        uid = uuid.UUID(str(form_id))
    except (ValueError, AttributeError):
        return None
    payment = (
        db.query(ExamPayment)
        .filter(ExamPayment.form_id == uid, ExamPayment.status == "success")
        .order_by(ExamPayment.reconciled_at.desc())
        .first()
    )
    if payment is None:
        return None
    return _payment_dto(payment, include_ref=True)


# Keep the module importable as a public surface for routes.