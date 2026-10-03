"""Deterministic plumbing for an LLM council (the llm-council skill's steps 3-4), so no response is re-typed.

  python council_tools.py anon  <dir>   advisor_<name>.md  ->  review_input.md (Responses A-E, shuffled) + mapping.json
  python council_tools.py chair <dir>   advisor_*.md + mapping.json + review_*.md -> chairman_input.md (de-anonymized)
  python council_tools.py check <dir>   asserts every expected file exists and is non-trivial

The 5 advisors: contrarian, first_principles, expansionist, outsider, executor. Reviewers write review_<name>.md."""
import json
import random
import re
import sys
from pathlib import Path

ADVISORS = ["contrarian", "first_principles", "expansionist", "outsider", "executor"]
LETTERS = ["A", "B", "C", "D", "E"]
TITLES = {"contrarian": "The Contrarian", "first_principles": "The First Principles Thinker",
          "expansionist": "The Expansionist", "outsider": "The Outsider", "executor": "The Executor"}


def read(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise SystemExit(f"{path.name} looks empty ({len(text)} chars)")
    return text


_NAMES = r"(?:The )?(?:Contrarian|First Principles Thinker|First Principles|Expansionist|Outsider|Executor)"


def scrub(text: str) -> str:
    """Remove author labels so reviewers judge content, not identity: drop a title line that is only an advisor name,
    drop the name from a 'Name: title' line, and drop any line that names a file or says where it was written."""
    out = []
    for line in text.splitlines():
        if re.fullmatch(rf"\s*\**{_NAMES}\**\s*", line, flags=re.IGNORECASE):
            continue
        if re.search(r"advisor_|written to|council\d", line, flags=re.IGNORECASE):
            continue
        out.append(re.sub(rf"^\*\*{_NAMES}:\s*", "**", line, flags=re.IGNORECASE))
    return "\n".join(out).strip()


def anon(d: Path) -> None:
    order = ADVISORS[:]
    random.SystemRandom().shuffle(order)
    mapping = dict(zip(LETTERS, order))
    parts = [f"**Response {letter}:**\n\n{scrub(read(d / f'advisor_{name}.md'))}\n" for letter, name in mapping.items()]
    (d / "review_input.md").write_text("\n---\n\n".join(parts), encoding="utf-8")
    (d / "mapping.json").write_text(json.dumps(mapping, indent=1), encoding="utf-8")
    print("wrote review_input.md (shuffled) and mapping.json")


def chair(d: Path) -> None:
    mapping = json.loads((d / "mapping.json").read_text(encoding="utf-8"))
    advisors = [f"**{TITLES[name]}:**\n\n{read(d / f'advisor_{name}.md')}\n" for name in ADVISORS]
    reviews = []
    for name in ADVISORS:
        reviews.append(f"**Review by the {TITLES[name]}'s reviewer** (letters refer to the anonymized order: "
                       f"{', '.join(f'{k}={TITLES[v]}' for k, v in mapping.items())}):\n\n{read(d / f'review_{name}.md')}\n")
    (d / "chairman_input.md").write_text("# ADVISOR RESPONSES\n\n" + "\n".join(advisors) + "\n# PEER REVIEWS\n\n" + "\n".join(reviews),
                                          encoding="utf-8")
    print("wrote chairman_input.md")


def check(d: Path) -> None:
    missing = [f for f in [*(f"advisor_{n}.md" for n in ADVISORS)] if not (d / f).exists()]
    print("missing:", missing or "none")
    for f in sorted(d.glob("*.md")):
        print(f.name, len(f.read_text(encoding="utf-8")))


if __name__ == "__main__":
    cmd, folder = sys.argv[1], Path(sys.argv[2])
    {"anon": anon, "chair": chair, "check": check}[cmd](folder)
