"""Storage adapter — KERNEL (ADR-009, ADR-012).

Backend selection happens HERE and only here: `LAB_DATABASE_URL` present →
activegraph's native PostgresEventStore (dedicated `activegraph` schema,
framework-owned tables, fork/replay native); else `DATABASE_URL` (Replit
reserves that name for its managed-Postgres module — see the ADR-009 note);
absent → SQLite under data/ (the dev/fixture default). No other code may
know which store is active — everything reads through runtime/event APIs,
never raw SQL against framework tables.

LAB_DATABASE_URL and DATABASE_URL are credentials (ADR-011): this module
never logs, stores, or echoes them. describe() returns only the backend
name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _db_url() -> str:
    """LAB_DATABASE_URL wins over DATABASE_URL (Replit reserves the latter)."""
    return (os.environ.get("LAB_DATABASE_URL", "").strip()
            or os.environ.get("DATABASE_URL", "").strip())


def store_url() -> str:
    """The persistence URL/path for Runtime(persist_to=...) / Runtime.load."""
    url = _db_url()
    if url:
        # activegraph's parser wants postgres:// — normalize the common alias.
        if url.startswith("postgresql://"):
            url = "postgres://" + url[len("postgresql://"):]
        return url
    data = _repo_root() / "data"
    data.mkdir(exist_ok=True)
    return str(os.environ.get("ACTIVEGRAPH_DB") or data / "lab.sqlite")


def backend() -> str:
    """'postgres' or 'sqlite' — for boot logs and /healthz, never the URL."""
    return "postgres" if _db_url() else "sqlite"


def store_has_run(url: Optional[str] = None) -> bool:
    """True if the configured store already holds a run (boot → resume)."""
    url = url or store_url()
    try:
        if url.startswith("postgres"):
            from activegraph.store.postgres import PostgresEventStore
            return PostgresEventStore.most_recent_run_id(url) is not None
        if "://" not in url and not os.path.exists(url):
            return False
        from activegraph.store import SQLiteEventStore
        return SQLiteEventStore.most_recent_run_id(url) is not None
    except Exception:
        return False


def repair_sequences(url: Optional[str] = None) -> int:
    """Restored-lineage repair (ADR-023). A row-level pg restore (data-only
    dump, CSV import, partial-run copy) moves the events rows but not the
    BIGSERIAL sequence behind events.seq. When nextval is at or below
    max(seq), every subsequent append dies with a UniqueViolation on
    events_pkey — committed in memory, never durable. Align the sequence
    past max(seq) before the runtime opens the store.

    Postgres only (SQLite's AUTOINCREMENT derives the next rowid from the
    table itself and cannot collide). Returns the number of sequence steps
    skipped forward, 0 when aligned or not applicable. Touching the
    framework's events table is legal HERE and only here: this module is the
    one place that knows the backend (ADR-009); projections still never read
    framework tables directly.
    """
    url = url or store_url()
    if not url.startswith("postgres"):
        return 0
    try:
        import psycopg
        with psycopg.connect(url, autocommit=True) as conn:
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            if not max_seq:
                return 0
            last, is_called = conn.execute(
                "SELECT last_value, is_called FROM events_seq_seq").fetchone()
            next_val = (last + 1) if is_called else last
            if next_val > max_seq:
                return 0
            conn.execute("SELECT setval('events_seq_seq', %s, true)", (max_seq,))
            return int(max_seq) - int(next_val) + 1
    except Exception:
        # A fresh database has no events table yet; anything else surfaces
        # the moment the runtime opens the store. Never block boot from here.
        return 0


def dev_reset() -> Optional[str]:
    """Wipe the dev store. SQLite only — for Postgres, state is managed and a
    reset is a deliberate manual act (drop the `activegraph` schema), not an
    HTTP endpoint. Returns an error string or None on success."""
    if backend() == "postgres":
        return ("reset is not supported on the postgres backend; "
                "drop the 'activegraph' schema manually if you really mean it")
    url = store_url()
    failed = []
    for p in (url, url + "-wal", url + "-shm"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            failed.append(p)
    return f"could not remove: {failed}" if failed else None
