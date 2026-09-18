"""
backend/app/public/routes.py

Public, unauthenticated endpoints:
  GET /api/public/suggested-questions -> {"questions": [...]}
  GET /api/health                      -> service + dependency status

The chat widget calls /api/public/suggested-questions on load.
"""

from __future__ import annotations

from app.config import settings
from app.ingest.generator import is_ollama_available, list_models
from app.utils import redis_client
from fastapi import APIRouter

router = APIRouter(tags=["public"])

DEFAULT_QUESTIONS = [
    "What are the undergraduate admission requirements?",
    "When does the academic session begin?",
    "How do I apply for a scholarship?",
    "What colleges are part of Cluster University Srinagar?",
    "What documents are needed for admission?",
    "How can I check my exam results?",
    "What are the hostel facilities?",
    "Who do I contact for fee-related queries?",
]


@router.get(f"{settings.API_PREFIX}/public/suggested-questions")
def suggested_questions():
    return {"questions": DEFAULT_QUESTIONS}


@router.get(f"{settings.API_PREFIX}/health")
def health():
    ollama_ok = is_ollama_available()
    # Redis: enabled=False is healthy (not configured); enabled=True
    # but unreachable is degraded.
    redis_enabled = redis_client.runtime.enabled()
    redis_ok = redis_client.runtime.health_ok()
    redis_status = "healthy" if redis_ok else ("degraded" if redis_enabled else "not_configured")
    try:
        from app.orchestrator.metrics import metrics_summary
        metrics = metrics_summary()
    except Exception:
        metrics = {}
    return {
        "status": "ok" if ollama_ok else "degraded",
        "service": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "ollama": {
            "reachable": ollama_ok,
            "base_url": settings.OLLAMA_BASE_URL,
            "llm_model": settings.LLM_MODEL,
            "embed_model": settings.EMBED_MODEL,
            "models": list_models() if ollama_ok else [],
        },
        "redis": {
            "enabled": redis_enabled,
            "status": redis_status,
        },
        "metrics": metrics,
    }


@router.get(f"{settings.API_PREFIX}/metrics")
def get_metrics():
    from app.orchestrator.metrics import metrics_summary
    return metrics_summary()
