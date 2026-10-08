"""Mock answers that the REAL verifier accepts (``retrieval.verify.verify_answer``), built from the prompt alone.

The service releases a cheap draft only when ``verify_answer`` returns no reason, and escalates otherwise, so a mock
that answers badly would measure the escalation path, not the load. This module writes drafts that pass, and (on
request) drafts that fail in exactly one way (a well-formed citation id the prompt does not hold: ``invalid_citation``).

How a draft is made, and why each rule exists (every rule is a check of ``verify.py``):

* ids are collected ONLY from the excerpt headers (a line that is exactly ``[<chunk id>]`` or ``[<doc id>]``) and from the
  graph blocks that precede the excerpts; never from the question or the instructions (a question can contain an
  id-shaped string that is not citable), and never from inside an excerpt's text;
* each line is ``- <one sentence copied from that excerpt> [<its id>]``: every sentence is cited, and the cited id is
  in the prompt, so ``citations_retrieved`` holds;
* a copied sentence is kept only when it has no digit, no bracket and no currency or percent sign (so the mock states no
  figure of its own and ``numbers_grounded`` cannot fail), and none of the removal vocabulary (``removal_claims`` judges
  sentences that say a disclosure was removed); sentences that look like instructions are dropped too, so an excerpt
  that carries an injection never makes the mock echo it;
* no excerpt gives a usable sentence: the risk summaries of the graph block are tried (same filters), then a neutral
  template that cites an id of the prompt; with no id at all, the draft is a refusal that passes ``refusal_shaped``;
* length: the draft is padded by repeating its own lines (still copied and cited) up to the sampled length.

Pure functions over strings and a ``random.Random``; no I/O.
"""

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .idgrammar import CHUNK_ID_PATTERN, DOC_ID_PATTERN, classify_id

EXCERPT_HEADER_RE = re.compile(rf"^\[({CHUNK_ID_PATTERN}|{DOC_ID_PATTERN})\]$")
RISK_LINE_RE = re.compile(rf"^- .{{1,80}}? \(.{{1,60}}?\): (?P<summary>.+) \[(?P<id>{CHUNK_ID_PATTERN})\]$")
# Every id in a graph line (a relationship, a metric, a rule, a risk). The same grammar as retrieval/ids.py.
_GRAPH_ID_RE = re.compile(r"\[([^\[\]\s]+)\]")

SECTION_PREFIX = "=== "
DELIMITER_PREFIX = "<<<DOC-"
MIN_SENTENCE_CHARS, MAX_SENTENCE_CHARS = 40, 300
MAX_PAD_LINES = 400                  # repeats added to reach a long target (a 2,000-token answer is 80 lines)
XBRL_LINE_CHANCE, FR_LINE_CHANCE = 0.25, 0.20

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_FORBIDDEN_CHARS = re.compile(r"[0-9\[\]$€£¥%<>{}|\\]")
_FORBIDDEN_WORDS = re.compile(
    r"remov|drop|delet|eliminat|withdr|discontinu|no longer|not found|ceas|abandon|omit|revok|terminat|"
    r"ignore|disregard|instruction|system prompt|system message|assistant|canary|jailbreak", re.I)

REFUSAL_TEXT = "The context does not contain information that answers this question."
TEMPLATE_PASSAGE = "The retrieved filing passage addresses this question [{cid}]."
TEMPLATE_XBRL = "- The reported figure for this period is shown in the cited XBRL fact [{cid}]."
TEMPLATE_FR = "- A Federal Register rule that the knowledge graph links to this company by keyword match is cited here [{cid}]."
TEMPLATE_FABRICATED = "- A further passage of the same filing makes the same point [{cid}]."


@dataclass(frozen=True)
class Excerpt:
    cid: str
    text: str


@dataclass(frozen=True)
class PromptFacts:
    excerpts: tuple[Excerpt, ...]
    graph_ids: tuple[str, ...]                    # ids printed in the graph blocks, in order, once each
    risk_lines: tuple[tuple[str, str], ...]       # (chunk id, summary) of the DISCLOSED RISKS block

    @property
    def all_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([e.cid for e in self.excerpts] + list(self.graph_ids)))


@dataclass(frozen=True)
class Draft:
    text: str
    cited: tuple[str, ...]
    source: str                                   # excerpts | risk_lines | template | refusal
    fabricated_id: str | None = None


def parse_prompt(prompt: str) -> PromptFacts:
    """The citable ids and the excerpt texts of a rendered answer prompt (current, legacy and workspace layouts)."""
    start = prompt.find("\n" + SECTION_PREFIX)
    region = prompt[start + 1:] if start >= 0 else prompt
    texts: dict[str, list[str]] = {}
    graph_ids: dict[str, None] = {}
    risks: list[tuple[str, str]] = []
    current: str | None = None
    for line in region.split("\n"):
        stripped = line.strip()
        header = EXCERPT_HEADER_RE.match(stripped)
        if header:
            current = header.group(1)
            texts.setdefault(current, [])
        elif line.startswith(SECTION_PREFIX) or stripped.startswith(DELIMITER_PREFIX):
            current = None
        elif current is not None:
            texts[current].append(line)
        elif not line.startswith("QUESTION:"):
            _collect_graph_line(line, graph_ids, risks)
    excerpts = tuple(Excerpt(cid, "\n".join(lines)) for cid, lines in texts.items())
    return PromptFacts(excerpts, tuple(graph_ids), tuple(risks))


def _collect_graph_line(line: str, graph_ids: dict[str, None], risks: list[tuple[str, str]]) -> None:
    for candidate in _GRAPH_ID_RE.findall(line):
        if classify_id(candidate) is not None:
            graph_ids.setdefault(candidate)
    risk = RISK_LINE_RE.match(line.strip())
    if risk:
        risks.append((risk.group("id"), risk.group("summary")))


def usable_sentences(text: str) -> list[str]:
    """The sentences of ``text`` the mock may copy: whole-text whitespace normalised, within the length bounds, starting
    with a capital and ending with a full stop, and free of every forbidden character and word (see the module doc)."""
    seen: dict[str, None] = {}
    for sentence in _SENTENCE_SPLIT.split(" ".join(text.split())):
        sentence = sentence.strip()
        if (MIN_SENTENCE_CHARS <= len(sentence) <= MAX_SENTENCE_CHARS and sentence[0].isupper() and sentence.endswith(".")
                and not _FORBIDDEN_CHARS.search(sentence) and not _FORBIDDEN_WORDS.search(sentence)):
            seen.setdefault(sentence)
    return list(seen)


def fabricate_id(rng: random.Random, taken: str) -> str:
    """A well-formed chunk id that ``taken`` (the whole prompt) does not contain: the verifier reads it as a fabricated
    source (``invalid_citation``), never as a pseudo-citation."""
    while True:
        cid = (f"{rng.randrange(10**9, 10**10):010d}-{rng.randrange(20, 27)}-{rng.randrange(10**5, 10**6):06d}"
               f":I.1A:{rng.randrange(9000, 10000):04d}")
        if cid not in taken:
            return cid


def _round_robin(pools: Sequence[tuple[str, list[str]]], target_chars: int) -> list[tuple[str, str]]:
    """(id, sentence) pairs: the best sentence of each source first, then the second of each, until ``target_chars``."""
    picked: list[tuple[str, str]] = []
    total = 0
    deepest = max(len(sentences) for _, sentences in pools)
    for depth in range(deepest):
        for cid, sentences in pools:
            if depth < len(sentences):
                picked.append((cid, sentences[depth]))
                total += len(sentences[depth]) + len(cid) + 5
                if total >= target_chars:
                    return picked
    return picked


def _pad(lines: list[str], target_chars: int) -> list[str]:
    total = sum(len(line) + 1 for line in lines)
    base = len(lines)
    padded = list(lines)
    while total < target_chars and len(padded) - base < MAX_PAD_LINES:
        line = lines[(len(padded) - base) % base]
        padded.append(line)
        total += len(line) + 1
    return padded


def _extra_lines(facts: PromptFacts, rng: random.Random) -> list[str]:
    """A neutral line citing an XBRL fact and one citing a Federal Register rule, now and then, so the other id forms
    reach the service too."""
    lines = []
    for kind, template, chance in (("xbrl", TEMPLATE_XBRL, XBRL_LINE_CHANCE), ("fr", TEMPLATE_FR, FR_LINE_CHANCE)):
        ids = [i for i in facts.graph_ids if classify_id(i) == kind]
        if ids and rng.random() < chance:
            lines.append(template.format(cid=rng.choice(ids)))
    return lines


def _pools(facts: PromptFacts) -> tuple[list[tuple[str, list[str]]], str]:
    excerpts = [(e.cid, usable_sentences(e.text)) for e in facts.excerpts]
    if any(sentences for _, sentences in excerpts):
        return [(cid, s) for cid, s in excerpts if s], "excerpts"
    risks = [(cid, usable_sentences(summary if summary.endswith(".") else summary + "."))
             for cid, summary in facts.risk_lines]
    return [(cid, s) for cid, s in risks if s], "risk_lines"


def compose(prompt: str, rng: random.Random, *, target_chars: int, fabricate: bool = False) -> Draft:
    """A draft of about ``target_chars`` characters for ``prompt`` (see the module doc). ``fabricate`` adds one line that
    cites an id the prompt does not hold."""
    facts = parse_prompt(prompt)
    pools, source = _pools(facts)
    target_chars = max(int(target_chars), 1)
    if pools:
        lines = [f"- {sentence} [{cid}]" for cid, sentence in _round_robin(pools, target_chars)]
        lines += _extra_lines(facts, rng)
        lines = _pad(lines, target_chars)
    else:
        usable = [i for i in facts.all_ids if classify_id(i) in ("chunk", "doc")] or list(facts.all_ids)
        source, lines = ("template", ["- " + TEMPLATE_PASSAGE.format(cid=usable[0])]) if usable else ("refusal", [REFUSAL_TEXT])
    fake = fabricate_id(rng, prompt) if fabricate else None
    if fake:
        lines.append(TEMPLATE_FABRICATED.format(cid=fake))
    text = "\n".join(lines)
    return Draft(text, tuple(dict.fromkeys(_cited_ids(text))), source, fake)


def _cited_ids(text: str) -> list[str]:
    return [m for m in _GRAPH_ID_RE.findall(text) if classify_id(m) is not None]
