"""Rate statistics and gate verdicts of the temporal evaluation (shared by ``scripts/verify_temporal.py`` and ``eval/narration.py``).

Pure functions, no data: Wilson 95% intervals and the PASS / FAIL / INSUFFICIENT-DATA verdict of a rate gate. The thresholds are the
plan's (docs/v2/M1B_PLAN.md H and L.3): precision >= 0.90, recall >= 0.80, and no rate gate passes on fewer than 5 positives.
"""

from math import comb, sqrt

PRECISION_MIN, RECALL_MIN = 0.90, 0.80
MIN_POSITIVES = 5
EARLY_FAIL_ALPHA = 0.05
Z95 = 1.96
PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT-DATA"


def wilson_interval(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval of k successes in n trials; None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _binomial_tail(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def rate_status(k: int, n: int, threshold: float) -> str:
    """PASS / FAIL / INSUFFICIENT-DATA for a rate gate. With 5 or more trials the point estimate decides. With fewer it never
    passes; it FAILS only when k successes in n trials are statistically incompatible with a true rate at the threshold (the
    one-sided exact binomial tail is below 0.05, e.g. two misses in a row against 0.80), else it is INSUFFICIENT-DATA."""
    if n >= MIN_POSITIVES:
        return PASS if k / n >= threshold else FAIL
    return FAIL if n > 0 and _binomial_tail(k, n, threshold) < EARLY_FAIL_ALPHA else INSUFFICIENT


def ratio(k: int, n: int) -> float | None:
    return round(k / n, 6) if n else None


def ci(k: int, n: int) -> list[float] | None:
    interval = wilson_interval(k, n)
    return None if interval is None else [round(interval[0], 6), round(interval[1], 6)]
