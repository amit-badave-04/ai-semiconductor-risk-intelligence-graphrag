"""SUPERSEDED 2026-10-03 -- do not use these numbers. This spike encoded questions with the prompt "Query: " (a trailing
space; production's QUERY_PROMPT ends "\\nQuery:") and read only the 53 examples (key `question`), not the 60
benchmark+example questions of the pre-registered check. Use scripts/verify_embedder_parity.py; the corrected result is
parity_prod_prompt.json (docs/v2/M5_DECISIONS.md section 1.4 item 12). parity_acc4.json (this script's output) is kept
only as the record of the wrong measurement.

Retrieval parity of two query encoders over the REAL corpus vectors (data/processed/embeddings/*.parquet, the 3,152
chunks the live site searches). A = the shipped q8 model (what production uses today), B = the same model with
accuracy_level=4. For every benchmark + example question: cosine(A, B), and brute-force top-k overlap against the corpus.
Read-only; run from the repo root with the dev env. argv[1] = model A, argv[2] = model B."""
import glob
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pandas as pd
from tokenizers import Tokenizer

A_PATH, B_PATH = sys.argv[1], sys.argv[2]
PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "


def collect(obj, out):
    if isinstance(obj, dict):
        q = obj.get("question")
        if isinstance(q, str) and 10 < len(q) < 600:
            out.append(q.strip())
        for v in obj.values():
            collect(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect(v, out)


questions = []
for p in ("src/semigraph/artifacts/benchmark.json", "src/semigraph/artifacts/examples.json"):
    collect(json.load(open(p, encoding="utf-8")), questions)
questions = list(dict.fromkeys(questions))

frames = [pd.read_parquet(f) for f in sorted(glob.glob("data/processed/embeddings/*.parquet"))]
corpus_df = pd.concat(frames, ignore_index=True)
corpus = np.vstack([np.asarray(e, dtype=np.float32) for e in corpus_df["embedding"]])
corpus /= np.linalg.norm(corpus, axis=1, keepdims=True)


class Enc:
    def __init__(self, path, threads=4):
        o = ort.SessionOptions()
        o.intra_op_num_threads = threads
        self.s = ort.InferenceSession(path, o, providers=["CPUExecutionProvider"])
        self.t = Tokenizer.from_file(str(Path(path).parent / "tokenizer.json"))
        self.t.enable_truncation(8192)

    def q(self, text):
        ids = np.array([self.t.encode(PROMPT + text).ids], dtype=np.int64)
        f = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
        for inp in self.s.get_inputs():
            if inp.name == "position_ids":
                f[inp.name] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
            elif inp.name.startswith("past_key_values"):
                shape = [1 if isinstance(d, str) else d for d in inp.shape]
                shape[2] = 0
                f[inp.name] = np.zeros(shape, dtype=np.float16 if "float16" in inp.type else np.float32)
        v = self.s.run(["last_hidden_state"], f)[0][0, -1, :].astype(np.float32)
        return v / np.linalg.norm(v)


A, B = Enc(A_PATH), Enc(B_PATH)
cos, ov8, ov10, top1, score_gap = [], [], [], [], []
t0 = time.perf_counter()
for q in questions:
    a, b = A.q(q), B.q(q)
    cos.append(float(a @ b))
    sa, sb = corpus @ a, corpus @ b
    ta, tb = np.argsort(-sa)[:10], np.argsort(-sb)[:10]
    ov8.append(len(set(ta[:8]) & set(tb[:8])) / 8)
    ov10.append(len(set(ta) & set(tb)) / 10)
    top1.append(bool(ta[0] == tb[0]))
    score_gap.append(float(np.max(np.abs(np.sort(sa)[::-1][:10] - np.sort(sb)[::-1][:10]))))
res = {
    "questions": len(questions), "corpus_vectors": int(corpus.shape[0]),
    "cosine_A_vs_B": {"min": round(min(cos), 5), "mean": round(statistics.mean(cos), 5), "p05": round(float(np.percentile(cos, 5)), 5)},
    "top8_overlap": {"mean": round(statistics.mean(ov8), 4), "min": min(ov8), "fraction_identical_set": round(sum(1 for x in ov8 if x == 1.0) / len(ov8), 3)},
    "top10_overlap": {"mean": round(statistics.mean(ov10), 4), "min": min(ov10)},
    "top1_same_fraction": round(sum(top1) / len(top1), 3),
    "max_top10_score_gap": round(max(score_gap), 5),
    "elapsed_s": round(time.perf_counter() - t0, 1),
}
worst = sorted(zip(ov10, questions))[:3]
res["lowest_overlap_questions"] = [{"overlap10": o, "q": q[:100]} for o, q in worst]
print(json.dumps(res))
