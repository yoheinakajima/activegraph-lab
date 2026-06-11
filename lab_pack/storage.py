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
