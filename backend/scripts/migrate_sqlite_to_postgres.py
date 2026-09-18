"""Migrate representative SQLite data to isolated PostgreSQL (Phase 3C-2, copy-only).

The SQLite source is never modified.  The script:
    1. takes a consistent snapshot of the live SQLite file (online backup API)
    2. builds a deterministic FK-safe insertion order (parents first)
    3. copies every table in batches
    4. ALWAYS respects PostgreSQL foreign keys: rows whose FK values have no
       parent row in the SOURCE (pre-existing orphans that SQLite tolerated
       because it does not enforce FKs) are excluded from the copy and are
       reported in a JSON orphan-inventory in the temp validation area.

This is a documented, deterministic decision — orphan rows are NOT silently
repaired or dropped from the source; they are itemized so their impact can be
assessed (see Phase 3C-2 §15 / final report).  No row is ever modified.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # backend/
sys.path.insert(0, str(ROOT))

if len(sys.argv) < 3:
    sqlite_path = os.environ.get("SQLITE_SOURCE", str(ROOT / "cus_ai.db"))
    pg_uri_file = os.environ.get("PG_URI_FILE", str(Path(tempfile.gettempdir()) / "opencode" / "p3c2" / "uri.txt"))
else:
    sqlite_path = sys.argv[1]
    pg_uri_file = sys.argv[2]

PG_URI = Path(pg_uri_file).read_text(encoding="utf-8").strip()
assert PG_URI.startswith("postgresql"), f"Expected a postgresql URI, got: {PG_URI}"

from sqlalchemy import create_engine, inspect, insert, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402


def _isolated_snapshot(source_path: str) -> str:
    import sqlite3

    snap_dir = Path(tempfile.gettempdir()) / "opencode" / "p3c2"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path = str(snap_dir / ("sqlite_snapshot_%d.db" % int(time.time() * 1000)))
    src = sqlite3.connect(source_path)
    try:
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"Snapshot created : {snap_path}")
    return snap_path


class MigrationPlanner:
    def __init__(self, src_eng, dst_eng, insp):
        self.src = Session(src_eng)
        self.dst = Session(dst_eng)
        self.insp = insp
        # PK values actually copied for each table (parents copied before children)
        self.copied_pk: dict[str, set] = {}
        self.orphan_inventory: dict[str, dict] = {}

    def _pk_values_of(self, table_name: str, rows) -> list:
        meta = Base.metadata.tables.get(table_name)
        if meta is None:
            return []
        pk_cols = list(meta.primary_key.columns)
        if not pk_cols:
            return []
        return [r.get(pk_cols[0].name) for r in rows if r.get(pk_cols[0].name) is not None]

    def _parent_copied_pk(self, ref_table: str) -> set:
        """PK values of the referenced table present in the TARGET (copied so far)."""
        if ref_table in self.copied_pk:
            return self.copied_pk[ref_table]
        # Fallback: nothing copied yet for that parent -> empty set
        return set()

    def _valid_rows(self, table_name: str, rows):
        """Split rows into (valid, excluded) using the targeted contract.

        A row is excluded if:
          * any non-null FK value names a row not present in the target
            (true source orphan -> cannot exist in PostgreSQL), or
          * any value exceeds its declared String(n) width (SQLite stores it
            silently; PostgreSQL rejects it).

        Exclusions are itemized -- never repaired.
        """
        if not rows:
            return rows, []
        fk_plan = []
        for fk in self.insp.get_foreign_keys(table_name):
            cols = fk.get("constrained_columns") or []
            ref_table = fk.get("referred_table")
            if not cols or not ref_table:
                continue
            meta = Base.metadata.tables.get(ref_table)
            if meta is None:
                continue
            fk_plan.append({
                "child_col": cols[0],
                "ref_table": ref_table,
                "parent_pk": self._parent_copied_pk(ref_table),
            })
        meta = Base.metadata.tables[table_name]
        length_limits = {}
        for col in meta.columns:
            m = re.fullmatch(r"(?:varchar|string)\((\d+)\)", str(col.type).lower())
            if m:
                length_limits[col.name] = int(m.group(1))
        valid, bad = [], []
        for r in rows:
            reason = None
            for entry in fk_plan:
                val = r.get(entry["child_col"])
                if val is not None and val not in entry["parent_pk"]:
                    reason = entry["ref_table"]
                    break
            if reason is None:
                for cname, limit in length_limits.items():
                    val = r.get(cname)
                    if isinstance(val, str) and len(val) > limit:
                        reason = f"over-length:{cname}(>{limit})"
                        break
            if reason:
                bad.append((r, reason))
            else:
                valid.append(r)
        if bad:
            self.orphan_inventory.setdefault(table_name, {
                "table": table_name,
                "total_rows": len(rows),
                "excluded_rows": len(bad),
                "reasons": sorted({r[1] for r in bad}),
                "example_ids": [str(r[0].get("id", r[0].get("conversation_id"))) for r in bad[:5]],
            })
        return [r for r in valid], [r for r, _ in bad]

    def copy_table(self, table_name: str) -> tuple[int, int]:
        meta = Base.metadata.tables.get(table_name)
        if meta is None:
            return 0, 0
        rows = self.src.execute(select(meta)).mappings().all()
        if not rows:
            self.copied_pk[table_name] = set()
            return 0, 0
        valid, bad = self._valid_rows(table_name, rows)
        cols = [c.name for c in meta.columns]
        batch_size = 500
        total = 0
        for i in range(0, len(valid), batch_size):
            batch = [{k: v for k, v in dict(r).items() if k in cols} for r in valid[i:i + batch_size]]
            if not batch:
                continue
            self.dst.execute(insert(meta), batch)
            total += len(batch)
        self.dst.commit()
        self.copied_pk[table_name] = set(self._pk_values_of(table_name, valid))
        return total, len(bad)


def _topo_sort_tables(insp) -> list[str]:
    tables = insp.get_table_names()
    fk_parents: dict[str, set[str]] = {t: set() for t in tables}
    for t in tables:
        for fk in insp.get_foreign_keys(t):
            ref = fk.get("referred_table")
            if ref and ref in fk_parents:
                fk_parents[t].add(ref)
    in_degree = {t: 0 for t in tables}
    children: dict[str, list[str]] = defaultdict(list)
    for child, parents in fk_parents.items():
        for p in parents:
            children[p].append(child)
            in_degree[child] += 1
    queue = sorted(t for t, d in in_degree.items() if d == 0)
    order: list[str] = []
    while queue:
        t = queue.pop(0)
        order.append(t)
        for c in sorted(children[t]):
            in_degree[c] -= 1
            if in_degree[c] == 0:
                queue.append(c)
    if len(order) != len(tables):
        raise RuntimeError(f"Circular FK dependency among: {set(tables) - set(order)}")
    return order


def main() -> int:
    snapshot = _isolated_snapshot(sqlite_path)
    src_eng = create_engine(f"sqlite:///{snapshot}")
    dst_eng = create_engine(PG_URI)
    print(f"PG target                : {PG_URI.split('@', 1)[-1] if '@' in PG_URI else PG_URI}")

    insp = inspect(src_eng)
    order = _topo_sort_tables(insp)
    print(f"Tables to migrate: {len(order)}")

    planner = MigrationPlanner(src_eng, dst_eng, insp)
    t0 = time.perf_counter()
    total_copied = 0
    total_orphans = 0
    for tbl in order:
        t = time.perf_counter()
        n, bad = planner.copy_table(tbl)
        dt = time.perf_counter() - t
        total_copied += n
        total_orphans += bad
        tag = "OK" if n > 0 else ("EMPTY" if bad == 0 else "ORPHANED")
        extra = f"  ({bad} orphan rows excluded)" if bad else ""
        print(f"  {tbl:42s} copied={n:>6d}  {dt*1000:>7.1f}ms  {tag}{extra}")
    elapsed = time.perf_counter() - t0
    print(f"\nCopy complete: {total_copied} rows across {len(order)} tables in {elapsed:.2f}s")
    print(f"Rows excluded (pre-existing orphan FKs / over-length values; reported, not repaired): {total_orphans}")

    exclusion_path = Path(tempfile.gettempdir()) / "opencode" / "p3c2" / "exclusion_inventory.json"
    exclusion_path.write_text(
        json.dumps(planner.orphan_inventory, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Exclusion inventory : {exclusion_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())