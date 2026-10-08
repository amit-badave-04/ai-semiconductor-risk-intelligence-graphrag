"""Counters of a level and a driver wrapper that counts the statements each operation sends (stdlib only).

``hybrid_retrieve`` is the service's real function and its number of queries is whatever it is ("about 5" in the decisions
file): it is COUNTED here, per operation, not assumed. The wrapper sits between the service's code and the real driver; it counts a
``session.run`` and a managed transaction (``execute_write`` / ``execute_read``) each as one statement, under the name of the
operation the calling thread is running (set by ``Tally.operation``). It changes nothing else: arguments go through untouched
(a managed transaction keeps the server-side timeout it carries).
"""

import threading
from collections import Counter
from contextlib import contextmanager
from typing import Any


class Tally:
    """Thread-safe counters. ``operation(name)`` tags the statements the current thread sends until the block ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local = threading.local()
        self.statements: Counter = Counter()           # op -> statements sent
        self.calls: Counter = Counter()                # op -> times the operation ran
        self.counters: Counter = Counter()             # named totals (live_started, errors, ...)

    @contextmanager
    def operation(self, op: str):
        previous = getattr(self._local, "op", None)
        self._local.op = op
        try:
            yield
        finally:
            self._local.op = previous

    def statement(self) -> None:
        with self._lock:
            self.statements[getattr(self._local, "op", None) or "untagged"] += 1

    def called(self, op: str) -> None:
        with self._lock:
            self.calls[op] += 1

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] += n

    def get(self, name: str) -> int:
        with self._lock:
            return self.counters[name]

    def statements_report(self) -> dict:
        with self._lock:
            return {op: {"count": count, "calls": self.calls[op],
                         "per_call": round(count / self.calls[op], 3) if self.calls[op] else None,
                         "per_ask": round(count / self.calls[op], 3) if self.calls[op] and op == "retrieval" else None}
                    for op, count in sorted(self.statements.items())}


class _CountedSession:
    def __init__(self, session: Any, tally: Tally):
        self._session, self._tally = session, tally

    def __enter__(self) -> "_CountedSession":
        self._session.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._session.__exit__(*exc_info)

    def run(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._tally.statement()
        return self._session.run(query, *args, **kwargs)

    def execute_write(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self._tally.statement()
        return self._session.execute_write(fn, *args, **kwargs)

    def execute_read(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self._tally.statement()
        return self._session.execute_read(fn, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class CountedDriver:
    def __init__(self, driver: Any, tally: Tally):
        self._driver, self._tally = driver, tally

    def session(self, **config: Any) -> _CountedSession:
        return _CountedSession(self._driver.session(**config), self._tally)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)
