"""Readable copies of a labelling packet: the file viewer truncates lines over ~2,000 characters and section texts hold
26,000-character lines, so a labeller cannot read the packet JSON itself.

    python scripts/make_reading_copy.py --packet <pair>.<side>.packet.json      # writes <packet stem>.items.txt and .other.txt

Wrapping only adds line breaks at spaces. Quote checks collapse whitespace, so a quote copied across a wrapped line still
matches the source. The copies carry exactly the packet's data (items or numbered sentences, and the other filing's full
section text) and nothing else: no instructions, no algorithm output.
"""

import argparse
import json
import sys
import textwrap
from pathlib import Path

WIDTH = 1200


def wrap(text: str, width: int = WIDTH) -> str:
    """Wrap every paragraph (line) of ``text`` at spaces to at most ``width`` characters; blank lines are kept."""
    return "\n".join("\n".join(textwrap.wrap(line, width=width, break_long_words=False, break_on_hyphens=False)) or ""
                     for line in text.split("\n"))


def items_text(packet: dict) -> str:
    """The items (or the numbered sentences of the items) of a packet as plain text."""
    items = packet["older_items"] if "older_items" in packet else packet["items"]
    blocks = []
    for item in items:
        head = f"=== ITEM {item['item_id']} ===\nHEADLINE: {item.get('headline') or '(none)'}"
        if "sentences" in item:
            body = "\n".join(f"[{s['sentence_id']}] {wrap(s['text'])}" for s in item["sentences"])
        else:
            body = "TEXT:\n" + wrap(item["text"])
        blocks.append(head + "\n" + body)
    return "\n\n".join(blocks) + "\n"


def other_text(packet: dict) -> str:
    return wrap(packet["newer_section_text"] if "newer_section_text" in packet else packet["other_section_text"]) + "\n"


def write_copies(packet_path: Path) -> tuple[Path, Path]:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    stem = packet_path.name[: -len(".json")]
    items_path, other_path = packet_path.with_name(stem + ".items.txt"), packet_path.with_name(stem + ".other.txt")
    items_path.write_text(items_text(packet), encoding="utf-8", newline="\n")
    other_path.write_text(other_text(packet), encoding="utf-8", newline="\n")
    return items_path, other_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", type=Path, action="append", required=True)
    for path in ap.parse_args(argv).packet:
        for out in write_copies(path):
            print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
