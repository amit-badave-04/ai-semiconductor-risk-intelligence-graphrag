"""scripts/make_reading_copy.py: viewer-safe copies of a labelling packet."""

import importlib.util
import json
import sys
from pathlib import Path

from semigraph.eval.gold import normalize

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_reading_copy.py"
spec = importlib.util.spec_from_file_location("make_reading_copy", SCRIPT)
mrc = importlib.util.module_from_spec(spec)
sys.modules["make_reading_copy"] = mrc
spec.loader.exec_module(mrc)

LONG = " ".join(["Export controls may disrupt our supply chain."] * 200)      # ~9,000 chars on one line


def test_no_line_exceeds_the_width_and_the_text_is_unchanged_up_to_whitespace():
    wrapped = mrc.wrap("Intro line.\n" + LONG + "\n\nLast.")
    assert max(len(line) for line in wrapped.split("\n")) <= mrc.WIDTH
    assert normalize(wrapped) == normalize("Intro line. " + LONG + " Last.")


def test_a_quote_copied_across_a_wrapped_line_still_matches_the_source():
    wrapped = mrc.wrap(LONG)
    seam = wrapped.split("\n")[0][-30:] + "\n" + wrapped.split("\n")[1][:30]
    assert normalize(seam) in normalize(LONG)


def test_item_and_sentence_packets_render_their_data_only():
    item_packet = {"older_items": [{"item_id": "a:i1", "headline": "Privacy", "text": "We have privacy risk."}],
                   "newer_section_text": "Newer text.", "instructions": "SECRET RULES"}
    text = mrc.items_text(item_packet)
    assert "=== ITEM a:i1 ===" in text and "HEADLINE: Privacy" in text and "We have privacy risk." in text
    sentence_packet = {"items": [{"item_id": "a:i1", "headline": "", "sentences": [{"sentence_id": "a:i1#s000", "text": "One sentence here."}]}],
                       "other_section_text": "Other filing.", "instructions": "SECRET RULES"}
    assert "[a:i1#s000] One sentence here." in mrc.items_text(sentence_packet) and "HEADLINE: (none)" in mrc.items_text(sentence_packet)
    assert mrc.other_text(item_packet).strip() == "Newer text." and mrc.other_text(sentence_packet).strip() == "Other filing."
    assert "SECRET" not in mrc.items_text(item_packet) + mrc.other_text(item_packet)


def test_write_copies_creates_two_text_files_next_to_the_packet(tmp_path):
    packet = tmp_path / "P.older.packet.json"
    packet.write_text(json.dumps({"older_items": [{"item_id": "x", "headline": "h", "text": "t"}], "newer_section_text": "o"}), encoding="utf-8")
    items, other = mrc.write_copies(packet)
    assert items.name == "P.older.packet.items.txt" and other.name == "P.older.packet.other.txt" and items.exists() and other.exists()
