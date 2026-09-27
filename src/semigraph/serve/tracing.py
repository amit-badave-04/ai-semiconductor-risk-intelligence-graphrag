"""Opt-in Langfuse tracing for the agent: fail-open, sampled, and private by default (docs/v2/M3_AGENT_PLAN.md section 6).

``get_tracer(settings)`` returns a no-op unless ``langfuse_public_key``, ``langfuse_secret_key`` and ``langfuse_host`` are ALL
set; only then is the optional ``langfuse`` package imported (never at module import: the slim serve image does not ship it).

Three properties are enforced HERE, whatever the caller passes:

* **Nothing free-text leaves the process.** Every attribute goes through :func:`scrub_attrs`: numbers, booleans and short
  identifier-like labels (model, fallback code ...) pass; any other string is reduced to its LENGTH; keys that name an IP,
  a header, a token or a key are dropped; ``question`` becomes ``question_chars`` plus a salted hash (the salt is random per
  process, or ``langfuse_hash_salt`` when the owner wants grouping across restarts AND it is at least ``MIN_SALT_CHARS`` long —
  a short or guessable configured salt is ignored, with a warning, because it would let whoever holds it dictionary-recover a
  hashed question; it is NEVER derived from the Langfuse secret, because the OTLP exporter sends that secret to the recipient
  of the traces, who could then confirm a guessed question). A ``tool``-keyed attribute is a label ONLY when it is one of the
  agent's own tool names (``_AGENT_TOOL_NAMES``); anything else — including a label-shaped string a steered planner invented —
  is reduced to a fixed placeholder instead of passing verbatim. Nothing is ever sent as an
  observation ``input`` or ``output``, and the client is built with a masking hook and an export filter that drops every span
  that is not ours (a litellm or OpenTelemetry integration would otherwise ship whole prompts).
* **Tracing can never break or slow an answer.** Every SDK call sits behind one guard: the first failure switches tracing off for
  the process and is logged ONCE (with the keys redacted); the caller sees nothing. A request-level ``flush()`` is a no-op (the
  SDK exports in a background thread); the real flush and shutdown happen once, in the lifespan teardown.
* **The sampling decision is per request.** ``for_request(question)`` draws once from the injected random source; the tracer it
  returns carries its own span stack. One shared stack cannot work here: ``_paid_stream`` is a sync generator that the SSE layer
  drives through the threadpool, so consecutive ``next()`` calls of one request may run on different threads while two requests
  interleave, and neither thread-locals nor context variables survive that.

The tracer object handed to the agent is duck-typed (``semigraph.agent.trace.Tracer``): ``span(name, **attrs)`` is a context
manager whose handle has ``set(**attrs)``; ``event(name, **attrs)``; ``generation(*, name, model, usage, cost_usd, input_chars,
output_chars)``; ``flush()``. None of them raises.

Langfuse Python SDK v4 calls used (verified against the SDK docs, not from memory): ``Langfuse(public_key=, secret_key=,
base_url=, timeout=, sample_rate=, mask=, should_export_span=)``; ``start_observation(name=, as_type=, metadata=, model=,
usage_details=, cost_details=)`` on the client and on an observation (an unmanaged child); ``update(metadata=, level=,
status_message=)``; ``end()``; ``flush()``; ``shutdown()``; ``langfuse.span_filter.is_langfuse_span``. Token usage uses the
``input`` / ``output`` / ``total`` keys and cost ``{"total": usd}``, the convention of the docs site."""

import hashlib
import hmac
import importlib
import logging
import math
import random
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger("semigraph.serve.tracing")

ROOT_NAME = "ask"
CLIENT_TIMEOUT_S = 3
SAMPLE_ALL = 1.0
MAX_ATTRS = 40           # attributes kept per call
MAX_DEPTH = 3            # nested dict levels kept below the top level
MAX_ITEMS = 20           # list items kept
HASH_CHARS = 16
MAX_LOG_CHARS = 300
UNNAMED = "unnamed"
SALT_BYTES = 32
MIN_SALT_CHARS = 32   # a configured LANGFUSE_HASH_SALT shorter than this is a dictionary-guessable key: rejected, not used

_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,59}")
_LABEL_RE = re.compile(r"[A-Za-z0-9_.:/\-]{1,80}")
_HEX_RE = re.compile(r"[0-9a-f]{8,64}")
_REASON_HEAD_RE = re.compile(r"[A-Za-z0-9_.\-]{1,60}")
# Keys that name a secret or an identity: dropped whatever the value (a numeric value is harmless and is kept before this test:
# ``prompt_tokens: 12`` is a count, not a token).
_SECRET_KEY_RE = re.compile(r"(?i)(^|_)(ips?|ip_?address|headers?|authorization|auth|bearer|cookies?|secret|password|passwd|"
                            r"api_?key|access_?key|tokens?|credentials?)($|_)")
_LABEL_KEYS = frozenset({"model", "planner_model", "answered_by", "strategy", "status", "kind", "routed", "finish_reason",
                         "error_type", "name", "stage", "node", "level"})
_REASON_KEYS = frozenset({"fallback_reason", "reason"})   # a code such as ``planner_error``: only the leading token is kept
_HEX_KEYS = frozenset({"question_hash"})
# The seven read-only tools the planner may call (semigraph/agent/tools.py TOOL_NAMES). Defense in depth for a steered or
# malicious tool name that is still label-shaped (e.g. "check_card_4111111111111111" matches ``_LABEL_RE`` but is not a real
# tool): tracing.py does NOT import agent code at runtime (docs/v2/M3_AGENT_PLAN.md section 6 keeps the slim serve image free
# of langgraph while the agent is off), so this is a hand-kept literal copy — tests/test_tracing.py cross-checks it against
# ``semigraph.agent.tools.TOOL_NAMES`` (a test-only import) so the two can never silently drift.
_AGENT_TOOL_NAMES = ("lookup_company", "search_filings", "financial_metrics", "risk_changes", "relationships",
                     "active_risks", "compute_change")
_INVALID_TOOL_LABEL = "invalid"
# A general, provider-agnostic scrub for a log line that may quote a third-party exception message (a bearer token or an
# ``sk-``/``pk_``-style API key embedded in a provider's own error text): distinct from ``_Health.redact``'s exact-match
# replacement of THIS process's own configured keys, which cannot know a key it never held.
_SECRET_SHAPED_RE = re.compile(r"(?i)(bearer\s+[a-z0-9._~+/=\-]{8,})|((?:sk|pk|rk|ak)[-_](?:live|test)?[-_]?[a-z0-9]{6,})")


# ---------------------------------------------------------------- scrubbing: what may leave the process

def _finite(value: float) -> bool:
    return not isinstance(value, float) or math.isfinite(value)


def _text_entries(key: str, text: str, hasher: Callable[[str], str] | None) -> dict[str, Any]:
    """One string attribute as the (at most two) entries that may be sent: a label, a code, a hash, or just a length."""
    if key == "question" and hasher is not None:
        return {"question_chars": len(text), "question_hash": hasher(text)}
    if key in _HEX_KEYS:
        return {key: text} if _HEX_RE.fullmatch(text) else {}
    if key in _REASON_KEYS:
        head = _REASON_HEAD_RE.match(text)
        return {key: head.group(0)} if head else {f"{key}_chars": len(text)}
    if key == "tool":
        # A closed set, not a label pattern: a steered planner's tool name can be label-shaped (``_LABEL_RE`` would pass it)
        # without being one of the seven real tools, so anything outside ``_AGENT_TOOL_NAMES`` is reduced to a placeholder.
        return {key: text if text in _AGENT_TOOL_NAMES else _INVALID_TOOL_LABEL}
    if key in _LABEL_KEYS and _LABEL_RE.fullmatch(text):
        return {key: text}
    return {f"{key}_chars": len(text)}


def _sequence_entries(key: str, items: list, hasher: Callable[[str], str] | None, depth: int) -> dict[str, Any]:
    items = items[:MAX_ITEMS]
    if all(isinstance(i, (bool, int, float)) for i in items):
        return {key: [i for i in items if _finite(i)]}
    if all(isinstance(i, Mapping) for i in items):
        return {key: [c for c in (scrub_attrs(i, _depth=depth + 1) for i in items) if c]} if depth < MAX_DEPTH else {}
    return {f"{key}_count": len(items)}


def _entries(key: str, value: Any, hasher: Callable[[str], str] | None, depth: int) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (bool, int, float)):
        return {key: value} if _finite(value) else {}
    if _SECRET_KEY_RE.search(key):
        return {}
    if isinstance(value, str):
        return _text_entries(key, value, hasher)
    if isinstance(value, Mapping):
        return {key: scrub_attrs(value, _depth=depth + 1)} if depth < MAX_DEPTH else {}
    if isinstance(value, (list, tuple, set, frozenset)):
        return _sequence_entries(key, list(value), hasher, depth)
    return {f"{key}_type": type(value).__name__}


def scrub_attrs(attrs: Mapping | None, *, hasher: Callable[[str], str] | None = None, _depth: int = 0) -> dict[str, Any]:
    """The part of ``attrs`` that is safe to send: numbers, booleans, short labels and lengths. Returns a new dict, never
    raises, is idempotent, and bounds width and depth. ``hasher`` (top level only) turns a ``question`` into a salted hash."""
    out: dict[str, Any] = {}
    if not isinstance(attrs, Mapping):
        return out
    for raw_key, value in attrs.items():
        if len(out) >= MAX_ATTRS:
            break
        key = str(raw_key)
        if _KEY_RE.fullmatch(key):
            out.update(_entries(key, value, hasher if _depth == 0 else None, _depth))
    return out


def mask_value(data: Any) -> Any:
    """The SDK's ``mask`` hook: the same rule applied to whatever the SDK is about to serialise (defence in depth: the tracer
    already sends only scrubbed metadata)."""
    if data is None or isinstance(data, (bool, int, float)):
        return data if _finite(data) else None
    if isinstance(data, str):
        return data if _LABEL_RE.fullmatch(data) else "[masked]"
    if isinstance(data, Mapping):
        return scrub_attrs(data)
    if isinstance(data, (list, tuple)):
        return [mask_value(item) for item in list(data)[:MAX_ITEMS]]
    return "[masked]"


def _mask(*, data: Any, **_: Any) -> Any:
    return mask_value(data)


def _label(text: Any, default: str = UNNAMED) -> str:
    return text if isinstance(text, str) and _LABEL_RE.fullmatch(text) else default


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value) or value < 0:
        return None
    return int(value)


def _usage_details(usage: Any) -> dict[str, int] | None:
    """LiteLLM ``prompt_tokens`` / ``completion_tokens`` (or the Anthropic names) as Langfuse ``input`` / ``output`` / ``total``."""
    if not isinstance(usage, Mapping):
        return None
    tokens_in = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
    tokens_out = _count(usage.get("completion_tokens", usage.get("output_tokens")))
    if tokens_in is None and tokens_out is None:
        return None
    tokens_in, tokens_out = tokens_in or 0, tokens_out or 0
    return {"input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out}


def _make_hasher(salt: bytes) -> Callable[[str], str]:
    return lambda text: hmac.new(salt, text.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:HASH_CHARS]


def redact_secret_shaped(text: str, *, max_chars: int = MAX_LOG_CHARS) -> str:
    """A log-safe copy of ``text``: secret-shaped substrings (a ``Bearer`` token, an ``sk-``/``pk_``-style API key) become
    ``***`` whatever process they came from, truncated to ``max_chars`` either way (an unbounded exception message is itself
    a way to fill the log). For a caller's OWN known keys, ``_Health.redact`` is exact and should run first; this catches a
    key it never held — one a third-party provider's own error text quotes. Shared by ``serve.routes`` (the mid-stream error
    log) and this module (``_Health.redact``); never raises."""
    return _SECRET_SHAPED_RE.sub("***", text)[:max_chars]


# ---------------------------------------------------------------- the no-op

class _NullSpan:
    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def set(self, **attrs: Any) -> None:
        return None


_NULL_SPAN = _NullSpan()


class NullTracer:
    """The tracer when tracing is off (and the tracer of an unsampled request): the whole contract, doing nothing."""

    enabled = False

    def span(self, name: str, **attrs: Any) -> _NullSpan:
        return _NULL_SPAN

    def event(self, name: str, **attrs: Any) -> None:
        return None

    def generation(self, *args: Any, **kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def for_request(self, *args: Any, **kwargs: Any) -> "NullTracer":
        return self

    def close(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


NULL_TRACER = NullTracer()


# ---------------------------------------------------------------- the Langfuse tracer

class _Health:
    """Shared by a tracer and every per-request tracer it hands out. The first failure switches tracing off for the process and
    is logged exactly once, with the configured keys redacted (an SDK error message can quote a URL or a credential)."""

    def __init__(self, secrets: tuple[str, ...]):
        self._secrets = tuple(s for s in secrets if s)
        self._lock = threading.Lock()
        self.dead = False

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return redact_secret_shaped(text)

    def fail(self, exc: Exception, where: str) -> None:
        with self._lock:
            first, self.dead = not self.dead, True
        if first:
            logger.warning("langfuse tracing disabled after a failure in %s: %s: %s", where, type(exc).__name__,
                           self.redact(str(exc)))

    def guarded(self, where: str, fn: Callable[[], Any]) -> Any:
        """Run one SDK interaction; a failure disables tracing and returns None instead of raising."""
        if self.dead:
            return None
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - fail open: tracing must never break an answer
            self.fail(exc, where)
            return None


class _Span:
    """The handle of ``request.span(...)``: a context manager that never swallows the body's exception."""

    def __init__(self, request: "_RequestTracer", name: str, attrs: Mapping):
        self._request, self._name, self._meta = request, name, request.scrub(attrs)
        self._obs: Any = None

    def __enter__(self) -> "_Span":
        self._obs = self._request.open_span(self._name, self._meta)
        return self

    def set(self, **attrs: Any) -> None:
        self._meta = {**self._meta, **self._request.scrub(attrs)}
        self._request.update(self._obs, metadata=self._meta)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._request.close_span(self._obs, self._meta, exc_type)
        return False


class _RequestTracer:
    """One sampled request: a root observation and its own span stack. Sequential use only (a request is one generator)."""

    enabled = True

    def __init__(self, owner: "LangfuseTracer", root: Any, root_meta: dict, one_shot: bool):
        self._owner, self._health, self._root, self._root_meta = owner, owner.health, root, root_meta
        self._stack: list[Any] = [root]
        self._lock = threading.RLock()     # a graph that runs tool calls on threads must not corrupt the stack (it may still mis-nest)
        self._one_shot, self._closed = one_shot, False
        self._started = time.monotonic()

    def scrub(self, attrs: Mapping) -> dict[str, Any]:
        return scrub_attrs(attrs, hasher=self._owner.hasher)

    # -- span plumbing (called by _Span)
    def open_span(self, name: str, meta: dict) -> Any:
        def go() -> Any:
            with self._lock:
                obs = self._stack[-1].start_observation(name=_label(name), as_type="span", metadata=dict(meta))
                self._stack.append(obs)
                return obs
        return None if self._closed else self._health.guarded("start_observation", go)

    def update(self, obs: Any, **kwargs: Any) -> None:
        if obs is not None and not self._closed:
            self._health.guarded("update", lambda: obs.update(**kwargs))

    def close_span(self, obs: Any, meta: dict, exc_type: Any) -> None:
        if obs is not None and not self._closed:
            extra: dict[str, Any] = {}
            if exc_type is not None and issubclass(exc_type, GeneratorExit):
                extra = {"cancelled": True}                     # the client went away mid-span
            elif exc_type is not None:
                extra = {"error_type": _label(exc_type.__name__)}

            def go() -> None:
                if extra:
                    kwargs: dict[str, Any] = {"metadata": {**meta, **extra}}
                    if "error_type" in extra:
                        kwargs.update(level="ERROR", status_message=extra["error_type"])
                    obs.update(**kwargs)
                obs.end()
            self._health.guarded("end", go)
        with self._lock:
            self._stack[:] = [o for o in self._stack if o is not obs]
        self._auto_close()

    # -- the Tracer contract
    def span(self, name: str, **attrs: Any) -> _Span:
        return _Span(self, name, attrs)

    def event(self, name: str, **attrs: Any) -> None:
        meta = self.scrub(attrs)

        def go() -> None:
            with self._lock:
                parent = self._stack[-1]
            parent.start_observation(name=_label(name), as_type="event", metadata=meta).end()
        if not self._closed:
            self._health.guarded("event", go)
        self._auto_close()

    def generation(self, *, name: str = UNNAMED, model: Any = None, usage: Any = None, cost_usd: Any = None,
                   input_chars: Any = None, output_chars: Any = None, **attrs: Any) -> None:
        kwargs: dict[str, Any] = {"name": _label(name), "as_type": "generation",
                                  "metadata": self.scrub({**attrs, "input_chars": input_chars, "output_chars": output_chars})}
        if isinstance(model, str) and _LABEL_RE.fullmatch(model):
            kwargs["model"] = model
        if (details := _usage_details(usage)) is not None:
            kwargs["usage_details"] = details
        if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) and _finite(cost_usd) and cost_usd >= 0:
            kwargs["cost_details"] = {"total": float(cost_usd)}
        if not self._closed:
            self._health.guarded("generation", lambda: self._parent().start_observation(**kwargs).end())
        self._auto_close()

    def _parent(self) -> Any:
        with self._lock:
            return self._stack[-1]

    def flush(self) -> None:
        """A no-op by design: the SDK exports in a background thread, and an answer must never wait for the network."""
        return None

    def for_request(self, *args: Any, **kwargs: Any) -> "_RequestTracer":
        return self

    def close(self) -> None:
        """End every observation still open (innermost first), then the root. Idempotent; the route calls it in a ``finally``."""
        if self._closed:
            return

        def go() -> None:
            with self._lock:
                while len(self._stack) > 1:
                    self._stack.pop().end()
            self._root.update(metadata={**self._root_meta, "latency_s": round(time.monotonic() - self._started, 3)})
            self._root.end()
        self._health.guarded("close", go)
        self._closed = True

    def shutdown(self) -> None:
        return None

    def _auto_close(self) -> None:
        if self._one_shot and len(self._stack) == 1:
            self.close()


class LangfuseTracer:
    """The tracer stored on ``app.state.tracer``: a factory of per-request tracers plus the client's lifecycle. Calling
    ``span`` / ``event`` / ``generation`` on it directly (a script, an eval) still traces: each top-level call is its own
    sampled, one-observation trace."""

    def __init__(self, client: Any, *, sample_rate: float, rng: Callable[[], float], salt: bytes, credentials: tuple[str, ...]):
        self._client, self._rate, self._rng = client, sample_rate, rng
        self.hasher = _make_hasher(salt)
        self.health = _Health(credentials)

    @property
    def enabled(self) -> bool:
        return not self.health.dead

    def _sampled(self) -> bool:
        return self._rate >= SAMPLE_ALL or self._rng() < self._rate

    def _new_request(self, question: str, strategy: str, one_shot: bool) -> "_RequestTracer | NullTracer":
        if self.health.dead:
            return NULL_TRACER
        sampled = self.health.guarded("sampling", self._sampled)
        if not sampled:
            return NULL_TRACER
        meta = scrub_attrs({"strategy": strategy, **({"question": question} if question else {})}, hasher=self.hasher)
        root = self.health.guarded("start_observation", lambda: self._client.start_observation(
            name=ROOT_NAME, as_type="span", metadata=dict(meta)))
        return NULL_TRACER if root is None else _RequestTracer(self, root, meta, one_shot)

    def for_request(self, question: str = "", *, strategy: str = "") -> "_RequestTracer | NullTracer":
        """The tracer of ONE question: draws the sampling decision once and records the question only as a length and a salted
        hash. Call ``close()`` on the result when the request ends."""
        return self._new_request(question, strategy, one_shot=False)

    def span(self, name: str, **attrs: Any) -> "_Span | _NullSpan":
        return self._new_request("", "", one_shot=True).span(name, **attrs)

    def event(self, name: str, **attrs: Any) -> None:
        self._new_request("", "", one_shot=True).event(name, **attrs)

    def generation(self, **kwargs: Any) -> None:
        self._new_request("", "", one_shot=True).generation(**kwargs)

    def flush(self) -> None:
        self.health.guarded("flush", self._client.flush)

    def close(self) -> None:
        return None

    def shutdown(self) -> None:
        """Flush and stop the client's background threads. Called once from the lifespan teardown; never raises."""
        for what in ("flush", "shutdown"):
            try:
                getattr(self._client, what)()
            except Exception as exc:  # noqa: BLE001 - a failing client at teardown must not break the shutdown
                logger.warning("langfuse %s failed at shutdown: %s: %s", what, type(exc).__name__,
                               self.health.redact(str(exc)))


# ---------------------------------------------------------------- construction

def _export_filter(module: Any) -> Callable[[Any], bool]:
    """The predicate that keeps only OUR observations (the docs place it in ``langfuse.span_filter``; the top-level attribute is
    the fallback). Without it the default filter also exports spans of known LLM instrumentation scopes, which would ship
    prompts, so its absence refuses tracing instead of running unfiltered."""
    try:
        return importlib.import_module("langfuse.span_filter").is_langfuse_span
    except (ImportError, AttributeError):
        return module.is_langfuse_span


def _salt(explicit: bytes | str | None, settings: Any) -> bytes:
    """The key of the question hash: the explicit argument (trusted, no length check — it is test/script controlled, never
    from an env var), else ``langfuse_hash_salt`` when it is at least ``MIN_SALT_CHARS`` long, else a random per-process key.
    A short or guessable configured salt (``"semigraph"``) would let whoever receives the traces dictionary-recover which
    question a hash corresponds to, so it is ignored — with a warning (this runs once, at ``get_tracer`` construction, not
    per request) — rather than trusted; ``.env.example`` documents the safe way to generate one (``secrets.token_hex(32)``)."""
    if explicit:
        return explicit.encode("utf-8") if isinstance(explicit, str) else bytes(explicit)
    configured = getattr(settings, "langfuse_hash_salt", "")
    if configured:
        if len(configured) >= MIN_SALT_CHARS:
            return configured.encode("utf-8")
        logger.warning("LANGFUSE_HASH_SALT is %d characters (< %d): too short to resist a dictionary guess — ignoring it "
                       "and using a random per-process salt instead. Generate a safe one with: "
                       "python -c \"import secrets; print(secrets.token_hex(32))\"", len(configured), MIN_SALT_CHARS)
    return secrets.token_bytes(SALT_BYTES)


def _credentials(settings: Any) -> tuple[str, str, str]:
    public, secret, host = (str(getattr(settings, name, "") or "")
                            for name in ("langfuse_public_key", "langfuse_secret_key", "langfuse_host"))
    return public, secret, host


def get_tracer(settings: Any, *, rng: Callable[[], float] | None = None, salt: bytes | str | None = None):
    """The tracer for this deployment: a no-op unless the public key, the secret key and the host are all set (and the sample
    rate is above zero). ``rng`` is the sampling random source (injected in tests); ``salt`` keys the question hash (default:
    ``settings.langfuse_hash_salt`` when set, else random per process: hashes then group repeats within one run and cannot be
    reversed by the trace recipient). Never raises."""
    public, secret, host = _credentials(settings)
    rate = getattr(settings, "langfuse_sample_rate", 0.0)
    if not (public and secret and host) or not isinstance(rate, (int, float)) or not rate > 0:
        return NULL_TRACER
    redactor = _Health((public, secret))
    try:
        module = importlib.import_module("langfuse")
        export_filter = _export_filter(module)
        client = module.Langfuse(public_key=public, secret_key=secret, base_url=host, timeout=CLIENT_TIMEOUT_S,
                                 sample_rate=SAMPLE_ALL, mask=_mask, should_export_span=export_filter)
    except Exception as exc:  # noqa: BLE001 - not installed, wrong version or a bad configuration: run untraced
        logger.warning("langfuse tracing disabled: could not start the client (%s: %s). Tracing needs the optional 'langfuse' "
                       "package with langfuse.span_filter.is_langfuse_span (langfuse>=4.14,<5)", type(exc).__name__,
                       redactor.redact(str(exc)))
        return NULL_TRACER
    return LangfuseTracer(client, sample_rate=float(rate), rng=rng or random.random, salt=_salt(salt, settings),
                          credentials=(public, secret))
