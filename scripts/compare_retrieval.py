"""Snapshot and diff what retrieval returns for the gold benchmark questions.

Two modes (both read-only against the graph; no LLM calls, no API spend):

    python scripts/compare_retrieval.py snapshot --out data/backups/retrieval_v1.json
    python scripts/compare_retrieval.py diff data/backups/retrieval_v1.json data/backups/retrieval_v2.json

`snapshot` runs ``hybrid_retrieve`` for every question in the packaged benchmark against
whatever Neo4j the environment points at (NEO4J_URI / NEO4J_PASSWORD / NEO4J_DATABASE) using
whichever ``semigraph`` is on PYTHONPATH — so the same script measures the v1 code on the v1
graph (PYTHONPATH = a ``git archive v1-final`` checkout) and the v2 code on the v2 graph.
`diff` lists, per question and per context layer, what was gained and lost. It is the
"every answer-relevant fact that changed" report of the M1 gate.

Two more modes for the M2a I0 measurements (docs/specs/m2a-answer-integrity.md section 8; read-only, no model call):

    python scripts/compare_retrieval.py dump --out data/backups/m2a_i0_contexts.json
    python scripts/compare_retrieval.py measure data/backups/m2a_i0_contexts.json \
        data/processed/eval_deployed.v2e.jsonl --out artifacts/m2a_i0_measurements.json

`dump` replays the deployed path's retrieval and context rendering (``hybrid_retrieve`` with the
``answer_stream`` defaults, then ``build_blocks``) for every benchmark question and keeps, per question, the
context, its valid ids, the edges in retrieval order with their chunk ids and quote lengths, and the excerpt
ranks. `measure` pairs each replayed context with the stored deployed answer and adds the read-only graph
lookups the spec names. Pin the graph and the embedder on the command line (NEO4J_URI, EMBEDDING_BACKEND=onnx).

One mode for decision 006's M2 reopen trigger (docs/decisions/006-m2-after-m2a.md; read-only, no model call):

    python scripts/compare_retrieval.py tripwire artifacts/m2_tripwire_gold.json --out artifacts/m2_tripwire_snapshot.json

It refuses a gold file that differs from the registered one, and exits non-zero if a gold chunk is missing from the graph.
"""

import argparse
import json
import math
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LAYERS = ("edges", "metrics", "risks", "temporal", "chunks")
_METRIC_HEADER_RE = re.compile(r"^(.+): fiscal year ended \S+$")
# The relation types RELATIONSHIPS prints (retriever.COMPANY_EDGES_QUERY): named, so an all-caps word inside a company name
# is never read as the relation.
_EDGE_LINE_RE = re.compile(r"^- (.+?) (SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH) (.+?) \(status=")


def summarize(retrieval: dict) -> dict:
    """A JSON-safe, order-independent fingerprint of one retrieval result."""
    edges = sorted({f"{e['source']} -{e['relation']}-> {e['target']}" for e in retrieval.get("edges", [])})
    metrics = sorted({f"{m['company']}|{m['metric']}|{m.get('period_end')}|{m.get('value')}|{m.get('unit', 'USD')}"
                      for m in retrieval.get("metrics", [])})
    risks = sorted({r["chunk_id"] for r in retrieval.get("risks", [])})
    temporal = sorted({f"{t['company']}|{t['lineage']}" for t in retrieval.get("temporal", [])})
    chunks = sorted({c["chunk_id"] for c in retrieval.get("chunks", [])})
    return {"anchors": sorted(retrieval.get("anchors", {})), "anchor_defaulted": retrieval.get("anchor_defaulted"),
            "edges": edges, "metrics": metrics, "risks": risks, "temporal": temporal, "chunks": chunks}


def diff_summaries(before: dict, after: dict) -> dict:
    """Per layer: what only ``before`` has (lost) and what only ``after`` has (gained)."""
    out: dict = {}
    for layer in LAYERS:
        b, a = set(before.get(layer, [])), set(after.get(layer, []))
        out[layer] = {"before": len(b), "after": len(a), "lost": sorted(b - a), "gained": sorted(a - b)}
    out["anchors_changed"] = before.get("anchors") != after.get("anchors")
    return out


def diff_snapshots(before: dict, after: dict) -> dict:
    """Question id -> per-layer diff (only questions present in both snapshots)."""
    return {qid: diff_summaries(before[qid], after[qid]) for qid in before if qid in after}


def render_diff(diff: dict, limit: int = 4) -> str:
    lines = []
    for qid, layers in diff.items():
        changed = [layer for layer in LAYERS if layers[layer]["lost"] or layers[layer]["gained"]]
        head = f"{qid}: " + ("identical" if not changed and not layers["anchors_changed"] else "CHANGED " + ",".join(changed))
        lines.append(head)
        for layer in changed:
            d = layers[layer]
            lines.append(f"    {layer}: {d['before']} -> {d['after']}  (-{len(d['lost'])} / +{len(d['gained'])})")
            for tag, items in (("lost", d["lost"]), ("gained", d["gained"])):
                for item in items[:limit]:
                    lines.append(f"        {tag}: {item[:150]}")
                if len(items) > limit:
                    lines.append(f"        {tag}: ... and {len(items) - limit} more")
    return "\n".join(lines)


# --- M2a I0: pure measurement helpers (tests/test_compare_retrieval.py) ---------------------------------------------------

def edge_rows(retrieval: dict) -> list[dict]:
    """Every edge in retrieval order (1-based rank) with its full chunk-id list and the stored quote's length."""
    return [{"rank": i, "edge": f"{e['source']} -{e['relation']}-> {e['target']}", "relation": e["relation"],
             "status": e.get("status"), "chunk_ids": list(e.get("chunk_ids") or []),
             "quote_chars": len(e.get("quote") or ""), "rule_id": e.get("rule_id")}
            for i, e in enumerate(retrieval.get("edges", []), start=1)]


def chunk_ranks(retrieval: dict) -> list[dict]:
    """The excerpts in the order the prompt shows them, with the vector score when the row has one."""
    return [{"rank": i, "chunk_id": c["chunk_id"], "score": c.get("score")}
            for i, c in enumerate(retrieval.get("chunks", []), start=1)]


def replay_check(stored_row: Mapping, replay_valid_ids: Iterable[str]) -> dict:
    """The spec's acceptance rule: every non-hallucinated id the stored answer cites is in the replayed context."""
    missing = sorted(set(stored_row.get("cited") or []) - set(stored_row.get("hallucinated") or [])
                     - set(replay_valid_ids))
    return {"accepted": not missing, "missing": missing, "error": bool(stored_row.get("error"))}


def distribution(values: Iterable[float]) -> dict:
    """n, min, nearest-rank p50 and p90, max."""
    xs = sorted(values)
    if not xs:
        return {"n": 0}
    rank = lambda q: xs[max(1, math.ceil(q * len(xs))) - 1]  # noqa: E731
    return {"n": len(xs), "min": xs[0], "p50": rank(0.5), "p90": rank(0.9), "max": xs[-1]}


def block_sizes(blocks) -> dict:
    """Characters per rendered context block (a ``ContextBlocks``), plus their total."""
    sizes = {name: len(text) for name, text in blocks._asdict().items()}
    return {**sizes, "total": sum(sizes.values())}


def metric_chars_by_company(metrics_block: str) -> dict[str, int]:
    """Characters of the METRICS block under each company's ``<company>: fiscal year ended`` headers (line lengths
    without newlines, so the parts do not add up to :func:`block_sizes`)."""
    out: dict[str, int] = {}
    company = None
    for line in metrics_block.splitlines():
        header = _METRIC_HEADER_RE.match(line)
        if header:
            company = header.group(1)
        if company is not None:
            out[company] = out.get(company, 0) + len(line)
    return out


def edge_chars_by_anchor(edges_block: str, anchors: Iterable[str]) -> dict[str, int]:
    """Characters of the RELATIONSHIPS lines that have each anchor as an endpoint (a line counts once per anchor; line
    lengths without newlines)."""
    names = list(anchors)
    out = dict.fromkeys(names, 0)
    for line in edges_block.splitlines():
        m = _EDGE_LINE_RE.match(line)
        if m:
            for name in names:
                if name in (m.group(1), m.group(3)):
                    out[name] += len(line)
    return out


def ids_shown(context: str, ids: Iterable[str]) -> dict[str, bool]:
    """Whether each id appears as a bracketed citation in the context."""
    return {i: f"[{i}]" in context for i in ids}


def take_snapshot(out: Path) -> None:
    from semigraph.artifacts import load_benchmark
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.retriever import hybrid_retrieve

    settings = get_settings()
    driver = client.get_driver(settings)
    embedder = Embedder()
    try:
        result = {q["id"]: summarize(hybrid_retrieve(q["q"], driver, embedder)) for q in load_benchmark()}
    finally:
        driver.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"wrote {len(result)} question fingerprints -> {out}")

# --- M2a I0: the read-only graph side --------------------------------------------------------------------------------------

NVIDIA_CIK = 1045810
# Gold passages of the two retrieval-scope defects (docs/v2/research/m2-triage-and-next-milestones-2026-11-11.md lines 18
# and 20; X4's are in its benchmark judge_notes). D1's passage is named there by its ordinal only, so it is looked up.
X4_GOLD = ("0001045810-26-000021:I.1:0323", "0001045810-26-000021:I.1A:0355", "0001045810-26-000021:II.7:0368")
D1_ORDINAL = ":0320"
SNAPSHOT_QUERY = "MATCH (s:Snapshot) RETURN s.id AS id, toString(s.as_of) AS as_of ORDER BY s.created_at DESC LIMIT 1"
SPAN_FACTS_QUERY = """UNWIND $ids AS cid MATCH (e:EvidenceSpan {chunk_id: cid})
OPTIONAL MATCH (e)-[:MENTIONS]->(c:Company)
RETURN e.chunk_id AS chunk_id, e.is_current AS is_current, e.retrievable AS retrievable, e.status AS status,
       toString(e.filing_date) AS filing_date, e.form AS form, collect(c.name) AS mentions"""
D1_PASSAGE_QUERY = """MATCH (e:EvidenceSpan) WHERE e.filer_cik = $cik AND e.is_current AND e.chunk_id ENDS WITH $ordinal
RETURN e.chunk_id AS chunk_id"""
CANDIDATE_MENTIONS_QUERY = """MATCH (node:EvidenceSpan)
SEARCH node IN (VECTOR INDEX evidence_embedding FOR $vec WHERE node.retrievable = true LIMIT $candidates) SCORE AS score
OPTIONAL MATCH (node)-[:MENTIONS]->(c:Company)
RETURN node.chunk_id AS chunk_id, score, collect(c.cik) AS mention_ciks ORDER BY score DESC"""
STATUS_QUERY = """MATCH (:Company)-[r:SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH|AFFECTED_BY]->()
RETURN type(r) AS relation, r.status AS status, count(*) AS n ORDER BY relation, status"""
SVC_QUERY = """MATCH (n) UNWIND labels(n) AS label WITH label WHERE label STARTS WITH 'Svc'
RETURN label, count(*) AS n ORDER BY label"""


def _session_rows(driver, query: str, **params) -> list[dict]:
    """The script's own queries, in a READ session: the server refuses a write there."""
    with driver.session(default_access_mode="READ") as session:
        return [dict(r) for r in session.run(query, **params)]


def local_graph(uri: str) -> str:
    """The graph URI, only if it is a local instance with no credentials in it (I0 measures G; the URI is recorded)."""
    parts = urlsplit(uri)
    if parts.hostname not in LOCAL_HOSTS or parts.username or parts.password:
        raise SystemExit("refusing: the I0 measurements read a local graph only (NEO4J_URI=bolt://localhost:<port>)")
    return uri


def backups_path(out: Path) -> Path:
    """The dump holds full rendered contexts, so it is written only under the git-ignored data/backups/."""
    resolved = out.resolve()
    if not resolved.is_relative_to((ROOT / "data" / "backups").resolve()):
        raise SystemExit("refusing: the context dump is written only under data/backups/ (git-ignored)")
    return resolved


def take_dump(out: Path) -> None:
    """Replay the deployed path's retrieval and context for every benchmark question (no model call)."""
    from semigraph.artifacts import load_benchmark
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.answerer import build_blocks
    from semigraph.retrieval.retriever import hybrid_retrieve

    out = backups_path(out)
    settings = get_settings()
    graph = local_graph(settings.neo4j_uri)
    driver = client.get_driver(settings)
    embedder = Embedder()
    questions: dict[str, dict] = {}
    quotes: dict[str, str] = {}
    try:
        snapshot = _session_rows(driver, SNAPSHOT_QUERY)
        for q in load_benchmark():
            r = hybrid_retrieve(q["q"], driver, embedder)       # answer_stream's defaults: k_chunks=8, hops=2
            blocks, context, valid_ids = build_blocks(r)
            for e in r["edges"]:
                if e.get("quote"):
                    quotes.setdefault(f"{e['source']} -{e['relation']}-> {e['target']}", e["quote"])
            questions[q["id"]] = {
                "q": q["q"], "anchors": r["anchors"], "anchor_defaulted": r["anchor_defaulted"],
                "context": context, "valid_ids": sorted(valid_ids), "edges": edge_rows(r), "chunks": chunk_ranks(r),
                "block_sizes": block_sizes(blocks), "metric_chars": metric_chars_by_company(blocks.metrics_block),
                "edge_chars": edge_chars_by_anchor(blocks.edges_block, r["anchors"])}
    finally:
        driver.close()
    dump = {"graph": graph, "embedding_backend": embedder.backend, "snapshot": snapshot,
            "questions": questions, "quotes": quotes}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dump, indent=1), encoding="utf-8")
    print(f"wrote {len(questions)} replayed contexts ({len(quotes)} distinct edge quotes) -> {out}")


def _replay_summary(dump: dict, runs: list[dict]) -> dict:
    """The replay guard over every pair; an errored stored row (no answer) is listed, never counted as accepted."""
    checks = {row["id"]: replay_check(row, dump["questions"][row["id"]]["valid_ids"])
              for row in runs if row["id"] in dump["questions"]}
    rejected = {qid: c["missing"] for qid, c in checks.items() if not c["accepted"]}
    errored = sorted(qid for qid, c in checks.items() if c["error"])
    stored_ids = {row["id"] for row in runs}
    return {"pairs": len(checks), "accepted": sum(c["accepted"] and not c["error"] for c in checks.values()),
            "rejected": rejected, "errored_rows": errored,
            "unpaired": {"stored_only": sorted(stored_ids - set(dump["questions"])),
                         "replay_only": sorted(set(dump["questions"]) - stored_ids)}}


def _sizes(dump: dict) -> dict:
    qs = dump["questions"].values()
    per_anchor = [n for q in qs for n in q["edge_chars"].values()]
    per_company = [n for q in qs for n in q["metric_chars"].values()]
    return {"context_total_chars": distribution(q["block_sizes"]["total"] for q in qs),
            "edges_block_chars": distribution(q["block_sizes"]["edges_block"] for q in qs),
            "external_block_chars": distribution(q["block_sizes"]["external_block"] for q in qs),
            "metrics_block_chars": distribution(q["block_sizes"]["metrics_block"] for q in qs),
            "edge_chars_per_anchor": distribution(per_anchor),
            "metric_chars_per_company": distribution(per_company),
            "anchor_count": distribution(len(q["anchors"]) for q in qs),
            "edge_lines_per_question": distribution(sum(e["relation"] != "AFFECTED_BY" for e in q["edges"]) for q in qs),
            "rule_lines_per_question": distribution(sum(e["relation"] == "AFFECTED_BY" for e in q["edges"]) for q in qs)}


def _scope_probe(driver, embedder, question: str, gold: list[str], anchor_cik: int) -> dict:
    """X4(b) / D1: where the gold passages rank among the excerpt candidates and whether the MENTIONS filter keeps them."""
    from semigraph.retrieval.retriever import EXCERPT_CANDIDATES

    rows = _session_rows(driver, CANDIDATE_MENTIONS_QUERY, vec=embedder.encode_query(question),
                         candidates=EXCERPT_CANDIDATES)
    ranks = {r["chunk_id"]: (i, r) for i, r in enumerate(rows, start=1)}
    return {"candidates": len(rows), "kept_by_mentions_filter": sum(anchor_cik in r["mention_ciks"] for r in rows),
            "gold": {g: ({"candidate_rank": ranks[g][0], "kept": anchor_cik in ranks[g][1]["mention_ciks"]}
                         if g in ranks else {"candidate_rank": None, "kept": False}) for g in gold},
            "gold_facts": _session_rows(driver, SPAN_FACTS_QUERY, ids=gold)}


def _benchmark_facts(dump: dict) -> dict:
    from semigraph.artifacts import load_benchmark

    bench = {q["id"]: q for q in load_benchmark()}
    yoy = {qid: ids_shown(dump["questions"][qid]["context"], q["metric_ids"])
           for qid, q in bench.items() if q.get("subtype") == "yoy" and qid in dump["questions"]}
    return {"yoy_metric_ids_shown": yoy}


def _edge_currency(driver, dump: dict, qids: Iterable[str]) -> dict:
    """D4 and D1: currency, date and mentions of every chunk id on the named questions' edges (first id = the one the
    stored quote belongs to)."""
    out = {}
    for qid in qids:
        edges = [e for e in dump["questions"][qid]["edges"] if e["relation"] != "AFFECTED_BY"]
        ids = sorted({i for e in edges for i in e["chunk_ids"]})
        facts = {f["chunk_id"]: f for f in _session_rows(driver, SPAN_FACTS_QUERY, ids=ids)}
        out[qid] = [{"edge": e["edge"], "status": e["status"], "quote": dump["quotes"].get(e["edge"]),
                     "ids": [{"id": i, **{k: facts.get(i, {}).get(k) for k in ("is_current", "filing_date", "form")}}
                             for i in e["chunk_ids"]]} for e in edges]
    return out


def _paid_spend(artifacts_dir: Path) -> dict:
    """The spec's rule (OD8): the remaining part of the $30 budget, from the artifacts' ``total_cost_usd``."""
    found = {}
    for f in sorted(artifacts_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("total_cost_usd"), (int, float)):
            found[f.name] = round(float(data["total_cost_usd"]), 6)
    spent = round(sum(found.values()), 6)
    return {"budget_usd": 30.0, "recorded_total_cost_usd": found, "spent_usd": spent,
            "remaining_usd": round(30.0 - spent, 6),
            "limit": "only artifacts with a top-level total_cost_usd are counted; paid steps that record cost elsewhere "
                     "(extraction, judging, agent evals) are not, so this remaining figure is an upper bound"}


def measure(dump_path: Path, runs_path: Path, out: Path) -> None:
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client

    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    runs = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    quote_lengths = [len(q) for q in dump["quotes"].values()]
    settings = get_settings()
    local_graph(settings.neo4j_uri)
    driver = client.get_driver(settings)
    embedder = Embedder()
    try:
        d1_gold = [r["chunk_id"] for r in _session_rows(driver, D1_PASSAGE_QUERY, cik=NVIDIA_CIK, ordinal=D1_ORDINAL)]
        report = {
            "graph": {"replay": {k: dump[k] for k in ("graph", "embedding_backend", "snapshot")},
                      "now": _session_rows(driver, SNAPSHOT_QUERY)},
            "replay": _replay_summary(dump, runs),
            "sizes": _sizes(dump),
            "quote_chars_distinct_edges": distribution(quote_lengths),
            "benchmark": _benchmark_facts(dump),
            "x4b": _scope_probe(driver, embedder, dump["questions"]["X4"]["q"], list(X4_GOLD), NVIDIA_CIK),
            "d1_scope": _scope_probe(driver, embedder, dump["questions"]["D1"]["q"], d1_gold, NVIDIA_CIK),
            "edges_with_currency": _edge_currency(driver, dump, ("D1", "D4")),
            "relation_statuses": _session_rows(driver, STATUS_QUERY),
            "svc_rows_in_graph": _session_rows(driver, SVC_QUERY),
            "paid_spend": _paid_spend(ROOT / "artifacts"),
        }
    finally:
        driver.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"wrote I0 measurements -> {out}")



# Decision 006 (docs/decisions/006-m2-after-m2a.md): the M2 reopen trigger. The gold was committed in 6c7e9d1 before any trace.
TRIPWIRE_GOLD_SHA256 = "7a9f4dfc055446195aa9a7750346ce5c3b612782da6709acd9c8626a3b2921b3"
TRIPWIRE_REOPEN_AT = 2
GOLD_MENTIONS_QUERY = """UNWIND $ids AS cid MATCH (e:EvidenceSpan {chunk_id: cid})
OPTIONAL MATCH (e)-[:MENTIONS]->(c:Company)
RETURN e.chunk_id AS chunk_id, collect(c.cik) AS mention_ciks"""


def file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def registered_gold(path: Path, expected_sha256: str = TRIPWIRE_GOLD_SHA256) -> dict:
    """The pre-registered gold, refused if it changed since it was registered (a wrong label is recorded, not edited)."""
    if file_sha256(path) != expected_sha256:
        raise SystemExit("refusing: the tripwire gold differs from the registered file (decision 006)")
    return json.loads(path.read_text(encoding="utf-8"))


def tripwire_verdict(question: Mapping, context: str, mentions: Mapping[str, list[int]]) -> dict:
    """Decision 006's rule for one question. A hit: any gold id appears in the context. A filer-own miss: no hit, and a gold
    chunk exists in the graph whose filer is an anchor and which has no MENTIONS edge to an anchor."""
    anchors = {int(a["cik"]) for a in question["anchors"]}
    gold = [g["chunk_id"] for g in question["gold"]]
    present = [cid for cid, shown in ids_shown(context, gold).items() if shown]
    missing = [cid for cid in gold if cid not in mentions]
    filer_own = [g["chunk_id"] for g in question["gold"]
                 if g["chunk_id"] in mentions and int(g["filer_cik"]) in anchors
                 and not anchors & {int(c) for c in mentions[g["chunk_id"]]}]
    return {"id": question["id"], "hit": bool(present), "present": present, "missing_from_graph": missing,
            "filer_own_unmentioned": filer_own, "filer_own_miss": not present and bool(filer_own)}


def tripwire_reopens(verdicts: Iterable[Mapping]) -> bool:
    return sum(1 for v in verdicts if v["filer_own_miss"]) >= TRIPWIRE_REOPEN_AT


def invalid_questions(verdicts: Iterable[Mapping]) -> list[str]:
    """Questions with a gold chunk the graph does not hold: a wrong label or another graph, so not a valid measurement."""
    return [v["id"] for v in verdicts if v["missing_from_graph"]]


def tripwire(gold_path: Path, out: Path) -> None:
    """Run decision 006's pool through the deployed hybrid path (context only, no model call) on a local graph."""
    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.answerer import build_blocks
    from semigraph.retrieval.retriever import hybrid_retrieve

    gold = registered_gold(gold_path)
    settings = get_settings()
    graph = local_graph(settings.neo4j_uri)
    driver = client.get_driver(settings)
    embedder = Embedder()
    verdicts = []
    try:
        snapshot = _session_rows(driver, SNAPSHOT_QUERY)
        for q in gold["questions"]:
            ids = [g["chunk_id"] for g in q["gold"]]
            mentions = {r["chunk_id"]: r["mention_ciks"] for r in _session_rows(driver, GOLD_MENTIONS_QUERY, ids=ids)}
            r = hybrid_retrieve(q["question"], driver, embedder)    # answer_stream's defaults: k_chunks=8, hops=2
            _, context, _ = build_blocks(r)
            v = tripwire_verdict(q, context, mentions)
            v["retrieval_anchors"] = r["anchors"]
            v["excerpts"] = [c["chunk_id"] for c in r["chunks"]]
            verdicts.append(v)
    finally:
        driver.close()
    report = {"decision": "docs/decisions/006-m2-after-m2a.md", "gold_sha256": TRIPWIRE_GOLD_SHA256, "graph": graph,
              "embedding_backend": embedder.backend, "snapshot": snapshot, "reopen_at": TRIPWIRE_REOPEN_AT,
              "filer_own_misses": sum(1 for v in verdicts if v["filer_own_miss"]),
              "reopens_m2": tripwire_reopens(verdicts), "invalid_questions": invalid_questions(verdicts),
              "questions": verdicts}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"{report['filer_own_misses']} filer-own misses of {len(verdicts)}; reopens M2: {report['reopens_m2']} -> {out}")
    if report["invalid_questions"]:
        raise SystemExit(f"NOT A VALID RUN: gold chunks missing from the graph for {report['invalid_questions']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("diff")
    d.add_argument("before", type=Path)
    d.add_argument("after", type=Path)
    dump = sub.add_parser("dump")
    dump.add_argument("--out", type=Path, required=True)
    m = sub.add_parser("measure")
    m.add_argument("dump", type=Path)
    m.add_argument("runs", type=Path)
    m.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("tripwire")
    t.add_argument("gold", type=Path)
    t.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        take_snapshot(args.out)
    elif args.cmd == "tripwire":
        tripwire(args.gold, args.out)
    elif args.cmd == "dump":
        take_dump(args.out)
    elif args.cmd == "measure":
        measure(args.dump, args.runs, args.out)
    else:
        before, after = (json.loads(p.read_text(encoding="utf-8")) for p in (args.before, args.after))
        print(render_diff(diff_snapshots(before, after)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
