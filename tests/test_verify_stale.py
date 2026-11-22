"""M2a R3 (``stale_citations``) and R7 (``cited_without_text``): additive, reported-only keys of the checks object,
derived from the context string alone. Neither one changes ``failed_check_names``."""

from semigraph.retrieval.context_layout import CONTEXT_HEADERS, SUPERSEDED_MARK
from semigraph.retrieval.verify import answer_checks, failed_check_names

OLD = "0001045810-23-000017:I.1A:0001"       # a superseded filing's chunk
NEW = "0001045810-24-000029:I.1A:0001"
EXCERPTED = "0001045810-24-000029:I.1A:0002"
SOLO = "0001045810-24-000029:I.1A:0003"


def context(relationships="", temporal="(none)", excerpts="(none)", risks="(none)"):
    blocks = [relationships or "(none)", "(none)", "(none)", risks, temporal, excerpts]
    return "".join(h + b for h, b in zip(CONTEXT_HEADERS, blocks, strict=True))


def checks_for(ctx, cited, text=None):
    text = text or "Nvidia supplies things " + " ".join(f"[{c}]" for c in sorted(cited)) + "."
    return answer_checks(text, set(cited), set(cited), ctx)


def edge(ids, marked=()):
    shown = " ".join(f"[{i}]" + (f" {SUPERSEDED_MARK}" if i in marked else "") for i in ids)
    return f"- Nvidia SUPPLIED_BY TSMC (status=active) {shown}"


def test_a_cited_id_marked_superseded_on_an_edge_line_is_stale():
    ctx = context(edge([NEW, OLD], marked={OLD}), excerpts=f"[{NEW}]\ntext\n")
    checks = checks_for(ctx, {OLD, NEW})
    assert checks.stale_citations == (OLD,)
    assert checks.as_dict()["stale_citations"] == [OLD]


def test_a_superseded_id_that_is_not_cited_is_not_reported():
    ctx = context(edge([NEW, OLD], marked={OLD}))
    assert checks_for(ctx, {NEW}).stale_citations == ()


def test_an_id_listed_in_the_temporal_block_is_exempt():
    temporal = f"Older filing:\n- in 'Supply': 'we depend on TSMC' [{OLD}]"
    ctx = context(edge([OLD], marked={OLD}), temporal=temporal)
    assert checks_for(ctx, {OLD}).stale_citations == ()


def test_the_mark_is_read_only_from_relationship_lines():
    ctx = context(risks=f"- Nvidia (supply): summary [{OLD}] {SUPERSEDED_MARK}", excerpts=f"[{OLD}]\n{SUPERSEDED_MARK}\n")
    assert checks_for(ctx, {OLD}).stale_citations == ()


def test_excerpt_text_that_looks_like_a_header_does_not_end_the_excerpts():
    ctx = context(edge([NEW]), excerpts=f"[{EXCERPTED}]\nRELATIONSHIPS:\n- x [{SOLO}]\n")
    assert checks_for(ctx, {EXCERPTED, SOLO, NEW}).cited_without_text == (NEW,)


def test_an_id_seen_only_on_an_edge_line_is_cited_without_text():
    ctx = context(edge([SOLO]))
    checks = checks_for(ctx, {SOLO})
    assert checks.cited_without_text == (SOLO,)
    assert checks.as_dict()["cited_without_text"] == [SOLO]


def test_an_evidence_quote_line_gives_the_id_text():
    ctx = context(edge([SOLO]) + f"\n  evidence [{SOLO}]: TSMC makes our GPUs")
    assert checks_for(ctx, {SOLO}).cited_without_text == ()


def test_a_superseded_quote_line_also_gives_the_id_text():
    ctx = context(edge([OLD], marked={OLD}) + f"\n  evidence [{OLD}] {SUPERSEDED_MARK}: TSMC makes our GPUs")
    checks = checks_for(ctx, {OLD})
    assert checks.cited_without_text == () and checks.stale_citations == (OLD,)


def test_an_excerpt_or_a_risk_line_gives_the_id_text():
    ctx = context(edge([EXCERPTED, SOLO]), excerpts=f"[{EXCERPTED}]\nthe passage\n",
                  risks=f"- Nvidia (supply): summary [{SOLO}]")
    assert checks_for(ctx, {EXCERPTED, SOLO}).cited_without_text == ()


def test_neither_key_changes_what_fails():
    ctx = context(edge([OLD, SOLO], marked={OLD}))
    checks = checks_for(ctx, {OLD, SOLO})
    assert checks.stale_citations and checks.cited_without_text
    assert failed_check_names(checks.as_dict()) == []
    assert failed_check_names({**checks.as_dict(), "stale_citations": ["x"], "cited_without_text": ["y"]}) == []


def test_without_a_context_both_are_empty_and_a_stored_context_gives_the_same_answer():
    assert answer_checks("a [x]", set(), set()).stale_citations == ()
    ctx = context(edge([OLD, SOLO], marked={OLD}))
    first, again = checks_for(ctx, {OLD, SOLO}), checks_for(str(ctx), {OLD, SOLO})
    assert first == again


def test_the_legacy_template_is_read_too():
    legacy = "RELATIONSHIPS:\n" + edge([OLD, SOLO], marked={OLD}) + "\n\nMETRICS:\n(none)\n\nEXCERPTS:\n(none)"
    checks = checks_for(legacy, {OLD, SOLO})
    assert checks.stale_citations == (OLD,) and checks.cited_without_text == (OLD, SOLO)


def test_a_superseded_mark_on_a_quote_line_makes_a_pushed_out_edge_id_stale():
    """R3: 4 evidence ids, the edge shows 3; the superseded one survives only on its quote line."""
    shown = [NEW, EXCERPTED, SOLO]
    ctx = context(edge(shown) + f"\n  evidence [{OLD}] {SUPERSEDED_MARK}: TSMC makes our GPUs")
    checks = checks_for(ctx, {OLD})
    assert checks.stale_citations == (OLD,)
