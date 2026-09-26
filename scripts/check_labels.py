"""Self-check for ONE labeller's file: run it on your own labels before you hand them in.

    python scripts/check_labels.py --packet <pair>.<side>.packet.json --labels <your labels file>.json

The packet kind (item or sentence) is detected from the packet itself. It validates only the given file with the same
machine rules the collector applies (verbatim quotes, search terms, contradictions) and prints every rejected label with
its reason plus the ids you have not labelled yet. It reads no other labeller's file and aggregates nothing.
Exit code 0 = nothing rejected and nothing missing; 1 = fix the listed labels; 2 = unreadable input.
"""

import argparse
import json
import sys
from pathlib import Path

from semigraph.eval import gold


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def check(packet: dict, labels: list) -> tuple[list[tuple[str, str]], list[str], int]:
    """(rejected, missing, accepted count) for one labeller's labels against one packet."""
    side = packet.get("side") or packet["pair"]["side"]      # item packets of the older side keep it under "pair"
    if "older_items" in packet or (packet.get("items") and "item_id" in packet["items"][0] and "sentences" not in packet["items"][0]):
        items = packet["older_items"] if side == "older" else packet["items"]
        other = packet["newer_section_text"] if side == "older" else packet["other_section_text"]
        result = gold.validate_annotation(labels, items, other, side=side)
    else:
        sentences = [s for item in packet["items"] for s in item["sentences"]]
        result = gold.validate_sentence_annotation(labels, sentences, packet["other_section_text"], side=side)
    return list(result.rejected), list(result.missing), len(result.accepted)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    args = ap.parse_args(argv)
    packet, labels = _load(args.packet), _load(args.labels)
    if not isinstance(labels, list):
        print("error: the labels file must be a JSON list of label objects", file=sys.stderr)
        return 2
    rejected, missing, accepted = check(packet, labels)
    print(f"accepted {accepted}; rejected {len(rejected)}; not yet labelled {len(missing)}")
    for label_id, reason in rejected:
        print(f"REJECTED {label_id}: {reason}")
    if missing:
        print("NOT LABELLED: " + ", ".join(missing[:60]) + (" ..." if len(missing) > 60 else ""))
    return 0 if not rejected and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
