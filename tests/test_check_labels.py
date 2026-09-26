"""scripts/check_labels.py: a labeller validates only their own file against a packet."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_labels.py"
spec = importlib.util.spec_from_file_location("check_labels", SCRIPT)
cl = importlib.util.module_from_spec(spec)
sys.modules["check_labels"] = cl
spec.loader.exec_module(cl)

QUOTE = "These state laws allow for statutory fines for noncompliance."
ITEM_PACKET = {"side": "older", "older_items": [
    {"item_id": "a:i1", "headline": "Privacy fines", "text": "We are subject to privacy laws and statutory fines. " + QUOTE},
    {"item_id": "a:i2", "headline": "Stock volatility", "text": "Our stock price is volatile. Market volatility could affect an investment."}],
    "newer_section_text": "We are subject to privacy laws and statutory fines. " + QUOTE + " We depend on TSMC for capacity."}


def test_a_clean_item_file_passes(tmp_path):
    labels = [{"item_id": "a:i1", "label": "unchanged", "quote": QUOTE},
              {"item_id": "a:i2", "label": "removed", "search_terms": ["volatile", "stock price", "market volatility"]}]
    rejected, missing, accepted = cl.check(ITEM_PACKET, labels)
    assert (rejected, missing, accepted) == ([], [], 2)


def test_a_fabricated_quote_and_a_missing_item_are_reported():
    labels = [{"item_id": "a:i1", "label": "unchanged", "quote": "A sentence that exists nowhere in the other filing at all."}]
    rejected, missing, accepted = cl.check(ITEM_PACKET, labels)
    assert rejected[0][0] == "a:i1" and "quote" in rejected[0][1] and missing == ["a:i1", "a:i2"] and accepted == 0


def test_sentence_packets_are_detected_and_checked_with_the_sentence_rules():
    packet = {"side": "older", "items": [{"item_id": "a:i1", "headline": "h", "sentences": [
        {"sentence_id": "a:i1#s000", "text": "We are subject to privacy laws and statutory fines under state law."}]}],
        "other_section_text": "We are subject to privacy laws and statutory fines under state law. More text follows here."}
    ok = [{"sentence_id": "a:i1#s000", "label": "present", "quote": "We are subject to privacy laws and statutory fines under state law."}]
    assert cl.check(packet, ok) == ([], [], 1)
    bad = [{"sentence_id": "a:i1#s000", "label": "removed", "search_terms": ["privacy", "statutory fines", "state law"]}]
    rejected, _, accepted = cl.check(packet, bad)
    assert accepted == 0 and "still" in rejected[0][1]


def test_main_exit_codes(tmp_path, capsys):
    packet, labels = tmp_path / "p.json", tmp_path / "l.json"
    packet.write_text(json.dumps(ITEM_PACKET), encoding="utf-8")
    labels.write_text(json.dumps([{"item_id": "a:i1", "label": "unchanged", "quote": QUOTE}]), encoding="utf-8")
    assert cl.main(["--packet", str(packet), "--labels", str(labels)]) == 1          # a:i2 not labelled yet
    assert "NOT LABELLED: a:i2" in capsys.readouterr().out
    labels.write_text("{not json", encoding="utf-8")
    try:
        cl.main(["--packet", str(packet), "--labels", str(labels)])
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 2
