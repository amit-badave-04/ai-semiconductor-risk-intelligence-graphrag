"""M2a R11 (counts only; the wider matching is deferred, see the spec): the stored 'examined N, matched K' counts, the cap,
and the proof that matching itself is unchanged."""

from semigraph.graph import export_rules as er


def rule(doc, kind="advanced_computing", published="2026-01-15", relevant=True):
    return {"document_number": doc, "title": "t", "publication_date": published, "kind": kind, "relevant": relevant,
            "topics": []}


def test_counts_cover_every_company_and_matched_is_pre_cap():
    rules = [rule(f"d{i:02d}", published=f"2025-{i % 12 + 1:02d}-01") for i in range(20)] + [rule("no", relevant=False)]
    rows, counts = er.build_affected_by({1: [{"chunk_id": "c", "text": "advanced computing"}]}, rules, ciks=[1, 2])
    assert len(rows) == er.MAX_AFFECTED_BY_PER_COMPANY == 15
    assert counts == {1: {"rules_examined": 20, "rules_matched": 20}, 2: {"rules_examined": 20, "rules_matched": 0}}


def test_the_rows_are_exactly_the_old_rows():
    rules = [er.normalize_rule(rule("a")), er.normalize_rule(rule("b", kind="semiconductor_equipment"))]
    exposures = {1: [{"chunk_id": "c", "text": "advanced computing and GPUs"}]}
    assert er.build_affected_by(exposures, rules)[0] == er.build_affected_by_rows(exposures, rules)
    assert [r["rule_id"] for r in er.build_affected_by_rows(exposures, rules)] == ["a"]


def test_matching_is_not_widened_equipment_text_about_euv_alone_does_not_link():
    r = er.normalize_rule(rule("eq", kind="semiconductor_equipment"))
    assert not er.rule_matches_evidence(r, "We need export licenses for EUV and DUV immersion systems.")
