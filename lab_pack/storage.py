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
import time
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


# Serverless Postgres (Neon) suspends idle compute and terminates every
# connection with it. Probe before an append when the store has sat idle
# longer than this; the suspend threshold is ~5 minutes, production hit it
# after >10.
_LIVENESS_IDLE_SECONDS = 300


def _connection_error_classes() -> tuple:
    """Errors that mean 'the connection died', not 'the statement is wrong'.
    AdminShutdown (Neon's idle-suspend kill) subclasses OperationalError;
    'the connection is closed' IS OperationalError; the SSL EOF / connection
    reset shapes can escape as ssl.SSLError or a builtin ConnectionError.
    UniqueViolation subclasses IntegrityError and is deliberately NOT here —
    constraint violations must surface immediately (ADR-023)."""
    import ssl

    import psycopg
    return (psycopg.OperationalError, psycopg.InterfaceError,
            ssl.SSLError, ConnectionError)


def harden_store(store, *, url: Optional[str] = None,
                 on_reconnect: Optional[Callable] = None) -> bool:
    """Wrap a PostgresEventStore's operations with reconnect-on-failure.

    The upstream store owns ONE boot-lifetime connection; serverless
    Postgres guarantees that connection dies at the first idle suspend.
    Production signature (twice): the first post-idle append fails
    AdminShutdown ('terminating connection due to administrator command'),
    every later write fails OperationalError ('the connection is closed')
    until the process restarts. Nothing commits.

    On a connection-class error this re-establishes the connection and
    retries the operation exactly once; a second failure propagates, so the
    caller's structured-error path (ADR-023) surfaces it. Non-connection
    errors are never retried. Every successful reconnect calls
    `on_reconnect(triggering_exc)` — the server points that at the
    diagnostics ring buffer (kind=store_reconnected), NOT the event log,
    because the log is exactly what may be broken. Appends after a long
    idle additionally get a cheap SELECT 1 probe first, so the stale
    connection is usually replaced before the write is even attempted.

    Retrying an append is safe against double-commit: events carry
    UNIQUE(id, run_id), so a write that actually landed before the
    connection died re-raises as UniqueViolation — which is not retried.

    Returns True when the store was wrapped; False (no-op) for SQLite, a
    borrowed connection, or a pool — lifecycles this module doesn't own.
    Legal HERE and only here: this is the one backend-aware module
    (ADR-009)."""
    try:
        from activegraph.store.postgres import PostgresEventStore
    except Exception:
        return False
    if not isinstance(store, PostgresEventStore):
        return False
    source = store._source
    if getattr(source, "_owned_conn", None) is None:
        return False
    import psycopg
    url = url or store_url()
    conn_errors = _connection_error_classes()
    state = {"last_op": time.monotonic()}

    def _reconnect(trigger: BaseException) -> None:
        # Connect FIRST: if the database is unreachable the old (dead but
        # non-None) connection stays in place, so the NEXT operation fails
        # connection-class again and gets its own reconnect attempt instead
        # of wedging on a closed source.
        fresh = psycopg.connect(url, autocommit=True)
        source.close()
        source._owned_conn = fresh
        source._conn = fresh
        if on_reconnect is not None:
            try:
                on_reconnect(trigger)
            except Exception:
                pass

    def _probe_if_idle() -> None:
        if time.monotonic() - state["last_op"] < _LIVENESS_IDLE_SECONDS:
            return
        try:
            with source.cursor() as cur:
                cur.execute("SELECT 1")
        except conn_errors as exc:
            _reconnect(exc)

    def _wrap(name: str, *, probe: bool = False, materialize: bool = False):
        original = getattr(store, name)

        def call(*args, **kwargs):
            if probe:
                _probe_if_idle()
            try:
                result = (list(original(*args, **kwargs)) if materialize
                          else original(*args, **kwargs))
            except conn_errors as exc:
                _reconnect(exc)
                result = (list(original(*args, **kwargs)) if materialize
                          else original(*args, **kwargs))
            state["last_op"] = time.monotonic()
            return iter(result) if materialize else result

        call.__name__ = name
        setattr(store, name, call)

    _wrap("append", probe=True)
    # iter_events is a generator: materialize inside the retry window so a
    # mid-iteration connection death is retried as a whole operation (the
    # upstream implementation already fetches all rows before yielding).
    _wrap("iter_events", materialize=True)
    for name in ("get_event", "count", "truncate_after", "get_run",
                 "upsert_run"):
        _wrap(name)
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
