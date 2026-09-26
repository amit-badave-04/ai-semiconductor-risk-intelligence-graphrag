"""scripts/select_heldout_items.py: flagged items always, plus a seeded random handful per side."""

import importlib.util
import sys
from pathlib import Path

from semigraph.graph.alignment import AlignmentResult, AlignParams, Evidence, NewerDecision, OlderDecision

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "select_heldout_items.py"
spec = importlib.util.spec_from_file_location("select_heldout_items", SCRIPT)
sel = importlib.util.module_from_spec(spec)
sys.modules["select_heldout_items"] = sel
spec.loader.exec_module(sel)


def result(older_labels, newer_labels):
    older = tuple(OlderDecision(f"o{i:02d}", lab, None, "hash", Evidence()) for i, lab in enumerate(older_labels))
    newer = tuple(NewerDecision(f"n{i:02d}", lab, None, "hash", Evidence()) for i, lab in enumerate(newer_labels))
    return AlignmentResult(older=older, newer=newer, params=AlignParams())


def test_every_flagged_item_is_selected_with_its_reason_and_random_extras_are_capped():
    r = result(["unchanged"] * 10 + ["removed", "uncertain", "merged"], ["carried"] * 9 + ["new", "uncertain"])
    chosen = sel.choose(r, pair_id="P")
    assert {"o10", "o11", "o12"} <= set(chosen["older"]) and {"n09", "n10"} <= set(chosen["newer"])
    assert chosen["reasons"]["o10"] == "removed" and chosen["reasons"]["n09"] == "new"
    assert sum(1 for i in chosen["older"] if chosen["reasons"][i] == "random") == sel.N_RANDOM
    assert sum(1 for i in chosen["newer"] if chosen["reasons"][i] == "random") == sel.N_RANDOM


def test_the_draw_is_deterministic_per_pair_and_seed_and_short_sides_are_taken_whole():
    r = result(["unchanged"] * 30, ["carried"] * 3)
    assert sel.choose(r, pair_id="P") == sel.choose(r, pair_id="P")
    assert sel.choose(r, pair_id="P") != sel.choose(r, pair_id="Q")
    assert sel.choose(r, pair_id="P", seed=1)["older"] != sel.choose(r, pair_id="P", seed=2)["older"]
    assert sel.choose(r, pair_id="P")["newer"] == ["n00", "n01", "n02"]
