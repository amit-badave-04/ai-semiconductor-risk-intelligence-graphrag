"""semigraph CLI — ingest | build-graph | query | eval.

Heavy imports happen inside commands so `semigraph --help` stays instant.
Paid LLM stages never run implicitly: extraction requires --extract and an
interactive confirmation after the cost estimate; `query` and `eval` state
their (cent-scale / benchmark-scale) spend before running.
"""

import json
import logging
from pathlib import Path

import typer

app = typer.Typer(
    no_args_is_help=True,
    help="AI Semiconductor Risk Intelligence GraphRAG SDK",
    pretty_exceptions_show_locals=False,
)


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _settings():
    from semigraph.config import get_settings
    return get_settings()


def _parse_as_of(value: str | None):
    """``--as-of`` as a date (None passes through); a bad value is a usage error."""
    from semigraph.ingestion.edgar import parse_as_of

    try:
        return parse_as_of(value)
    except ValueError as e:
        raise typer.BadParameter(f"--as-of must be an ISO date (YYYY-MM-DD): {e}") from e


@app.command()
def ingest(
    ticker: list[str] = typer.Option(None, "--ticker", "-t",
                                     help="Tickers to ingest (default: full 14-company universe)"),
    skip_download: bool = typer.Option(False, help="Skip EDGAR/XBRL/Federal-Register downloads; only re-parse/re-chunk"),
    as_of: str = typer.Option(None, "--as-of", help="Only filings/rules published on or before this date (YYYY-MM-DD)"),
    refresh_xbrl: bool = typer.Option(False, "--refresh-xbrl", help="Re-download Company Facts and re-curate metrics"),
    refresh_fr: bool = typer.Option(False, "--refresh-fr", help="Re-fetch every BIS rule and re-classify"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Download filings + XBRL + export-control rules, then parse and chunk.

    Incremental: only accessions the data lake does not hold are fetched, and
    chunking appends after existing rows (chunk ids never change). Free of LLM
    cost; network-bound (SEC fair-access throttled)."""
    _setup_logging(verbose)
    from semigraph.ingestion import edgar, federal_register, xbrl
    from semigraph.parsing import chunker, segmentation

    bound = _parse_as_of(as_of)
    settings = _settings()
    tickers = list(ticker) if ticker else None
    failed: list[dict] = []
    if not skip_download:
        typer.echo("== EDGAR filings ==")
        downloaded = edgar.download_filings(settings, tickers, as_of=bound)
        typer.echo(json.dumps(downloaded, default=str))
        failed = [{"ticker": t, **f} for t, s in downloaded.get("tickers", {}).items() for f in s.get("failed", [])]
        typer.echo("== XBRL company facts -> key metrics ==")
        typer.echo(json.dumps(xbrl.extract_metrics(settings, tickers, refresh=refresh_xbrl), default=str))
        typer.echo("== Federal Register export-control rules ==")
        rules = federal_register.download_bis_rules(settings, refresh=refresh_fr, as_of=bound)
        typer.echo(f"{len(rules)} BIS rules ({sum(1 for r in rules if r.get('relevant'))} relevant)")
    typer.echo("== Semantic segmentation ==")
    typer.echo(json.dumps(segmentation.segment_filings(settings, tickers), default=str))
    typer.echo("== Chunking ==")
    typer.echo(json.dumps(chunker.chunk_filings(settings, tickers), default=str))
    if failed:
        typer.echo(f"{len(failed)} filing(s) NOT ingested (re-run `ingest` to retry):", err=True)
        for f in failed:
            typer.echo(f"  {f['ticker']} {f['form']} {f['filing_date']} {f['accession_no']}: {f['error']}", err=True)
        raise typer.Exit(2)


@app.command()
def freshness(
    ticker: list[str] = typer.Option(None, "--ticker", "-t"),
    as_of: str = typer.Option(None, "--as-of", help="Bound both checks by this date (YYYY-MM-DD)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    verbose: bool = typer.Option(False, "-v"),
):
    """What is newer at the source than the data lake (free, read-only).

    Lists in-scope EDGAR filings the manifest does not hold and compares the
    stored BIS rules with a live Federal Register count."""
    _setup_logging(verbose)
    from semigraph.ingestion import freshness as fresh

    bound = _parse_as_of(as_of)
    settings = _settings()
    pending = fresh.pending_filings(settings, list(ticker) if ticker else None, as_of=bound)
    rules = fresh.federal_register_pending(settings, as_of=bound)
    if as_json:
        typer.echo(json.dumps({"pending_filings": pending, "federal_register": rules}, default=str, indent=2))
        return
    typer.echo(f"== {len(pending)} in-scope filings on EDGAR not yet ingested ==")
    for p in pending:
        typer.echo(f"  {p['ticker']:<6} {p['form']:<7} filed {p['filing_date']}  period {p.get('period_of_report')}  {p['accession_no']}")
    typer.echo("== Federal Register (BIS rules) ==")
    typer.echo(f"  stored {rules['stored_count']} (latest {rules['stored_latest_date']}) | live {rules['live_count']} | "
               f"{rules['new_since_stored']} new since the stored cache")


@app.command()
def snapshot(
    as_of: str = typer.Option(None, "--as-of", help="As-of date embedded in the id (YYYY-MM-DD)"),
):
    """Print the snapshot id of the current data lake (stamped on graph nodes, part of cache keys)."""
    from semigraph.snapshot import compute_snapshot_id

    typer.echo(compute_snapshot_id(_settings(), _parse_as_of(as_of)))


@app.command()
def extract(
    ticker: list[str] = typer.Option(None, "--ticker", "-t"),
    max_usd: float = typer.Option(None, "--max-usd", help="Refuse to start if the WORST-CASE estimate exceeds this (required unless --dry-run)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the estimate and stop; spends nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    verbose: bool = typer.Option(False, "-v"),
):
    """PAID: LLM-extract chunks not yet extracted, then resolve entities.

    Checkpointed per chunk (an interruption never re-bills). Prints the cost
    estimate first and refuses to start when the worst case exceeds --max-usd."""
    _setup_logging(verbose)
    from semigraph.extraction import extractor, resolution

    if max_usd is None and not dry_run:
        raise typer.BadParameter("--max-usd is required for a paid run (use --dry-run to only see the estimate)")
    settings = _settings()
    tickers = list(ticker) if ticker else None
    _, todo = extractor.build_extraction_plan(settings, tickers)
    est = extractor.estimate_extraction_cost(todo, settings)
    typer.echo(f"Extraction estimate: {json.dumps(est, default=str)}")
    if max_usd is not None and est["worst_case_usd"] > max_usd:
        typer.echo(f"Worst case ${est['worst_case_usd']} exceeds --max-usd ${max_usd}: nothing was spent.")
        raise typer.Exit(3)
    if dry_run:
        return
    if not yes and not typer.confirm(f"Spend up to ${est['worst_case_usd']} on {est['n_chunks']} chunks?"):
        raise typer.Abort()
    typer.echo(json.dumps(extractor.run_extraction(settings, tickers), default=str))
    typer.echo("== Entity resolution ==")
    resolution.resolve_extractions(settings, tickers)


def _graph_counts(driver) -> dict:
    """Node counts per label (service state excluded) — recorded on the Snapshot node."""
    from semigraph.graph import client

    rows = client.run_cypher(driver, """MATCH (n) WHERE NOT any(l IN labels(n) WHERE l STARTS WITH 'Svc')
        RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC""")
    return {r["label"]: r["n"] for r in rows}


def _resolve_as_of(settings, declared):
    """The snapshot's as-of date: the declared ``--as-of`` (refused when the lake holds
    anything newer, so a snapshot can never be mislabelled) or the newest date in the lake."""
    from semigraph.snapshot import newest_lake_date

    newest = newest_lake_date(settings)
    if declared is not None and newest is not None and newest > declared:
        raise typer.BadParameter(
            f"the data lake holds data ({newest}) newer than --as-of {declared}; "
            "re-ingest with --as-of or declare a later date")
    return declared or newest


def _refuse_partial_extraction(todo: dict, allow_partial: bool) -> None:
    """A graph built while in-scope chunks of current filings lack extraction records would
    falsely close risk lineages (the latest annual would look like it dropped them)."""
    missing = {t: len(rows) for t, rows in todo.items() if len(rows)}
    if not missing:
        return
    detail = ", ".join(f"{t}: {n}" for t, n in sorted(missing.items()))
    if not allow_partial:
        typer.echo(f"{sum(missing.values())} in-scope chunks of current filings have no extraction record ({detail}); "
                   "building now would falsely close risk lineages. Run `semigraph extract` first, "
                   "or pass --allow-partial to build anyway.")
        raise typer.Exit(4)
    typer.echo(f"WARNING: partial build — {sum(missing.values())} in-scope chunks are not extracted ({detail}); "
               "lineage closure may be wrong for these filers.")


@app.command("build-graph")
def build_graph(
    ticker: list[str] = typer.Option(None, "--ticker", "-t"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Empty the graph (service state kept) and rebuild the WHOLE graph from the data lake"),
    as_of: str = typer.Option(None, "--as-of", help="Declared as-of date of this snapshot; refused if the lake holds newer data"),
    allow_partial: bool = typer.Option(False, "--allow-partial", help="Build even if in-scope chunks are not extracted yet"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the --rebuild confirmation prompt"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Apply schema and load the graph from the data lake. Spends no API money.

    Loads the deterministic layer, extraction records, embeddings (local), export
    controls, runs bitemporal closure and stamps a Snapshot node. Paid extraction is
    a separate step (`semigraph extract --max-usd N`). --rebuild wipes the target
    graph first (required when the vector-index definitions changed) and always
    rebuilds the whole universe."""
    _setup_logging(verbose)
    from semigraph.embeddings import Embedder
    from semigraph.extraction import extractor
    from semigraph.graph import client, loaders, schema, temporal
    from semigraph.snapshot import compute_snapshot_id

    settings = _settings()
    if rebuild and ticker:
        raise typer.BadParameter("--rebuild always rebuilds the whole graph; it cannot be combined with --ticker")
    tickers = list(ticker) if ticker else None
    snapshot_as_of = _resolve_as_of(settings, _parse_as_of(as_of))
    _, todo = extractor.build_extraction_plan(settings, tickers)
    _refuse_partial_extraction(todo, allow_partial)
    if rebuild and not yes:
        target = f"{settings.neo4j_uri} database '{settings.neo4j_database}'"
        if not typer.confirm(f"--rebuild will DELETE every non-service node in {target}. Continue?"):
            raise typer.Abort()
    snapshot_id = compute_snapshot_id(settings, snapshot_as_of)
    typer.echo(f"snapshot {snapshot_id}")

    driver = client.get_driver(settings)
    try:
        if rebuild:
            typer.echo(f"== Reset == {json.dumps(schema.reset_graph(driver))}")
        typer.echo("== Schema ==")
        schema.apply_schema(driver)
        typer.echo("== Deterministic layer (companies, filings, sections, XBRL metrics) ==")
        loaders.load_companies(driver, settings, snapshot_id=snapshot_id)
        loaders.load_filings_and_sections(driver, settings, tickers, snapshot_id=snapshot_id)
        loaders.load_metrics(driver, settings, tickers, snapshot_id=snapshot_id)
        embedder = Embedder()
        typer.echo("== Evidence spans (local embeddings) ==")
        loaders.load_evidence_spans(driver, settings, embedder, tickers, snapshot_id=snapshot_id)
        typer.echo("== Knowledge (relations, risks, products) ==")
        loaders.load_knowledge(driver, settings, embedder, tickers, snapshot_id=snapshot_id)
        typer.echo("== Categories + export controls + AFFECTED_BY ==")
        temporal.normalize_categories(driver)          # before AFFECTED_BY matches on the category
        loaders.load_export_controls(driver, settings, snapshot_id=snapshot_id)
        typer.echo("== Bitemporal closure ==")
        temporal.apply_closure(driver, settings)
        loaders.stamp_snapshot(driver, snapshot_id, snapshot_as_of, _graph_counts(driver))
        typer.echo("build-graph complete")
    finally:
        driver.close()


@app.command()
def query(
    question: str = typer.Argument(..., help="Natural-language question"),
    strategy: str = typer.Option("hybrid", help="hybrid | vector"),
    show_context: bool = typer.Option(False, help="Print the full retrieved context"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Answer one question against the graph (one Sonnet call — cents)."""
    _setup_logging(verbose)
    from semigraph.embeddings import Embedder
    from semigraph.graph import client
    from semigraph.retrieval.answerer import answer

    settings = _settings()
    driver = client.get_driver(settings)
    try:
        result = answer(question, driver, Embedder(), strategy=strategy)
    finally:
        driver.close()
    typer.echo(result["answer"])
    if result.get("citations"):
        typer.echo("\nCitations:")
        for c in result["citations"]:
            typer.echo(f"  - {c}")
    if show_context:
        typer.echo("\n--- retrieved context ---")
        typer.echo(result["context"])


@app.command("eval")
def eval_cmd(
    limit: int = typer.Option(None, help="Only the first N benchmark questions"),
    systems: str = typer.Option("hybrid,vector", help="Comma-separated systems to run"),
    judge_model: str = typer.Option(None, help="Override the faithfulness/correctness judge (e.g. anthropic/claude-haiku-4-5 to cross-check judge self-preference)"),
    rescore: bool = typer.Option(False, help="Do not answer again: re-score the checkpointed runs (judge calls only)"),
    analyze: bool = typer.Option(False, help="After scoring, label every failing run with the failure taxonomy -> artifacts/error_analysis.json"),
    report_suffix: str = typer.Option("", help="Suffix for eval_report/eval_scores file names (keeps a re-scoring next to the primary report)"),
    runs_file: str = typer.Option("eval_runs.jsonl", help="Checkpoint log inside data/processed. Use a NEW name per data snapshot: the default holds v1's runs and would be resumed, not re-answered"),
    max_answer_usd: float = typer.Option(None, "--max-answer-usd", help="Stop before the next paid answer once the answering spend in the runs file reaches this (judge spend is not counted)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend confirmation"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Run the gold benchmark (PAID: answering + judging LLM calls)."""
    _setup_logging(verbose)
    from semigraph.artifacts import load_benchmark
    from semigraph.eval.error_analysis import analyze_failures
    from semigraph.eval.runner import AnswerBudgetExceeded, run_benchmark
    from semigraph.graph import client

    settings = _settings()
    sys_tuple = tuple(s.strip() for s in systems.split(",") if s.strip())
    n = limit or "all 20"
    what = "judge calls only (re-scoring)" if rescore else "paid answer+judge calls"
    if not yes and not typer.confirm(
        f"This runs {n} benchmark questions x {len(sys_tuple)} systems with {what}. Proceed?"
    ):
        raise typer.Abort()
    driver = embedder = None
    if not rescore:
        from semigraph.embeddings import Embedder
        driver = client.get_driver(settings)
        embedder = Embedder()
    try:
        out = run_benchmark(settings, driver, embedder, systems=sys_tuple, limit=limit,
                            judge_model=judge_model, rescore=rescore, report_suffix=report_suffix,
                            runs_file=runs_file, max_answer_usd=max_answer_usd)
    except AnswerBudgetExceeded as e:
        typer.echo(f"Stopped: {e}. Nothing further was spent; re-run with a higher cap to resume.", err=True)
        raise typer.Exit(5) from e
    finally:
        if driver is not None:
            driver.close()
    typer.echo(json.dumps(out["report"], indent=2, default=str))
    if analyze:
        bench = load_benchmark()[:limit] if limit else load_benchmark()
        analysis = analyze_failures(out["scored_df"].to_dict(orient="records"), out["runs"], bench,
                                    model=settings.critic_model)
        typer.echo(json.dumps({k: analysis[k] for k in ("n_failures", "counts", "mechanical_share")},
                              indent=2, default=str))


@app.command("bakeoff")
def bakeoff_cmd(
    models: str = typer.Option(..., help="Comma-separated LiteLLM model ids to test against the baseline"),
    baseline_runs: str = typer.Option("eval_runs.v2-baseline.jsonl", help="Baseline benchmark log in data/processed; its saved contexts are the prompts every model receives"),
    max_usd: float = typer.Option(..., "--max-usd", help="Hard cap on ALL spend: answers plus the judge (judge calls are counted at a conservative bound)"),
    votes: int = typer.Option(3, help="Correctness-judge votes per open answer (majority)"),
    report_name: str = typer.Option("bakeoff.json", help="Report file name inside artifacts/"),
    reuse_judged: bool = typer.Option(False, "--reuse-judged", help="Reuse judge results already in the report file (same vote count) instead of paying again"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the worst-case estimate and stop; spends nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Can a cheaper model answer the benchmark from the baseline's own retrieved context? (PAID)"""
    _setup_logging(verbose)
    from semigraph.artifacts import load_benchmark
    from semigraph.eval import bakeoff as bo
    from semigraph.llm import llm_json
    from semigraph.retrieval.answerer import usage_cost

    settings = _settings()
    candidates = [m.strip() for m in models.split(",") if m.strip()]
    path = settings.processed_dir / baseline_runs
    base = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    base = [r for r in base if r["system"] == "hybrid"]
    benchmark = load_benchmark()
    try:
        prices = [(m, *bo.model_prices(m)) for m in candidates]
    except Exception as e:  # noqa: BLE001
        raise typer.BadParameter(f"a model has no price in LiteLLM's cost map ({e}); refusing to spend blind") from e
    n_open = sum(1 for b in benchmark if b["type"] != "refusal" and not b.get("expect"))
    est = bo.estimate(base, prices, votes=votes, open_questions=n_open)
    typer.echo(f"Bake-off estimate: {json.dumps(est, default=str)}")
    if dry_run:
        return
    if est["total_worst_case_usd"] > max_usd:
        typer.echo(f"Worst case ${est['total_worst_case_usd']:.2f} exceeds --max-usd ${max_usd:.2f}: nothing was spent.")
        raise typer.Exit(3)
    if not yes and not typer.confirm(f"Spend up to ${est['total_worst_case_usd']:.2f} on {len(candidates)} models?"):
        raise typer.Abort()
    live = []
    for m in candidates:
        ok, detail = bo.probe_model(m)
        typer.echo(f"  probe {m}: {'ok' if ok else 'FAILED'} ({detail})")
        if ok:
            live.append(m)
    report_path = Path("artifacts") / report_name
    previous = json.loads(report_path.read_text(encoding="utf-8")) if reuse_judged and report_path.exists() else None
    answers_cap = max_usd - est["judge_worst_case_usd"] - est["baseline_rejudge_usd"]
    try:
        report = bo.run_bakeoff(base, benchmark, live, complete=bo.litellm_complete, judge=llm_json,
                                runs_path=settings.processed_dir / "bakeoff.jsonl", max_usd=answers_cap, votes=votes,
                                price=usage_cost, previous=previous)
    except bo.AnswerBudgetExceeded as e:
        typer.echo(f"Stopped: {e}. Nothing further was spent; answers so far are checkpointed.", err=True)
        raise typer.Exit(5) from e
    report["skipped_models"] = [m for m in candidates if m not in live]
    out = report_path
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    typer.echo(f"{'model':46} {'mech':>6} {'cite':>5} {'esc':>5} {'$/ans':>8} {'open':>5}  gates")
    for name, s in [("baseline (saved Sonnet run)", report["baseline"]), *report["models"].items()]:
        j = s["judged"]
        typer.echo(f"{name:46} {s['mechanical']['passed']}/{s['mechanical']['of']:<3} {s['citation_validity']:>5.2f} "
                   f"{s['escalation_rate']:>5.2f} {s['avg_cost_usd']:>8.4f} {(str(j['open_correct']) + '/' + str(j['open_of'])) if j else '-':>5}  "
                   f"{'CLEARS' if s.get('clears_all_gates') else '; '.join(s['gates_failed']) or ('below baseline' if j else '')}")
    typer.echo(f"report -> {out}")


@app.command("eval-deployed")
def eval_deployed_cmd(
    model: str = typer.Option(None, help="Cheap default model (default: ANSWER_MODEL from settings)"),
    escalation_model: str = typer.Option(None, help="Strong model for routed/rejected answers (default: ESCALATION_MODEL from settings)"),
    max_usd: float = typer.Option(..., "--max-usd", help="Hard cap on answering spend (the judge is estimated separately and printed)"),
    votes: int = typer.Option(3, help="Correctness-judge votes per open answer (majority)"),
    runs_file: str = typer.Option("eval_deployed.jsonl", help="Checkpoint log inside data/processed"),
    report_name: str = typer.Option("eval_report.deployed.json", help="Report file name inside artifacts/"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Run the 20-question benchmark through the DEPLOYED path: router, cheap draft, verifier, escalation. (PAID)"""
    _setup_logging(verbose)
    from semigraph.artifacts import load_benchmark
    from semigraph.embeddings import Embedder
    from semigraph.eval import bakeoff as bo
    from semigraph.graph import client
    from semigraph.llm import llm_json

    settings = _settings()
    model = model or settings.answer_model
    escalation_model = escalation_model or settings.escalation_model
    if not escalation_model:
        raise typer.BadParameter("no escalation model: pass --escalation-model or set ESCALATION_MODEL")
    benchmark = load_benchmark()
    n_open = sum(1 for b in benchmark if b["type"] != "refusal" and not b.get("expect"))
    judge_usd = n_open * votes * bo.JUDGE_CALL_USD
    typer.echo(f"eval-deployed: {len(benchmark)} questions, default {model}, escalation {escalation_model}; "
               f"answers capped at ${max_usd:.2f}, judge worst case ${judge_usd:.2f}")
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Abort()
    driver = client.get_driver(settings)
    try:
        rows = bo.run_deployed(benchmark, driver, Embedder(), settings.processed_dir / runs_file, model=model,
                               escalation_model=escalation_model, max_usd=max_usd)
    except bo.AnswerBudgetExceeded as e:
        typer.echo(f"Stopped: {e}. Nothing further was spent; answers so far are checkpointed.", err=True)
        raise typer.Exit(5) from e
    finally:
        driver.close()
    report = {"model": model, "escalation_model": escalation_model, "votes": votes,
              **bo.score_deployed(rows, benchmark, llm_json, votes=votes)}
    out = Path("artifacts") / report_name
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    j = report["judged"]
    typer.echo(json.dumps({k: report[k] for k in ("mechanical", "citation_validity", "routes", "escalated", "escalated_ids",
                                                   "avg_cost_usd", "total_cost_usd", "avg_latency_s", "errors")}, indent=2, default=str))
    typer.echo(f"open questions correct: {j['open_correct']}/{j['open_of']}  votes {j['votes']}")
    typer.echo(f"report -> {out}")


if __name__ == "__main__":
    app()
