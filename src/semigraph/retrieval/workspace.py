"""Answering over an upload workspace (M4, docs/v2/M4_PLAN.md 4.2, 4.3, 14.5).

``stream_workspace_answer`` is the entry point ``serve.routes._paid_stream`` calls exactly like ``answer_stream``: it
runs the UNCHANGED SEC ``hybrid_retrieve`` alongside a filtered vector search over the caller's own workspace
(:func:`workspace_retrieve`), renders a SEPARATE template (``answer_workspace.txt`` — the rules of ``answer.txt`` plus
an uploaded-excerpts block wrapped in a per-request random delimiter, uploaded text is data, never instructions), and
hands the rendered prompt to :func:`semigraph.retrieval.answerer.stream_answer_for_prompt` — the SAME writer, router,
escalation, verifier and ``done`` grammar the SEC path uses, so those stay ONE implementation (docs/v2/M4_PLAN.md 4.3).

The answer is always buffered (``force_buffered=True``) and post-processed with :func:`strip_links_images` before a
single byte reaches the client: a link or an image driven by untrusted uploaded text must never be shown, not even for
the instant before a live stream could be edited (docs/v2/M4_PLAN.md risk 5). The terminal ``done`` event gains a
``workspace`` block (``id_hash``, ``doc_chunks``, ``stale_citations``, ``suspicious``); nothing here ever touches the
answer cache or the agent planner.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import string

from ..artifacts import read_prompt
from .answerer import build_blocks, sources_from_context, stream_answer_for_prompt
from .ids import CITE_RE, classify_id
from .retriever import hybrid_retrieve

WORKSPACE_PROMPT = read_prompt("answer_workspace")

DEFAULT_K_DOC_CHUNKS = 6
NO_DOC_CHUNKS_TEXT = "(no matching passages were retrieved from your uploaded documents)"

# A heuristic that uploaded text carries a prompt-injection attempt: it FLAGS, it never blocks (docs/v2/M4_PLAN.md 5).
# The workspace template already treats every uploaded byte as data, never as an instruction, regardless of this flag.
#
# LINEAR TIME ONLY (M4 review finding 4 / docs/v2/M4_PLAN.md 15.12): the two `...instructions?` alternatives below
# used to have TWO adjacent, unbounded whitespace quantifiers (`\s+...?\s*`), and the role-marker line used a bare
# `^\s*` under re.M — each is catastrophic-backtracking-shaped over a long whitespace run (`\s` also matches `\n`,
# so `^\s*` under MULTILINE re-attempts the same scan at every line start across a run of blank lines). Fixed here
# by folding the optional word into a single non-capturing, non-ambiguous group and by anchoring the role-marker
# line to `[ \t]*` (never crosses a newline, so it cannot cascade across a run of blank lines). Proven at 2,000,000
# characters in tests/test_retrieval_workspace.py.
_SUSPICIOUS_RE = re.compile(
    r"ignore\s+(?:(?:all|the|any)\s+)?(?:previous|prior|above)\s+instructions?|"
    r"system\s+prompt|disregard\s+(?:(?:all|the|any)\s+)?(?:previous|prior|above)|"
    r"you\s+are\s+(?:a|an|now)\b|"
    r"^[ \t]*(?:system|user|assistant)[ \t]*:|"
    r"<<<|"
    r"[A-Za-z0-9+/]{100,}={0,2}",     # a long base64-looking run
    re.I | re.M,
)

# A markdown image, a markdown link, a reference-style image/link (``![x][ref]`` / ``[x][ref]``), a reference-style
# link DEFINITION (``[ref]: url``, its own line), an HTML tag or autolink (``<img ...>``, ``<https://...>``), and a
# bare URL (``https://``, any other ``scheme://``, protocol-relative ``//host/...``, or ``www.host/...``) — each
# stopped at a bracket, a paren, an angle bracket or whitespace so a following citation (``[doc:...]``) is never
# consumed by an unbounded match.
#
# LINEAR TIME ONLY (M5a I2 decision D3; tests/test_retrieval_workspace_regex.py holds a verbatim copy of the old
# patterns and proves the output is byte for byte the same). The old patterns were quadratic: 222 ms for 10,000
# characters of letters, 886 ms for 20,000. An answer can be thousands of characters long and a prompt-injected
# document can influence it, and `re` holds the GIL for a whole match, so no worker thread frees the event loop:
# the patterns themselves had to become linear. Every quantifier is possessive where the character that follows it
# is excluded from its class (so giving characters back could never help), which removes the backtracking; what
# remained was RESTARTING, and each pattern below says how it no longer restarts over text it already scanned.
#
# The image, link and reference-definition patterns have an open-ended class that crosses the character that starts
# a match (`[^\]]` crosses `[` and `!`; `[^)]` crosses `[`; `[^\]]` crosses a newline). Every start inside one such
# span therefore ends at the same `]` or `)`, and fails for the same reason as the first start did. So each pattern
# has a second alternative that matches exactly the span the first one failed on (a `![...` whose `]` or `)` never
# arrives, or arrives in the wrong shape) and the substitution hands that span back unchanged: the next attempt
# begins after it, and no character is scanned more than about twice. The second alternative ends before any
# character that could start a match, and it only fires where the first alternative could not have matched.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*+\]\([^)]*+\)|(!\[[^\]]*+(?:\](?:\([^)]*+)?)?)")
# `[^)]` crosses `[`, but after `](` the first alternative can only fail when no `)` is left in the REST of the text,
# so every later start fails too: the second alternative hands that rest back, once.
_MD_LINK_RE = re.compile(r"\[([^\[\]]++)\]\(([^)]*+)\)|(\[[^\[\]]++\]\([^)]*+)")
# Already linear (the classes exclude `[` and `]`, so an attempt stops at the next bracket and the next attempt
# starts there); possessive only to skip the pointless give-back.
_REF_LINK_RE = re.compile(r"!?\[([^\[\]]++)\]\[([^\[\]]++)\]")
# The first alternative is the old pattern (`$` dropped: a possessive `.*` always stops where `$` holds). The second
# hands back `[`...`]` (or `[`... to the end) when there is no `]:` + target; a `[` directly followed by `]` fails
# both and costs nothing.
_REF_LINK_DEF_RE = re.compile(r"^[ \t]*+\[[^\]]++\]:[ \t]*+\S.*+|^([ \t]*+\[[^\]]++\]?)", re.M)
# Already linear (an attempt stops at the next `<` or `>`, and the next attempt starts there).
_HTML_TAG_RE = re.compile(r"<\/?[a-zA-Z][^<>]*+>")
# The scheme alternative restarted at EVERY letter of a run of `[a-z0-9+.\-]` (a long word, `a1a1...`, `http` * n):
# each start rescanned the run to the end looking for `://`, and `:` is not in the class, so every start in a run
# ends where the run ends and shares one answer. It now starts only where a run starts (the lookbehind), skips the
# digits, `+`, `.` and `-` before the run's first letter (that letter is where the old match began, so they are given
# back with `\1`) and scans the run once. A letters-only lookbehind would stay quadratic on `a1a1...`. The `//` and
# `www.` alternatives fail in a few characters or consume the rest of their token, and `www.` may still start in the
# middle of a run (`x.www.evil.test`): only the scheme alternative carries the lookbehind.
_BARE_URL_RE = re.compile(
    r"(?<![a-z0-9+.\-])([0-9+.\-]*+)[a-z][a-z0-9+.\-]*+://[^\s\[\]()<>]+|//[^\s\[\]()<>]+|\bwww\.[^\s\[\]()<>]+",
    re.I)


def looks_suspicious(text: str) -> bool:
    """True when ``text`` carries a shape often used for prompt injection (role markers, "ignore previous
    instructions", a fenced-delimiter collision attempt, a long base64 run). Advisory only."""
    return bool(_SUSPICIOUS_RE.search(text or ""))


def _link_replacement(match: re.Match) -> str:
    """``[id](url)`` keeps ``[id]`` when ``id`` is itself a well-formed citation (drops only the URL); any other
    markdown link (real link text, a real URL) is removed whole. A `[x](` with no `)` left anywhere after it is not a
    link: the second alternative of ``_MD_LINK_RE`` hands that unclosed rest back unchanged."""
    unclosed = match.group(3)
    if unclosed is not None:
        return unclosed
    label = match.group(1)
    return f"[{label}]" if CITE_RE.fullmatch(f"[{label}]") else ""


def _ref_link_replacement(match: re.Match) -> str:
    """``![label][ref]`` / ``[label][ref]``: removed whole, UNLESS either bracket is itself a well-formed citation —
    two adjacent real citations with no separator (``[doc:a][doc:b]``) look exactly like this reference-style link
    grammar, and must never be eaten by it."""
    first, second = f"[{match.group(1)}]", f"[{match.group(2)}]"
    if CITE_RE.fullmatch(first) or CITE_RE.fullmatch(second):
        return match.group(0)
    return ""


def strip_links_images(text: str) -> str:
    """Removes markdown images and links (inline and reference-style), reference-link definitions, HTML tags and
    autolinks, and bare/protocol-relative/``www.`` URLs from ``text`` — but never a ``[doc:...]``, ``[fr:...]`` or
    chunk citation (docs/v2/M4_PLAN.md risk 5): an answer driven by untrusted uploaded text must never carry a link
    or an image to the client. Linear time in ``len(text)`` (see the patterns above): it runs on the event loop in the
    async answer path, where it must never stall every other request."""
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(_ref_link_replacement, text)
    text = _MD_LINK_RE.sub(_link_replacement, text)
    text = _REF_LINK_DEF_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    return _BARE_URL_RE.sub(r"\1", text)


def workspace_retrieve(question: str, workspace_id: str, driver, embedder, *, as_of: str | None = None,
                       k: int = DEFAULT_K_DOC_CHUNKS, query_vec: list[float] | None = None) -> dict:
    """Filtered vector search over this workspace's chunks (current only, or as of ``as_of``): ``{"doc_chunks":
    [...], "stale_ids": [...]}`` — ``stale_ids`` names any retrieved chunk that is not the document's current one
    (only possible with ``as_of``; a follow-up ask's stale citations are computed from the ANSWER's cited ids
    instead, see :func:`_stale_citations`, since a plain current-only search never retrieves a superseded chunk).
    ``query_vec`` is the question's embedding when the caller already has it (the async path embeds once and passes it to
    both this and the SEC retrieval, where the sync path embeds twice)."""
    from ..uploads import repo
    from ..uploads.versions import as_of_cutoff

    vec = query_vec if query_vec is not None else embedder.encode_query(question)
    cutoff = as_of_cutoff(as_of) if as_of else None
    rows = repo.search_chunks(driver, workspace_id, vec, k, cutoff)
    doc_chunks = [{"chunk_id": r["chunk_id"], "text": r["text"], "document_id": r["document_id"],
                  "version": r["version"], "is_current": r["is_current"], "title": r["title"]} for r in rows]
    stale_ids = [c["chunk_id"] for c in doc_chunks if not c["is_current"]]
    return {"doc_chunks": doc_chunks, "stale_ids": stale_ids}


def make_delimiter(doc_chunks: list[dict]) -> str:
    """``<<<DOC-XXXXXXXXXXXX>>>`` (12 random uppercase letters), regenerated on the rare chance it collides with
    text a retrieved chunk actually contains (a document could otherwise fake a delimiter of its own)."""
    while True:
        token = "".join(secrets.choice(string.ascii_uppercase) for _ in range(12))
        delimiter = f"<<<DOC-{token}>>>"
        if not any(delimiter in c["text"] for c in doc_chunks):
            return delimiter


def _doc_excerpt_block(doc_chunks: list[dict]) -> str:
    if not doc_chunks:
        return NO_DOC_CHUNKS_TEXT
    return "\n".join(f"[{c['chunk_id']}]\n{c['text']}\n" for c in doc_chunks)


def build_workspace_prompt(question: str, r_sec: dict, r_ws: dict, delimiter: str):
    """The rendered workspace prompt, its full context (for numeric grounding), the citable ids (SEC + this
    workspace's retrieved chunks), the SEC chunk ids, and the EXPLICIT sources map (docs/v2/M4_PLAN.md 14.5):
    ``sources_from_context`` of the SEC context alone, merged with ``{doc chunk id: its own text}`` — never derived
    by re-parsing the combined context, so a chunk's grounding text is exactly what was retrieved, nothing more."""
    blocks, sec_context, valid_ids = build_blocks(r_sec)
    doc_chunks = r_ws["doc_chunks"]
    doc_block = _doc_excerpt_block(doc_chunks)
    prompt = WORKSPACE_PROMPT.format(question=question, delimiter=delimiter, doc_block=doc_block,
                                     **blocks._asdict())
    full_context = sec_context + (
        f"\n\nUPLOADED DOCUMENT EXCERPTS (user-provided, not filing evidence):\n{doc_block}\n")
    valid_ids = valid_ids | {c["chunk_id"] for c in doc_chunks}
    chunk_ids = [c["chunk_id"] for c in r_sec["chunks"]]
    sources = {**sources_from_context(sec_context), **{c["chunk_id"]: c["text"] for c in doc_chunks}}
    return prompt, full_context, valid_ids, chunk_ids, sources


def _id_hash(workspace_id: str) -> str:
    """The first 12 hex characters of ``sha256(workspace_id)`` — never the workspace id itself — for the ``done``
    event (docs/v2/M4_PLAN.md 5: never log or return a raw workspace id outside its own token-gated routes)."""
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]


def _stale_citations(driver, workspace_id: str, cited: set[str]) -> list[str]:
    """The cited ``doc:`` ids that name a chunk which is no longer current: a follow-up ask can cite an older id
    either because ``as_of`` retrieved it or because the question itself named it; either way this reads the
    workspace's CURRENT truth for exactly the cited ids, rather than trusting what retrieval happened to return."""
    from ..uploads import repo

    doc_ids = sorted(c for c in cited if classify_id(c) == "doc")
    if not doc_ids:
        return []
    texts = repo.chunk_texts(driver, workspace_id, doc_ids)
    return sorted(cid for cid in doc_ids if texts.get(cid, {}).get("is_current") is False)


def stream_workspace_answer(question: str, driver, embedder, *, strategy: str = "hybrid", workspace_id: str,
                            as_of: str | None = None, timeout: float | None = None, max_tokens: int | None = None,
                            escalation_model: str | None = None, escalation_stream=None, llm_stream=None,
                            k_chunks: int = 8, hops: int = 2, k_doc_chunks: int = DEFAULT_K_DOC_CHUNKS,
                            **stream_kwargs):
    """Streaming generator of event dicts, exactly like :func:`semigraph.retrieval.answerer.answer_stream` but over
    an upload workspace (docs/v2/M4_PLAN.md 4.2). Never the agent, never the answer cache."""
    r_sec = hybrid_retrieve(question, driver, embedder, k_chunks=k_chunks, hops=hops)
    r_ws = workspace_retrieve(question, workspace_id, driver, embedder, as_of=as_of, k=k_doc_chunks)
    doc_chunks = r_ws["doc_chunks"]
    delimiter = make_delimiter(doc_chunks)
    prompt, full_context, valid_ids, chunk_ids, sources = build_workspace_prompt(question, r_sec, r_ws, delimiter)
    yield {"event": "retrieval", "anchors": r_sec["anchors"],
          "counts": {k: len(r_sec[k]) for k in ("edges", "metrics", "risks", "temporal", "chunks")},
          "anchor_defaulted": bool(r_sec.get("anchor_defaulted", False)), "doc_chunks": len(doc_chunks)}
    suspicious = looks_suspicious("\n".join(c["text"] for c in doc_chunks))
    # Only forwarded when set: TextStream's own defaults (a 1200-token budget, no timeout) must not be overridden by
    # a bare None from a caller (e.g. a direct test call) that never passed them.
    optional = {k: v for k, v in {"timeout": timeout, "max_tokens": max_tokens}.items() if v is not None}
    for ev in stream_answer_for_prompt(question, prompt, full_context, valid_ids, chunk_ids, strategy,
                                       sources=sources, llm_stream=llm_stream, escalation_model=escalation_model,
                                       escalation_stream=escalation_stream, postprocess=strip_links_images,
                                       force_buffered=True, **optional, **stream_kwargs):
        if ev["event"] == "done":
            cited = set(ev.get("citations") or [])
            ev = {**ev, "workspace": {"id_hash": _id_hash(workspace_id), "doc_chunks": len(doc_chunks),
                                      "stale_citations": _stale_citations(driver, workspace_id, cited),
                                      "suspicious": suspicious}}
        yield ev
