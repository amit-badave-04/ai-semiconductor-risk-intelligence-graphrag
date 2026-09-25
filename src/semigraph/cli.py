"""semigraph CLI — ingest | build-graph | query | eval.

Heavy imports happen inside commands so `semigraph --help` stays instant.
Paid LLM stages never run implicitly: extraction requires --extract and an
interactive confirmation after the cost estimate; `query` and `eval` state
their (cent-scale / benchmark-scale) spend before running.
"""

import json
import logging

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


@app.command("build-graph")
def build_graph(
    ticker: list[str] = typer.Option(None, "--ticker", "-t"),
    extract: bool = typer.Option(False, help="Run PAID LLM extraction for chunks not yet extracted (asks for confirmation after a cost estimate)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the paid-extraction confirmation prompt"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Apply schema and load the graph from the data lake.

    Without --extract this spends no API money: it loads the deterministic
    layer, existing extraction jsonl, embeddings (local), export controls,
    and runs bitemporal closure."""
    _setup_logging(verbose)
    from semigraph.embeddings import Embedder
    from semigraph.extraction import extractor, resolution
    from semigraph.graph import client, loaders, schema, temporal

    settings = _settings()
    tickers = list(ticker) if ticker else None
    driver = client.get_driver(settings)
    try:
        typer.echo("== Schema ==")
        schema.apply_schema(driver)
        typer.echo("== Deterministic layer (companies, filings, sections, XBRL metrics) ==")
        loaders.load_companies(driver, settings)
        loaders.load_filings_and_sections(driver, settings, tickers)
        loaders.load_metrics(driver, settings, tickers)

        if extract:
            _, todo = extractor.build_extraction_plan(settings, tickers)
            est = extractor.estimate_extraction_cost(todo)
            typer.echo(f"PAID extraction estimate: {json.dumps(est, default=str)}")
            if not yes and not typer.confirm("Proceed with paid LLM extraction?"):
                raise typer.Abort()
            extractor.run_extraction(settings, tickers)
            resolution.resolve_extractions(settings, tickers)

        embedder = Embedder()
        typer.echo("== Evidence spans (local embeddings) ==")
        loaders.load_evidence_spans(driver, settings, embedder, tickers)
        typer.echo("== Knowledge (relations, risks, products) ==")
        loaders.load_knowledge(driver, settings, embedder, tickers)
        typer.echo("== Export controls + AFFECTED_BY ==")
        loaders.load_export_controls(driver, settings)
        typer.echo("== Bitemporal closure ==")
        temporal.apply_closure(driver, settings)
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
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the spend confirmation"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Run the gold benchmark (PAID: answering + judging LLM calls)."""
    _setup_logging(verbose)
    from semigraph.artifacts import load_benchmark
    from semigraph.eval.error_analysis import analyze_failures
    from semigraph.eval.runner import run_benchmark
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
                            judge_model=judge_model, rescore=rescore, report_suffix=report_suffix)
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


if __name__ == "__main__":
    app()
