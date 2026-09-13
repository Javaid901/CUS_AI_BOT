"""
Test isolation: run the suite against a throwaway SQLite database so that
test-created records (authorities, grievances, users, ...) never leak into
the live development database (cus_ai.db).

The Website Sync state machine and the Phase 1 raw-document store are also
routed into throwaway locations — tests must never touch the tracked
./sync_downloads/website_sync_state.json nor the default ./data/sync_documents
raw directory. These are set unconditionally so they win regardless of which
test module is imported first (settings freeze at the first app.config import).
"""
import os
import tempfile

_TEST_ROOT = tempfile.mkdtemp(prefix="cus_test_env_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TEST_ROOT, 'test.db')}"

# Website sync runtime state must stay out of the tracked production file.
os.environ["WEBSITE_SYNC_STATE_FILE"] = os.path.join(_TEST_ROOT, "website_sync_state.json")

# Phase 1 raw-document preservation must stay out of the production raw store.
os.environ["WEBSITE_SYNC_RAW_DIR"] = os.path.join(_TEST_ROOT, "sync_documents")