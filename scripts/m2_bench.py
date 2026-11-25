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
    return f"{ticker}:{hashlib.sha1(normalise(text)[:200].encode('utf-8')).hexdigest()[:10]}"


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


ARMS = ("baseline", "mention", "filer", "period", "filer+period")


def arm_config(arm: str):
    """The FusionConfig of a candidate arm (the mention leg is always on), or None for the baseline (today's exact path).
    ``mention`` is the fused path with the mention leg alone: the reference for filer / period, so those arms are not
    credited with what the slot policy (k=50, dedup, accession cap, RRF order) does by itself."""
    if arm not in ARMS:
        raise SystemExit(f"unknown arm {arm!r}; one of {', '.join(ARMS)}")
    if arm == "baseline":
        return None
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.retrieval.fusion import FusionConfig

    return FusionConfig(mention=True, filer="filer" in arm, period="period" in arm)


def run_dev(out: Path, arm: str = "baseline") -> dict:
    """Run one arm of the retrieval on the frozen dev split (main and reserves) on a LOCAL graph; ids only, no context text.

    The test split is never run here: it runs once, in I6, after all tuning on dev.
    """
    import sys
    from urllib.parse import urlsplit

    sys.path.insert(0, str(ROOT / "src"))
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.answerer import build_blocks
    from semigraph.retrieval.retriever import hybrid_retrieve

    fusion = arm_config(arm)
    bench = registered_bench(ROOT / "artifacts" / "m2_bench" / "m2_bench_dev.json", REGISTERED["dev"])["questions"]
    settings = get_settings()
    host = urlsplit(settings.neo4j_uri)
    if host.hostname not in {"localhost", "127.0.0.1", "::1"} or host.username or host.password:
        raise SystemExit("refusing: M2-Bench runs read a local graph only")
    driver, embedder, run = client.get_driver(settings), Embedder(), {}
    try:
        with driver.session(default_access_mode="READ") as ses:
            snapshot = ses.run("MATCH (s:Snapshot) RETURN s.id AS id ORDER BY s.created_at DESC LIMIT 1").single()["id"]
        for q in bench:
            r = hybrid_retrieve(q["q"], driver, embedder, fusion=fusion)  # answer_stream's defaults: k_chunks=8, hops=2
            _, _, valid_ids = build_blocks(r)
            run[q["id"]] = {"excerpts": [c["chunk_id"] for c in r["chunks"]], "context_ids": sorted(valid_ids)}
            if "leg_ranks" in r:
                run[q["id"]]["leg_ranks"] = r["leg_ranks"]
    finally:
        driver.close()
    scored = score_run(bench, run)
    main_ids = {q["id"] for q in bench if q["role"] == "main"}
    report = {"split": "dev", "arm": arm, "snapshot": snapshot, "embedding_backend": embedder.backend,
              "summary_main": summarise({k: v for k, v in scored.items() if k in main_ids}),
              "summary_all": summarise(scored), "questions": {k: {**scored[k], **run[k]} for k in scored}}
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="M2-Bench (docs/specs/m2-search.md section 7)")
    ap.add_argument("cmd", choices=["run-dev"])
    ap.add_argument("--arm", choices=ARMS, default="baseline")
    ap.add_argument("--out", type=Path, help="default: artifacts/m2_bench/<arm>_dev.json")
    args = ap.parse_args()
    rep = run_dev(args.out or ROOT / "artifacts" / "m2_bench" / f"{args.arm.replace('+', '_')}_dev.json", args.arm)
    print("main:", rep["summary_main"], "| all:", rep["summary_all"], "| snapshot:", rep["snapshot"])
