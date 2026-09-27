# Known limits and scope notes (engineering record)

Moved verbatim from the README so that the README stays a product page; nothing was edited except relative links.

## Known limits (deliberate)

- **Corpus scope.** 14 keystone companies, 74 filings (latest annual reports plus the risk-factor
  history and latest quarterlies). Samsung does not file with the SEC and is an entity-only node;
  ASML's 2023/2024 20-Fs could not be sectioned by the parser and are skipped by design.
- **Context precision is low (~0.3).** Retrieval always returns eight excerpts even when the graph
  blocks already answer; a reranker with adaptive k is the planned fix.
- **Temporal recall is ~0.6.** The bitemporal layer wins the temporal questions, but the retriever
  still surfaces only part of the required facts; the stale-risk-leak failures follow from that.
- **Judges are LLMs.** Sonnet judging Sonnet inflates agreement; the Haiku re-scoring is the check,
  and it disagrees on 2 of 20 hybrid answers. Judge runs vary slightly between invocations.
- **A single machine per app, no HA.** Per-address windows are process-local by design; the daily
  ceiling and kill switch are in the database so they survive restarts.
- **Aborted requests still bill.** If a visitor closes the tab mid-answer, the model call completes
  and counts against the ceiling.
- **Prompt changes need a re-benchmark.** The M1b prompt changes (external-event labelling, `xbrl:` / `fr:`
  citations) are merged on `v2` but their accuracy effect is unmeasured until the source-text-grounded
  benchmark exists; do not quote an accuracy number for them.
