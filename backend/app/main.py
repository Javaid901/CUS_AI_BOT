"""
backend/app/main.py

FastAPI application entrypoint.

Wires routers, CORS, structured error handlers, and startup tasks
(admin seeding + table creation).

Endpoint contract note:
  The existing frontend calls /api/documents, /api/chat/ask, etc.
  The original task spec lists /api/admin/documents, /api/admin/upload, ...
  Both are mounted (see alias wiring) so the frontend works unchanged and the
  spec-style paths also resolve.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.admin.profile import router as admin_profile_router
from app.admin.routes import router as admin_router
from app.admin.notices import router as admin_notices_router
from app.admin.sync_documents import router as admin_sync_documents_router
from app.admin.university_documents import router as admin_university_documents_router
from app.analytics.routes import router as analytics_router
from app.auth.routes import router as auth_router
from app.authority.routes import public_router as authority_lookup_router
from app.authority.routes import router as authority_admin_router
from app.authority_admin.routes import router as authority_admins_router
from app.authority_admin.routes import self_router as authority_admin_self_router
from app.catalogue.routes import router as catalogue_router
from app.chat.routes import router as chat_router
from app.college.routes import router as college_router
from app.config import settings
from app.database import create_all
from app.examination.routes import router as examination_router
from app.grievance.routes import router as grievance_router
from app.notices.routes import router as notices_router
from app.public.routes import router as public_router
from app.student.routes import router as student_router
from app.student_admin.routes import router as student_admin_router
from app.student_results.routes import admin_router as student_results_admin_router
from app.student_results.routes import router as student_results_router
from app.student_admit_card.routes import admin_router as student_admit_card_admin_router
from app.student_admit_card.routes import router as student_admit_card_router
from app.student_exam_form.routes import admin_router as student_exam_form_admin_router
from app.student_exam_form.routes import router as student_exam_form_router
from app.student_exam_form.session_routes import router as exam_session_admin_router
from app.university_documents.routes import router as university_documents_router
from app.utils.errors import register_exception_handlers
from app.utils.logging import log
from app.utils.postgres_ensure import ensure_postgresql_running

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="RAG-based university AI assistant for Cluster University Srinagar.",
)

# ----- CORS -----
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_origin_list != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----- Structured errors -----
register_exception_handlers(app)

# ----- Routers (frontend contract paths) -----
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(admin_profile_router)
app.include_router(admin_notices_router)
app.include_router(admin_sync_documents_router)
app.include_router(admin_university_documents_router)
app.include_router(college_router)
app.include_router(analytics_router)
app.include_router(catalogue_router)
app.include_router(public_router)
app.include_router(notices_router)
app.include_router(examination_router)
app.include_router(university_documents_router)
app.include_router(authority_admin_router)
app.include_router(authority_lookup_router)
app.include_router(authority_admins_router)
app.include_router(authority_admin_self_router)
app.include_router(grievance_router)
app.include_router(student_router)
app.include_router(student_admin_router)
app.include_router(student_results_router)
app.include_router(student_results_admin_router)
app.include_router(student_admit_card_router)
app.include_router(student_admit_card_admin_router)
app.include_router(student_exam_form_router)
app.include_router(student_exam_form_admin_router)
app.include_router(exam_session_admin_router)


# ----- Spec-style aliases (for compatibility with the task spec) -----
# The frontend uses /api/documents etc.; these add_api_route calls expose
# the same handlers under the original spec paths.
from app.admin.routes import (
    delete_document,
    list_documents,
    reindex_document,
    upload_document,
)

app.add_api_route(
    f"{settings.API_PREFIX}/admin/documents",
    list_documents,
    methods=["GET"],
    include_in_schema=False,
)
app.add_api_route(
    f"{settings.API_PREFIX}/admin/upload",
    upload_document,
    methods=["POST"],
    include_in_schema=False,
)
app.add_api_route(
    f"{settings.API_PREFIX}/admin/document/{{doc_id}}",
    delete_document,
    methods=["DELETE"],
    include_in_schema=False,
)
app.add_api_route(
    f"{settings.API_PREFIX}/admin/reindex/{{doc_id}}",
    reindex_document,
    methods=["POST"],
    include_in_schema=False,
)


# ----- Startup -----
@app.on_event("startup")
def on_startup() -> None:
    log.info("Starting %s (env=%s)", settings.APP_NAME, settings.ENVIRONMENT)
    ensure_postgresql_running()
    create_all()
    _seed_admin()
    if settings.DEMO_MODE:
        # Demo mode: seed full synthetic dataset (students + all service data)
        _seed_demo_service_data()
    else:
        # Non-demo: seed only the minimal 5 test students
        _seed_students()
    _init_redis()
    _warmup_models()
    _start_ollama_watchdog()
    _warmup_intent_classifier()
    _warmup_retrieval()
    _start_analytics_scheduler()
    _backfill_analytics()
    _start_website_sync_scheduler()
    _start_background_worker()
    _start_request_queue_worker()
    _warmup_authority_cache()
    log.info("Analytics module initialized")


def _init_redis() -> None:
    """Startup probe for the Redis layer (non-fatal).

    Runs the async probe in a dedicated thread so it can never block the app's
    event loop for the Redis socket timeout, and never aborts startup.
    """
    from app.utils import redis_client

    holder: dict[str, bool] = {}

    def _runner() -> None:
        try:
            asyncio.run(redis_client.startup_check())
            holder["ok"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis startup probe failed: %s", exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=8.0)
    log.info("Redis layer initialized (enabled=%s)", redis_client.runtime.enabled())


@app.on_event("shutdown")
def on_shutdown() -> None:
    from app.utils import redis_client

    try:
        redis_client.shutdown_close()
        log.info("Redis client pools closed")
    except Exception as exc:  # noqa: BLE001
        log.debug("Redis shutdown: %s", exc)


def _start_analytics_scheduler() -> None:
    """Start the analytics background scheduler."""
    from app.analytics.scheduler import start
    try:
        asyncio.get_running_loop()
        start()
        log.info("Analytics background scheduler started")
    except RuntimeError:
        try:
            asyncio.run(start())
            log.info("Analytics background scheduler started")
        except RuntimeError:
            log.debug("Analytics scheduler deferred (no event loop)")


def _backfill_analytics() -> None:
    """Backfill analytics from existing conversation data if empty."""
    try:
        from app.analytics.service import ensure_analytics_data
        count = ensure_analytics_data()
        if count:
            log.info("Analytics backfilled: %d events created", count)
    except Exception as exc:
        log.warning("Analytics backfill skipped: %s", exc)


def _start_background_worker() -> None:
    """Start the background ingestion worker."""
    from app.ingest.worker import worker
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(worker.start())
        else:
            asyncio.run(worker.start())
        log.info("Background ingestion worker started")
    except Exception as exc:
        log.warning("Background worker start deferred: %s", exc)


def _start_website_sync_scheduler() -> None:
    """Start the website knowledge sync scheduler (dashboard-controllable)."""
    try:
        from app.knowledge_sync.web_scheduler import start as _web_sched_start

        _web_sched_start()
        log.info("Website Sync scheduler thread started")
    except Exception as exc:
        log.warning("Website Sync scheduler deferred: %s", exc)


def _start_request_queue_worker() -> None:
    """Start the worker pool that dequeues queued requests.

    The worker pool is what releases the admission slots; without it, a
    queued request would wait forever on `slot.wait()`. Execution itself
    happens in the SSE stream (the orchestrator generator), so the pool's
    processor is a bookkeeping no-op that only completes the queue entry.
    """
    from app.request_manager.worker_pool import worker_pool

    async def _release_only(request):
        return None

    try:
        worker_pool.set_processor(_release_only)
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(worker_pool.start())
        else:
            asyncio.run(worker_pool.start())
        log.info("Request queue worker started")
    except Exception as exc:
        log.warning("Request queue worker start deferred: %s", exc)


_OLLAMA_RETRY_BUDGET = 300.0  # seconds — total bounded retry window for model warmup
_OLLAMA_RETRY_BASE = 2.0      # seconds — initial backoff wait between warmup attempts
_OLLAMA_RETRY_MAX = 30.0      # seconds — upper bound for each backoff wait
_OLLAMA_WATCH_INTERVAL = 30.0 # seconds — how often the recovery watchdog probes Ollama


def _warmup_when_ready(
    warmup_fn,
    label,
    probe_fn=None,
    retry_budget=None,
    retry_base=None,
    retry_max=None,
) -> bool:
    """Run ``warmup_fn`` once Ollama is reachable, with bounded retry/backoff.

    Probe failures and warmup exceptions are retried until ``retry_budget``
    seconds have elapsed (default 300s). Returns True when the warmup ran,
    False when the budget was exhausted (non-fatal). Used by the startup
    warmup threads and the Ollama recovery watchdog.
    """
    import time

    from app.ingest.generator import is_ollama_available

    probe = probe_fn or is_ollama_available
    budget = retry_budget if retry_budget is not None else _OLLAMA_RETRY_BUDGET
    base = retry_base if retry_base is not None else _OLLAMA_RETRY_BASE
    cap = retry_max if retry_max is not None else _OLLAMA_RETRY_MAX

    deadline = time.monotonic() + budget
    wait = base
    while True:
        if probe():
            try:
                warmup_fn()
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("%s warmup failed (will retry): %s", label, exc)
                if time.monotonic() >= deadline:
                    return False
                time.sleep(wait)
                wait = min(wait * 2.0, cap)
                continue
        if time.monotonic() >= deadline:
            log.warning("%s warmup deferred (Ollama not ready within budget)", label)
            return False
        time.sleep(wait)
        wait = min(wait * 2.0, cap)


def _warmup_models() -> None:
    """Pre-load Ollama models so first user request is fast.

    Retries with bounded backoff while Ollama is not yet ready, so a startup
    that briefly precedes Ollama still ends up healthy instead of permanently
    degraded. If Ollama is down at startup the daemon threads keep retrying in
    the background and the Ollama watchdog re-warms when it recovers.
    Skipped under the test runner like the other heavy warmups, so the suite
    never spawns retry threads against a live Ollama.
    """
    if _is_testing():
        log.debug("Ollama model warmup skipped (test runner)")
        return
    import threading

    from app.ingest.generator import is_ollama_available

    def _warmup_llm():
        from app.ingest.generator import _build_payload as _build_llm_payload
        payload = _build_llm_payload("warmup", "CUS is a university")
        import httpx
        with httpx.Client(timeout=300.0) as c:
            resp = c.post(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                json={**payload, "stream": False, "keep_alive": f"{settings.OLLAMA_KEEP_ALIVE}s", "options": {**payload.get("options", {}), "num_predict": 1}},
            )
            if resp.status_code == 200:
                log.info("LLM model '%s' warmed up", settings.LLM_MODEL)
            else:
                raise RuntimeError(f"LLM warmup returned HTTP {resp.status_code}")

    def _warmup_embed():
        from app.ingest.embed import _ollama_embed
        _ollama_embed(["warmup"])
        log.info("Embed model '%s' warmed up", settings.EMBED_MODEL)

    # Warm up in parallel daemon threads — retries run inside them, so startup
    # is never blocked beyond the existing join budget.
    t1 = threading.Thread(target=_warmup_when_ready, args=(_warmup_embed, "Embed"), daemon=True)
    t2 = threading.Thread(target=_warmup_when_ready, args=(_warmup_llm, "LLM"), daemon=True)
    t1.start()
    t2.start()

    if not is_ollama_available():
        # Ollama not up yet — do not hold startup. The daemon threads keep
        # retrying with bounded backoff and the watchdog re-warms on recovery.
        log.info("Ollama not ready at startup — model warmup continues in the background")
        return

    # Ollama is reachable right now — wait for the warmups to finish so the
    # first user request is served with models already loaded.
    t1.join(timeout=120)
    t2.join(timeout=120)


def _ollama_probe() -> bool:
    """Quiet Ollama reachability probe with a short, bounded timeout.

    Deliberately not the shared 180s generation client — the watchdog must
    never hang on a half-open connection, and must not spam warning logs while
    Ollama is down.
    """
    import httpx
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


def _start_ollama_watchdog() -> None:
    """Recover the AI layer automatically when Ollama becomes unavailable.

    Health endpoints already probe Ollama per request, so the dashboard status
    is always truthful. This watchdog only reacts to a down->up transition by
    re-running the model warmup (keep-alive restoration), so a temporary
    Ollama outage heals without restarting FastAPI. Daemon thread, quiet
    bounded probes; never blocks, never crashes the app.
    """
    if _is_testing():
        log.debug("Ollama watchdog skipped (test runner)")
        return
    import threading
    import time

    def _loop() -> None:
        available = _ollama_probe()
        while True:
            time.sleep(_OLLAMA_WATCH_INTERVAL)
            now = _ollama_probe()
            if now and not available:
                log.info("Ollama became available — re-warming models")
                _warmup_models()
            elif available and not now:
                log.warning("Ollama became unavailable — will recover when it returns")
            available = now

    threading.Thread(target=_loop, daemon=True, name="ollama-watchdog").start()
    log.info("Ollama watchdog started (probe every %.0fs)", _OLLAMA_WATCH_INTERVAL)


def _is_testing() -> bool:
    """True when running under pytest (or explicitly asked to skip warmups).

    Heavy startup warmups are skipped here so `TestClient(app)` test suites
    don't pay a ~20s model load on every entered client. Production/dev startup
    (uvicorn) performs the warmups and first user queries stay fast.
    """
    return "pytest" in sys.modules or os.getenv("CUS_SKIP_STARTUP_WARMUP") == "1"


def _warmup_intent_classifier() -> None:
    """Pre-load the semantic intent model + centroids so first query is fast.

    Runs synchronously (blocking): the sentence-transformers import + model
    load takes ~15-20s on this machine, and deferring it to a daemon thread
    leaves the first user query paying the cold start on the request path.
    """
    if _is_testing():
        log.debug("Intent classifier warmup skipped (test runner)")
        return
    try:
        from app.orchestrator.intent_classifier import warmup
        warmup()
        log.info("Intent classifier warmed up (model + centroids ready)")
    except Exception as exc:
        log.warning("Intent classifier warmup failed (non-fatal): %s", exc)


def _warmup_retrieval() -> None:
    """Pre-build the BM25 index and open the Chroma client so first RAG is fast.

    The first `hybrid_search` on a fresh process spends ~7-9s refreshing the
    BM25 index (reads every chunk from on-disk Chroma). Warming it at startup
    moves that one-time cost off the first user query.
    """
    if _is_testing():
        log.debug("Retrieval warmup skipped (test runner)")
        return
    try:
        from app.ingest.retriever import get_bm25

        get_bm25().search("warmup", top_k=1)
        log.info("Retrieval warmup complete (BM25 index + Chroma client ready)")
    except Exception as exc:
        log.warning("Retrieval warmup failed (non-fatal): %s", exc)


def _seed_admin() -> None:
    import uuid

    from sqlalchemy.orm import Session

    from app.auth.security import hash_password
    from app.database import SessionLocal
    from app.models import User

    db: Session = SessionLocal()
    try:
        # Seed only when no admin/superadmin exists at all — the default admin may
        # have renamed themselves (profile feature), and we must not re-create it.
        existing = db.query(User).filter(User.role.in_(["admin", "superadmin"])).first()
        if existing:
            return
        admin = User(
            id=uuid.uuid4(),
            username=settings.SEED_ADMIN_USERNAME,
            email=settings.SEED_ADMIN_EMAIL,
            hashed_password=hash_password(settings.SEED_ADMIN_PASSWORD),
            role="superadmin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        log.info("Seeded superadmin user '%s'", settings.SEED_ADMIN_USERNAME)
    finally:
        db.close()


def _seed_students() -> None:
    """Seed test student accounts for development/demo."""
    import uuid

    from sqlalchemy.orm import Session

    from app.database import SessionLocal
    from app.models import Student
    from app.student.dob import hash_dob

    test_students = [
        {"reg_no": "CUS-2023-0001", "roll_no": "23001", "name": "Aarav Sharma", "father_name": "Rajesh Sharma", "mother_name": "Sunita Sharma", "dob": "15-Apr-2005", "gender": "Male", "category": "General", "college": "Sri Pratap College, Srinagar", "programme": "bca", "semester": 4, "admission_year": 2023, "batch": "2023-2026", "status": "active"},
        {"reg_no": "CUS-2023-0002", "roll_no": "23002", "name": "Priya Singh", "father_name": "Vikram Singh", "mother_name": "Anita Singh", "dob": "22-Aug-2004", "gender": "Female", "category": "OBC", "college": "Amar Singh College, Srinagar", "programme": "bba", "semester": 4, "admission_year": 2023, "batch": "2023-2026", "status": "active"},
        {"reg_no": "CUS-2022-0003", "roll_no": "22003", "name": "Rohit Kumar", "father_name": "Suresh Kumar", "mother_name": "Geeta Devi", "dob": "10-Jan-2003", "gender": "Male", "category": "SC", "college": "Government Degree College, Bemina", "programme": "bsc", "semester": 6, "admission_year": 2022, "batch": "2022-2025", "status": "active"},
        {"reg_no": "CUS-2024-0004", "roll_no": "24004", "name": "Anjali Verma", "father_name": "Ravi Verma", "mother_name": "Sita Verma", "dob": "05-Jun-2006", "gender": "Female", "category": "General", "college": "Women's College, Sopore", "programme": "ba", "semester": 2, "admission_year": 2024, "batch": "2024-2027", "status": "active"},
        {"reg_no": "CUS-2023-0005", "roll_no": "23005", "name": "Vikram Patel", "father_name": "Mohan Patel", "mother_name": "Kavita Patel", "dob": "18-Nov-2004", "gender": "Male", "category": "General", "college": "Sri Pratap College, Srinagar", "programme": "bcom", "semester": 4, "admission_year": 2023, "batch": "2023-2026", "status": "active"},
    ]

    db: Session = SessionLocal()
    try:
        existing_count = db.query(Student).count()
        if existing_count > 0:
            log.info("Students table already has %d records — skipping seed", existing_count)
            return
        for s in test_students:
            student = Student(
                id=uuid.uuid4(),
                reg_no=s["reg_no"],
                roll_no=s.get("roll_no"),
                name=s["name"],
                father_name=s.get("father_name"),
                mother_name=s.get("mother_name"),
                gender=s.get("gender"),
                category=s.get("category"),
                college=s.get("college"),
                programme=s["programme"],
                current_semester=s["semester"],
                admission_year=s["admission_year"],
                batch=s.get("batch"),
                status=s.get("status", "active"),
                hashed_password=hash_dob(s["dob"]),
                is_active=True,
            )
            db.add(student)
        db.commit()
        log.info("Seeded %d test students", len(test_students))
    finally:
        db.close()


def _seed_demo_service_data() -> None:
    """Seed demo service data (results, attendance, fees, etc.) if tables are empty."""
    from sqlalchemy.orm import Session

    from app.database import SessionLocal

    from app.catalogue.seed import seed_catalogue
    from app.seeders.demo_data import seed_demo_data
    db: Session = SessionLocal()
    try:
        count = seed_demo_data(db, count=settings.DEMO_STUDENT_COUNT)
        if count:
            log.info("Demo data seeded for %d students", count)
        prog_count = seed_catalogue(db)
        if prog_count:
            log.info("Academic catalogue seeded with %d programmes", prog_count)
    except Exception as exc:
        log.warning("Demo data seeding skipped: %s", exc)
    finally:
        db.close()


def _warmup_authority_cache() -> None:
    """Load authority cache on startup so lookups are instant."""
    from app.authority.service import authority_service
    from app.database import SessionLocal
    try:
        db = SessionLocal()
        try:
            authority_service.load_cache(db)
            log.info("Authority cache loaded (%d offices)", len(authority_service.list_active()))
        finally:
            db.close()
    except Exception as exc:
        log.warning("Authority cache warmup skipped (table may not exist yet): %s", exc)


# ----- Uploaded files (avatars) served under /api/uploads -----
_uploads_dir = Path(__file__).resolve().parent.parent / "uploads"
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    f"{settings.API_PREFIX}/uploads",
    StaticFiles(directory=str(_uploads_dir)),
    name="uploads",
)

# ----- Frontend static files (serves the site on the configured PORT) -----
_frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_redirect():
    return RedirectResponse(url="/pages/admin.html")


@app.get("/authority-admin", include_in_schema=False)
@app.get("/authority-admin/", include_in_schema=False)
@app.get("/authority/login", include_in_schema=False)
@app.get("/authority/login/", include_in_schema=False)
@app.get("/authority/dashboard", include_in_schema=False)
@app.get("/authority/dashboard/", include_in_schema=False)
def authority_admin_redirect():
    return RedirectResponse(url="/pages/authority-admin.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/pages/index.html")


if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")


# ----- API health checked from the public router -----
# GET /api/health is defined in app.public.routes
