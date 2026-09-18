"""
backend/app/examination/routes.py — secure Model Paper file endpoint.

Every read is structurally gated to ``classification_status == "verified"``
model-paper rows and validates path containment server-side via
``raw_store.resolve_contained`` — mirroring the notices file endpoint.
Unverified / hidden / foreign-category pages are never served; any
escape attempt (``..``, absolute/drive path) returns 404.

Exam Fee Structure and Division Improvement have no file endpoint today (no
official corpus content exists); their chat responses are token/detail cards
served through the SSE stream, not file downloads.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.security import get_current_user
from app.database import get_db
from app.examination import service as examination
from app.models import User
from app.orchestrator.engine import apply_model_paper_selection
from app.orchestrator.state import get_state, set_state

router = APIRouter(tags=["examinations"])

_PREFIX = "/api/examinations"


def _media_type(ext: str) -> str:
    return {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")


@router.get(f"{_PREFIX}/model-papers/{{page_id}}/file")
def public_get_model_paper_file(
    page_id: str,
    download: bool | None = Query(default=False),
    db: Session = Depends(get_db),
):
    paper = examination.get_model_paper(db, page_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Model paper not available")
    path: Path | None = examination.resolve_model_paper_file(paper)
    if path is None:
        raise HTTPException(status_code=404, detail="Model paper not available")
    raw_path = paper.get("raw_path") or ""
    ext = (raw_path.rsplit(".", 1)[-1].lower()
           if "." in raw_path
           else (paper.get("content_type") or "pdf"))
    filename = examination.safe_filename(paper.get("title") or "ModelPaper")
    if ext:
        if filename.lower().endswith(f".{ext}"):
            filename = filename[: -(len(ext) + 1)]
        filename = f"{filename}.{ext}"
    return FileResponse(
        str(path),
        media_type=_media_type(ext),
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
    )


class _SelectModelPaperRequest(BaseModel):
    """Request body for the single-paper selection endpoint."""

    chat_id: str | None = None


@router.post(f"{_PREFIX}/model-papers/{{page_id}}/select")
async def select_model_paper(
    page_id: str,
    body: _SelectModelPaperRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Server-validated single-paper selection.

    Resolves ``page_id`` through the SAME verified/on-disk gate as the file
    endpoint and then pins the conversation's RAG document scope to exactly
    ONE document. The client-supplied page ID is never trusted; there is no
    way to scope the conversation to an unverified or foreign paper.
    Commits to the conversation state so the very next /ask inherits the
    scope via ``ctx.exam_document_ids``.
    """
    paper = examination.select_model_paper(db, page_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="Model paper not available")
    chat_id = (body.chat_id or "").strip()
    state = await get_state(chat_id)
    apply_model_paper_selection(state.context, paper)
    # Phase 3C-5: persist the mutated scope immediately (and to Redis when
    # enabled) so the very next /ask — on any worker — inherits it.
    await set_state(chat_id, state)
    return {
        "ok": True,
        "id": paper["id"],
        "title": paper.get("title"),
        "document_id": paper.get("document_id"),
    }