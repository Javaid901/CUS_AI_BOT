"""Verify SQLite snapshot vs PostgreSQL migration parity (Phase 3C-2).

Compares:
    - table list
    - row counts per table
    - FK orphans in PostgreSQL (rows with dangling FK parents)
    - a deterministic sample of rows (UUID-when-reading, timestamps, JSON,
      nullable fields) for a set of key tables

Outputs a per-table verification report.  Zero unexplained differences is the
acceptance bar.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # backend/
sys.path.insert(0, str(ROOT))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from sqlalchemy import create_engine, func, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

if len(sys.argv) < 3:
    snapshot = os.environ.get("SQLITE_SNAPSHOT")
    pg_uri_file = os.environ.get(
        "PG_URI_FILE",
        str(Path(tempfile.gettempdir()) / "opencode" / "p3c2" / "uri.txt"),
    )
else:
    snapshot = sys.argv[1]
    pg_uri_file = sys.argv[2]

assert snapshot, "snapshot path required"
PG_URI = Path(pg_uri_file).read_text(encoding="utf-8").strip()
assert PG_URI.startswith("postgresql")

SRC = f"sqlite:///{snapshot}"
INVENTORY = Path(tempfile.gettempdir()) / "opencode" / "p3c2" / "exclusion_inventory.json"
EXCLUDED: dict[str, int] = {}
if INVENTORY.exists():
    EXCLUDED = {
        t: v.get("excluded_rows", 0)
        for t, v in json.loads(INVENTORY.read_text(encoding="utf-8")).items()
    }


def _norm(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True, default=str)
    if isinstance(v, (list, tuple)):
        return json.dumps(list(v), sort_keys=True, default=str)
    if isinstance(v, (int, float, bool, str)):
        return v
    if isinstance(v, datetime):
        # SQLite stores naive wall-clock; PG returns the same wall-clock with a
        # session-timezone offset.  Compare wall-clock to prove copy-faithfulness.
        return v.replace(tzinfo=None).isoformat()
    return str(v)


def main() -> int:
    src_eng = create_engine(SRC)
    dst_eng = create_engine(PG_URI)
    src_insp = inspect(src_eng)
    dst_insp = inspect(dst_eng)

    src_tables = set(src_insp.get_table_names())
    dst_tables = set(dst_insp.get_table_names())
    # Tables present in SQLite but absent from the ORM metadata are outside the
    # authoritative schema / Alembic baseline.  Reported separately.
    unmanaged = sorted(t for t in src_tables if t not in Base.metadata.tables)
    missing_in_pg = sorted(t for t in src_tables - dst_tables if t in Base.metadata.tables)
    extra_in_pg = sorted(dst_tables - src_tables - {"alembic_version"})
    if unmanaged:
        print(f"UNMANAGED (non-ORM) SOURCE TABLES: {unmanaged}")
        with Session(src_eng) as s:
            for t in unmanaged:
                n = s.execute(text(f"SELECT count(*) FROM \"{t}\"")).scalar() or 0
                print(f"    {t}: {n} rows (not in ORM/Alembic metadata)")

    print("=" * 78)
    print("ROW-COUNT COMPARISON (SQLite snapshot vs PostgreSQL)")
    print("=" * 78)
    total_s = total_p = 0
    hard_diffs = []
    explained = []
    with Session(src_eng) as s, Session(dst_eng) as d:
        for tbl in sorted(src_tables):
            if tbl in missing_in_pg or tbl in unmanaged:
                continue
            meta = Base.metadata.tables[tbl]
            rs = s.execute(select(func.count()).select_from(meta)).scalar() or 0
            rp = d.execute(select(func.count()).select_from(meta)).scalar() or 0
            total_s += rs
            total_p += rp
            expected = EXCLUDED.get(tbl, 0)
            delta = rs - rp
            if delta == 0:
                status = "MATCH"
            elif expected > 0 and delta == expected:
                status = f"OK-EXPECTED (inventory={expected})"
                explained.append((tbl, delta))
            else:
                status = f"!! UNEXPLAINED delta={delta} (inventory={expected})"
                hard_diffs.append(tbl)
            mark = "OK " if status.startswith("OK") or status == "MATCH" else "!! "
            print(f"{mark}{tbl:42s} sqlite={rs:>7d}  pg={rp:>7d}  {status}")
    print("-" * 78)
    print(f"TOTAL  sqlite={total_s}  pg={total_p}  expected-excluded={sum(EXCLUDED.values())}")
    print(f"MISSING TABLES IN PG : {missing_in_pg or 'none'}")
    print(f"EXTRA TABLES IN PG   : {extra_in_pg or 'none (besides alembic_version)'}")

    print("\nFK-ORPHAN CHECK (PostgreSQL, strict enforcement):")
    orphans_total = 0
    with Session(dst_eng) as d:
        for tbl in sorted(dst_tables - {"alembic_version"}):
            meta = Base.metadata.tables.get(tbl)
            if meta is None:
                continue
            for fk in dst_insp.get_foreign_keys(tbl):
                cols = fk.get("constrained_columns") or []
                ref_table = fk.get("referred_table")
                ref_cols = fk.get("referred_columns") or []
                if not cols or not ref_table:
                    continue
                parent = Base.metadata.tables.get(ref_table)
                if parent is None:
                    continue
                child_col = meta.c[cols[0]]
                parent_col = parent.c[ref_cols[0]]
                parent_vals = d.execute(select(parent_col)).scalars().all()
                parent_set = set(parent_vals)
                bad = d.execute(
                    select(func.count()).select_from(meta).where(
                        child_col.isnot(None),
                        ~child_col.in_(parent_set),
                    )
                ).scalar() or 0
                if bad:
                    orphans_total += bad
                    print(f"  !! {tbl}.{cols[0]} -> {ref_table}: {bad} orphan rows")
    if orphans_total == 0:
        print("  no orphan rows found")

    KEY_TABLES = [
        "users", "students", "authorities", "grievances", "grievance_status_history",
        "grievance_notifications", "university_notices", "date_sheet_entries",
        "programmes", "programme_subjects", "student_results", "student_exam_forms",
        "documents", "conversations", "messages", "analytics_sessions",
        "aggregated_metrics", "website_pages", "grievance_categories", "fee_receipts",
    ]
    print("\nREPRESENTATIVE ROW SAMPLES (first 3 rows per key table):")
    sample_issues = 0
    with Session(src_eng) as s, Session(dst_eng) as d2:
        for tbl in KEY_TABLES:
            if tbl not in src_tables or tbl in missing_in_pg:
                continue
            meta = Base.metadata.tables[tbl]
            a_rows = s.execute(select(meta).limit(3)).mappings().all()
            b_rows = d2.execute(select(meta).limit(3)).mappings().all()
            if len(a_rows) != len(b_rows):
                print(f"  !! {tbl}: sample slice length differs ({len(a_rows)} vs {len(b_rows)})")
                sample_issues += 1
                continue
            for a, b in zip(a_rows, b_rows):
                diffs_found = [
                    c.name for c in meta.columns if _norm(a.get(c.name)) != _norm(b.get(c.name))
                ]
                if diffs_found:
                    sample_issues += 1
                    print(f"  !! {tbl}: fields differ -> {diffs_found[:5]}")
                    for name in diffs_found[:3]:
                        print(f"      - {name}: {_norm(a.get(name))!r} vs {_norm(b.get(name))!r}")
    if sample_issues == 0:
        print("  samples match")

    print("\n" + "=" * 78)
    unmanaged_rows = 0
    with Session(src_eng) as s:
        for t in unmanaged:
            unmanaged_rows += s.execute(text(f"SELECT count(*) FROM \"{t}\"")).scalar() or 0
    ok = (not hard_diffs) and (not missing_in_pg) and orphans_total == 0 and sample_issues == 0 and unmanaged_rows == 0
    print("VERIFICATION:", "PASS" if ok else "FAIL")
    if explained:
        print(f"Explained diffs matched exclusion inventory: {sum(d for _, d in explained)} rows")
    if unmanaged and unmanaged_rows == 0:
        print(f"Unmanaged source tables ({len(unmanaged)}) are empty -> no data left behind")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())