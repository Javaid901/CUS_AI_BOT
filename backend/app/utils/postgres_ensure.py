"""
backend/app/utils/postgres_ensure.py

Automatic PostgreSQL availability for the project's embedded PostgreSQL 18.x
instance, invoked synchronously at FastAPI startup (before create_all / any
database access) so ``python -m uvicorn app.main:app`` "just works".

Rules honored here (see the surgical-startup task):
  * uvicorn stays the only HTTP server; this helper only manages the database.
  * PostgreSQL is started ONLY when the configured instance is NOT running
    (never on every boot, never "another server might be running" warnings).
  * The port is read from DATABASE_URL (63874 here), never assumed as 5432.
  * Binaries (pg_ctl/postgres) come from the SAME embedded_postgres install the
    running instance uses (CUS_AI_PG_BIN override OK).
  * Data directory is the existing cluster (CUS_AI_PGDATA override OK), never
    created, recreated, initialized, migrated, or modified.
  * Startup fails loudly (real pg_ctl output + postgres log tail) if the
    instance cannot become ready within a bounded timeout.
  * PostgreSQL is deliberately NOT stopped when uvicorn shuts down: it is a
    database service and stays running for the next start.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("cus_ai")

_START_TIMEOUT: int = 60    # bound for `pg_ctl -w start` itself
_READY_TIMEOUT: int = 30    # bound for polling a real configured-DB connection
_PROBE_INTERVAL: float = 0.5


def _is_test() -> bool:
    """Never touch postgres from the test runner (conftest uses temp SQLite)."""
    return "pytest" in sys.modules or os.getenv("CUS_SKIP_STARTUP_WARMUP") == "1"


def _resolve_pg_tools() -> tuple[Path, Path, Path, Path]:
    """Resolve pg_ctl/postgres/psql + data dir from the actual environment."""
    pg_bin_env = os.getenv("CUS_AI_PG_BIN")
    if pg_bin_env:
        pg_bin = Path(pg_bin_env)
    else:
        import embedded_postgres  # the install already used by this instance

        pg_bin = Path(embedded_postgres.__file__).parent / "pginstall" / "bin"
    pg_ctl = pg_bin / "pg_ctl.exe"
    psql = pg_bin / "psql.exe"
    if not pg_ctl.exists() or not psql.exists():
        raise RuntimeError(
            f"PostgreSQL binaries not found under {pg_bin} "
            f"(looked for pg_ctl.exe and psql.exe). Set CUS_AI_PG_BIN to the bin dir."
        )
    pgdata = Path(
        os.getenv("CUS_AI_PGDATA")
        or (Path(os.environ.get("TEMP", os.environ.get("TMP", "."))) / "opencode" / "p3c2" / "pgdata")
    )
    if not (pgdata / "PG_VERSION").exists():
        raise RuntimeError(
            f"PostgreSQL data directory not found or invalid: {pgdata} "
            f"(want the existing cluster, set CUS_AI_PGDATA)."
        )
    return pg_bin, pgdata, pg_ctl, psql


def _pg_ctl_status(pg_ctl: Path, pgdata: Path) -> tuple[int, str]:
    """Run `pg_ctl status -D <pgdata>`; return (returncode, combined output)."""
    proc = subprocess.run(
        [str(pg_ctl), "status", "-D", str(pgdata)],
        capture_output=True,
        timeout=_START_TIMEOUT,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out + err


def _readiness_probe(host: str, port: int, user: str, password: str, dbname: str) -> bool:
    """Try a real connection to the configured database with configured creds."""
    import psycopg

    try:
        with psycopg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
            connect_timeout=3,
        ) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _run_pg_ctl_start(pg_ctl: Path, pgdata: Path, logfile: Path, host: str, port: int) -> tuple[int, str]:
    """Start ONLY the configured instance; wait for pg_ctl to return; give output.

    Critical Windows detail: `pg_ctl start` spawns a long-lived postgres
    process that INHERITS pg_ctl's stdout/stderr handles. Using PIPE here
    would block forever reading from handles postgres never closes (the
    embedded_postgres library documents exactly this). We therefore capture
    pg_ctl's output into temporary FILES (no pipes), which are safe to read
    back even while postgres holds copies of the freshly-opened file handles.
    """
    import tempfile

    with tempfile.TemporaryFile("w+") as stdout_f, tempfile.TemporaryFile("w+") as stderr_f:
        proc = subprocess.run(
            [
                str(pg_ctl),
                "start",
                "-D",
                str(pgdata),
                "-l",
                str(logfile),
                "-o",
                f'-p {port} -h "{host}"',
                "-w",
                "-t",
                str(_START_TIMEOUT),
            ],
            stdout=stdout_f,
            stderr=stderr_f,
            timeout=_START_TIMEOUT + 15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        stdout_f.seek(0)
        stderr_f.seek(0)
        out = stdout_f.read()
        err = stderr_f.read()
    return proc.returncode, out + err


def ensure_postgresql_running() -> None:
    """Idempotently ensure the configured PostgreSQL instance accepts connections.

    Returns normally when PostgreSQL is ready; raises RuntimeError (with the
    real error) when it cannot be made ready within a bounded timeout. Never
    stops PostgreSQL. No-op for SQLite setups and inside pytest.
    """
    if _is_test():
        return

    from app.config import settings

    url = settings.DATABASE_URL
    if not (url or "").startswith("postgresql"):
        return  # SQLite/other backend: nothing to ensure

    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    host = parsed.host or "127.0.0.1"
    port = parsed.port or 5432
    user = parsed.username or "postgres"
    password = parsed.password or ""
    dbname = parsed.database or "postgres"

    pg_bin, pgdata, pg_ctl, psql = _resolve_pg_tools()
    logfile = pgdata / "log"
    _ = psql  # keep the tuple simple; psql available if ever needed

    # A. already running? (pg_ctl status rc=0 means a LIVE postmaster for this pgdata)
    rc, status_out = _pg_ctl_status(pg_ctl, pgdata)
    if rc == 0:
        if _readiness_probe(host, port, user, password, dbname):
            logger.info("PostgreSQL: already running (host=%s port=%d db=%s)", host, port, dbname)
            return
        raise RuntimeError(
            "PostgreSQL status says running on port %d but the configured database "
            "%s is NOT accepting connections with DATABASE_URL credentials.\n%s"
            % (port, dbname, status_out.strip())
        )

    # B. not running -> start ONLY this configured instance, then prove readiness
    logger.info("PostgreSQL: starting... (pgdata=%s host=%s port=%d)", pgdata, host, port)
    start_rc, start_out = _run_pg_ctl_start(pg_ctl, pgdata, logfile, host, port)
    if start_rc != 0:
        tail = ""
        if logfile.exists():
            lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-20:]) if lines else ""
        raise RuntimeError(
            "PostgreSQL failed to start.\npg_ctl output:\n%s%s"
            % (start_out.strip(), ("\npostgres log tail:\n" + tail if tail else ""))
        )

    # C. wait until the configured database actually accepts connections
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if _readiness_probe(host, port, user, password, dbname):
            logger.info("PostgreSQL: ready (host=%s port=%d db=%s)", host, port, dbname)
            return
        time.sleep(_PROBE_INTERVAL)

    tail = ""
    if logfile.exists():
        lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-20:]) if lines else ""
    raise RuntimeError(
        "PostgreSQL failed to become ready within %ds.\npg_ctl output:\n%s%s"
        % (_READY_TIMEOUT, start_out.strip(), ("\npostgres log tail:\n" + tail if tail else ""))
    )