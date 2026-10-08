"""The thread gates of the S7 replay: the same shape as the service's ``anyio.CapacityLimiter`` pools (M5a I2).

The service holds no thread while it waits; every blocking hop to the database takes one token of a named limiter
(``serve/limiters.py``): ``state`` (4 tokens) for the state operations and ``db`` (32) for the graph reads. A caller waits for a
``state`` token at most ``state_op_timeout_s`` and is then refused (``stream_runtime.slot_call``: ``NoStateSlot``); a settle waits
as long as it takes. The replay has no event loop, so each limiter is a counting gate with the same two kinds of wait:
``acquire(timeout)`` is False after ``timeout`` seconds, ``acquire(None)`` blocks until a token frees. The wait is part of what the
route sees, so the replay times it inside the operation.

Stdlib only.
"""

import threading


class Gate:
    """A counting gate with ``n`` tokens. ``release`` without a matching ``acquire`` raises (a bounded semaphore)."""

    def __init__(self, n: int):
        if type(n) is not int or n < 1:
            raise ValueError(f"a gate needs 1 or more tokens, got {n!r}")
        self.size = n
        self._tokens = threading.BoundedSemaphore(n)

    def acquire(self, timeout: float | None) -> bool:
        """Take a token; ``timeout`` seconds at most (``None``: as long as it takes). False means none was free in time."""
        if timeout is None:
            return self._tokens.acquire()
        return self._tokens.acquire(timeout=max(0.0, timeout))

    def release(self) -> None:
        self._tokens.release()
