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

import functools
import os
from pathlib import Path
from typing import Callable, Optional


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


# The EventStore protocol surface the runtime exercises (append/iter_events/
# upsert_run at runtime; the rest for completeness — close stays unwrapped:
# closing a dead connection must never trigger a reconnect).
_STORE_OPS = ("append", "iter_events", "get_event", "count",
              "truncate_after", "get_run", "upsert_run")


def _is_connection_error(exc: BaseException) -> bool:
    """The failure class a dead connection produces. psycopg maps
    AdminShutdown (Neon's idle-suspend kill, sqlstate 57P01), 'the
    connection is closed', SSL EOF and connection reset all onto
    OperationalError; InterfaceError covers driver-level closed state.
    UniqueViolation and every other constraint/programming error sits
    under IntegrityError/ProgrammingError — disjoint, never retried."""
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def harden_store(store, *, url: Optional[str] = None,
                 record: Optional[Callable[[BaseException], object]] = None) -> bool:
    """Reconnect-on-failure for the runtime's PostgresEventStore.

    The production incident (twice, identical signature): Neon suspends an
    idle compute and kills its connections; the store holds a single
    boot-lifetime connection, so the first write after the suspend fails
    AdminShutdown ('terminating connection due to administrator command')
    and every subsequent write fails OperationalError ('the connection is
    closed') until a process restart. ADR-023 surfaced these correctly;
    nothing committed.

    Wraps the store's operations in place: on a connection-class error the
    connection is re-established and the operation retried exactly ONCE; a
    second failure — or a failed reconnect — surfaces structured per
    ADR-023. Non-connection errors are NEVER retried: a retried append
    whose first attempt actually committed trips UNIQUE(id, run_id) and
    surfaces as UniqueViolation instead of duplicating silently.

    `record(exc)` is invoked once per successful reconnect with the
    triggering exception; the server points it at the diagnostics ring
    buffer (kind=store_reconnected), NOT the event log — the log may be
    the casualty (ADR-023).

    Returns True when armed (idempotent). No-op on SQLite, and on stores
    whose connection the lab does not own (pool-backed or borrowed —
    upstream defines those lifecycles, ADR-009 note in the postgres store).
    Reaching into the store's connection internals is legal HERE and only
    here (ADR-009); the upstream candidate — reconnect-with-bounded-retry
    belongs in the store itself — is queued in LIVE_FINDINGS.
    """
    url = url or store_url()
    if not url.startswith("postgres"):
        return False
    source = getattr(store, "_source", None)
    if source is None or getattr(source, "_owned_conn", None) is None:
        return False
    if getattr(store, "_lab_reconnect_armed", False):
        return True

    def reconnect() -> None:
        import psycopg
        try:
            source._owned_conn.close()
        except Exception:
            pass
        conn = psycopg.connect(url, autocommit=True)
        source._owned_conn = conn
        source._conn = conn

    def retried(fn, materialize: bool = False):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            def attempt():
                out = fn(*args, **kwargs)
                return list(out) if materialize else out
            try:
                return attempt()
            except Exception as exc:
                if not _is_connection_error(exc):
                    raise
                reconnect()
                if record is not None:
                    try:
                        record(exc)
                    except Exception:
                        pass
                return attempt()
        return wrapped

    for name in _STORE_OPS:
        fn = getattr(store, name, None)
        if fn is None:
            continue
        # iter_events is lazy — the query runs on first next(), outside any
        # guard. Materialize inside the retry; upstream already fetchall()s,
        # so the memory profile is unchanged and a list iterates the same.
        setattr(store, name, retried(fn, materialize=(name == "iter_events")))
    store._lab_reconnect_armed = True
    return True


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
