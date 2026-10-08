"""The per-ask salt that keeps live asks uncached (council 5, owner item P). Pure stdlib.

Every live ask (pool, unique-suffix and agent) is sent as ``f"{question} (ref {worker}{n:06d})"``: one worker digit and a
six-digit zero-padded counter, so the whole token is ONE run of seven digits. That is what keeps it invisible to the year
detectors the question text feeds:

* ``retrieval.retriever._YEAR`` = ``(?<![\\d$.,])(20\\d{2})(?!\\d|...)``: a ``20xx`` inside a longer digit run is preceded or followed
  by a digit and never matches;
* ``retrieval.router._YEAR`` = ``\\b(?:19|20)\\d\\d\\b``: a word boundary cannot fall inside a digit run.

An unpadded counter would walk through 1900-2099 and change routing and retrieval. The width is fixed, so the counter may not
pass :data:`MAX_COUNTER` (:class:`SaltExhausted`); the tests sweep every worker digit across the 1900-2099 region against the
server's own compiled patterns, and ``salt_check`` re-runs the decision comparison for every pool question before staging.

The salt changes the answer-cache key (``store.cache_key`` lowercases, collapses whitespace and strips only trailing ``?.! ``;
the salt ends in ``)``) and the embedding input, so both caches miss, with no server change. The cached examples are sent
unsalted. A staging run is only as unique as its counters: runs that share an answer cache (a pilot, a restart, the soak) need
different ``start`` values, or a reset cache.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

COUNTER_WIDTH = 6
MAX_COUNTER = 10**COUNTER_WIDTH - 1
MAX_WORKER = 9
SALT_FORMAT = "{q} (ref {worker}{n:06d})"
SALT_SUFFIX_LEN = len(" (ref 0000000)")                      # 14
MAX_QUESTION_CHARS = 500                                      # Settings.max_question_chars
MAX_BASE_CHARS = MAX_QUESTION_CHARS - SALT_SUFFIX_LEN         # 486: the longest question that can still be salted
_SALT_RE = re.compile(r" \(ref (\d)(\d{6})\)\Z")

# The three salts salt_check pins (chosen to be awkward for the year detectors, not random): a worker whose digit starts a
# "20xx" run at counter 1999, the padded edge 9999, and the widest token.
ADVERSARIAL_SALTS: tuple[tuple[int, int], ...] = ((2, 1999), (1, 9999), (9, MAX_COUNTER))


class SaltExhausted(RuntimeError):
    """The worker used all 1,000,000 counter values: a wider token would no longer be one fixed-width digit run."""


def _check(worker: int, n: int) -> None:
    if type(worker) is not int or not 0 <= worker <= MAX_WORKER:
        raise ValueError(f"worker must be a digit 0-{MAX_WORKER}, got {worker!r}")
    if type(n) is not int or n < 0:
        raise ValueError(f"counter must be an int in 0..{MAX_COUNTER}, got {n!r}")
    if n > MAX_COUNTER:
        raise SaltExhausted(f"counter {n} is past {MAX_COUNTER}: the token would no longer be a fixed-width digit run")


def salt_token(worker: int, n: int) -> str:
    """The seven digits: ``worker`` then ``n`` zero-padded to six."""
    _check(worker, n)
    return f"{worker}{n:0{COUNTER_WIDTH}d}"


def salt_suffix(worker: int, n: int) -> str:
    return f" (ref {salt_token(worker, n)})"


def apply_salt(question: str, worker: int, n: int) -> str:
    _check(worker, n)
    return SALT_FORMAT.format(q=question, worker=worker, n=n)


def split_salt(text: str) -> tuple[str, int, int] | None:
    """``(base_question, worker, n)`` of a salted question, or None when it carries no salt."""
    match = _SALT_RE.search(text)
    if match is None:
        return None
    return text[:match.start()], int(match.group(1)), int(match.group(2))


def normalize_question(question: str) -> str:
    """The text ``store.cache_key`` hashes: lowercase, whitespace collapsed, trailing ``?.! `` stripped (pinned to the server's
    own function by a test, so a change there cannot leave the pool's de-duplication behind)."""
    return " ".join(question.lower().split()).rstrip("?.! ")


def distinct_normalized(questions) -> int:
    """How many different cache keys these question texts make (before the strategy and template, which are constant)."""
    return len({normalize_question(q) for q in questions})


@dataclass(frozen=True)
class SaltedQuestion:
    text: str
    base: str
    worker: int
    n: int

    @property
    def token(self) -> str:
        return salt_token(self.worker, self.n)


class Salter:
    """A worker's salt sequence: ``next(question)`` returns the question with the next counter value. Deterministic for a
    given ``(worker, start)``; thread- and greenlet-safe."""

    def __init__(self, worker: int, start: int = 0) -> None:
        _check(worker, start)
        self.worker, self._next, self._start = worker, start, start
        self._lock = threading.Lock()

    def next(self, question: str) -> SaltedQuestion:
        with self._lock:
            n = self._next
            _check(self.worker, n)                             # raises SaltExhausted past MAX_COUNTER
            self._next = n + 1
        return SaltedQuestion(apply_salt(question, self.worker, n), question, self.worker, n)

    @property
    def issued(self) -> int:
        return self._next - self._start

    @property
    def remaining(self) -> int:
        return MAX_COUNTER + 1 - self._next


def resume_start(paths: Iterable[Path | str], worker: int, run_id: str, default: int = 0) -> int:
    """The first counter a restarted worker may use: one past the highest this ``worker`` digit has already sent in this ``run_id``
    according to earlier raw logs (``events.*.jsonl``), or ``default``. A worker that restarts mid-run with an unchanged
    ``LOADTEST_SALT_START`` would re-send salts it has used, and the second use of a salted question is an answer-cache hit."""
    highest = default - 1
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"salt"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            token = record.get("salt")
            if (record.get("run_id") == run_id and record.get("worker") == worker and isinstance(token, str)
                    and len(token) == 1 + COUNTER_WIDTH and token.isdigit() and int(token[0]) == worker):
                highest = max(highest, int(token[1:]))
    return highest + 1
