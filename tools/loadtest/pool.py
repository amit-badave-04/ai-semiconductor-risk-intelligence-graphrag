"""The 300-question live pool (owner item P; M5_PLAN.md section 6 "40 % live from a 300-question pool").

Two halves:

* **loading** (stdlib only, runs in the generator image): :func:`load_pool` reads the committed ``pool.json`` and validates
  it (exact size, no two questions with the same cache-key normalization, every question short enough to carry the salt);
* **building** (developer side; imports ``semigraph`` lazily): :func:`build_pool` assembles it from
  ``artifacts/benchmark.json``, ``artifacts/agent_benchmark.json`` and the packaged ``examples.json``, topped up to 300 with
  templated company / metric / year questions, de-duplicated by the server's own cache-key normalization
  (:func:`tools.loadtest.salt.normalize_question`, pinned to ``store.cache_key`` by a test). It FAILS unless the result is
  exactly :data:`POOL_SIZE` questions: a short pool silently changes the repeat structure the salt exists to remove.

``pool.json`` carries its own composition (per source and type, how many the router sends straight to the strong model,
companies and years named, what de-duplication dropped and why) and is mirrored to ``pool_composition.json`` so the owner can
read the composition without opening 300 questions. The honest summary of that composition is: the real questions are a
minority (the examples are a subset of the benchmark), most of the pool is templated, and that has to be said in the report.

    python -m tools.loadtest.pool build      # rewrite pool.json and pool_composition.json
    python -m tools.loadtest.pool show       # print the composition
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from tools.loadtest import salt

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_POOL_PATH = HERE / "pool.json"
DEFAULT_COMPOSITION_PATH = HERE / "pool_composition.json"
POOL_VERSION = 1
POOL_SIZE = 300
MIN_QUESTION_CHARS = 8                       # guard.validate_question
SEED = 20261007
MAX_EVIDENCE_IDS = 150

SOURCE_FILES = {
    "benchmark": REPO_ROOT / "artifacts" / "benchmark.json",
    "agent_benchmark": REPO_ROOT / "artifacts" / "agent_benchmark.json",
    "examples": REPO_ROOT / "src" / "semigraph" / "artifacts" / "examples.json",
}
SOURCE_PRIORITY = ("benchmark", "examples", "agent_benchmark")     # which copy of a duplicated question is kept

# ---- the templated top-up -------------------------------------------------------------------------------------------------

METRICS = ("total revenue", "net income", "research and development expense", "capital expenditures")
YEARS = (2023, 2024, 2025)
RISK_THEMES = ("export control", "supply chain concentration", "customer concentration", "intellectual property",
               "geopolitical and Taiwan", "cybersecurity", "manufacturing capacity", "competition")
DEPENDENCIES = ("wafer fabrication", "advanced packaging", "memory supply", "manufacturing and assembly")
COMPARISON_PAIRS = (("Nvidia", "AMD"), ("Nvidia", "Intel"), ("AMD", "Intel"), ("Broadcom", "Qualcomm"),
                    ("Micron", "Intel"), ("Apple", "Microsoft"), ("Amazon", "Alphabet"), ("Meta", "Alphabet"),
                    ("TSMC", "Intel"), ("ASML", "Micron"), ("Qualcomm", "Apple"), ("AMD", "Broadcom"))
FAMILY_SHARES = (("numeric", 0.38), ("numeric_change", 0.12), ("risk", 0.22), ("temporal", 0.10), ("dependency", 0.08),
                 ("comparison", 0.10))


def _template_candidates(companies: list[str]) -> dict[str, list[tuple[str, str]]]:
    """family -> [(id suffix, question)] in a fixed order. No year is ever written as a bare token outside a ``fiscal`` phrase."""
    out: dict[str, list[tuple[str, str]]] = {name: [] for name, _ in FAMILY_SHARES}
    for c in companies:
        for m in METRICS:
            for y in YEARS:
                out["numeric"].append((f"{c}:{m}:{y}", f"What was {c}'s {m} for fiscal {y}?"))
            for y1, y2 in zip(YEARS, YEARS[1:]):
                out["numeric_change"].append((f"{c}:{m}:{y1}-{y2}",
                                              f"How did {c}'s {m} in fiscal {y2} compare with fiscal {y1}?"))
        for t in RISK_THEMES:
            out["risk"].append((f"{c}:{t}", f"What does {c} say about {t} risk in its most recent annual report?"))
        for y1, y2 in zip(YEARS, YEARS[1:]):
            out["temporal"].append((f"{c}:{y1}-{y2}",
                                    f"How have {c}'s risk factor disclosures changed between fiscal {y1} and fiscal {y2}?"))
        for d in DEPENDENCIES:
            out["dependency"].append((f"{c}:{d}", f"Which suppliers or foundries does {c} depend on for {d}?"))
    known = set(companies)
    for a, b in COMPARISON_PAIRS:
        if a in known and b in known:
            for m in METRICS:
                for y in YEARS[-2:]:
                    out["comparison"].append((f"{a}+{b}:{m}:{y}", f"Compare {a}'s and {b}'s {m} for fiscal {y}."))
    return out


def _quotas(total: int) -> dict[str, int]:
    quotas = {name: int(total * share) for name, share in FAMILY_SHARES}
    leftover = total - sum(quotas.values())
    for name, _ in FAMILY_SHARES:                           # the rounding remainder goes to the largest families first
        if leftover <= 0:
            break
        quotas[name] += 1
        leftover -= 1
    return quotas


# ---- loading -----------------------------------------------------------------------------------------------------------------

class PoolError(ValueError):
    """The pool file is malformed, the wrong size, or holds two questions with one cache key."""


@dataclass(frozen=True)
class PoolQuestion:
    id: str
    text: str
    source: str
    type: str
    routed: str | None = None


@dataclass(frozen=True)
class Pool:
    live: tuple[PoolQuestion, ...]
    agent: tuple[PoolQuestion, ...]
    examples: tuple[dict, ...]
    evidence_ids: tuple[str, ...]
    tickers: tuple[str, ...]
    composition: dict
    sha256: str

    def pick(self, rng: random.Random, klass: str) -> PoolQuestion:
        """The question for one ask of class ``live_pool`` / ``live_unique`` (any of the 300) or ``live_agent`` (agent set)."""
        items = self.agent if klass == "live_agent" else self.live
        return items[rng.randrange(len(items))]


def _question(raw: dict) -> PoolQuestion:
    return PoolQuestion(raw["id"], raw["q"], raw["source"], raw["type"], raw.get("routed"))


def pool_digest(live: list[dict]) -> str:
    return hashlib.sha256(json.dumps([[q["id"], q["q"]] for q in live], ensure_ascii=False).encode("utf-8")).hexdigest()


def load_pool(path: Path | str | None = None, *, size: int = POOL_SIZE) -> Pool:
    raw = json.loads(Path(path or DEFAULT_POOL_PATH).read_text(encoding="utf-8"))
    if raw.get("version") != POOL_VERSION:
        raise PoolError(f"pool version {raw.get('version')!r}, expected {POOL_VERSION}")
    live = raw.get("live") or []
    if len(live) != size:
        raise PoolError(f"the pool has {len(live)} questions, not {size}")
    normalized = Counter(salt.normalize_question(q["q"]) for q in live)
    duplicated = [k for k, n in normalized.items() if n > 1]
    if duplicated:
        raise PoolError(f"{len(duplicated)} questions share a cache key, e.g. {duplicated[0]!r}")
    for q in live:
        if not MIN_QUESTION_CHARS <= len(" ".join(q["q"].split())) <= salt.MAX_BASE_CHARS:
            raise PoolError(f"{q['id']}: {len(q['q'])} chars cannot carry the {salt.SALT_SUFFIX_LEN}-char salt")
    if raw.get("sha256") != pool_digest(live):
        raise PoolError("pool.json was edited by hand: its sha256 does not match its questions")
    ids = {q["id"] for q in live}
    agent = [q for q in raw.get("agent") or [] if q["id"] in ids]
    if not agent or len(agent) != len(raw.get("agent") or []):
        raise PoolError("the agent set must be a non-empty subset of the pool")
    return Pool(tuple(_question(q) for q in live), tuple(_question(q) for q in agent), tuple(raw.get("examples") or ()),
                tuple(raw.get("evidence_ids") or ()), tuple(raw.get("tickers") or ()), raw.get("composition") or {},
                raw["sha256"])


# ---- building (developer side) ---------------------------------------------------------------------------------------------

def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _real_candidates(sources: dict[str, Path]) -> list[dict]:
    """Every question of the three files, in SOURCE_PRIORITY order and file order within a source."""
    benchmark = _read(sources["benchmark"])
    agent = _read(sources["agent_benchmark"])["questions"]
    examples = _read(sources["examples"])["examples"]
    by_source = {
        "benchmark": [{"id": f"B:{x['id']}", "q": x["q"], "type": x.get("type", "")} for x in benchmark],
        "examples": [{"id": f"E:{x['id']}", "q": x["question"], "type": x.get("type", "")} for x in examples],
        "agent_benchmark": [{"id": f"A:{x['id']}", "q": x["q"], "type": x.get("type", "")} for x in agent],
    }
    return [{**c, "source": source} for source in SOURCE_PRIORITY for c in by_source[source]]


def _years_named(texts: list[str]) -> dict[str, int]:
    from semigraph.retrieval import retriever

    years: Counter = Counter()
    for t in texts:
        for y in retriever.mentioned_periods(t)["years"]:
            years[str(y)] += 1
    return dict(sorted(years.items()))


def build_pool(sources: dict[str, Path] | None = None, *, size: int = POOL_SIZE, seed: int = SEED) -> dict:
    """The pool document (not yet written). Raises :class:`PoolError` unless it holds exactly ``size`` distinct questions."""
    from semigraph.retrieval import retriever, router
    from semigraph.universe import FILERS

    sources = sources or SOURCE_FILES
    kept: dict[str, dict] = {}                                # normalized key -> question
    dropped: list[dict] = []
    for cand in _real_candidates(sources):
        text = " ".join(cand["q"].split())
        key = salt.normalize_question(text)
        if not MIN_QUESTION_CHARS <= len(text) <= salt.MAX_BASE_CHARS:
            dropped.append({"id": cand["id"], "source": cand["source"], "reason": f"length {len(text)}"})
        elif key in kept:
            dropped.append({"id": cand["id"], "source": cand["source"], "reason": f"same cache key as {kept[key]['id']}"})
        else:
            kept[key] = {**cand, "q": text}
    real = list(kept.values())
    companies = sorted({name for name, _, _ in FILERS.values()})
    candidates = _template_candidates(companies)
    quotas = _quotas(size - len(real))
    rng = random.Random(seed)
    templated: list[dict] = []
    for family, _ in FAMILY_SHARES:
        pool = list(candidates[family])
        rng.shuffle(pool)
        taken = 0
        for suffix, text in pool:
            if taken == quotas[family]:
                break
            key = salt.normalize_question(text)
            if key in kept:
                continue
            kept[key] = {"id": f"T:{family}:{suffix}", "q": text, "source": "template", "type": family}
            templated.append(kept[key])
            taken += 1
        if taken < quotas[family]:
            raise PoolError(f"template family {family!r} can only supply {taken} of {quotas[family]} questions")
    live = real + templated
    if len(live) != size:
        raise PoolError(f"the de-duplicated pool has {len(live)} questions, not {size}")
    for q in live:
        q["routed"] = "strong" if router.needs_strong_model(q["q"]) else "cheap"
    agent = [q for q in live if q["source"] == "agent_benchmark"]
    example_doc = _read(sources["examples"])
    citations = sorted({c for ex in example_doc["examples"] for c in ex.get("citations", [])})
    step = max(1, len(citations) // MAX_EVIDENCE_IDS)
    return {
        "version": POOL_VERSION, "size": len(live), "sha256": pool_digest(live),
        "sources": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()},
        "live": live, "agent": agent,
        "examples": [{"id": e["id"], "question": e["question"]} for e in example_doc["examples"]],
        "evidence_ids": citations[::step][:MAX_EVIDENCE_IDS],
        "tickers": sorted(FILERS),
        "composition": _composition(live, agent, dropped, quotas, retriever, sources),
    }


def _composition(live, agent, dropped, quotas, retriever, sources) -> dict:
    lengths = [len(q["q"]) for q in live]
    anchored = Counter()
    for q in live:
        for name in retriever.detect_anchors(q["q"]):
            anchored[name] += 1
    real = [q for q in live if q["source"] != "template"]
    return {
        "total": len(live),
        "real_questions": len(real), "templated_questions": len(live) - len(real),
        "by_source": dict(Counter(q["source"] for q in live)),
        "by_type": dict(sorted(Counter(q["type"] for q in live).items())),
        "templated_by_family": {k: v for k, v in quotas.items()},
        "routed": dict(Counter(q["routed"] for q in live)),
        "agent_questions": len(agent),
        "questions_naming_no_company": sum(1 for q in live if not retriever.detect_anchors(q["q"])),
        "companies_named": dict(sorted(anchored.items())),
        "years_named": _years_named([q["q"] for q in live]),
        "chars": {"min": min(lengths), "max": max(lengths), "mean": round(sum(lengths) / len(lengths), 1),
                  "limit_for_salt": salt.MAX_BASE_CHARS},
        "dropped_by_dedupe_or_length": {
            "count": len(dropped),
            "by_source": dict(Counter(d["source"] for d in dropped)),
            "items": [f"{d['id']} ({d['reason']})" for d in dropped],
        },
        "source_files": {name: str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
                         for name, path in sources.items()},
        "note": ("The real questions are the minority of the pool (the examples are a subset of the benchmark); the rest are "
                 "templated company x metric x year, risk-theme, dependency, temporal and comparison questions. Every live "
                 "ask is salted, so none of them repeats a cache key whatever the pool size."),
    }


def write_pool(doc: dict, pool_path: Path = DEFAULT_POOL_PATH, composition_path: Path = DEFAULT_COMPOSITION_PATH) -> None:
    pool_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {"sha256": doc["sha256"], "sources": doc["sources"], **doc["composition"]}
    composition_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def is_stale(pool_path: Path = DEFAULT_POOL_PATH, sources: dict[str, Path] | None = None) -> list[str]:
    """Names of the source files whose content changed since ``pool.json`` was built (empty = current)."""
    recorded = json.loads(Path(pool_path).read_text(encoding="utf-8")).get("sources", {})
    sources = sources or SOURCE_FILES
    return [name for name, path in sources.items() if recorded.get(name) != hashlib.sha256(path.read_bytes()).hexdigest()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("build", "show", "check"))
    parser.add_argument("--size", type=int, default=POOL_SIZE)
    args = parser.parse_args(argv)
    if args.command == "build":
        doc = build_pool(size=args.size)
        write_pool(doc)
        print(f"wrote {DEFAULT_POOL_PATH.name} ({doc['size']} questions) and {DEFAULT_COMPOSITION_PATH.name}")
    elif args.command == "check":
        stale = is_stale()
        load_pool()
        print("pool.json is current" if not stale else f"pool.json is STALE against: {', '.join(stale)} (rebuild it)")
        return 1 if stale else 0
    print(json.dumps(load_pool().composition, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
