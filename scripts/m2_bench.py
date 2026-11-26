"""M2-Bench: the blind retrieval benchmark of docs/specs/m2-search.md (section 7, OD3).

Pure helpers used to build, freeze and score the benchmark. No model call, no network, no database:

- ``in_scope_accessions``: the gold scope, the newest annual filing per filer plus the 10-Qs filed after it.
- ``bind_quote``: every chunk holding a verbatim copy of a gold quote; grade 2 in scope (current), grade 1 older.
- ``split_of``: the deterministic dev/test split by passage family.
- ``success_at``, ``ndcg_at``, ``sign_test``: the pre-registered metrics and test.
- ``registered_bench``: a frozen bench file is refused if its SHA-256 changed (a wrong label is recorded, not edited).
"""

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

ANNUAL_FORMS = frozenset({"10-K", "20-F"})
QUARTERLY_FORMS = frozenset({"10-Q"})
DEV_SHARE_MOD = 3  # one family in three goes to dev (30 dev, 60 test)
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", " ": " ",
                         "–": "-", "—": "-", "…": "...", "​": None, "‌": None,
                         "‍": None, "﻿": None})
MIN_QUOTE_CHARS = 25  # a shorter quote matches too many chunks to be gold
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lower case, straight quotes and dashes, no zero-width characters, single spaces: how quotes and chunks compare."""
    return _SPACE.sub(" ", text.translate(_QUOTES)).strip().lower()


def in_scope_accessions(chunks) -> set[str]:
    """Per filer, the newest annual filing and every 10-Q filed after it (the tripwire gold policy)."""
    scope: set[str] = set()
    filings = chunks[["cik", "form", "filing_date", "accession_no"]].drop_duplicates()
    for _, rows in filings.groupby("cik"):
        annual = rows[rows["form"].isin(ANNUAL_FORMS)]
        if annual.empty:
            continue
        newest = annual.sort_values(["filing_date", "accession_no"], kind="stable").iloc[-1]
        scope.add(newest["accession_no"])
        later = rows[rows["form"].isin(QUARTERLY_FORMS) & (rows["filing_date"] > newest["filing_date"])]
        scope.update(later["accession_no"])
    return scope


def bind_quote(quote: str, chunks) -> list[dict]:
    """Every chunk whose text holds the quote verbatim (after ``normalise``): grade 2 in scope, grade 1 older."""
    needle = normalise(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        raise ValueError(f"quote too short to bind as gold ({len(needle)} < {MIN_QUOTE_CHARS} characters)")
    scope = in_scope_accessions(chunks)
    hits = chunks[chunks["text"].map(lambda t: needle in normalise(t))]
    rows = [(0 if acc in scope else 1, date, cid) for cid, acc, date in
            zip(hits["chunk_id"], hits["accession_no"], hits["filing_date"])]
    return [{"chunk_id": cid, "grade": 2 if rank == 0 else 1} for rank, _, cid in sorted(rows)]


TYPES = ("A", "B", "C", "D", "E", "F")
RISK_SECTIONS = frozenset({"I.1A", "II.1A", "I.3"})
MIN_PASSAGE_CHARS = 150
WE = re.compile(r"\b(?:[Ww]e|[Oo]ur|[Uu]s|WE|OUR)\b")  # case-sensitive: "US" is not "us"
ID_LIKE = re.compile(r"\b(?!FY\d)(?:\d[A-Z]\d{3}[a-z]?|[A-Z]{1,5}\d{2,}[A-Z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z]+)\b")
_NUMBER = re.compile(r"\$\s?\d|\d+(?:\.\d+)?\s?%|\b\d[\d,.]*\s(?:million|billion)\b", re.IGNORECASE)
_PERIOD = re.compile(r"three months ended|six months ended|nine months ended|\bquarter\b|fiscal (?:year )?20\d\d", re.IGNORECASE)
PAIR_TOPICS = ("export", "china", "tsmc", "supply", "artificial intelligence", "data center", "tariff", "competition",
               "foundry", "memory", "cybersecurity", "litigation")


def _names_filer(text: str, aliases: Iterable[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"\b{re.escape(a.lower())}\b", low) for a in aliases)


def _family(ticker: str, text: str) -> str:
    # not a security use: a short deterministic id for a passage family (the frozen M2-Bench sample depends on it)
    return f"{ticker}:{hashlib.sha1(normalise(text)[:200].encode('utf-8')).hexdigest()[:10]}"  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1


def _item(kind: str, rows: list) -> dict:
    return {"type": kind, "chunk_ids": [r.chunk_id for r in rows], "ciks": [int(r.cik) for r in rows],
            "tickers": [r.ticker for r in rows], "family": "+".join(_family(r.ticker, r.text) for r in rows)}


def _single_type(kind: str, row, aliases: Mapping[int, list[str]]) -> bool:
    text = row.text
    if len(text) < MIN_PASSAGE_CHARS:
        return False
    if kind == "A":
        return bool(WE.search(text)) and not _names_filer(text, aliases.get(int(row.cik), []))
    if kind == "B":
        return bool(ID_LIKE.search(text))
    if kind == "C":
        return bool(_NUMBER.search(text))
    if kind == "E":
        return row.section_id in RISK_SECTIONS
    return bool(_PERIOD.search(text))  # F


def _pairs(rows: list, rng, per_type: int) -> list[list]:
    by_topic: dict[str, list] = {t: [r for r in rows if t in r.text.lower()] for t in PAIR_TOPICS}
    pairs, seen = [], set()
    topics = list(PAIR_TOPICS)
    for _ in range(per_type * 20):
        if len(pairs) == per_type:
            break
        pool = by_topic[rng.choice(topics)]
        if len(pool) < 2:
            continue
        a, b = rng.sample(pool, 2)
        key = tuple(sorted((a.chunk_id, b.chunk_id)))
        if a.cik != b.cik and key not in seen:
            seen.add(key)
            pairs.append(sorted([a, b], key=lambda r: r.chunk_id))
    return pairs


def sample_passages(chunks, seed: int, per_type: "int | Mapping[str, int]", aliases: Mapping[int, list[str]],
                    exclude_families: Iterable[str] = ()) -> dict[str, list[dict]]:
    """A fixed-seed draw of in-scope passages per type (A-F), one draw per text family across types and rounds.

    per_type is one count for every type or a count per type; exclude_families are families drawn in an earlier round.
    """
    import random

    scope = in_scope_accessions(chunks)
    rows = sorted((r for r in chunks.itertuples(index=False) if r.accession_no in scope), key=lambda r: r.chunk_id)
    fam = {r.chunk_id: _family(r.ticker, r.text) for r in rows}
    used: set[str] = set(exclude_families)  # text families already drawn: a passage repeated across filings is drawn once
    out: dict[str, list[dict]] = {}
    for kind in ("D", "A", "B", "C", "E", "F"):  # D first: its families never reach another type's split
        rng = random.Random(f"{seed}:{kind}")
        want = per_type if isinstance(per_type, int) else per_type.get(kind, 0)
        free, seen = [], set()
        for r in rows:
            if fam[r.chunk_id] not in used and fam[r.chunk_id] not in seen and len(r.text) >= MIN_PASSAGE_CHARS:
                seen.add(fam[r.chunk_id])
                free.append(r)
        if kind == "D":
            picked = _pairs(free, rng, want) if want else []
        else:
            cands = [r for r in free if _single_type(kind, r, aliases)]
            picked = [[r] for r in rng.sample(cands, min(want, len(cands)))]
        out[kind] = [_item(kind, group) for group in picked]
        used.update(fam[r.chunk_id] for group in picked for r in group)
    return {k: out[k] for k in TYPES}


_WORD = re.compile(r"[a-z]{4,}")
MAX_TOPIC_POOL = 300


def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(normalise(text)))


def sample_topic_pairs(chunks, seed: int, n: int, topics: Iterable[str],
                       exclude_families: Iterable[str] = ()) -> list[dict]:
    """Type D: pairs of in-scope passages from two different filers that contain the same specific topic phrase.

    Within a topic the most similar pair (Jaccard of content words) is taken first; topics are visited round-robin in a
    seeded order until ``n`` pairs are found. No chunk or text family is used twice, and excluded families are skipped.
    """
    import random

    scope = in_scope_accessions(chunks)
    rows = sorted((r for r in chunks.itertuples(index=False)
                   if r.accession_no in scope and len(r.text) >= MIN_PASSAGE_CHARS), key=lambda r: r.chunk_id)
    fam = {r.chunk_id: _family(r.ticker, r.text) for r in rows}
    used = set(exclude_families)
    order = list(topics)
    random.Random(f"{seed}:D-topics").shuffle(order)
    ranked: dict[str, list[tuple]] = {}
    for topic in order:
        pool = [r for r in rows if topic in normalise(r.text)][:MAX_TOPIC_POOL]
        words = {r.chunk_id: _words(r.text) for r in pool}
        pairs = []
        for i, a in enumerate(pool):
            for b in pool[i + 1:]:
                if a.cik != b.cik:
                    union = words[a.chunk_id] | words[b.chunk_id]
                    score = len(words[a.chunk_id] & words[b.chunk_id]) / len(union) if union else 0.0
                    pairs.append((-score, a.chunk_id, b.chunk_id, a, b))
        ranked[topic] = sorted(pairs, key=lambda p: p[:3])
    out: list[dict] = []
    while len(out) < n and any(ranked.values()):
        for topic in order:
            while ranked[topic]:
                _, _, _, a, b = ranked[topic].pop(0)
                if fam[a.chunk_id] not in used and fam[b.chunk_id] not in used and fam[a.chunk_id] != fam[b.chunk_id]:
                    used.update((fam[a.chunk_id], fam[b.chunk_id]))
                    out.append({**_item("D", [a, b]), "topic": topic})
                    break
            if len(out) == n:
                break
    return out


def split_of(family_id: str) -> str:
    """Deterministic split from the family id's hash: about one family in three is dev."""
    digest = int(hashlib.sha256(family_id.encode("utf-8")).hexdigest(), 16)
    return "dev" if digest % DEV_SHARE_MOD == 0 else "test"


def success_at(ranked: Iterable[str], gold: Iterable[Mapping], k: int, per_anchor: bool = False) -> bool:
    """Grade-2 gold in the top k. ``per_anchor`` (type D): every anchor needs its own grade-2 gold chunk in the top k."""
    top = set(list(ranked)[:k])
    current = [g for g in gold if g["grade"] == 2]
    if not per_anchor:
        return any(g["chunk_id"] in top for g in current)
    anchors = {g["anchor"] for g in gold}  # every anchor named in the gold, not only those with current copies
    return bool(anchors) and all(any(g["chunk_id"] in top for g in current if g["anchor"] == a) for a in anchors)


def ndcg_at(ranked: Iterable[str], gold: Iterable[Mapping], k: int) -> float:
    """Graded nDCG@k with gains 2^grade - 1."""
    grades = {g["chunk_id"]: g["grade"] for g in gold}
    unique = list(dict.fromkeys(ranked))[:k]
    dcg = sum((2 ** grades.get(cid, 0) - 1) / math.log2(i + 2) for i, cid in enumerate(unique))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test on the discordant pairs (ties dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1))
    return min(1.0, 2 * tail / 2 ** n)


ROOT = Path(__file__).resolve().parents[1]
# Frozen 2026-11-26 (commits 2cbe9ed and 4eab018, tag m2-bench-freeze): the SHA-256 of each git blob (LF line endings).
REGISTERED = {
    "dev": "8b1cfe3f918f819afef0c7199cb1d2cd8e368560c0e2c22bd4266ec6f60eb153",
    "test": "b122c3deff637cb4767a5d2676f4a274ac2e04163fdad72a8cec5c0038f10946",
}


def file_sha256(path: Path) -> str:
    """SHA-256 with CRLF read as LF, so a Windows checkout (core.autocrlf) hashes like the committed blob."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def registered_bench(path: Path, expected_sha256: str) -> dict:
    """A frozen bench file, refused if it differs from the registered one."""
    if file_sha256(path) != expected_sha256:
        raise SystemExit(f"refusing: {path.name} differs from the registered bench file (docs/specs/m2-search.md section 7)")
    return json.loads(path.read_text(encoding="utf-8"))


def score_run(bench: Iterable[Mapping], run: Mapping[str, Mapping]) -> dict[str, dict]:
    """Per question: Success@8 over the excerpts (primary), a hit anywhere in the context, and graded nDCG@10."""
    scored = {}
    for q in bench:
        r = run[q["id"]]
        d = q["type"] == "D"
        ctx = list(r["context_ids"])
        scored[q["id"]] = {"type": q["type"], "success8": success_at(r["excerpts"], q["gold"], k=8, per_anchor=d),
                           "context_hit": success_at(ctx, q["gold"], k=len(ctx), per_anchor=d),
                           "ndcg10": ndcg_at(r["excerpts"], q["gold"], k=10)}
    return scored


def summarise(scored: Mapping[str, Mapping]) -> dict:
    n = len(scored)
    hits = sum(1 for s in scored.values() if s["success8"])
    return {"n": n, "success8": hits, "context_hit": sum(1 for s in scored.values() if s["context_hit"]), "misses": n - hits}


ARMS = ("baseline", "mention", "filer", "period", "filer+period", "keyword", "keyword_ctx", "all", "all+rerank_minilm",
        "all+rerank_mxbai", "served")
# The legs of each candidate arm besides the always-on mention leg. ``keyword`` is bm25s without the context field,
# ``keyword_ctx`` with it, ``all`` is mention + filer + period + keyword with the context field.
ARM_LEGS = {"mention": {}, "filer": {"filer": True}, "period": {"period": True}, "filer+period": {"filer": True, "period": True},
            "keyword": {"keyword": True}, "keyword_ctx": {"keyword": True, "keyword_context": True},
            "all": {"filer": True, "period": True, "keyword": True, "keyword_context": True}}
# I3b: the ``all`` legs plus the local cross-encoder over the top 20 fused candidates (the slot policy then fills the slots).
ARM_LEGS["all+rerank_minilm"] = {**ARM_LEGS["all"], "rerank": "minilm", "rerank_depth": 20}
ARM_LEGS["all+rerank_mxbai"] = {**ARM_LEGS["all"], "rerank": "mxbai", "rerank_depth": 20}
# Decision 012: what the service serves with RETRIEVAL_RERANK=minilm (fusion.serving_config): the frozen arm at a 192-token pair window
# and at most 25 candidates. The frozen arms above keep depth 20 / 384 tokens / no cap so the recorded numbers stay reproducible.
ARM_LEGS["served"] = {**ARM_LEGS["all"], "rerank": "minilm", "rerank_depth": 20, "rerank_max_tokens": 192, "rerank_max_candidates": 25}


def require_reranker(fusion) -> None:
    """A rerank arm without its model would silently run as ``all`` under a rerank label: refuse before any work."""
    if fusion is None or not fusion.rerank:
        return
    from semigraph.retrieval.rerank import get_reranker

    if get_reranker(fusion.rerank, max_tokens=fusion.rerank_max_tokens) is None:
        raise SystemExit(f"refusing: the {fusion.rerank} reranker model is missing or unloadable (models/ directory); "
                         "this arm would run without reranking")


def arm_config(arm: str):
    """The FusionConfig of a candidate arm (the mention leg is always on), or None for the baseline (today's exact path).
    ``mention`` is the fused path with the mention leg alone: the reference for the other arms, so they are not credited
    with what the slot policy (k=50, dedup, accession cap, RRF order) does by itself. The new legs are current-only
    (FusionConfig's default, decision 011)."""
    if arm not in ARMS:
        raise SystemExit(f"unknown arm {arm!r}; one of {', '.join(ARMS)}")
    if arm == "baseline":
        return None
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.retrieval.fusion import FusionConfig

    return FusionConfig(mention=True, **ARM_LEGS[arm])


def index_for(cfg, rows):
    """The keyword index a config needs, built from the loader's rows (the context field only when the arm has it), or None
    when the arm has no keyword leg."""
    if cfg is None or not cfg.keyword:
        return None
    from semigraph.retrieval.keyword import build_index

    return build_index(rows, with_context=cfg.keyword_context)


def timing_summary(seconds: Iterable[float]) -> dict:
    """Wall seconds per question: count, mean, nearest-rank p95 and max."""
    xs = sorted(seconds)
    if not xs:
        return {"n": 0, "mean_s": 0.0, "p95_s": 0.0, "max_s": 0.0}
    return {"n": len(xs), "mean_s": sum(xs) / len(xs), "p95_s": xs[math.ceil(0.95 * len(xs)) - 1], "max_s": xs[-1]}


def run_dev(out: Path, arm: str = "baseline") -> dict:
    """Run one arm of the retrieval on the frozen dev split (main and reserves) on a LOCAL graph; ids only, no context text.

    The test split is never run here: it runs once, in I6, after all tuning on dev.
    """
    import sys
    import time

    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.answerer import build_blocks
    from semigraph.retrieval.keyword import load_rows
    from semigraph.retrieval.retriever import hybrid_retrieve

    fusion = arm_config(arm)
    require_reranker(fusion)
    bench = registered_bench(ROOT / "artifacts" / "m2_bench" / "m2_bench_dev.json", REGISTERED["dev"])["questions"]
    settings = get_settings()
    require_local_graph(settings.neo4j_uri)
    driver, embedder, run = client.get_driver(settings), Embedder(), {}
    index_build_s = 0.0
    try:
        snapshot = _snapshot(driver)
        started = time.perf_counter()
        index = index_for(fusion, load_rows(driver) if fusion is not None and fusion.keyword else [])   # at run start, from the local graph
        index_build_s = time.perf_counter() - started
        for q in bench:
            began = time.perf_counter()
            r = hybrid_retrieve(q["q"], driver, embedder, fusion=fusion, keyword_index=index,
                            rerank_budget_s=0)  # answer_stream's defaults: k_chunks=8, hops=2
            elapsed = time.perf_counter() - began                  # retrieval only: the context rendering below is not timed
            _, _, valid_ids = build_blocks(r)
            run[q["id"]] = {"excerpts": [c["chunk_id"] for c in r["chunks"]], "context_ids": sorted(valid_ids),
                            "retrieval_s": round(elapsed, 4)}
            if "leg_ranks" in r:
                run[q["id"]]["leg_ranks"] = r["leg_ranks"]
            if "rerank_s" in r:
                run[q["id"]]["rerank_s"] = r["rerank_s"]
    finally:
        driver.close()
    scored = score_run(bench, run)
    main_ids = {q["id"] for q in bench if q["role"] == "main"}
    report = {"split": "dev", "arm": arm, "snapshot": snapshot, "embedding_backend": embedder.backend,
              "summary_main": summarise({k: v for k, v in scored.items() if k in main_ids}),
              "summary_all": summarise(scored), "retrieval_timing": timing_summary(v["retrieval_s"] for v in run.values()),
              "keyword_index_build_s": round(index_build_s, 2),
              "rerank_timing": timing_summary(v["rerank_s"] for v in run.values() if "rerank_s" in v), "questions": {k: {**scored[k], **run[k]} for k in scored}}
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report



# ---- I6: the one-time M2-Bench test run (spec section 7: primary test, no-loss gates; R2, R3, R21) ----

ALPHA = 0.05
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261126
CONTROL_LOSS_LIMIT = 2
FUSED_LOG_DEPTH = 50
BENCH_DIR = ROOT / "artifacts" / "m2_bench"
_KNOWN_ID = re.compile(r"\b\d{10}-\d{2}-\d{6}:[A-Za-z0-9.]+:\d+\b|\bxbrl:\d+:[a-z_]+:\d{4}-\d{2}-\d{2}\b|\bfr:\d{4}-\d{5}\b")


RUN_ONCE_FILES = ("test_started.json", "test_strata.json", "test_report.json")
# SHA-256 of artifacts/m2_bench/frozen_settings.json as frozen on dev (commit 76ced9e), LF line endings.
FROZEN_SETTINGS_SHA256 = "c5018d87ddd9a9d18e9b4468456fe253d054368d2e80f6142b4c9520778540ce"
SNAPSHOT_QUERY = "MATCH (s:Snapshot) RETURN s.id AS id ORDER BY s.created_at DESC LIMIT 1"


def refuse_if_already_run(out_dir: Path) -> None:
    """The test split runs once: a second look is a new experiment, so there is no override. Any trace of a run counts."""
    for name in RUN_ONCE_FILES:
        if (out_dir / name).exists():
            raise SystemExit(f"refusing: {name} exists; the M2-Bench test runs once (spec section 7)")


def claim_test_run(out_dir: Path, meta: Mapping) -> None:
    """Take the one run (exclusive create) right before the first test question; a crash later cannot re-open it."""
    try:
        with open(out_dir / "test_started.json", "x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(meta, indent=1) + "\n")
    except FileExistsError:
        raise SystemExit("refusing: test_started.json exists; the M2-Bench test runs once (spec section 7)") from None


def require_frozen_settings(path: Path) -> str:
    """The frozen settings must be the ones frozen on dev (registered hash); returns the hash."""
    digest = file_sha256(path)
    if digest != FROZEN_SETTINGS_SHA256:
        raise SystemExit(f"refusing: {path.name} differs from the frozen settings (registered SHA-256 {FROZEN_SETTINGS_SHA256[:16]})")
    return digest


def check_reranker_hash(path: Path, expected_prefix: str) -> str:
    """The reranker file the run would load must be the one frozen (SHA-256 prefix); returns the prefix."""
    if not path.is_file():
        raise SystemExit(f"refusing: the frozen reranker file {path} is missing")
    prefix = hashlib.sha256(path.read_bytes()).hexdigest()[:len(expected_prefix)]
    if prefix != expected_prefix:
        raise SystemExit(f"refusing: the reranker file differs from the frozen one ({prefix} != {expected_prefix})")
    return prefix


def check_frozen_reranker(frozen: Mapping) -> str:
    from semigraph.retrieval.rerank import RERANK_MODELS, default_root

    directory, onnx_file = RERANK_MODELS[frozen["config"]["rerank"]]
    if onnx_file != frozen["reranker"]["file"]:
        raise SystemExit("refusing: the reranker file name differs from the frozen settings")
    return check_reranker_hash(default_root() / directory / onnx_file, frozen["reranker"]["sha256_prefix"])


def frozen_candidate(path: Path):
    """The candidate FusionConfig, read from the frozen settings artifact."""
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.retrieval.fusion import FusionConfig

    return FusionConfig(**json.loads(path.read_text(encoding="utf-8"))["config"])


def recall_at(ranked: Iterable[str], gold: Iterable[Mapping], k: int) -> float:
    """Share of the grade-2 gold chunks inside the first k distinct ranked ids."""
    current = {g["chunk_id"] for g in gold if g["grade"] == 2}
    top = set(list(dict.fromkeys(ranked))[:k])
    return len(current & top) / len(current) if current else 0.0


def recall_ranking(excerpts: Iterable[str], context_ids: Iterable[str]) -> list[str]:
    """The ranked list Recall@20 reads: the excerpts in rank order, then the other context ids (sorted; unranked)."""
    first = list(excerpts)
    return first + sorted(set(context_ids) - set(first))


def classify_strata(baseline_scored: Mapping[str, Mapping]) -> dict[str, str]:
    """Headroom: the baseline context lacks the grade-2 gold. Control: it has it. Decided from the baseline run alone."""
    return {qid: "control" if s["context_hit"] else "headroom" for qid, s in baseline_scored.items()}


def paired_outcomes(base: Mapping, cand: Mapping, ids: Iterable[str]) -> tuple[int, int]:
    """(wins, losses) of the candidate over the base on Success@8; ties are dropped."""
    ids = list(ids)
    wins = sum(1 for i in ids if cand[i]["success8"] and not base[i]["success8"])
    losses = sum(1 for i in ids if base[i]["success8"] and not cand[i]["success8"])
    return wins, losses


def sign_step(base: Mapping, cand: Mapping, ids: Iterable[str]) -> dict:
    wins, losses = paired_outcomes(base, cand, ids)
    p = sign_test(wins, losses)
    return {"wins": wins, "losses": losses, "p": p, "alpha": ALPHA, "passes": p < ALPHA and wins > losses}


def primary_test(base: Mapping, cand: Mapping, filer_period: Mapping, ids: Iterable[str]) -> dict:
    """H1 candidate vs baseline; H2 (candidate vs filer+period) only if H1 passes; H1 without type A as a sensitivity."""
    ids = list(ids)
    h1 = sign_step(base, cand, ids)
    no_a = [i for i in ids if base[i]["type"] != "A"]
    return {"h1": h1, "h2": {"run": True, **sign_step(filer_period, cand, ids)} if h1["passes"] else {"run": False},
            "h1_without_type_a": sign_step(base, cand, no_a)}


def per_type_gate(base: Mapping, cand: Mapping, ids: Iterable[str]) -> dict:
    """Wins minus losses at least 0 in every type (test split, main questions)."""
    ids = list(ids)
    by_type = {t: (lambda w_l: w_l[0] - w_l[1])(paired_outcomes(base, cand, [i for i in ids if base[i]["type"] == t]))
               for t in sorted({base[i]["type"] for i in ids})}
    return {"pass": all(v >= 0 for v in by_type.values()), "by_type": by_type}


def control_gate(base: Mapping, cand: Mapping, ids: Iterable[str], strata: Mapping[str, str]) -> dict:
    """At most two losses among the control-stratum questions."""
    control = [i for i in ids if strata[i] == "control"]
    losses = paired_outcomes(base, cand, control)[1]
    ctx_losses = sum(1 for i in control if base[i]["context_hit"] and not cand[i]["context_hit"])   # reported, not gated
    return {"pass": losses <= CONTROL_LOSS_LIMIT, "losses": losses, "context_hit_losses": ctx_losses,
            "control_n": len(control), "limit": CONTROL_LOSS_LIMIT}


def tripwire_gate(base_verdicts: Iterable[Mapping], cand_verdicts: Iterable[Mapping]) -> dict:
    """Decision 006's verdicts (compare.tripwire_verdict, ``hit``): every question that hits on the baseline must still hit.

    A question with a gold chunk the graph does not hold is not a clean measurement (decision 006 recorded R2): it is left out
    of the gate and listed under ``excluded``; the gold itself is never edited."""
    base_v, cand_v = list(base_verdicts), list(cand_verdicts)
    excluded = sorted({v["id"] for v in base_v + cand_v if v.get("missing_from_graph")})
    base = {v["id"]: v["hit"] for v in base_v if v["id"] not in excluded}
    cand = {v["id"]: v["hit"] for v in cand_v if v["id"] not in excluded}
    lost = [i for i in base if base[i] and not cand[i]]
    return {"pass": not lost, "lost": lost, "baseline_hits": sum(base.values()), "candidate_hits": sum(cand.values()),
            "n": len(base), "excluded": excluded}


def known_ids(question: Mapping) -> set[str]:
    """The evidence ids an old-benchmark question names in metric_ids, facts and judge_notes (chunk, xbrl and fr ids)."""
    text = json.dumps([question.get(k) for k in ("metric_ids", "facts", "judge_notes")], ensure_ascii=False)
    return set(_KNOWN_ID.findall(text))


def old_sixty_gate(questions: Iterable[Mapping], base_ctx: Mapping[str, Iterable[str]], cand_ctx: Mapping[str, Iterable[str]]) -> dict:
    """Of the questions with known evidence ids: a context miss is none of them in the candidate context, and a lost id is one the
    baseline context held that the candidate's does not (cp-add-company: "none loses a gold id from context"). Both fail the gate."""
    misses, baseline_misses, lost, checked, skipped = [], [], {}, 0, 0
    for q in questions:
        ids = known_ids(q)
        if not ids:
            skipped += 1
            continue
        checked += 1
        base, cand = set(base_ctx[q["id"]]), set(cand_ctx[q["id"]])
        if not ids & cand:
            misses.append(q["id"])
        if not ids & base:
            baseline_misses.append(q["id"])
        if (ids & base) - cand:
            lost[q["id"]] = sorted((ids & base) - cand)
    return {"pass": not misses and not lost, "checked": checked, "misses": misses, "baseline_misses": baseline_misses,
            "lost": lost, "skipped": skipped}


def paired_bootstrap(diffs: Mapping[str, list[float]], resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED):
    """Mean paired difference of each resample, drawing whole passage families with replacement (fixed seed)."""
    import numpy as np

    fams = sorted(diffs)
    sums = np.array([sum(diffs[f]) for f in fams], dtype=float)
    counts = np.array([len(diffs[f]) for f in fams], dtype=float)
    idx = np.random.default_rng(seed).integers(0, len(fams), size=(resamples, len(fams)))
    return sums[idx].sum(axis=1) / counts[idx].sum(axis=1)


def holm(pvals: Mapping[str, float], alpha: float = ALPHA) -> dict[str, dict]:
    """Holm step-down: per metric whether it is significant and the alpha its interval is cut at (alpha / (m - rank))."""
    out, failed = {}, False
    for rank, (name, p) in enumerate(sorted(pvals.items(), key=lambda kv: kv[1])):
        level = alpha / (len(pvals) - rank)
        failed = failed or p >= level
        out[name] = {"significant": not failed, "alpha": level}
    return out


def _bootstrap_p(means) -> float:
    n = len(means)
    return min(1.0, 2 * min(((means <= 0).sum() + 1) / (n + 1), ((means >= 0).sum() + 1) / (n + 1)))


def secondary_metrics(base: Mapping, cand: Mapping, ids: Iterable[str], family: Mapping[str, str],
                      resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> dict[str, dict]:
    """Success@8, nDCG@10 and Recall@20 paired differences with family-bootstrap intervals at the Holm level."""
    import numpy as np

    ids, boots, mean_diff = list(ids), {}, {}
    for name in ("success8", "ndcg10", "recall20"):
        diffs: dict[str, list[float]] = {}
        for i in ids:
            diffs.setdefault(family[i], []).append(float(cand[i][name]) - float(base[i][name]))
        boots[name] = paired_bootstrap(diffs, resamples, seed)
        mean_diff[name] = sum(sum(v) for v in diffs.values()) / len(ids)
    levels = holm({n: _bootstrap_p(m) for n, m in boots.items()})
    return {n: {"mean_diff": mean_diff[n], "holm": levels[n], "p": _bootstrap_p(boots[n]),
                "ci": [float(np.quantile(boots[n], levels[n]["alpha"] / 2)),
                       float(np.quantile(boots[n], 1 - levels[n]["alpha"] / 2))]} for n in boots}


def fused_ranking(leg_ranks: Mapping[str, list[str]], rrf_k: int) -> list[str]:
    """Reciprocal rank fusion of the logged leg lists (the offline stand-in for the fused list; no rerank, no slot policy)."""
    score: dict[str, float] = {}
    for ranked in leg_ranks.values():
        for pos, cid in enumerate(ranked):
            score[cid] = score.get(cid, 0.0) + 1.0 / (rrf_k + pos + 1)
    first = {cid: n for n, cid in enumerate(dict.fromkeys(c for r in leg_ranks.values() for c in r))}
    return sorted(score, key=lambda c: (-score[c], first[c]))


def reranker_trigger(questions: Mapping[str, Mapping], gold: Mapping[str, list], ids: Iterable[str], rrf_k: int) -> dict:
    """R21: of the dev misses, the fraction with grade-2 gold in the fused top 50 but not in the fused top 8 (offline)."""
    misses = [i for i in ids if not questions[i]["success8"]]
    deep = 0
    for i in misses:
        fused = fused_ranking(questions[i].get("leg_ranks", {}), rrf_k)
        current = {g["chunk_id"] for g in gold[i] if g["grade"] == 2}
        if current & set(fused[:FUSED_LOG_DEPTH]) - set(fused[:8]):
            deep += 1
    return {"misses": len(misses), "gold_in_top50_beyond_8": deep, "fraction": deep / len(misses) if misses else 0.0}


def verdict_line(primary: Mapping, gates: Mapping[str, Mapping]) -> str:
    """One line: H1, H2 and the no-loss gates."""
    def step(name, s):
        return f"{name} not run" if not s.get("run", True) else \
            f"{name} {'pass' if s['passes'] else 'fail'} {s['wins']}-{s['losses']} (p={s['p']:.4g})"

    failed = [n for n, g in gates.items() if not g["pass"]]
    tail = "gates OK" if not failed else "HOLD, failed gates: " + ", ".join(failed)
    return " | ".join([step("H1", primary["h1"]), step("H2", primary["h2"]), tail])


def score_arm(bench: Iterable[Mapping], run: Mapping[str, Mapping]) -> dict[str, dict]:
    """score_run plus Recall@20 over the excerpts-then-context ranking."""
    bench = list(bench)
    scored = score_run(bench, run)
    for q in bench:
        r = run[q["id"]]
        scored[q["id"]]["recall20"] = recall_at(recall_ranking(r["excerpts"], r["context_ids"]), q["gold"], 20)
    return scored


def analyse_test(bench: list[Mapping], scored: Mapping[str, Mapping[str, dict]], strata: Mapping[str, str]) -> dict:
    """Primary tests, secondary intervals and the per-type and control gates, on the main questions only."""
    main = [q["id"] for q in bench if q["role"] == "main"]
    family = {q["id"]: q["family"] for q in bench}
    base, cand, fp = scored["baseline"], scored["candidate"], scored["filer+period"]
    return {"primary": primary_test(base, cand, fp, main),
            "secondary_vs_baseline": secondary_metrics(base, cand, main, family),
            "gates": {"per_type": per_type_gate(base, cand, main), "control": control_gate(base, cand, main, strata)}}


def require_local_graph(uri: str) -> None:
    from urllib.parse import urlsplit

    host = urlsplit(uri)
    if host.hostname not in {"localhost", "127.0.0.1", "::1"} or host.username or host.password:
        raise SystemExit("refusing: M2-Bench runs read a local graph only")


def _load_script(name: str):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _retrieve_ids(items: Iterable[Mapping], driver, embedder, fusion, index) -> dict[str, dict]:
    """Ids only (no context text): the excerpts in rank order, the rendered context's ids, the logged leg lists."""
    from semigraph.retrieval.answerer import build_blocks
    from semigraph.retrieval.retriever import hybrid_retrieve

    run = {}
    for it in items:
        r = hybrid_retrieve(it["q"], driver, embedder, fusion=fusion, keyword_index=index,
                            rerank_budget_s=0)  # answer_stream's defaults
        _, _, valid_ids = build_blocks(r)
        run[it["id"]] = {"excerpts": [c["chunk_id"] for c in r["chunks"]], "context_ids": sorted(valid_ids)}
        if "leg_ranks" in r:
            run[it["id"]]["leg_ranks"] = r["leg_ranks"]
    return run


def _retrieve_context(question: str, driver, embedder, fusion, index) -> str:
    """The rendered context text, held in memory for decision 006's tripwire verdict only (never written)."""
    from semigraph.retrieval.answerer import build_blocks
    from semigraph.retrieval.retriever import hybrid_retrieve

    return build_blocks(hybrid_retrieve(question, driver, embedder, fusion=fusion, keyword_index=index, rerank_budget_s=0))[1]


def _slim(scored: Mapping[str, dict], run: Mapping[str, dict]) -> dict[str, dict]:
    return {qid: {**s, "excerpts": run[qid]["excerpts"]} for qid, s in scored.items()}


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8", newline="\n")


def _dev_trigger(frozen_rrf_k: int) -> dict:
    """R21 from the dev report of the unreranked ``all`` arm (offline; no graph)."""
    dev = registered_bench(BENCH_DIR / "m2_bench_dev.json", REGISTERED["dev"])["questions"]
    report = json.loads((BENCH_DIR / "all_dev.json").read_text(encoding="utf-8"))
    main = [q["id"] for q in dev if q["role"] == "main"]
    return reranker_trigger(report["questions"], {q["id"]: q["gold"] for q in dev}, main, frozen_rrf_k)


def _git_state() -> dict:
    """HEAD and the uncommitted paths (other tracks edit src/ while this runs, so the run records what it ran on)."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()

    dirty = [ln for ln in git("status", "--porcelain").splitlines() if not ln.startswith("??")]
    return {"head": git("rev-parse", "HEAD"), "dirty": bool(dirty), "modified_tracked_files": dirty[:60]}


def _prepare_offline(out_dir: Path) -> dict:
    """Everything fallible that needs no graph: a failure here has not looked at the test split."""
    refuse_if_already_run(out_dir)
    frozen_path = out_dir / "frozen_settings.json"
    frozen_sha = require_frozen_settings(frozen_path)
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    arms = {"baseline": None, "filer+period": arm_config("filer+period"), "candidate": frozen_candidate(frozen_path)}
    for fusion in arms.values():
        require_reranker(fusion)
    reranker_sha = check_frozen_reranker(frozen)
    compare = _load_script("compare_retrieval")
    old = json.loads((ROOT / "src" / "semigraph" / "artifacts" / "benchmark.json").read_text(encoding="utf-8"))
    return {"frozen": frozen, "arms": arms, "compare": compare, "old": old,
            "bench": registered_bench(out_dir / "m2_bench_test.json", REGISTERED["test"])["questions"],
            "tripwire": compare.registered_gold(ROOT / "artifacts" / "m2_tripwire_gold.json")["questions"],
            "trigger": _dev_trigger(frozen["config"].get("rrf_k", 60)),
            "provenance": {"frozen_settings_sha256": frozen_sha, "reranker_sha256_prefix": reranker_sha, **_git_state()}}


def _snapshot(driver) -> str:
    with driver.session(default_access_mode="READ") as ses:
        return ses.run(SNAPSHOT_QUERY).single()["id"]


def _tripwire_mentions(prep: Mapping, driver) -> dict:
    """Decision 006's MENTIONS lookup for the tripwire gold. A gold chunk the graph does not hold is not an error here: its
    question's verdict carries ``missing_from_graph`` and ``tripwire_gate`` leaves it out and lists it (decision 006, R2)."""
    compare = prep["compare"]
    ids = sorted({g["chunk_id"] for q in prep["tripwire"] for g in q["gold"]})
    return {r["chunk_id"]: r["mention_ciks"] for r in compare._session_rows(driver, compare.GOLD_MENTIONS_QUERY, ids=ids)}


def _test_split_runs(prep: Mapping, out_dir: Path, driver, embedder, indexes: Mapping, snapshot: str) -> tuple[dict, dict, dict]:
    """Baseline first, strata written from it, then the other two arms (one pass over every test question)."""
    arms, bench = prep["arms"], prep["bench"]
    items = [{"id": q["id"], "q": q["q"]} for q in bench]
    runs = {"baseline": _retrieve_ids(items, driver, embedder, None, None)}
    scored = {"baseline": score_arm(bench, runs["baseline"])}
    strata = classify_strata(scored["baseline"])                          # before any candidate is scored
    _write_json(out_dir / "test_strata.json", {"snapshot": snapshot, "strata": strata,
                                               "counts": {s: list(strata.values()).count(s) for s in ("headroom", "control")}})
    for name in ("filer+period", "candidate"):
        runs[name] = _retrieve_ids(items, driver, embedder, arms[name], indexes[name])
        scored[name] = score_arm(bench, runs[name])
    _write_json(out_dir / "test_runs.json", {"snapshot": snapshot, "runs": runs})   # ids only, before any analysis can fail
    return runs, scored, strata


def _no_loss_runs(prep: Mapping, driver, embedder, indexes: Mapping, mentions: Mapping) -> dict:
    """The 9 tripwire questions (decision 006 verdicts on the rendered context) and the old 60 (context ids), baseline and candidate."""
    arms, compare = prep["arms"], prep["compare"]
    out = {"tripwire": {}, "old": {}}
    for name in ("baseline", "candidate"):
        out["tripwire"][name] = [compare.tripwire_verdict(
            q, _retrieve_context(q["question"], driver, embedder, arms[name], indexes[name]), mentions) for q in prep["tripwire"]]
        run = _retrieve_ids([{"id": q["id"], "q": q["q"]} for q in prep["old"]], driver, embedder, arms[name], indexes[name])
        out["old"][name] = {k: v["context_ids"] for k, v in run.items()}
    return out


def _graph_phase(prep: Mapping, out_dir: Path) -> dict:
    """Connect (local graph only), claim the one run, then run. Nothing here is repeatable."""
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.keyword import load_rows

    settings = get_settings()
    require_local_graph(settings.neo4j_uri)
    driver, embedder = client.get_driver(settings), Embedder()
    try:
        snapshot, rows = _snapshot(driver), load_rows(driver)
        indexes = {n: index_for(f, rows) for n, f in prep["arms"].items()}   # each keyword index is built once
        mentions = _tripwire_mentions(prep, driver)
        claim_test_run(out_dir, {"snapshot": snapshot, **prep["provenance"]})   # the first test question is asked after this line
        runs, scored, strata = _test_split_runs(prep, out_dir, driver, embedder, indexes, snapshot)
        no_loss = _no_loss_runs(prep, driver, embedder, indexes, mentions)
        _write_json(out_dir / "test_noloss_runs.json", {"tripwire": no_loss["tripwire"], "old_context_ids": no_loss["old"]})
    finally:
        driver.close()
    return {"snapshot": snapshot, "backend": embedder.backend, "runs": runs, "scored": scored, "strata": strata, "no_loss": no_loss}


def _tripwire_hits(verdicts: Iterable[Mapping]) -> list[dict]:
    return [{"id": v["id"], "hit": v["hit"], "missing_from_graph": list(v.get("missing_from_graph") or [])} for v in verdicts]


REPORT_NOTES = {
    "recall20": "approximate: ranks 1-8 are the excerpts in rank order, ranks 9-20 are the other context ids sorted alphabetically; not for public claims",
    "control_gate": "control = baseline context holds grade-2 gold; the gate counts Success@8 losses, context_hit_losses is reported beside it",
    "old_sixty": "pass needs no context miss (any-of) AND no known id that the baseline context held but the candidate context lost",
}


def run_test(out_dir: Path = BENCH_DIR) -> dict:
    """The one-time M2-Bench test run: baseline, filer+period and the frozen candidate on every test question, then the
    no-loss sets. Offline checks first, then the claim (test_started.json, exclusive create), then the graph. Strata are
    written from the baseline run before any candidate is scored. Local graph only."""
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    prep = _prepare_offline(out_dir)
    got = _graph_phase(prep, out_dir)
    bench, scored, no_loss = prep["bench"], got["scored"], got["no_loss"]
    analysis = analyse_test(bench, scored, got["strata"])
    trip = tripwire_gate(_tripwire_hits(no_loss["tripwire"]["baseline"]), _tripwire_hits(no_loss["tripwire"]["candidate"]))
    sixty = old_sixty_gate(prep["old"], no_loss["old"]["baseline"], no_loss["old"]["candidate"])
    gates = {**analysis["gates"], "tripwire": trip, "old_sixty": sixty}
    report = {"split": "test", "snapshot": got["snapshot"], "embedding_backend": got["backend"], "frozen": prep["frozen"]["frozen"],
              "provenance": prep["provenance"], "notes": REPORT_NOTES,
              "arms": {n: _slim(scored[n], got["runs"][n]) for n in scored}, "strata": got["strata"], **analysis, "gates": gates,
              "reranker_trigger_dev": prep["trigger"], "verdict": verdict_line(analysis["primary"], gates)}
    _write_json(out_dir / "test_report.json", report)
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="M2-Bench (docs/specs/m2-search.md section 7)")
    ap.add_argument("cmd", choices=["run-dev", "run-test"])
    ap.add_argument("--arm", choices=ARMS, default="baseline", help="run-dev only")
    ap.add_argument("--out", type=Path, help="run-dev only; default: artifacts/m2_bench/<arm>_dev.json")
    args = ap.parse_args()
    if args.cmd == "run-test":      # no flags: the run is guarded to happen once
        print(run_test()["verdict"])
    else:
        rep = run_dev(args.out or ROOT / "artifacts" / "m2_bench" / f"{args.arm.replace('+', '_')}_dev.json", args.arm)
        print("main:", rep["summary_main"], "| all:", rep["summary_all"], "| snapshot:", rep["snapshot"])
