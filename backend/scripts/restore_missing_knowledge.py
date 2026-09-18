"""CLI: restore ready-but-unindexed documents into the live Chroma collection.

Dry-run by default. Pass --apply to write vectors.

    python scripts/restore_missing_knowledge.py            # dry run
    python scripts/restore_missing_knowledge.py --apply    # write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.ingest.restore_missing import restore_missing_vectors  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write vectors to Chroma")
    parser.add_argument("--limit", type=int, default=None, help="max documents to process")
    parser.add_argument("--sqlite", default=None, help="path to cus_ai.db snapshot")
    args = parser.parse_args()

    print("CHROMA_PERSIST_DIR:", settings.CHROMA_PERSIST_DIR)  # noqa: T201
    print("resolved:", Path(settings.CHROMA_PERSIST_DIR).resolve())  # noqa: T201

    db = SessionLocal()
    try:
        report = restore_missing_vectors(
            db, sqlite_path=args.sqlite, apply=args.apply, limit=args.limit
        )
    finally:
        db.close()

    print(json.dumps(report, indent=2, default=str))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
