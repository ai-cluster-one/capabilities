"""Test aid for tests/test_manager_migrate_plan_tables.py: with
CAPABILITIES_TEST_TABLES_BEHIND set, every migration through the shared
database layer runs all of an owner's steps but its last, so a store's
tables stand one step behind the code."""
import os

if os.environ.get("CAPABILITIES_TEST_TABLES_BEHIND"):
    try:
        import capabilities_contract.db as _db
    except ImportError:
        _db = None
    if _db is not None:
        _real = _db.migrate

        def _behind(conn, owner, steps, *, major, minor, schema=None):
            steps = list(steps)
            return _real(conn, owner, steps[:-1] if len(steps) > 1 else steps,
                         major=major, minor=minor, schema=schema)

        _db.migrate = _behind
