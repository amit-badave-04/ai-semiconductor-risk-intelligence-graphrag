"""Query-embedding profile: how much of one question's embed time is fixed per-call overhead, and what removes it.
Self-contained and read-only: loads the shipped q8 ONNX model in THIS process only. Same script runs locally and in
the Fly machine (via flyctl ssh). Prints one JSON line.

argv[1] = model path, argv[2] = comma list of intra-op thread counts (default "1,2")."""
import json
import statistics
import sys
import time

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL = sys.argv[1]
THREADS = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "1,2").split(",")]
PROMPT = ("Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: ")
QUESTIONS = [
    "What export-control risks does Nvidia report in its latest annual filing?",
    "Which foundries does Qualcomm rely on to manufacture its chips?",
    "What was Micron's net income for the fiscal year ended August 28, 2025?",
    "What does Intel disclose about U.S. export controls affecting its business?",
    "Compare the competition risks disclosed by AMD and Nvidia.",
    "Which companies does Nvidia depend on for chip manufacturing and assembly?",
]
tok = Tokenizer.from_file(MODEL.rsplit("/", 1)[0] + "/tokenizer.json")
tok.enable_truncation(8192)


def feeds_for(session, ids, kv_dtype_cache={}):
    f = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
    for inp in session.get_inputs():
        if inp.name == "position_ids":
            f[inp.name] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
        elif inp.name.startswith("past_key_values"):
            shape = [1 if isinstance(d, str) else d for d in inp.shape]
            shape[2] = 0
            f[inp.name] = np.zeros(shape, dtype=np.float16 if "float16" in inp.type else np.float32)
    return f


def run_cfg(threads, outputs_mode):
    opts = ort.SessionOptions()
    if threads:
        opts.intra_op_num_threads = threads
    t0 = time.perf_counter()
    sess = ort.InferenceSession(MODEL, opts, providers=["CPUExecutionProvider"])
    load_s = time.perf_counter() - t0
    names = [o.name for o in sess.get_outputs()]
    fetch = ["last_hidden_state"] if outputs_mode == "last_only" else None
    texts = [PROMPT + q for q in QUESTIONS]
    for t in texts[:2]:   # warm-up
        sess.run(fetch, feeds_for(sess, np.array([tok.encode(t).ids], dtype=np.int64)))
    tot, tk, run = [], [], []
    ntok = []
    for _ in range(3):
        for t in texts:
            a = time.perf_counter()
            ids = np.array([tok.encode(t).ids], dtype=np.int64)
            b = time.perf_counter()
            out = sess.run(fetch, feeds_for(sess, ids))
            c = time.perf_counter()
            tk.append(b - a); run.append(c - b); tot.append(c - a); ntok.append(ids.shape[1])
    return {"threads": threads, "outputs": outputs_mode, "n_outputs_in_graph": len(names), "load_s": round(load_s, 2),
            "tokens_median": statistics.median(ntok), "total_median_s": round(statistics.median(tot), 3),
            "total_min_s": round(min(tot), 3), "total_max_s": round(max(tot), 3),
            "tokenize_median_s": round(statistics.median(tk), 4), "run_median_s": round(statistics.median(run), 3)}


def length_probe(threads):
    opts = ort.SessionOptions()
    if threads:
        opts.intra_op_num_threads = threads
    sess = ort.InferenceSession(MODEL, opts, providers=["CPUExecutionProvider"])
    rows = []
    base = tok.encode("risk " * 600).ids
    for n in (4, 16, 45, 129, 257):
        ids = np.array([base[:n]], dtype=np.int64)
        sess.run(["last_hidden_state"], feeds_for(sess, ids))
        ts = []
        for _ in range(3):
            a = time.perf_counter()
            sess.run(["last_hidden_state"], feeds_for(sess, ids))
            ts.append(time.perf_counter() - a)
        rows.append({"tokens": n, "median_s": round(statistics.median(ts), 3)})
    return rows


result = {"ort_version": ort.__version__, "model": MODEL.rsplit("/", 1)[-1], "configs": []}
for th in THREADS:
    result["configs"].append(run_cfg(th, "last_only"))
result["configs"].append(run_cfg(THREADS[0], "all_outputs"))
result["length_probe"] = {"threads": THREADS[0], "rows": length_probe(THREADS[0])}
print(json.dumps(result))
