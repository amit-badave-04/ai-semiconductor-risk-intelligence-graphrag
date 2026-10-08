"""Record the contexts that ``tests/test_tools_mockllm.py`` builds its prompts from (``tests/data/mockllm_contexts.json``).

    uv run python -m tools.mockllm.record_contexts            # needs data/processed/eval_runs*.jsonl (gitignored, local only)

A recorded run row holds the question and the text of the chunks that retrieval returned for it (``chunk_texts``). This keeps,
for each of the first ``--count`` distinct questions, the first ``--chunks`` chunks in retrieval order with the text cut at the
last sentence end within ``--max-chars``: real SEC filing prose, real chunk ids, real table-like chunks. The test renders
them through the real ``build_blocks`` / ``render_prompt``, so the mock sees the production prompt layout. No cherry-picking:
the order of the recorded runs decides. The filings are public SEC documents.
"""

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS = ("data/processed/eval_runs.v2-baseline.jsonl", "data/processed/eval_runs.jsonl")
DEFAULT_OUT = ROOT / "tests" / "data" / "mockllm_contexts.json"


def cut_at_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", head)]
    return head[: ends[-1]] if ends else head


def record(rows: Sequence[dict], count: int, chunks: int, max_chars: int) -> list[dict]:
    seen: set[str] = set()
    contexts = []
    for row in rows:
        texts = row.get("chunk_texts") or {}
        if row["q"] in seen or len(texts) < 2:
            continue
        seen.add(row["q"])
        contexts.append({"id": row["id"], "type": row.get("type", ""), "question": row["q"],
                         "chunks": [{"chunk_id": cid, "text": cut_at_sentence(text, max_chars)}
                                    for cid, text in list(texts.items())[:chunks]]})
        if len(contexts) == count:
            break
    return contexts


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=Path, nargs="+", default=[ROOT / r for r in DEFAULT_RUNS])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--count", type=int, default=24)
    p.add_argument("--chunks", type=int, default=5)
    p.add_argument("--max-chars", type=int, default=900)
    args = p.parse_args(argv)
    rows = [json.loads(line) for path in args.runs for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    contexts = record(rows, args.count, args.chunks, args.max_chars)
    doc = {"_about": ("Recorded retrieval contexts (question + the first chunks of the recorded run, text cut at a sentence end) "
                      "for tests/test_tools_mockllm.py. Regenerate: uv run python -m tools.mockllm.record_contexts"),
           "source": [str(Path(r).as_posix()) for r in DEFAULT_RUNS], "contexts": contexts}
    args.out.write_bytes((json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))      # LF on every platform
    print(f"wrote {len(contexts)} contexts to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
