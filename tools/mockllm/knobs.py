"""The fault knobs of the mock: ``invalid_id_rate``, ``rate_429``, ``slow_ttft_rate`` (plus the delays they use).

Three ways to set them, in increasing priority:

1. the environment at start (``MOCKLLM_INVALID_ID_RATE``, ``MOCKLLM_RATE_429``, ``MOCKLLM_SLOW_TTFT_RATE``,
   ``MOCKLLM_SLOW_TTFT_S``, ``MOCKLLM_RETRY_AFTER_S``); ``invalid_id_rate`` defaults to the profile's measured
   escalation rate, the others to 0;
2. ``PUT /admin/knobs`` while the mock runs (JSON body, any subset of the fields; needs ``MOCKLLM_ADMIN_TOKEN`` as a bearer
   token). This is how a load test enters its fault phase: the service under test is the mock's client and forwards no
   custom header, so a header cannot drive a fleet-wide phase;
3. a request header, for that request only: ``X-Mock-Invalid-Id-Rate``, ``X-Mock-429-Rate``, ``X-Mock-Slow-Ttft-Rate``,
   ``X-Mock-Slow-Ttft-S`` (for tests and for direct probes of the mock).

``invalid_id_rate`` applies to the DRAFT role only: it makes a draft cite an id the prompt does not hold, which the real
verifier rejects (``invalid_citation``) and the service answers by escalating to the strong model. The strong model's answer is
not verified before release, so it is never made invalid. ``rate_429`` and ``slow_ttft_rate`` apply to every role.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

RATE_FIELDS = ("invalid_id_rate", "rate_429", "slow_ttft_rate")
SECONDS_FIELDS = ("slow_ttft_s", "retry_after_s")
MAX_SECONDS = 600.0
HEADER_FIELDS = {"x-mock-invalid-id-rate": "invalid_id_rate", "x-mock-429-rate": "rate_429",
                 "x-mock-slow-ttft-rate": "slow_ttft_rate", "x-mock-slow-ttft-s": "slow_ttft_s"}
ENV_FIELDS = {"MOCKLLM_INVALID_ID_RATE": "invalid_id_rate", "MOCKLLM_RATE_429": "rate_429",
              "MOCKLLM_SLOW_TTFT_RATE": "slow_ttft_rate", "MOCKLLM_SLOW_TTFT_S": "slow_ttft_s",
              "MOCKLLM_RETRY_AFTER_S": "retry_after_s"}


class KnobError(ValueError):
    """A knob value that is not accepted (names the field; never echoes more than the field and the number)."""


@dataclass(frozen=True)
class Knobs:
    invalid_id_rate: float = 0.0
    rate_429: float = 0.0
    slow_ttft_rate: float = 0.0
    slow_ttft_s: float = 5.0
    retry_after_s: float = 1.0

    def as_dict(self) -> dict:
        return asdict(self)


def _number(field: str, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise KnobError(f"{field} must be a number")
    try:
        value = float(raw)
    except ValueError as e:
        raise KnobError(f"{field} must be a number") from e
    if value != value or value in (float("inf"), float("-inf")):
        raise KnobError(f"{field} must be finite")
    if field in RATE_FIELDS and not 0.0 <= value <= 1.0:
        raise KnobError(f"{field} must be within 0..1, got {value:g}")
    if field in SECONDS_FIELDS and not 0.0 <= value <= MAX_SECONDS:
        raise KnobError(f"{field} must be within 0..{MAX_SECONDS:g} seconds, got {value:g}")
    return value


def apply_patch(knobs: Knobs, patch: Mapping[str, object]) -> Knobs:
    """``knobs`` with the fields of ``patch`` replaced (a new object). An unknown field is an error, not ignored."""
    known = set(Knobs.__dataclass_fields__)
    unknown = sorted(set(patch) - known)
    if unknown:
        raise KnobError(f"unknown knob(s): {', '.join(unknown)}")
    return replace(knobs, **{field: _number(field, value) for field, value in patch.items()})


def from_headers(knobs: Knobs, headers: Mapping[str, str]) -> Knobs:
    """``knobs`` overridden by the ``X-Mock-*`` headers of one request (``headers`` keys are lower case)."""
    patch = {field: headers[name] for name, field in HEADER_FIELDS.items() if name in headers}
    return apply_patch(knobs, patch) if patch else knobs


def from_env(env: Mapping[str, str], *, default_invalid_id_rate: float, default_slow_ttft_s: float = 5.0) -> Knobs:
    base = Knobs(invalid_id_rate=default_invalid_id_rate, slow_ttft_s=default_slow_ttft_s)
    patch = {field: env[name] for name, field in ENV_FIELDS.items() if env.get(name, "") != ""}
    return apply_patch(base, patch) if patch else base
