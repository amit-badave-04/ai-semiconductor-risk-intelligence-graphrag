"""The async twin of the upload-workspace answer stream (M5a I2, docs/v2/M5A_BUILD_PLAN.md section 4).

``workspace.stream_workspace_answer`` is a sync generator: a served workspace ask holds one worker thread from the
first graph read to the last token. :func:`astream_workspace_answer` yields EXACTLY the same events (same dicts, same
key order, same values) as an async generator, so a stream that waits for the model holds no thread. Every pure helper
(the delimiter, the prompt, the suspicion heuristic, the link stripper, the id hash) is imported from ``workspace`` and
the writer tail is :func:`answerer_async.astream_answer_for_prompt`; only the blocking hops differ:

* the question is embedded ONCE, on a worker thread under ``limiters.embed`` (the sync path embeds it twice: once for
  the SEC retrieval, once for the workspace search), and that vector goes to both retrievals as ``query_vec``;
* the SEC retrieval, the workspace search and the stale-citation read run on worker threads under ``limiters.db``, in
  the sync order (SEC, then workspace, then, once the answer is done, the stale-citation read);
* every one of those hops, the embedding included, goes through ``answerer_async._hop``, which checkpoints once the
  thread has returned (see the cancellation paragraph below); the link stripper is the writer's ``postprocess`` hook
  and ``answerer_async`` runs it the same way, on a worker thread under ``limiters.db``.

The pure functions that stay on the loop were measured on 2026-10-03 (p95 over 60 repetitions, development machine;
inputs: eight SEC chunks of 9,000 characters, document chunks of 1,300 characters, a 2,400-token answer of 9,600
characters):

=============================================  ===========  ============
function                                       6 doc chunks 120 doc chunks
=============================================  ===========  ============
``build_workspace_prompt``                     0.13 ms      0.18 ms
``make_delimiter``                             0.01 ms      0.05 ms
``looks_suspicious`` (156k characters at 120)  0.49 ms      9.8 ms
=============================================  ===========  ============

and ``strip_links_images`` on a 2,400-token answer: 0.5 to 0.9 ms. The three functions of the table are under the 10 ms
bar, so they run inline; the stripper is offloaded with the writer's other checks (above). The 120-chunk column is the
cap of one document version, not what an ask retrieves (the route retrieves 6). A worker thread would not shorten any of
it: ``looks_suspicious`` is ONE ``re`` call and ``re`` holds the GIL from start to end, so on a thread it stalls the
loop just the same and only adds the hop. That is also why one input shape is a known gap that the offload does not
close: ``strip_links_images`` is quadratic on a long unbroken run of letters (10,000 characters cost ~220 ms, 20,000
~890 ms, 40,000 ~3.6 s), and because ``re`` holds the GIL for a whole match the loop lags as long as the strip takes
wherever it runs (measured on 2026-10-04 for 10,000 characters: 204 ms inline, 217 ms on a worker thread). A full ask
over such an answer under ``LoopLagMonitor`` is pinned by a strict expected-failure test (20,000 characters). The fix
is a linear-time regex in ``workspace.py``; the offload still earns its keep for everything that waits or releases the
GIL (pinned by a test with a slow stripper).

Cancellation: a hop is shielded (``abandon_on_cancel=False``) and returns without a checkpoint, so a cancelled anyio
scope (what sse-starlette does on a disconnect) would be noticed only at the next real suspension, and the next
statement is a yield into a consumer that may never suspend, or the start of a writer whose first model call is paid.
``_hop``'s checkpoint puts that suspension right after the hop: the cancellation is raised before the ``retrieval``
event is yielded, before the writer is built, and before the ``done`` event (the stale-citation read is a hop too). The
module has no cleanup of its own: the ``aclosing`` around the writer's events hands that to ``answerer_async``, whose
docstring says what a cancellation guarantees there (including what a bare native ``task.cancel()`` does not).

Security: this module never imports the agent and never touches the answer cache (an upload workspace is never cached
and never planned over), logs nothing of its own (no question, no uploaded text, no answer) and names the workspace
only by its hash. Links and images never reach the client: the stripper runs before the single delta is released and
on the ``partial`` of an ``error`` event, exactly as in the sync path (``force_buffered=True``).
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from functools import partial
from typing import TYPE_CHECKING

from .answerer_async import _hop, astream_answer_for_prompt
from .retriever import company_edges_query, hybrid_retrieve
from .workspace import (
    DEFAULT_K_DOC_CHUNKS,
    _id_hash,
    _stale_citations,
    build_workspace_prompt,
    looks_suspicious,
    make_delimiter,
    strip_links_images,
    workspace_retrieve,
)

if TYPE_CHECKING:
    from ..serve.limiters import Limiters

RETRIEVAL_COUNT_LAYERS = ("edges", "metrics", "risks", "temporal", "chunks")


async def _on_db_thread(limiters: "Limiters", fn, *args, **kwargs):
    """Run a blocking graph read on a worker thread under the graph pool. Every thread hop of this module goes through
    ``answerer_async._hop`` (this one and the embedding), which raises a cancellation that landed during the hop before
    the caller yields anything built on the result."""
    return await _hop(partial(fn, *args, **kwargs), limiter=limiters.db)


def _retrieval_event(r_sec: dict, doc_chunks: list[dict]) -> dict:
    return {"event": "retrieval", "anchors": r_sec["anchors"],
            "counts": {k: len(r_sec[k]) for k in RETRIEVAL_COUNT_LAYERS},
            "anchor_defaulted": bool(r_sec.get("anchor_defaulted", False)), "doc_chunks": len(doc_chunks)}


async def _with_workspace_block(done: dict, driver, workspace_id: str, limiters: "Limiters", *, doc_chunks: int,
                                suspicious: bool) -> dict:
    """``done`` plus the ``workspace`` block, in the sync key order; ``stale_citations`` is a graph read."""
    cited = set(done.get("citations") or [])
    stale = await _on_db_thread(limiters, _stale_citations, driver, workspace_id, cited)
    return {**done, "workspace": {"id_hash": _id_hash(workspace_id), "doc_chunks": doc_chunks,
                                  "stale_citations": stale, "suspicious": suspicious}}


async def astream_workspace_answer(question: str, driver, embedder, *, limiters: "Limiters", strategy: str = "hybrid",
                                   workspace_id: str, as_of: str | None = None, timeout: float | None = None,
                                   max_tokens: int | None = None, escalation_model: str | None = None,
                                   escalation_stream=None, llm_stream=None, k_chunks: int = 8, hops: int = 2,
                                   k_doc_chunks: int = DEFAULT_K_DOC_CHUNKS,
                                   **stream_kwargs) -> AsyncIterator[dict]:
    """Async twin of :func:`workspace.stream_workspace_answer`: the same events for the same inputs, with every
    blocking hop off the event loop (see the module docstring). ``llm_stream`` and ``escalation_stream`` are
    injectable: ``callable(prompt) -> async iterable[str]``. A bad ``hops`` is the sync retrieval's ValueError on the
    first ``__anext__``, before anything is embedded. Never the agent, never the answer cache."""
    company_edges_query(hops)       # rejects a bad ``hops`` up front, as ``hybrid_retrieve`` does before any work
    vec = await _hop(embedder.encode_query, question, limiter=limiters.embed)
    r_sec = await _on_db_thread(limiters, hybrid_retrieve, question, driver, embedder, k_chunks=k_chunks, hops=hops,
                                query_vec=vec)
    r_ws = await _on_db_thread(limiters, workspace_retrieve, question, workspace_id, driver, embedder, as_of=as_of,
                               k=k_doc_chunks, query_vec=vec)
    doc_chunks = r_ws["doc_chunks"]
    delimiter = make_delimiter(doc_chunks)
    prompt, full_context, valid_ids, chunk_ids, sources = build_workspace_prompt(question, r_sec, r_ws, delimiter)
    yield _retrieval_event(r_sec, doc_chunks)
    suspicious = looks_suspicious("\n".join(c["text"] for c in doc_chunks))
    # Only forwarded when set: the model stream's own defaults (a 1200-token budget, no timeout) must not be overridden
    # by a bare None from a caller that never passed them.
    optional = {k: v for k, v in {"timeout": timeout, "max_tokens": max_tokens}.items() if v is not None}
    events = astream_answer_for_prompt(
        question, prompt, full_context, valid_ids, chunk_ids, strategy, sources=sources, llm_stream=llm_stream,
        escalation_model=escalation_model, escalation_stream=escalation_stream, postprocess=strip_links_images,
        force_buffered=True, limiters=limiters, **optional, **stream_kwargs)
    async with aclosing(events) as inner:
        async for event in inner:
            if event["event"] == "done":
                event = await _with_workspace_block(event, driver, workspace_id, limiters, doc_chunks=len(doc_chunks),
                                                    suspicious=suspicious)
            yield event
