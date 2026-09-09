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


@app.command()
def ingest(
    ticker: list[str] = typer.Option(None, "--ticker", "-t",
                                     help="Tickers to ingest (default: full 14-company universe)"),
    skip_download: bool = typer.Option(False, help="Skip EDGAR/XBRL/Federal-Register downloads; only re-parse/re-chunk"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Download filings + XBRL + export-control rules, then parse and chunk.

    Free of LLM cost; network-bound (SEC fair-access throttled)."""
    _setup_logging(verbose)
    from semigraph.ingestion import edgar, federal_register, xbrl
    from semigraph.parsing import chunker, segmentation

    settings = _settings()
    tickers = list(ticker) if ticker else None
    if not skip_download:
        typer.echo("== EDGAR filings ==")
        typer.echo(json.dumps(edgar.download_filings(settings, tickers), default=str))
        typer.echo("== XBRL company facts -> key metrics ==")
        typer.echo(json.dumps(xbrl.extract_metrics(settings, tickers), default=str))
        typer.echo("== Federal Register export-control rules ==")
        rules = federal_register.download_bis_rules(settings)
        typer.echo(f"{len(rules)} BIS rules cached")
    typer.echo("== Semantic segmentation ==")
    typer.echo(json.dumps(segmentation.segment_filings(settings, tickers), default=str))
    typer.echo("== Chunking ==")
    typer.echo(json.dumps(chunker.chunk_filings(settings, tickers), default=str))


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
