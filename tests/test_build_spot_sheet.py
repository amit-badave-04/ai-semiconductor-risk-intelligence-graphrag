"""scripts/build_spot_sheet.py: the seeded stratified draw and the agreement scoring of the owner spot check."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_spot_sheet.py"
spec = importlib.util.spec_from_file_location("build_spot_sheet", SCRIPT)
bss = importlib.util.module_from_spec(spec)
sys.modules["build_spot_sheet"] = bss
spec.loader.exec_module(bss)


def pool(n, pair_ids=("P1", "P2", "P3")):
    return [{"sentence_id": f"{pair_ids[i % len(pair_ids)]}:s{i:03d}", "pair_id": pair_ids[i % len(pair_ids)]} for i in range(n)]


def test_the_draw_is_deterministic_and_takes_the_wanted_number_per_stratum_round_robin_across_pairs():
    cands = {"gold_removed": pool(30), "algo_fp": pool(30), "gold_present": pool(30)}
    a, b = bss.select(cands), bss.select(cands)
    assert a == b and len(a) == 36
    counts = {s: sum(1 for r in a if r["stratum"] == s) for s, _ in bss.STRATA}
    assert counts == {"gold_removed": 12, "algo_fp": 12, "gold_present": 12}
    per_pair = {p: sum(1 for r in a if r["stratum"] == "algo_fp" and r["pair_id"] == p) for p in ("P1", "P2", "P3")}
    assert set(per_pair.values()) == {4}                                          # no pair dominates a stratum


def test_a_small_stratum_is_used_up_without_error_and_the_seed_changes_the_draw():
    cands = {"gold_removed": pool(5), "algo_fp": pool(3), "gold_present": pool(40)}
    assert sum(1 for r in bss.select(cands) if r["stratum"] == "algo_fp") == 3
    assert bss.select(cands, seed=1) != bss.select(cands, seed=2)


KEY = {"R01": {"gold": "removed", "side": "older", "stratum": "gold_removed"},
       "R02": {"gold": "present", "side": "older", "stratum": "gold_present"},
       "R03": {"gold": "reworded", "side": "older", "stratum": "algo_fp"},
       "R04": {"gold": "added", "side": "newer", "stratum": "gold_removed"}}


def test_scoring_collapses_present_and_reworded_and_reports_the_disagreements_and_unsure_rows():
    labels = {"R01": "absent", "R02": "present", "R03": "absent", "R04": "unsure"}
    result = bss.score(labels, KEY)
    assert result["n"] == 3 and result["unsure"] == 1 and result["disagreements"] == ["R03"]
    assert abs(result["agreement_present_vs_absent"] - 2 / 3) < 1e-9
    assert result["by_stratum"]["algo_fp"] == {"agree": 0, "of": 1}


def test_exact_agreement_needs_the_same_three_way_label_where_the_human_only_sees_absent():
    result = bss.score({"R01": "absent", "R03": "reworded"}, KEY)
    assert result["agreement_exact"] == 1.0 and result["agreement_present_vs_absent"] == 1.0


def test_closest_returns_the_most_similar_sentences_first():
    others = ["We depend on TSMC for capacity.", "Export licence requirements for China may reduce our sales.", "Stock is volatile."]
    assert bss.closest("Export licences for China could reduce our sales.", others, k=1) == [others[1]]


def test_render_html_embeds_rows_and_texts_and_never_lets_the_data_close_the_script_tag():
    sheet = [{"row_id": "R01", "statement": "A </script><b>x</b> sentence", "other_file": "f.txt", "closest": [], "ticker": "X",
              "item_headline": "h", "direction": "d"}]
    page = bss.render_html(sheet, {"f.txt": "text with </script> inside"}, "<script>const ROWS = __ROWS__; const TEXTS = __TEXTS__;</script>")
    assert page.count("</script>") == 1                   # only the template's own closing tag
    assert '"R01"' in page and "f.txt" in page and "<\/script>" in page
