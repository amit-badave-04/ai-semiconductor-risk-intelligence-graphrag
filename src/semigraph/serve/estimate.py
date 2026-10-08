"""Per-ask spend estimates for the estimate-based daily cap (M5a decision 5, docs/v2/M5A_BUILD_PLAN.md step 0-I4).

What an estimate is for: the cap check admits an ask only when the day's spend plus the ask's estimate fits under the
cap, and a lease that dies (a restart, a crash) is charged its estimate at the next boot. A settled ask is charged what
it actually cost, so the estimate is headroom for the asks in flight and a charge for dead leases, never a charge on
every ask. It is a ceiling, not a prediction: the recorded asks cost 1.5 cents on average and 7 cents at most, and the
estimate is what the dearest ask the caps allow could cost (see THE CONTEXT CEILING for what is capped and what is not).

The formula, per ask type (``hybrid``, ``vector``, ``agent``, ``workspace``), in integer micro-dollars rounded up once::

    draft    answer_model         x DRAFT_ATTEMPTS  x (prompt tokens x input price + answer tokens x output price)
    strong   escalation_model     x STREAM_ATTEMPTS x (the same prompt, the same output cap)  [with an escalation model]
    planner  agent_planner_model  x agent_max_model_calls x (planner prompt x input price + PLANNER_MAX_TOKENS x output)

where the answer tokens are ``llm_answer_max_tokens``.

With no escalation model (or one equal to the answer model) the answer model is the only call and carries
STREAM_ATTEMPTS. The prompt tokens are ``(template + question + context ceiling) / the model's characters per token``
rounded up, PER MODEL (:func:`chars_per_token`: 2.0 for Claude Sonnet 5, 2.5 for every other model, so the strong
call of a hybrid ask is 161,856 tokens and the draft 129,485 for the same 323,712 characters); the context ceiling is the
retrieval caps below. Prices are KNOWN_PRICES_PER_MTOK by model id; ``openai/mock-*`` staging models take the real
models' prices and characters per token (:func:`real_model`: :data:`MOCK_ALIASES`, then the model of their role); any
other model uses the configured list prices and is flagged.

LONG CONTEXT: no price tier is modelled, and for Claude Sonnet 5, which carries 97 to 98% of every estimate, none exists.
Anthropic's pricing page (https://platform.claude.com/docs/en/about-claude/pricing, checked 2026-10-08) says Claude 4.6 and
later models, Sonnet 5 included, have the full 1,000,000-token context window at standard pricing (a 900,000-token request
is billed at the same per-token rate as a 9,000-token one), and LiteLLM 1.100.0's cost map for claude-sonnet-5 lists no
above-200k price (its ``claude-sonnet-5`` entry has max_input_tokens 1,000,000 and no size-tiered key, read from the bundled
JSON on 2026-10-08). So the flat Sonnet price in KNOWN_PRICES_PER_MTOK is the right price for every ceiling below, the agent's
235,012-token strong call (past 200,000) included, and every Sonnet ceiling fits the window (tests/test_serve_estimate.py).
GPT-6 Luna is the exception: the note on its price in ``llm_shape`` records a long-context tier (a probe that priced a 1M-token
prompt returned twice the base input price). ``openai/gpt-6-luna`` is NOT listed in LiteLLM 1.100.0's bundled cost map (the
similarly named ``gpt-5.6-luna``, another model with a base input price of $0.20 per million, doubles its price above 272,000
tokens and has a 922,000-token window: the family's pattern, not a fact about gpt-6-luna), so its threshold and its window are
not recorded anywhere here. The largest prompt Luna is priced at (188,010 tokens, the agent's draft) is held under 200,000 by a
test as a tripwire, not as a verified threshold; Luna's share of an estimate is 2 to 3%.

THE CONTEXT CEILING: what bounds the prompt, and what is still an allowance (``(template + question + context) /
the model's characters per token``; the context ceiling is :func:`context_chars`).

- CAPPED IN THE CODE: the excerpts (retrieval returns at most K_CHUNKS of at most CHUNK_TEXT_MAX_CHARS characters; the
  agent adds up to AGENT_MAX_CHUNKS), the companies a plain ask anchors (``retriever.MAX_ANCHORS``, read from the
  retriever when an estimate is computed, so the two cannot drift: the first companies in detection order are kept and
  the rest are named in a note) and, per company, the rules, the metrics rows and the temporal block (the retriever's
  caps and the layout's cuts). One company's blocks at their caps were MEASURED by rendering a worst-case retrieval
  through the real ``build_blocks`` / ``render_prompt`` (tests/test_serve_estimate.py): 39,686 characters at the most
  (two pairs), allowed at GRAPH_CHARS_PER_ANCHOR.
- CAPPED IN THE CODE, THE AGENT TOO: its companies. The agent's tools add companies themselves, but a tool that would
  take the merged context past ``retriever.MAX_ANCHORS`` companies in all is refused before it runs a query
  (agent/merge.check_company_cap; the default company counts when none was detected), so an agent ask carries as many
  companies as a plain one and :func:`agent_company_blocks` reads that same cap. What its tools add per company is
  larger (every annual metric row, and filing pairs), priced at AGENT_GRAPH_CHARS_PER_COMPANY. Without the cap the
  estimate had to price all 13 SEC filers: $1.57 and a 368,010-token prompt; with it, $0.83 and 188,010 tokens (at the
  single 2.5 characters per token; at Sonnet's 2.0 the agent ceiling is $1.02 and 235,012 tokens on Sonnet, inside its
  1,000,000-token window at its flat price: see LONG CONTEXT above).
- CAPPED IN THE CODE, THE AGENT'S FILING PAIRS: those of one company. Each ``risk_changes`` call adds up to 2 pairs per
  company, so 4 calls could give one company 8 (66,410 characters, an agent prompt of 518,870 characters: about $0.91
  at the single 2.5 characters per token, $1.12 at Sonnet's 2.0) if the graph held 9 annual filings of it. ``agent.merge.merge_temporal`` now holds a
  company to MAX_PAIRS_HELD_PER_COMPANY (5) pairs in all, whatever the calls return and whatever the graph holds (today
  at most 5 annual filings of a company, AMD: four 10-Ks and a 10-K/A, so at most 4 consecutive comparisons), and
  AGENT_GRAPH_CHARS_PER_COMPANY covers exactly 5 (tests/test_serve_estimate.py offers 10 pairs of every company, the
  prefetch's 2 and four calls of 2, to the real merge and renders what it holds through the real layout).
- ALLOWANCES, NOT CAPS (measured, rounded up): the RELATIONSHIPS block (EDGE_LINES_ALLOWED lines: the code has no cap,
  the graph has 73 company relations), the earlier wording of a reworded item (542 characters at most in the corpus; the
  layout does not cut it), a rule title (234) and the note that names the companies a question named beyond the cap.
- ASSUMPTIONS: the characters per token of each model (below).

Other things the estimate does not count: that a provider does not bill a request that failed is an unverified
assumption (a timeout after the provider started is the doubtful case), and a price tier for Luna (see LONG CONTEXT; no
prompt it is priced at reaches 200,000 tokens). No model call on the answer path makes provider-level retries any more
(LiteLLM ``num_retries``): the draft, the strong stream (the escalation, the question routed straight to the strong model
and the sole answer model) and the planner all pass 0, and the attempts the strong stream makes itself are exactly
STREAM_ATTEMPTS.

Pure: constants and functions, no I/O at import, no litellm, no prompt files (tests pin the constants to the code they
mirror; the retriever is imported when the anchor cap is read, because importing it loads the answerer).
"""

import logging
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from ..config import Settings
from ..llm_shape import KNOWN_PRICES_PER_MTOK, MOCK_ALIASES, MOCK_MODEL_PREFIX

logger = logging.getLogger("semigraph.serve.estimate")

ASK_TYPES = ("hybrid", "vector", "agent", "workspace")
MICRO_PER_MTOK = 1_000_000      # tokens x (micro-dollars per million tokens) / 1e6 = micro-dollars

# --- tokens ----------------------------------------------------------------------------------------------------------
# Characters per token, PER MODEL: how many characters of prompt one billed input token stands for. An ASSUMPTION, never
# a bound: a prompt denser than the figure (fewer characters per token: chunk ids, tables and numbers make up a large
# share of the worst-case blocks) costs more than its estimate. What is measured is in
# artifacts/chars_per_token_check.json: the recorded runs' prompts rebuilt and divided by the prompt tokens the
# provider billed. It is a join of the runs' saved contexts and usages over the 20 benchmark questions (prompts of 11 to
# 64 thousand characters), not a sample of what visitors ask and far below the 106 to 470 thousand characters of the
# ceilings below, so no figure here is a proof:
#   anthropic/claude-sonnet-5  lowest 2.0997 over 40 exact rows (hybrid 2.0997, vector 2.8318), 7 of the 20 hybrid
#       rows below 2.5. So the old single figure (2.5) did NOT hold for it: SONNET_CHARS_PER_TOKEN is 2.0, about 5%
#       below the lowest exact row. The three APPROXIMATE deployed rows (their prompts were never saved, the baseline's
#       prompt of the same id stands in; left out of that verdict) go lower still (Sonnet 1.47 to 1.89, Luna's lowest
#       1.99): they are not evidence of what was billed, and neither are they evidence that these figures hold.
#   openai/gpt-6-luna  lowest 3.0224 over 20 rows (bake-off joins), none below 2.5: the default 2.5 is 17% below it.
#   every other model  the default 2.5, unmeasured. THE PLANNER RUNS ON LUNA AND ITS PROMPTS WERE NEVER RECORDED (the system
#       prompt, the tool schemas and the tool results are JSON, not prose): its 2.5 is Luna's prose figure carried over,
#       not a measurement. Neither were workspace prompts or the prompts of today's template and context layout.
# The PaidMeter logs ``meter_ratio`` (model, prompt characters, billed prompt tokens) for every completed call: a preview
# run measures the real prompts, planner and workspace included. A mock model (``openai/mock-*``) is the real model it
# stands for, first (:func:`real_model`), so a staging run exercises the live arithmetic.
DEFAULT_CHARS_PER_TOKEN = Decimal("2.5")
SONNET_CHARS_PER_TOKEN = Decimal("2.0")
CHARS_PER_TOKEN_BY_MODEL = {"anthropic/claude-sonnet-5": SONNET_CHARS_PER_TOKEN}
# There is no single CHARS_PER_TOKEN any more (the old alias of the default is gone: its last importers, the two scripts, now
# go through chars_per_token(model, role)). The default is not every model's figure.

# --- the prompt: templates, excerpts, graph blocks -------------------------------------------------------------------
ANSWER_TEMPLATE_CHARS = 9_200          # prompts/answer.txt is 9,108 characters
WORKSPACE_TEMPLATE_CHARS = 10_300      # prompts/answer_workspace.txt is 10,208
K_CHUNKS = 8                           # hybrid_retrieve k_chunks and vector_retrieve k; the route passes neither
# The longest excerpt in the ingested corpus is 10,217 characters (5,894 chunks, 13 filers; measured); the chunker's
# hard cap is 1,100 embedder tokens (parsing/chunker.MAX_TOKENS). A chunk's id line and the blank lines around it add
# the framing.
CHUNK_TEXT_MAX_CHARS = 12_000
EXCERPT_FRAMING_CHARS = 64
# One company's rules, metrics rows and temporal block at their caps, MEASURED through the real build_blocks (the test
# renders it): a plain ask shows 8 rules, 4 metric series of 11 rows (the latest 3 years and 4 named years, each with
# its prior) and, for a question that names fiscal years, two filing pairs sharing the 26-item, 16-passage budget:
# 39,686 characters (37,772 with one pair), allowed at 42,000. The recorded contexts are far smaller (at most 16,121
# graph characters for one company) and no recorded question names more than two companies.
GRAPH_CHARS_PER_ANCHOR = 42_000
# What every company count shares: the RELATIONSHIPS block. The code does not cap it (the traversal is two hops, every
# relation line is one edge); the graph bounds it: the loader MERGEs one edge per (source, relation, target) between
# canonical companies and the extractions hold 73 of them, so 150 lines is twice that. A line is a name, a relation, a
# name, a status and up to three ids (226 characters built from the longest names), allowed at EDGE_LINE_CHARS. The
# 6 risk lines of the plain retrieval come with it.
EDGE_LINES_ALLOWED = 150
# The note that names the companies a question named beyond the anchor cap: at most 22 names (26 detectable, 4 kept).
ANCHOR_NOTE_CHARS = 300
# What the agent's tools add per company: every annual metric row (up to 19 per series, all shown) and temporal pairs
# (up to MAX_PAIRS_PER_COMPANY per call, so one company can collect several over the tool calls). Both the company COUNT
# (agent_company_blocks) and the pairs per company are capped in the code: agent/merge.py holds a company to
# MAX_PAIRS_HELD_PER_COMPANY = 5 pairs in all, however many the calls return. Measured through the real renderer: 46,490
# characters per company at 2 pairs and 48,650 at 5 (42,177 at 3: the pairs share one line budget), allowed at 50,000.
# At 6 pairs a company is 54,570 and at 8 (what 4 calls could add without the cap) 66,410: the cap is what keeps those
# out, so a change to it must come with a new measurement here (a test pins the cap to 5 and renders the result).
AGENT_GRAPH_CHARS_PER_COMPANY = 50_000
# Uploaded-document excerpts (the workspace ask): DEFAULT_K_DOC_CHUNKS of at most uploads.units.DEFAULT_MAX_CHARS
# characters.
K_DOC_CHUNKS = 6
DOC_CHUNK_MAX_CHARS = 1_800
# What the agent's tools add to the retrieval (agent/merge.py caps), at the length of one line each.
AGENT_MAX_CHUNKS = 16
AGENT_MAX_EDGES_ADDED = 40
AGENT_MAX_RISKS = 12
AGENT_MAX_COMPUTED = 4
ACTIVE_RISKS_TOP = 6                   # the risks the plain retrieval already holds
EDGE_LINE_CHARS, RISK_LINE_CHARS, COMPUTED_LINE_CHARS = 300, 700, 400
# The planner's prompt: the system prompt and the tool schemas (1,685 + 5,967 characters measured), then the question,
# the prefetch summary (about 16,000), up to agent_max_tool_calls results (about 7,000 each) and its own earlier turns.
# The runs showed at most 2,300 input tokens per call; every planner call is priced at this ceiling.
PLANNER_FIXED_CHARS = 8_000
PLANNER_GROWTH_CHARS = 50_000
PLANNER_MAX_TOKENS = 400               # agent.planner.PLANNER_MAX_TOKENS

# --- calls that can be billed ----------------------------------------------------------------------------------------
DRAFT_ATTEMPTS = 1      # answerer._draft_kwargs forces one attempt on the cheap draft
# TextStream / AsyncTextStream default: the strong model (and a sole answer model) is retried when its first attempt
# returns no text, and that first attempt's input was billed.
STREAM_ATTEMPTS = 2

# --- staging ---------------------------------------------------------------------------------------------------------
# A mock that names a real model (llm_shape.MOCK_ALIASES, also what answerer.usage_cost prices a settled call with) is
# priced as that model; any other mock id is priced as the real model of its role, so a staging run with the live shape
# (cheap draft, strong escalation, cheap planner) exercises the live cap arithmetic.
MOCK_ROLE_MODELS = {"draft": "openai/gpt-6-luna", "strong": "anthropic/claude-sonnet-5", "planner": "openai/gpt-6-luna"}


@dataclass(frozen=True)
class Price:
    """Micro-dollars per million tokens (an integer: the price in USD times 1e6, rounded up)."""

    input: int
    output: int
    listed: bool


@dataclass(frozen=True)
class Component:
    """One model's share of an estimate: ``calls`` billed calls of ``input_tokens`` in, ``output_tokens`` out."""

    name: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    input_price: int
    output_price: int
    listed: bool

    @property
    def weighted(self) -> int:
        """Micro-dollars times 1e6: kept exact so the total rounds once."""
        return self.calls * (self.input_tokens * self.input_price + self.output_tokens * self.output_price)

    @property
    def micro(self) -> int:
        return _ceil_div(self.weighted, MICRO_PER_MTOK)


@dataclass(frozen=True)
class AskEstimate:
    ask_type: str
    prompt_tokens: int
    context_chars: int
    components: tuple[Component, ...]
    anchors_max: int = 0          # the retriever's anchor cap the estimate was computed with
    company_blocks: int = 0       # the companies whose graph blocks the ceiling carries (0 for a vector ask)

    @property
    def micro(self) -> int:
        return _ceil_div(sum(c.weighted for c in self.components), MICRO_PER_MTOK)

    @property
    def usd(self) -> float:
        return self.micro / 1_000_000

    @property
    def unpriced(self) -> tuple[str, ...]:
        """Models that had no listed price (priced at the configured list prices), each once, in component order."""
        return tuple(dict.fromkeys(c.model for c in self.components if not c.listed))

    def as_dict(self) -> dict:
        components = [{"name": c.name, "model": c.model, "calls": c.calls, "input_tokens": c.input_tokens,
                       "output_tokens": c.output_tokens, "micro": c.micro} for c in self.components]
        return {"ask_type": self.ask_type, "micro": self.micro, "usd": self.usd, "prompt_tokens": self.prompt_tokens,
                "context_chars": self.context_chars, "anchors_max": self.anchors_max,
                "company_blocks": self.company_blocks, "unpriced": list(self.unpriced), "components": components}


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def real_model(model: str, role: str | None = None) -> str:
    """The model a name stands for: a mock alias (``openai/mock-sonnet``) is the real model it names, any other mock
    (``openai/mock-anything``) is the real model of its ``role`` when the role is one of ``draft`` / ``strong`` /
    ``planner``, and every other model (a mock of no known role included) is itself. Prices and characters per token
    both go through this, so a mock can never be priced as one model and sized as another."""
    if model.startswith(MOCK_MODEL_PREFIX):
        return MOCK_ALIASES.get(model) or MOCK_ROLE_MODELS.get(role, model)
    return model


def chars_per_token(model: str, role: str | None = None) -> Decimal:
    """The characters of prompt one input token stands for on ``model`` (in ``role``, which only decides what an unaliased
    mock stands for): 2.0 for Claude Sonnet 5, 2.5 for every other model. An assumption below the lowest measurement, not
    a bound; see the comment on :data:`SONNET_CHARS_PER_TOKEN` for what was measured and what was not."""
    return CHARS_PER_TOKEN_BY_MODEL.get(real_model(model, role), DEFAULT_CHARS_PER_TOKEN)


def tokens_for_chars(chars: int, model: str, role: str | None = None) -> int:
    """The input tokens ``chars`` characters of prompt are priced at on ``model``: ``chars / chars_per_token``, rounded up.
    The estimate and the meter's per-call bounds both come through here, so they cannot drift apart."""
    return math.ceil(Decimal(chars) / chars_per_token(model, role))


def _micro_per_mtok(usd_per_mtok: float) -> int:
    price = Decimal(str(usd_per_mtok))
    if not price.is_finite() or price < 0:
        raise ValueError(f"a configured price per million tokens must be finite and not negative, got {usd_per_mtok!r}")
    return int((price * MICRO_PER_MTOK).to_integral_value(ROUND_CEILING))


def _excerpts(count: int, text_chars: int) -> int:
    return count * (text_chars + EXCERPT_FRAMING_CHARS)


def _check_ask_type(ask_type: str) -> None:
    if ask_type not in ASK_TYPES:
        raise ValueError(f"unknown ask type {ask_type!r}: expected one of {', '.join(ASK_TYPES)}")


def max_anchors() -> int:
    """The anchor cap the retrieval enforces, read from the retriever each time (imported here, not at the top of the
    module: importing the retriever loads the answerer and litellm, and this module stays importable without them)."""
    from ..retrieval.retriever import MAX_ANCHORS
    return MAX_ANCHORS


def agent_company_blocks(settings: Settings | None = None) -> int:
    """How many companies' graph blocks an agent ask can carry: the same cap as a plain ask. The agent's tools add
    companies, but ``agent.merge`` refuses any call that would take the context past ``retriever.MAX_ANCHORS`` companies
    in all (the question's own anchors count first), so the tool-call limit no longer changes what the prompt can hold.
    ``settings`` is accepted for the callers that pass it and no longer changes the result. This is the one place that
    would change if the agent ever got its own cap; a test pins it equal to ``agent.merge.company_cap()``."""
    return max_anchors()


def company_blocks(ask_type: str, settings: Settings | None = None) -> int:
    """The companies whose graph blocks the ``ask_type`` ceiling carries (``settings``: see
    :func:`agent_company_blocks`)."""
    _check_ask_type(ask_type)
    if ask_type == "vector":
        return 0
    return agent_company_blocks(settings) if ask_type == "agent" else max_anchors()


def _graph_shared_chars() -> int:
    return EDGE_LINES_ALLOWED * EDGE_LINE_CHARS + ACTIVE_RISKS_TOP * RISK_LINE_CHARS


def context_chars(ask_type: str, settings: Settings | None = None) -> int:
    """The context ceiling in characters (see the module docstring for what is capped and what is an allowance).
    ``settings`` no longer changes it: the agent's company count is the anchor cap, whatever its tool-call limit."""
    _check_ask_type(ask_type)
    chunks = _excerpts(K_CHUNKS, CHUNK_TEXT_MAX_CHARS)
    if ask_type == "vector":
        return chunks
    shared = chunks + _graph_shared_chars() + ANCHOR_NOTE_CHARS
    if ask_type == "agent":
        return (shared + _excerpts(AGENT_MAX_CHUNKS - K_CHUNKS, CHUNK_TEXT_MAX_CHARS)
                + AGENT_MAX_EDGES_ADDED * EDGE_LINE_CHARS + (AGENT_MAX_RISKS - ACTIVE_RISKS_TOP) * RISK_LINE_CHARS
                + AGENT_MAX_COMPUTED * COMPUTED_LINE_CHARS
                + agent_company_blocks(settings) * AGENT_GRAPH_CHARS_PER_COMPANY)
    sec = shared + max_anchors() * GRAPH_CHARS_PER_ANCHOR
    if ask_type == "workspace":
        return sec + _excerpts(K_DOC_CHUNKS, DOC_CHUNK_MAX_CHARS)
    return sec


def prompt_chars(ask_type: str, settings: Settings) -> int:
    """The ceiling on one answering call's prompt in characters: the template, the longest question and the context."""
    template = WORKSPACE_TEMPLATE_CHARS if ask_type == "workspace" else ANSWER_TEMPLATE_CHARS
    return template + settings.max_question_chars + context_chars(ask_type, settings)


def prompt_tokens(ask_type: str, settings: Settings, model: str, role: str | None = None) -> int:
    """The ceiling on one answering call's input tokens ON ``model`` (``role`` decides what an unaliased mock stands
    for): :func:`prompt_chars` over that model's characters per token, up. It is per model: the same prompt is 161,856
    tokens on Claude Sonnet 5 (2.0 characters per token) and 129,485 on Luna (2.5)."""
    return tokens_for_chars(prompt_chars(ask_type, settings), model, role)


def resolve_price(model: str, role: str, settings: Settings) -> Price:
    """The price of ``model`` in ``role`` (``draft`` / ``strong`` / ``planner``): the listed price, the real model a
    mock stands for (:func:`real_model`), else the configured list prices (``listed`` False). Never a network call."""
    model = real_model(model, role)
    if model in KNOWN_PRICES_PER_MTOK:
        per_in, per_out = KNOWN_PRICES_PER_MTOK[model]
        return Price(_micro_per_mtok(per_in), _micro_per_mtok(per_out), True)
    return Price(_micro_per_mtok(settings.llm_input_price_per_mtok),
                 _micro_per_mtok(settings.llm_output_price_per_mtok), False)


def _component(name: str, model: str, role: str, calls: int, chars_in: int, tokens_out: int,
               settings: Settings) -> Component:
    """``calls`` billed calls on ``model`` of a prompt of ``chars_in`` characters (tokenised by THAT model's ratio)."""
    price = resolve_price(model, role, settings)
    return Component(name, model, calls, tokens_for_chars(chars_in, model, role), tokens_out, price.input, price.output,
                     price.listed)


def _answer_components(chars_in: int, tokens_out: int, settings: Settings) -> list[Component]:
    answer, escalation = settings.answer_model, settings.escalation_model
    if escalation and escalation != answer:      # an escalation equal to the answer model is one model in both roles
        return [_component("draft", answer, "draft", DRAFT_ATTEMPTS, chars_in, tokens_out, settings),
                _component("strong", escalation, "strong", STREAM_ATTEMPTS, chars_in, tokens_out, settings)]
    return [_component("answer", answer, "strong", STREAM_ATTEMPTS, chars_in, tokens_out, settings)]


def _planner_component(settings: Settings) -> Component:
    chars_in = PLANNER_FIXED_CHARS + settings.max_question_chars + PLANNER_GROWTH_CHARS
    return _component("planner", settings.agent_planner_model, "planner", max(0, int(settings.agent_max_model_calls)),
                      chars_in, PLANNER_MAX_TOKENS, settings)


def estimate(ask_type: str, settings: Settings) -> AskEstimate:
    """The ceiling on what one ask of ``ask_type`` can cost on the configured models, with its components. Each
    component tokenises the same prompt with ITS model's characters per token, so ``prompt_tokens`` of the result is the
    largest input any answering call is priced at (the planner's own calls are in their component).

    Raises ``ValueError`` for an unknown ask type, a configured price that is negative or not finite, and an estimate
    that is not positive (zero would let the cap check admit asks without headroom and charge a dead lease nothing)."""
    _check_ask_type(ask_type)
    components = _answer_components(prompt_chars(ask_type, settings), max(0, int(settings.llm_answer_max_tokens)),
                                    settings)
    tokens_in = max(c.input_tokens for c in components)
    if ask_type == "agent":
        components.append(_planner_component(settings))
    result = AskEstimate(ask_type, tokens_in, context_chars(ask_type, settings), tuple(components),
                         anchors_max=max_anchors(), company_blocks=company_blocks(ask_type, settings))
    if result.micro < 1:
        raise ValueError(f"the {ask_type} estimate is not positive: check the model prices and the token budget")
    return result


def estimate_micro(ask_type: str, settings: Settings) -> int:
    return estimate(ask_type, settings).micro


def estimate_usd(ask_type: str, settings: Settings) -> float:
    return estimate(ask_type, settings).usd


def _log_line(e: AskEstimate) -> str:
    parts = " + ".join(f"{c.name}[{c.model} x{c.calls}: in {c.input_tokens} out {c.output_tokens} = {c.micro}]"
                       for c in e.components)
    unpriced = list(e.unpriced) if e.unpriced else "none"
    return (f"ask_type={e.ask_type} usd={e.usd:.6f} micro={e.micro} prompt_tokens={e.prompt_tokens} "
            f"context_chars={e.context_chars} anchors_max={e.anchors_max} company_blocks={e.company_blocks} "
            f"parts={parts} unpriced={unpriced}")


def boot_estimates(settings: Settings) -> dict[str, dict]:
    """Every ask type's estimate as plain data, one INFO line each (the number, its components, the anchor cap and the
    company count the ceiling carries), and one WARNING for each model priced at the configured list prices because it
    has no listed price."""
    estimates = {ask_type: estimate(ask_type, settings) for ask_type in ASK_TYPES}
    for e in estimates.values():
        logger.info("%s", _log_line(e))
    unpriced = dict.fromkeys(model for e in estimates.values() for model in e.unpriced)
    for model in unpriced:
        logger.warning("no listed price for %s: its estimates use the configured list prices", model)
    return {ask_type: e.as_dict() for ask_type, e in estimates.items()}
