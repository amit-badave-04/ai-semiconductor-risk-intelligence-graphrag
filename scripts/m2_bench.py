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
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", " ": " "})
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lower case, straight quotes, single spaces: the form quotes and chunks are compared in."""
    return _SPACE.sub(" ", text.translate(_QUOTES)).strip().lower()


def in_scope_accessions(chunks) -> set[str]:
    """Per filer, the newest annual filing and every 10-Q filed after it (the tripwire gold policy)."""
    scope: set[str] = set()
    filings = chunks[["cik", "form", "filing_date", "accession_no"]].drop_duplicates()
    for _, rows in filings.groupby("cik"):
        annual = rows[rows["form"].isin(ANNUAL_FORMS)]
        if annual.empty:
            continue
        newest = annual.sort_values("filing_date").iloc[-1]
        scope.add(newest["accession_no"])
        later = rows[rows["form"].isin(QUARTERLY_FORMS) & (rows["filing_date"] > newest["filing_date"])]
        scope.update(later["accession_no"])
    return scope


def bind_quote(quote: str, chunks) -> list[dict]:
    """Every chunk whose text holds the quote verbatim (after ``normalise``): grade 2 in scope, grade 1 older."""
    needle = normalise(quote)
    if not needle:
        return []
    scope = in_scope_accessions(chunks)
    hits = chunks[chunks["text"].map(lambda t: needle in normalise(t))]
    rows = [(0 if acc in scope else 1, date, cid) for cid, acc, date in
            zip(hits["chunk_id"], hits["accession_no"], hits["filing_date"])]
    return [{"chunk_id": cid, "grade": 2 if rank == 0 else 1} for rank, _, cid in sorted(rows)]


TYPES = ("A", "B", "C", "D", "E", "F")
RISK_SECTIONS = frozenset({"I.1A", "II.1A", "I.3"})
MIN_PASSAGE_CHARS = 150
_WE = re.compile(r"\b(we|our|us)\b")
_ID_LIKE = re.compile(r"\b(?:\d[A-Z]\d{3}[a-z]?|[A-Z]{1,5}\d{2,}[A-Z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z]+)\b")
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
        return bool(_WE.search(text.lower())) and not _names_filer(text, aliases.get(int(row.cik), []))
    if kind == "B":
        return bool(_ID_LIKE.search(text))
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


def sample_passages(chunks, seed: int, per_type: int, aliases: Mapping[int, list[str]]) -> dict[str, list[dict]]:
    """A fixed-seed draw of in-scope passages per type (A-F), each chunk used at most once across types."""
    import random

    scope = in_scope_accessions(chunks)
    rows = sorted((r for r in chunks.itertuples(index=False) if r.accession_no in scope), key=lambda r: r.chunk_id)
    used: set[str] = set()
    out: dict[str, list[dict]] = {}
    for kind in TYPES:
        rng = random.Random(f"{seed}:{kind}")
        free = [r for r in rows if r.chunk_id not in used and len(r.text) >= MIN_PASSAGE_CHARS]
        if kind == "D":
            picked = _pairs(free, rng, per_type)
        else:
            cands = [r for r in free if _single_type(kind, r, aliases)]
            picked = [[r] for r in rng.sample(cands, min(per_type, len(cands)))]
        out[kind] = [_item(kind, group) for group in picked]
        used.update(r.chunk_id for group in picked for r in group)
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
    anchors = {g["anchor"] for g in current}
    return bool(anchors) and all(any(g["chunk_id"] in top for g in current if g["anchor"] == a) for a in anchors)


def ndcg_at(ranked: Iterable[str], gold: Iterable[Mapping], k: int) -> float:
    """Graded nDCG@k with gains 2^grade - 1."""
    grades = {g["chunk_id"]: g["grade"] for g in gold}
    dcg = sum((2 ** grades.get(cid, 0) - 1) / math.log2(i + 2) for i, cid in enumerate(list(ranked)[:k]))
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


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registered_bench(path: Path, expected_sha256: str) -> dict:
    """A frozen bench file, refused if it differs from the registered one."""
    if file_sha256(path) != expected_sha256:
        raise SystemExit(f"refusing: {path.name} differs from the registered bench file (docs/specs/m2-search.md section 7)")
    return json.loads(path.read_text(encoding="utf-8"))
