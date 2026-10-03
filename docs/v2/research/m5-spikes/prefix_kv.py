"""Prefix-KV reuse for the constant Qwen3 query instruction: compute the prefix's KV cache once, then run only each
question's own tokens with that cache. Compares vectors and latency against the plain full-sequence run.
Read-only. argv[1] = model path (the accuracy_level=4 copy), argv[2] = threads (default 1)."""
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL = sys.argv[1]
THREADS = int(sys.argv[2]) if len(sys.argv) > 2 else 1
PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "
QS = ["What export-control risks does Nvidia report in its latest annual filing?",
      "Which foundries does Qualcomm rely on to manufacture its chips?",
      "What was Micron's net income for the fiscal year ended August 28, 2025?",
      "What does Intel disclose about U.S. export controls affecting its business?",
      "Compare the competition risks disclosed by AMD and Nvidia.",
      "Which companies does Nvidia depend on for chip manufacturing and assembly?",
      "Did Meta remove any risk factors between its FY2024 and FY2025 annual reports?",
      "By what percentage did Broadcom's total revenue change from fiscal 2024 to fiscal 2025?"]
tok = Tokenizer.from_file(str(Path(MODEL).parent / "tokenizer.json"))
opts = ort.SessionOptions()
opts.intra_op_num_threads = THREADS
sess = ort.InferenceSession(MODEL, opts, providers=["CPUExecutionProvider"])
in_names = [i.name for i in sess.get_inputs()]
out_names = [o.name for o in sess.get_outputs()]
kv_in = [n for n in in_names if n.startswith("past_key_values")]
kv_out = [n for n in out_names if n.startswith("present")]
assert len(kv_in) == len(kv_out) and kv_in, (kv_in[:2], kv_out[:2])
kv_in_info = {i.name: i for i in sess.get_inputs()}


def empty_past():
    d = {}
    for n in kv_in:
        shape = [1 if isinstance(x, str) else x for x in kv_in_info[n].shape]
        shape[2] = 0
        d[n] = np.zeros(shape, dtype=np.float16 if "float16" in kv_in_info[n].type else np.float32)
    return d


def run(ids, past, past_len):
    n = ids.shape[1]
    f = {"input_ids": ids, "attention_mask": np.ones((1, past_len + n), dtype=np.int64)}
    if "position_ids" in in_names:
        f["position_ids"] = np.arange(past_len, past_len + n, dtype=np.int64)[None, :]
    f.update(past)
    return f


# longest common token prefix of PROMPT+question across the sample (BPE may merge the trailing space with word 1)
enc = [tok.encode(PROMPT + q).ids for q in QS]
lcp = 0
while all(len(e) > lcp for e in enc) and len({e[lcp] for e in enc}) == 1:
    lcp += 1
prefix_ids = np.array([enc[0][:lcp]], dtype=np.int64)
t0 = time.perf_counter()
res = sess.run(["last_hidden_state"] + kv_out, run(prefix_ids, empty_past(), 0))
prefix_s = time.perf_counter() - t0
prefix_past = {kin: res[1 + i] for i, kin in enumerate(kv_in)}


def plain(ids):
    return sess.run(["last_hidden_state"], run(ids, empty_past(), 0))[0][0, -1, :]


def cached(tail_ids):
    return sess.run(["last_hidden_state"], run(tail_ids, prefix_past, lcp))[0][0, -1, :]


cos, t_plain, t_cached = [], [], []
for e in enc:
    ids = np.array([e], dtype=np.int64)
    tail = np.array([e[lcp:]], dtype=np.int64)
    plain(ids); cached(tail)   # warm
    a = time.perf_counter(); vp = plain(ids); b = time.perf_counter(); vc = cached(tail); c = time.perf_counter()
    vp, vc = vp.astype(np.float32), vc.astype(np.float32)
    cos.append(float(vp @ vc / (np.linalg.norm(vp) * np.linalg.norm(vc))))
    t_plain.append(b - a); t_cached.append(c - b)
print(json.dumps({"threads": THREADS, "prefix_tokens": int(lcp), "question_tokens_median": statistics.median(len(e) - lcp for e in enc),
                  "prefix_compute_s_once": round(prefix_s, 3),
                  "plain_median_s": round(statistics.median(t_plain), 3), "cached_median_s": round(statistics.median(t_cached), 3),
                  "speedup": round(statistics.median(t_plain) / statistics.median(t_cached), 2),
                  "cosine_cached_vs_plain_min": round(min(cos), 6), "cosine_mean": round(statistics.mean(cos), 6)}))
