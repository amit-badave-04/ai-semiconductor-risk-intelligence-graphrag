"""Where does a short-query embed spend its time? ORT's own per-node profiler, aggregated by operator type.
Read-only; loads the model in this process. argv[1] = model path, argv[2] = intra-op threads (default 1)."""
import json
import os
import sys
import tempfile
from collections import defaultdict

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL, THREADS = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1
tok = Tokenizer.from_file(MODEL.rsplit("/", 1)[0] + "/tokenizer.json")
opts = ort.SessionOptions()
opts.intra_op_num_threads = THREADS
opts.enable_profiling = True
opts.profile_file_prefix = os.path.join(tempfile.gettempdir(), "ort_prof")
sess = ort.InferenceSession(MODEL, opts, providers=["CPUExecutionProvider"])


def feeds(ids):
    f = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
    for inp in sess.get_inputs():
        if inp.name == "position_ids":
            f[inp.name] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
        elif inp.name.startswith("past_key_values"):
            shape = [1 if isinstance(d, str) else d for d in inp.shape]
            shape[2] = 0
            f[inp.name] = np.zeros(shape, dtype=np.float16 if "float16" in inp.type else np.float32)
    return f


text = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: Which foundries does Qualcomm rely on?"
ids = np.array([tok.encode(text).ids], dtype=np.int64)
sess.run(["last_hidden_state"], feeds(ids))   # warm-up (profiled too; excluded below by taking the last 3 runs)
RUNS = 3
for _ in range(RUNS):
    sess.run(["last_hidden_state"], feeds(ids))
path = sess.end_profiling()
events = json.load(open(path))
os.remove(path)
node_events = [e for e in events if e.get("cat") == "Node" and e.get("name", "").endswith("_kernel_time")]
per_op_us, per_op_n = defaultdict(float), defaultdict(int)
for e in node_events:
    op = e.get("args", {}).get("op_name", "?")
    per_op_us[op] += e["dur"]
    per_op_n[op] += 1
total = sum(per_op_us.values())
runs_total = (RUNS + 1)
rows = sorted(((op, us / runs_total / 1e6, per_op_n[op] // runs_total) for op, us in per_op_us.items()),
              key=lambda r: -r[1])
print(json.dumps({"ort": ort.__version__, "threads": THREADS, "tokens": int(ids.shape[1]),
                  "kernel_total_s_per_call": round(total / runs_total / 1e6, 3),
                  "by_op_s_per_call": [{"op": op, "s": round(s, 3), "nodes": n} for op, s, n in rows[:12]]}))
