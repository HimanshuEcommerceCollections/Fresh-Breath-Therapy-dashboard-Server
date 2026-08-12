"""Phase timing for the import pipeline: wall time, query count, query time.

Instrumentation lives at the engine, not at the call sites. Hand-placed
timers around individual queries drift out of date the moment someone adds a
statement, and they cannot see the queries SQLAlchemy issues on your behalf —
lazy loads, flush ordering, pool pre-ping. A `before_cursor_execute` /
`after_cursor_execute` pair counts everything that actually reaches the wire.

Off unless a `profile()` block is active, so production pays nothing beyond
two no-op function calls per statement.

Why query TIME is reported separately from wall time: this database is a
~300ms round trip away, so nearly all of a phase's wall time is usually
waiting rather than working. Seeing the two side by side is what tells you
whether a phase needs fewer round trips or less computation — and for this
pipeline the answer is almost always fewer round trips.
"""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import event


@dataclass
class PhaseStat:
    name: str
    wall: float = 0.0
    queries: int = 0
    query_time: float = 0.0
    rows: int = 0
    # Counted separately because none of these fire before_cursor_execute, yet
    # every one is a full round trip. Leaving them out is what made phase
    # timings look unexplained: a phase with "2 queries" taking 2.3s was
    # really 2 queries plus a COMMIT plus a pre_ping on checkout.
    commits: int = 0
    rollbacks: int = 0
    checkouts: int = 0

    @property
    def round_trips(self) -> int:
        """Everything that crosses the wire. This is the number that matters:
        at ~500ms each, wall time is round_trips × latency and little else."""
        return self.queries + self.commits + self.rollbacks + self.checkouts

    @property
    def waiting_pct(self) -> float:
        return (self.query_time / self.wall * 100) if self.wall else 0.0


@dataclass
class Profile:
    phases: list[PhaseStat] = field(default_factory=list)
    _current: PhaseStat | None = None

    @property
    def total_wall(self) -> float:
        return sum(p.wall for p in self.phases)

    @property
    def total_queries(self) -> int:
        return sum(p.queries for p in self.phases)

    @property
    def total_round_trips(self) -> int:
        return sum(p.round_trips for p in self.phases)

    def table(self, title: str = "") -> str:
        out = []
        if title:
            out.append(title)
        out.append(
            f"{'phase':<32}{'wall':>8}{'qry':>5}{'cmt':>5}{'chk':>5}"
            f"{'trips':>7}{'ms/trip':>9}"
        )
        out.append("-" * 71)
        for p in self.phases:
            per = (p.wall / p.round_trips * 1000) if p.round_trips else 0
            out.append(
                f"{p.name:<32}{p.wall:7.2f}s{p.queries:5}{p.commits:5}"
                f"{p.checkouts:5}{p.round_trips:7}{per:8.0f}"
            )
        out.append("-" * 71)
        trips = self.total_round_trips
        per = (self.total_wall / trips * 1000) if trips else 0
        out.append(
            f"{'TOTAL':<32}{self.total_wall:7.2f}s"
            f"{self.total_queries:5}{sum(p.commits for p in self.phases):5}"
            f"{sum(p.checkouts for p in self.phases):5}{trips:7}{per:8.0f}"
        )
        return "\n".join(out)


_active: contextvars.ContextVar[Profile | None] = contextvars.ContextVar(
    "import_profile", default=None
)
_installed = False


def install(engine) -> None:
    """Attach the counters. Idempotent; safe to call from anywhere."""
    global _installed
    if _installed:
        return
    sync_engine = getattr(engine, "sync_engine", engine)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if _active.get() is not None:
            conn.info["_profile_t0"] = time.perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        stat = _stat()
        if stat is None:
            return
        started = conn.info.pop("_profile_t0", None)
        stat.queries += 1
        if started is not None:
            stat.query_time += time.perf_counter() - started

    # COMMIT, ROLLBACK and connection checkout never reach
    # before_cursor_execute, but each is a full round trip. Counting only
    # cursor executes is what left ~5s of a 6.6s preview unexplained.
    @event.listens_for(sync_engine, "commit")
    def _commit(conn):
        stat = _stat()
        if stat is not None:
            stat.commits += 1

    @event.listens_for(sync_engine, "rollback")
    def _rollback(conn):
        stat = _stat()
        if stat is not None:
            stat.rollbacks += 1

    @event.listens_for(sync_engine.pool, "checkout")
    def _checkout(dbapi_conn, record, proxy):
        # With pool_pre_ping on, a checkout costs its own SELECT 1 — measured
        # at ~423ms, and invisible unless counted here.
        stat = _stat()
        if stat is not None:
            stat.checkouts += 1

    _installed = True


def _stat() -> PhaseStat | None:
    profile = _active.get()
    return profile._current if profile is not None else None


@contextmanager
def profile():
    """Collect phase stats for everything inside."""
    p = Profile()
    token = _active.set(p)
    try:
        yield p
    finally:
        _active.reset(token)


@contextmanager
def phase(name: str, rows: int = 0):
    """Time one named phase. A no-op outside a `profile()` block."""
    p = _active.get()
    if p is None:
        yield None
        return
    stat = PhaseStat(name=name, rows=rows)
    previous, p._current = p._current, stat
    started = time.perf_counter()
    try:
        yield stat
    finally:
        stat.wall = time.perf_counter() - started
        p.phases.append(stat)
        p._current = previous
