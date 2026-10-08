"""Planner mode: a request that carries ``tools`` is the agent planner (``agent/planner.LiteLLMPlanner``), not an answer.

The mock is STATELESS about it. The first turn of a planning run has no ``tool`` message: the mock returns ONE tool call,
to a tool that the request offers. Every later turn has a ``tool`` message in ``messages`` (the loop appends the tool's
result): the mock returns the single word ``DONE`` with no tool call and ``finish_reason: stop``, which the planner prompt
tells the real model to say when nothing more is needed. ``tool_choice: "none"`` is honoured the same way, and a
``tool_choice`` that names a tool forces that tool.

Which tool: ``risk_changes`` when the question talks about disclosures (risk, removed, changed, ...), ``financial_metrics``
when it talks about figures only, the first of those two that the request offers, otherwise the first tool of the request. The companies come from the
planner's own first message (``companies_in_question`` or ``defaulted_to`` of the prefetch summary it carries), so the call
names companies the graph knows and the tool actually runs; the arguments are filled from the tool's JSON schema (the
required fields, and ``companies`` whenever the tool takes it).
"""

import json
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .shapes import new_tool_call_id

FINAL_TEXT = "DONE"
DEFAULT_COMPANY = "Nvidia"
MAX_COMPANIES = 2
_MARKER = "ALREADY RETRIEVED"
_QUESTION_RE = re.compile(r"QUESTION \(untrusted user text\):\n(?P<q>.*?)\n\nALREADY RETRIEVED", re.S)
_FIGURE_WORDS = re.compile(r"revenue|income|capex|capital expenditure|r&d|research and development|\brnd\b|spend|"
                           r"margin|fiscal year ended|percent|%|grew|growth|compare", re.I)
_DISCLOSURE_WORDS = re.compile(r"risk|remov|disclos|reworded|changed|dropped|added", re.I)
_PREFERRED = {"figures": ("financial_metrics", "risk_changes"), "disclosures": ("risk_changes", "financial_metrics")}


@dataclass(frozen=True)
class OfferedTool:
    name: str
    schema: Mapping


@dataclass(frozen=True)
class PlannerReply:
    """Either one tool call (``tool_call`` = id, name, JSON arguments) or the final text."""

    tool_call: tuple[str, str, str] | None
    content: str | None


def offered_tools(body: Mapping) -> list[OfferedTool]:
    found = []
    for tool in body.get("tools") or []:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            found.append(OfferedTool(function["name"], function.get("parameters") or {}))
    return found


def message_text(message: Mapping) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, Mapping))
    return ""


def is_final_turn(body: Mapping) -> bool:
    if body.get("tool_choice") == "none":
        return True
    return any(isinstance(m, Mapping) and m.get("role") == "tool" for m in body.get("messages") or [])


def _first_user_text(body: Mapping) -> str:
    for message in body.get("messages") or []:
        if isinstance(message, Mapping) and message.get("role") == "user":
            return message_text(message)
    return ""


def _prefetch_companies(user_text: str) -> list[str]:
    marker = user_text.find(_MARKER)
    brace = user_text.find("{", marker) if marker >= 0 else -1
    if brace < 0:
        return [DEFAULT_COMPANY]
    try:
        summary, _ = json.JSONDecoder().raw_decode(user_text, brace)
    except ValueError:
        return [DEFAULT_COMPANY]
    names = list(summary.get("companies_in_question") or []) if isinstance(summary, dict) else []
    if not names and isinstance(summary, dict):
        names = [summary.get("defaulted_to")] if summary.get("defaulted_to") else list(summary.get("metrics") or {})
    clean = [n for n in names if isinstance(n, str) and n.strip()]
    return clean[:MAX_COMPANIES] or [DEFAULT_COMPANY]


def _question(user_text: str) -> str:
    match = _QUESTION_RE.search(user_text)
    return (match.group("q") if match else user_text).strip()


def choose_tool(offered: Sequence[OfferedTool], question: str, tool_choice: object) -> OfferedTool:
    by_name = {t.name: t for t in offered}
    if isinstance(tool_choice, Mapping):
        forced = (tool_choice.get("function") or {}).get("name")
        if forced in by_name:
            return by_name[forced]
    kind = "figures" if _FIGURE_WORDS.search(question) and not _DISCLOSURE_WORDS.search(question) else "disclosures"
    for name in _PREFERRED[kind]:
        if name in by_name:
            return by_name[name]
    return offered[0]


def _value(key: str, spec: Mapping, companies: list[str], question: str):
    if "enum" in spec:
        return spec["enum"][0]
    kind = spec.get("type")
    if key == "companies":
        return companies[:max(1, int(spec.get("maxItems") or len(companies)))]
    if key in ("company", "name"):
        return companies[0]
    if kind == "array":
        item = _value(key + "[]", spec.get("items") or {}, companies, question)
        return [item] * int(spec.get("minItems") or 0)
    if kind == "integer":
        return int(spec.get("default", spec.get("minimum", 0)))
    if kind == "boolean":
        return bool(spec.get("default", False))
    if key == "query":
        return question[: int(spec.get("maxLength") or 300)] or "export controls"
    if key.startswith("metric"):
        return "revenue"
    if key.endswith("period_end"):
        return "2025-01-26"
    return spec.get("default", "x" * int(spec.get("minLength") or 0))


def fill_arguments(schema: Mapping, companies: list[str], question: str) -> dict:
    """Arguments for a tool of JSON schema ``schema``: every required field, plus ``companies`` when the tool has it."""
    properties = schema.get("properties") or {}
    args = {key: _value(key, properties.get(key) or {}, companies, question) for key in schema.get("required") or []}
    if "companies" in properties and "companies" not in args:
        args["companies"] = _value("companies", properties["companies"], companies, question)
    return args


def plan_reply(body: Mapping, rng: random.Random) -> PlannerReply:
    offered = offered_tools(body)
    if not offered or is_final_turn(body):
        return PlannerReply(None, FINAL_TEXT)
    user_text = _first_user_text(body)
    question = _question(user_text)
    tool = choose_tool(offered, question, body.get("tool_choice"))
    arguments = json.dumps(fill_arguments(tool.schema, _prefetch_companies(user_text), question), separators=(",", ":"))
    return PlannerReply((new_tool_call_id(rng), tool.name, arguments), None)
