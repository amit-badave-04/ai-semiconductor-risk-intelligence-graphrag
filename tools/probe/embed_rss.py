"""Resident memory of ONE query-embedder model, loaded and used the way the service does (M5 decision 14, window W1).

    python embed_rss.py MODEL [--compare Q8_RESULT.json | Q8_MODEL] [--threads 1] [--passage-tokens 15849] [--out FILE]

Why. The binding constraint on the embedder choice is memory, not speed: the M4 live gate (artifacts/m4_live_gates.json,
G4.memory) saw the uvicorn process peak at VmHWM 1,125,116 kB during a max-size upload (a 30-page PDF, 15,849 tokens embedded
as passages) against a 2,400,000 kB limit. The pre-dequantized fp32 model adds about 640 MB on Windows; on Linux ONNX Runtime
may hold its whole 2.3 GB external-data file resident. scripts/embedder_timing.py cannot answer that (it holds both models in
one process). This probe loads ONE model in a fresh process and reads the process's own counters.

What it does, in one process, in this order. (1) imports what the service imports and records the baseline; (2) builds
``semigraph.embeddings_onnx.OnnxBackend(model, None, threads)`` exactly as ``Embedder()`` does for the service, and makes the
warm-up query ``serve.main.bootstrap`` makes; (3) 60 query embeds through ``encode_query`` (the production prompt is added by
the backend): 30 questions from the benchmark and the examples, each plain and each with council 5's per-ask salt, alternating
which comes first; (4) the passage workload of the G4 upload: synthetic prose cut by the REAL chunker
(``semigraph.uploads.units``, 512-token cap) until 15,849 tokens, embedded one chunk per ``encode_passages([text])`` call like
``uploads.jobs._embed_chunks``. After each step it records VmRSS and VmHWM (and RssAnon / RssFile / RssShmem / VmSwap, which
tell a reclaimable mapped file from private memory) from /proc/self/status, and the median and p90 of the calls.

The prediction. ``predicted_uvicorn_vmhwm_kb = 1,125,116 + (this model's peak - the q8 model's peak)``, the peaks being the
final VmHWM of two probe processes on the SAME machine. ``--compare`` gives the q8 side: the JSON a q8 run of this probe
wrote, or a q8 model file (then a fresh child process measures it first). It is judged against 2,400,000 kB and the margin is
reported. Without ``--compare`` there is no q8 peak, so no prediction (``verdict`` is ``NO_REFERENCE``).

NOT every run is evidence for the Linux gate. ``gate_evidence`` is true only on Linux, with the G4 workload (60 queries, 15,849
passage tokens). On Windows the numbers come from psutil (working set / peak working set, NOT VmHWM), the run says so, and its
verdict is ``INDICATIVE_PASS`` / ``INDICATIVE_FAIL``. The workload is sequential: the concurrent asks of G4 are not replayed,
because the delta method assumes that overhead is the same for both models.

Output. ONE JSON object on stdout; progress goes to stderr; the model appears by file name only. Exit code: 0 (measured; PASS
or no verdict), 1 (FAIL), 2 (the run could not be done: an ONNX Runtime or file error must not look like a memory FAIL).

SELF-CONTAINED ON PURPOSE: the module imports only the standard library at top level, so this single file can be copied into
a Fly machine of the serve image (``/srv/models/embed_rss.py``, Dockerfile ``KEEP_UNPATCHED=1``) and run there. onnxruntime,
tokenizers, semigraph and psutil are imported inside the functions that need them; nothing here imports ``tools``.
"""

import argparse
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROBE_VERSION = 1

# The live measurement the prediction starts from (artifacts/m4_live_gates.json, G4.memory) and its limit.
LIVE_G4_VMHWM_KB = 1_125_116
UVICORN_LIMIT_KB = 2_400_000

# The G4 upload: 30-page PDF, 15,849 tokens embedded as passages (artifacts/m4_spikes.json, G4_local_baseline: 60 chunks).
G4_PASSAGE_TOKENS = 15_849
MAX_CHUNK_TOKENS = 512                    # Settings.upload_max_chunk_tokens
PARAGRAPHS_PER_PAGE = 4

QUERY_QUESTIONS = 30                      # each embedded plain and salted: 60 calls
QUERY_CALLS = 2 * QUERY_QUESTIONS
DEFAULT_THREADS = 1                       # ONNX_THREADS in fly.toml and deploy/staging/fly.stg.toml
WARMUP_QUESTION = "warm-up: export controls and HBM supply"          # serve.main.bootstrap
SALT_FORMAT = "{q} (ref {worker}{n:06d})"                            # tools/loadtest/salt.py (council 5)
SALT_WORKER = 1

SOURCE_PROC = "linux_proc_status"
SOURCE_PSUTIL = "psutil_peak_wset"
PROC_STATUS = "/proc/self/status"
PROC_FIELDS = {"VmRSS": "vmrss_kb", "VmHWM": "vmhwm_kb", "RssAnon": "rss_anon_kb", "RssFile": "rss_file_kb",
               "RssShmem": "rss_shmem_kb", "VmSwap": "vm_swap_kb"}
STAGES = ("baseline", "constructed", "after_load", "after_queries", "after_passages")

Sample = dict[str, int | None]


class ProbeError(RuntimeError):
    """The run cannot be done (a missing file, an unusable reference, no memory counter)."""


# ------------------------------------------------------------------------------------------ memory counters

@dataclass(frozen=True)
class MemorySource:
    read: Callable[[], Sample]
    source_id: str
    label: str
    is_linux: bool


def parse_proc_status(text: str) -> Sample:
    """The counters of /proc/self/status in kB; a line the kernel does not write (no RssAnon on old kernels) is None."""
    found: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in PROC_FIELDS and rest.split():
            found[name] = int(rest.split()[0])
    return {key: found.get(name) for name, key in PROC_FIELDS.items()}


def read_proc_status(path: str = PROC_STATUS) -> Sample:
    return parse_proc_status(Path(path).read_text(encoding="ascii", errors="replace"))


def _kb(value: int | None) -> int | None:
    return None if value is None else int(value) // 1024


def read_working_set(psutil_module=None) -> Sample:
    """Windows (and any other system without /proc): the working set and its peak from psutil, in kB. The peak is the
    working-set peak, which is NOT Linux's VmHWM; ``private`` is the committed private memory (no mapped file)."""
    if psutil_module is None:
        import psutil as psutil_module
    info = psutil_module.Process().memory_info()
    peak = getattr(info, "peak_wset", None) or info.rss
    private = getattr(info, "private", None) or getattr(info, "pagefile", None)
    peak_private = getattr(info, "peak_pagefile", None)
    return {"vmrss_kb": _kb(info.rss), "vmhwm_kb": _kb(peak), "private_kb": _kb(private), "peak_private_kb": _kb(peak_private)}


def pick_source(platform_name: str | None = None, proc_path: str = PROC_STATUS, psutil_module=None) -> MemorySource:
    """Linux: /proc/self/status. Anything else (or Linux without /proc): psutil, labelled as not the Linux number."""
    if (platform_name or sys.platform).startswith("linux") and os.access(proc_path, os.R_OK):
        return MemorySource(lambda: read_proc_status(proc_path), SOURCE_PROC, "VmRSS and VmHWM from the Linux proc status file", True)
    try:
        if psutil_module is None:
            import psutil as psutil_module
    except ImportError as exc:
        raise ProbeError("no memory counter: there is no Linux proc status file and psutil is not installed") from exc
    return MemorySource(lambda: read_working_set(psutil_module), SOURCE_PSUTIL,
                        "psutil working set and PEAK working set (not Linux VmHWM: not the Linux number)", False)


def peak_kb(samples: dict[str, Sample]) -> int | None:
    peaks = [s["vmhwm_kb"] for s in samples.values() if s.get("vmhwm_kb") is not None]
    return max(peaks) if peaks else None


# ------------------------------------------------------------------------------------------ statistics

def _percentile(ordered: Sequence[float], q: float) -> float:
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(seconds: Sequence[float]) -> dict:
    """Count, median, p90 (linear interpolation), min, max and sum of per-call seconds."""
    if not seconds:
        raise ValueError("no timings to summarize")
    ordered = sorted(seconds)
    return {"n": len(ordered), "median_s": round(_percentile(ordered, 0.5), 5), "p90_s": round(_percentile(ordered, 0.9), 5),
            "min_s": round(ordered[0], 5), "max_s": round(ordered[-1], 5), "total_s": round(sum(ordered), 4)}


# ------------------------------------------------------------------------------------------ the query workload

def load_question_pool() -> list[str]:
    """The benchmark's and the examples' questions (the ones the live site is asked), de-duplicated, in file order."""
    from semigraph.artifacts import load_benchmark, load_examples

    pool = [row["q"] for row in load_benchmark() if isinstance(row.get("q"), str)]
    pool += [row["question"] for row in load_examples().get("examples", []) if isinstance(row.get("question"), str)]
    unique = list(dict.fromkeys(text.strip() for text in pool if text.strip()))
    if not unique:
        raise ProbeError("the packaged benchmark and examples hold no questions")
    return unique


def pick_questions(pool: Sequence[str], count: int = QUERY_QUESTIONS) -> list[str]:
    """``count`` questions spread evenly over the pool sorted by length (short to long), so the sample keeps its length mix."""
    ordered = sorted(dict.fromkeys(pool), key=lambda text: (len(text), text))
    if len(ordered) <= count or count < 2:
        return ordered[:count]
    return [ordered[round(i * (len(ordered) - 1) / (count - 1))] for i in range(count)]


def apply_salt(question: str, worker: int, n: int) -> str:
    return SALT_FORMAT.format(q=question, worker=worker, n=n)


def query_plan(questions: Sequence[str]) -> list[tuple[str, bool]]:
    """(text, salted) per call: every question once plain and once salted, the order alternating by round so that neither
    kind always runs first in its pair (drift then hits both alike)."""
    plan: list[tuple[str, bool]] = []
    for n, question in enumerate(questions):
        pair = [(question, False), (apply_salt(question, SALT_WORKER, n), True)]
        plan.extend(pair if n % 2 == 0 else reversed(pair))
    return plan


def run_queries(backend, plan: Sequence[tuple[str, bool]], clock: Callable[[], float]) -> tuple[list[float], list[float]]:
    plain: list[float] = []
    salted: list[float] = []
    for text, is_salted in plan:
        started = clock()
        backend.encode_query(text)
        (salted if is_salted else plain).append(clock() - started)
    return plain, salted


# ------------------------------------------------------------------------------------------ the passage workload

SENTENCES = (
    "The Company depends on a limited number of third-party suppliers and foundries to manufacture, assemble and test its "
    "products, and a disruption at any one of them could delay shipments to customers.",
    "Changes in export control regulations may restrict the sale of certain products to customers in some countries, which "
    "could reduce revenue and harm the Company's competitive position.",
    "Revenue for the fiscal year was {a}.{b} billion dollars, an increase of {c} percent compared with the prior fiscal year, "
    "driven primarily by higher demand in the data center market.",
    "Gross margin may vary from period to period because of product mix, supply costs, inventory provisions and pricing "
    "pressure from competitors.",
    "Demand for the Company's products depends on the pace at which customers deploy new infrastructure, and customers may "
    "delay or cancel orders when economic conditions weaken.",
    "The Company has entered into long-term purchase commitments for wafers, substrates and memory totaling {d} million "
    "dollars, which it may be unable to cancel if demand declines.",
    "Intellectual property claims by third parties could require the Company to pay damages, obtain licenses on unfavorable "
    "terms or redesign products at significant cost.",
    "The Company is subject to cybersecurity threats, and a breach of its systems or those of its suppliers could expose "
    "confidential information and interrupt operations.",
    "Management believes that existing cash, cash equivalents and marketable securities, together with cash generated from "
    "operations, will be sufficient to meet working capital needs for at least the next twelve months.",
    "Climate-related events, including earthquakes, floods and droughts in regions where suppliers operate, could interrupt "
    "the availability of materials and manufacturing capacity.",
    "Competition in the markets in which the Company operates is intense, and competitors may introduce products with better "
    "performance, lower prices or broader software support.",
    "The Company recognizes revenue when control of the product transfers to the customer, generally upon shipment, and "
    "records a reserve for expected returns and rebates.",
    "Results of operations for the quarter ended {e} included charges of {f} million dollars related to inventory "
    "write-downs and excess purchase obligations.",
    "A significant portion of the workforce and a substantial amount of the supplier base are located in Asia, and "
    "geopolitical tensions could disrupt trade and operations.",
    # figure-heavy sentences: digits are one token each, so they make the dense chunks a real financial table does
    "Net revenue by quarter was {a}.{b}, {c}.{b}, {d}.{c} and {f}.{a} billion dollars, and operating income was {c}.{a}, "
    "{a}.{d}, {f}.{b} and {d}.{f} million dollars, respectively.",
    "Accounts receivable of {d}.{f} million, inventories of {f}.{d} million and deferred revenue of {c}.{a} million were "
    "recorded at {e}, against {a}.{c}, {b}.{f} and {d}.{b} million a year earlier.",
)
QUARTER_ENDS = ("March 29, 2025", "June 28, 2025", "September 27, 2025", "December 27, 2025")
# Paragraph lengths of a real filing vary (a short note, a long risk factor): the cycle gives the chunker both, so chunk sizes
# spread from the 1200-character target up to its 1800-character ceiling (G4: 60 chunks, the largest 420 tokens).
SENTENCES_PER_PARAGRAPH = (3, 4, 5, 3, 9, 4, 6, 3, 4, 8)


def paragraph(index: int) -> str:
    """Deterministic SEC-style prose: 3 to 9 sentences, rotating through ``SENTENCES`` with figures that change per paragraph."""
    figures = {"a": 10 + index % 90, "b": (index * 7) % 10, "c": 5 + (index * 3) % 60, "d": 100 + (index * 37) % 900,
               "e": QUARTER_ENDS[index % len(QUARTER_ENDS)], "f": 10 + (index * 11) % 400}
    count = SENTENCES_PER_PARAGRAPH[index % len(SENTENCES_PER_PARAGRAPH)]
    picks = (SENTENCES[(index * 3 + k) % len(SENTENCES)] for k in range(count))
    return f"Note {index + 1}. " + " ".join(sentence.format(**figures) for sentence in picks)    # the number: no two alike


def fit_to_tokens(text: str, tokens: int, count_tokens: Callable[[str], int]) -> str:
    """The longest word-prefix of ``text`` of at most ``tokens`` tokens, padded with one-token ' the' to the exact count."""
    words = text.split()
    low, high = 0, len(words)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(" ".join(words[:mid])) <= tokens:
            low = mid
        else:
            high = mid - 1
    fitted = " ".join(words[:low])
    pad = tokens - (count_tokens(fitted) if fitted else 0)
    return (fitted + " the" * max(pad, 0)).strip()


def take_tokens(texts: Sequence[str], total: int, count_tokens: Callable[[str], int]) -> list[str]:
    """The leading texts whose token counts add up to ``total``; the text that would overshoot is cut to the remainder
    (a document's last chunk is shorter than the others)."""
    taken: list[str] = []
    used = 0
    for text in texts:
        size = count_tokens(text)
        if used + size <= total:
            taken.append(text)
            used += size
            continue
        if total - used > 0:
            taken.append(fit_to_tokens(text, total - used, count_tokens))
        break
    return taken


def build_passage_chunks(count_tokens: Callable[[str], int], total_tokens: int) -> list[str]:
    """Chunks of ``total_tokens`` tokens in total, cut by the upload path's own chunker from synthetic prose: paragraph blocks
    -> ``detect_units`` -> ``chunk_units`` with the 512-token cap and the 1200/1800 character defaults (as
    ``uploads.jobs._build_units_and_chunks``)."""
    from semigraph.uploads.parse import Block
    from semigraph.uploads.units import canonical_text, chunk_units, detect_units

    blocks: list[Block] = []
    running = 0
    while running < total_tokens + MAX_CHUNK_TOKENS:                  # enough text that the cut-off falls inside a chunk
        index = len(blocks)
        text = paragraph(index)
        blocks.append(Block(text=text, page=index // PARAGRAPHS_PER_PAGE + 1, size=10.0, bold=False, kind_hint="paragraph"))
        running += count_tokens(text)
    document = canonical_text(blocks)
    chunks = chunk_units(document, detect_units(blocks, "pdf"), count_tokens=count_tokens, max_tokens=MAX_CHUNK_TOKENS)
    return take_tokens([document[c.char_start:c.char_end] for c in chunks], total_tokens, count_tokens)


def run_passages(backend, texts: Sequence[str], clock: Callable[[], float]) -> list[float]:
    """One chunk per call through ``encode_passages([text])``, as ``uploads.jobs._embed_chunks`` embeds them."""
    seconds: list[float] = []
    for text in texts:
        started = clock()
        backend.encode_passages([text])
        seconds.append(clock() - started)
    return seconds


# ------------------------------------------------------------------------------------------ measuring one model

def preload_modules() -> None:
    """What the service has imported by the time it loads the embedder (so the baseline includes it, and the model's own
    cost is not mixed with the libraries')."""
    import onnxruntime  # noqa: F401
    import tokenizers  # noqa: F401

    from semigraph import embeddings_onnx  # noqa: F401
    from semigraph.uploads import units  # noqa: F401


def load_backend(model: Path, threads: int):
    """Exactly ``embeddings._make_backend('onnx')``: the tokenizer beside the model, the service's thread setting."""
    from semigraph.embeddings_onnx import OnnxBackend

    return OnnxBackend(model, None, threads=threads)


def stderr_line(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Hooks:
    """Everything that touches the machine, replaceable so the tests run with a stub backend and a fake /proc."""

    source: MemorySource | None = None
    preload: Callable[[], None] = preload_modules
    load_backend: Callable[[Path, int], object] = load_backend
    load_questions: Callable[[], list[str]] = load_question_pool
    build_chunks: Callable[[Callable[[str], int], int], list[str]] = build_passage_chunks
    run_reference: Callable[[str, argparse.Namespace], dict] | None = None
    clock: Callable[[], float] = time.perf_counter
    log: Callable[[str], None] = stderr_line


def measure(model: Path, threads: int, passage_tokens: int, hooks: Hooks, source: MemorySource) -> dict:
    """Load, warm up, 60 queries, the passage workload; the memory counters after each step. No prediction yet."""
    hooks.preload()
    memory = {"baseline": source.read()}
    hooks.log(f"loading {model.name} with {threads} thread(s)")
    started = hooks.clock()
    backend = hooks.load_backend(model, threads)
    load_s = hooks.clock() - started
    memory["constructed"] = source.read()
    texts = hooks.build_chunks(backend.count_tokens, passage_tokens)
    started = hooks.clock()
    backend.encode_query(WARMUP_QUESTION)
    warmup_s = hooks.clock() - started
    memory["after_load"] = source.read()
    hooks.log(f"{QUERY_CALLS} query embeds")
    plain, salted = run_queries(backend, query_plan(pick_questions(hooks.load_questions())), hooks.clock)
    memory["after_queries"] = source.read()
    hooks.log(f"{len(texts)} passage chunks")
    passage_s = run_passages(backend, texts, hooks.clock)
    memory["after_passages"] = source.read()
    sizes = [backend.count_tokens(text) for text in texts]
    return {"variant": getattr(backend, "variant", "unknown"), "memory_kb": memory, "load_s": round(load_s, 3),
            "warmup_s": round(warmup_s, 3), "plain_s": plain, "salted_s": salted, "passage_s": passage_s,
            "passage_tokens": sum(sizes), "largest_chunk_tokens": max(sizes, default=0)}


# ------------------------------------------------------------------------------------------ prediction and report

def not_gate_evidence_reasons(source: MemorySource, passage_tokens: int, queries: int) -> list[str]:
    reasons = []
    if not source.is_linux:
        reasons.append("not Linux: the counters are a Windows working set, not the VmHWM of the live gate")
    if passage_tokens != G4_PASSAGE_TOKENS:
        reasons.append(f"passage workload is {passage_tokens} tokens, the G4 upload was {G4_PASSAGE_TOKENS}")
    if queries != QUERY_CALLS:
        reasons.append(f"{queries} query calls, the probe's workload is {QUERY_CALLS}")
    return reasons


def reference_problem(reference: object, mine: dict) -> str | None:
    """Why ``reference`` cannot serve as the q8 side of the delta, or None when it can."""
    if not isinstance(reference, dict) or reference.get("probe") != "embed_rss":
        return "the reference is not the output of this probe"
    if not isinstance(reference.get("peak_kb"), int):
        return "the reference has no peak (its counters were unavailable)"
    if reference.get("memory_source") != mine["memory_source"]:
        return f"the reference counted memory with {reference.get('memory_source')!r}, this run with {mine['memory_source']!r}"
    model, workload = reference.get("model") or {}, reference.get("workload") or {}
    if model.get("variant") != "q8":
        return f"the reference model is {model.get('variant')!r}, not q8"
    if model.get("threads") != mine["threads"] or workload.get("passage_tokens_requested") != mine["passage_tokens"]:
        return "the reference ran another thread count or passage workload"
    return None


def predict(peak: int | None, reference: object, mine: dict, binding: bool, limit_kb: int = UVICORN_LIMIT_KB) -> dict:
    """The G4 peak moved by this model's difference from the q8 model, judged against the limit."""
    empty = {"predicted_uvicorn_vmhwm_kb": None, "limit_kb": limit_kb, "margin_kb": None, "verdict": "NO_REFERENCE"}
    why = "no --compare: there is no q8 peak from this machine to subtract" if reference is None else reference_problem(reference, mine)
    if peak is None and why is None:
        why = "this run's counters were unavailable"
    if why is not None:
        return {**empty, "prediction": {"live_g4_vmhwm_kb": LIVE_G4_VMHWM_KB, "reason": why}}
    delta = peak - reference["peak_kb"]
    predicted = LIVE_G4_VMHWM_KB + delta
    passed = predicted <= limit_kb
    verdict = ("PASS" if passed else "FAIL") if binding else ("INDICATIVE_PASS" if passed else "INDICATIVE_FAIL")
    return {"predicted_uvicorn_vmhwm_kb": predicted, "limit_kb": limit_kb, "margin_kb": limit_kb - predicted,
            "verdict": verdict,
            "prediction": {"live_g4_vmhwm_kb": LIVE_G4_VMHWM_KB, "this_peak_kb": peak, "q8_reference_peak_kb": reference["peak_kb"],
                           "delta_kb": delta, "margin_pct_of_limit": round(100 * (limit_kb - predicted) / limit_kb, 1)}}


def _onnxruntime_version() -> str | None:
    try:
        return importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        return None


def _mem_total_kb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def build_report(model: Path, threads: int, passage_tokens: int, measured: dict, source: MemorySource,
                 reference: object, system: str) -> dict:
    memory = measured["memory_kb"]
    peak = peak_kb(memory)
    plain, salted = summarize(measured["plain_s"]), summarize(measured["salted_s"])
    calls = len(measured["plain_s"]) + len(measured["salted_s"])
    reasons = not_gate_evidence_reasons(source, passage_tokens, calls)
    mine = {"memory_source": source.source_id, "threads": threads, "passage_tokens": passage_tokens}
    return {
        "probe": "embed_rss", "version": PROBE_VERSION,
        "platform": {"system": system, "is_linux": source.is_linux, "python": sys.version.split()[0],
                     "onnxruntime": _onnxruntime_version(), "cpu_count": os.cpu_count(), "mem_total_kb": _mem_total_kb()},
        "memory_source": source.source_id, "memory_source_label": source.label,
        "model": {"file": model.name, "variant": measured["variant"], "threads": threads},
        "workload": {"query_calls": calls, "query_questions": len(measured["plain_s"]),
                     "passage_tokens_requested": passage_tokens, "passage_tokens_embedded": measured["passage_tokens"],
                     "passage_chunks": len(measured["passage_s"]), "largest_chunk_tokens": measured["largest_chunk_tokens"]},
        "memory_kb": {stage: memory[stage] for stage in STAGES},
        "peak_kb": peak,
        "timing": {"load_s": measured["load_s"], "warmup_query_s": measured["warmup_s"],
                   "queries_plain": plain, "queries_salted": salted,
                   "salt_delta_pct": round(100 * (salted["median_s"] / max(plain["median_s"], 1e-9) - 1), 1),
                   "passages": {**summarize(measured["passage_s"]),
                                "tokens_per_s": round(measured["passage_tokens"] / max(sum(measured["passage_s"]), 1e-9), 1)}},
        "gate_evidence": not reasons, "not_gate_evidence_because": reasons,
        **predict(peak, reference, mine, binding=not reasons),
    }


# ------------------------------------------------------------------------------------------ the command line

def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("model", help="the model file (model_q8.onnx, or model_fp32.onnx with its .data beside it)")
    ap.add_argument("--compare", help="the q8 side of the delta: the JSON of a q8 run of this probe (ends in .json), or a q8 "
                                      "model file (a fresh child process measures it first)")
    ap.add_argument("--threads", type=_positive, default=DEFAULT_THREADS,
                    help=f"ONNX Runtime intra-op threads, as ONNX_THREADS in fly.toml (default {DEFAULT_THREADS})")
    ap.add_argument("--passage-tokens", type=_positive, default=G4_PASSAGE_TOKENS,
                    help=f"tokens of passage chunks (default {G4_PASSAGE_TOKENS}, the G4 upload; any other value is a smoke "
                         "run and not gate evidence)")
    ap.add_argument("--out", help="also write the JSON here")
    return ap.parse_args(argv)


def run_reference_process(model: str, args: argparse.Namespace) -> dict:
    """The q8 side measured in a FRESH process (this one has loaded nothing yet), with the same threads and workload."""
    command = [sys.executable, str(Path(__file__).resolve()), model, "--threads", str(args.threads),
               "--passage-tokens", str(args.passage_tokens)]
    done = subprocess.run(command, stdout=subprocess.PIPE, text=True, check=False)
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise ProbeError(f"the q8 reference run produced no JSON (exit code {done.returncode})") from exc


def load_reference(args: argparse.Namespace, hooks: Hooks) -> object:
    if not args.compare:
        return None
    if args.compare.lower().endswith(".json"):
        try:
            return json.loads(Path(args.compare).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProbeError(f"cannot read the --compare file: {type(exc).__name__}") from exc
    if not Path(args.compare).is_file():
        raise ProbeError(f"the --compare model file does not exist: {Path(args.compare).name}")
    return (hooks.run_reference or run_reference_process)(args.compare, args)


def run(args: argparse.Namespace, hooks: Hooks | None = None, system: str | None = None) -> int:
    hooks = hooks or Hooks()
    model = Path(args.model)
    if not model.is_file():
        raise ProbeError(f"the model file does not exist: {model.name}")
    source = hooks.source or pick_source()
    reference = load_reference(args, hooks)                  # before this process loads anything
    measured = measure(model, args.threads, args.passage_tokens, hooks, source)
    report = build_report(model, args.threads, args.passage_tokens, measured, source, reference,
                          system or platform.system().lower())
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 1 if report["verdict"] in ("FAIL", "INDICATIVE_FAIL") else 0


def main(argv: Sequence[str] | None = None, hooks: Hooks | None = None) -> int:
    try:
        return run(parse_args(argv), hooks)
    except ProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # an ONNX Runtime load/run error must not look like a memory FAIL (exit 1)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
