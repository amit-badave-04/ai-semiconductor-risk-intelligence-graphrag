"""scripts/check_chars_per_token.py: the offline check of the one assumption a paid call's bound rests on.

``serve.estimate.chars_per_token(model)`` (2.0 for Claude Sonnet 5, 2.5 for every other model) turns the characters of a
worst-case prompt into the tokens it is billed for. The check joins what the recorded runs saved (a prompt's context, or a
model's usage on it) and reports the characters per token, per model, and every row below ITS MODEL's assumption. Every
fixture here is a small fake file under ``tmp_path``; the real ``data/processed`` is read only by the one test that re-derives
the committed artifact, and only when it is present (it is git-ignored).
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_chars_per_token.py"
ARTIFACT = ROOT / "artifacts" / "chars_per_token_check.json"
PROCESSED = ROOT / "data" / "processed"


def _load():
    spec = importlib.util.spec_from_file_location("check_chars_per_token", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_chars_per_token"] = module
    spec.loader.exec_module(module)
    return module


check = _load()
LUNA, SONNET, OTHER = "openai/gpt-6-luna", "anthropic/claude-sonnet-5", "gemini/gemini-3.8-flash"


# --- fixtures: the recorded runs, in miniature ----------------------------------------------------------------------

def legacy_context(edges="E" * 40, metrics="M" * 40, risks="R" * 40, dropped="D" * 40, chunks="C" * 400) -> str:
    """A context as the benchmark runs saved it: the five legacy blocks under their headers."""
    return "".join(h + b for h, b in zip(check.RUN_HEADERS, (edges, metrics, risks, dropped, chunks), strict=True))


def prompt_of(question: str, **blocks) -> str:
    edges, metrics, risks, dropped, chunks = (blocks.get(k, v) for k, v in zip(
        ("edges", "metrics", "risks", "dropped", "chunks"), ("E" * 40, "M" * 40, "R" * 40, "D" * 40, "C" * 400), strict=True))
    return check.RUN_TEMPLATE.format(question=question, edges_block=edges, metrics_block=metrics, risks_block=risks,
                                     temporal_block=dropped, chunks_block=chunks)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def processed(tmp_path: Path, *, baseline=(), bakeoff=(), deployed=()) -> Path:
    write_jsonl(tmp_path / check.BASELINE, list(baseline))
    write_jsonl(tmp_path / check.BAKEOFF, list(bakeoff))
    write_jsonl(tmp_path / check.DEPLOYED, list(deployed))
    return tmp_path


def base_row(id_="N1", system="hybrid", tokens=300, **ctx) -> dict:
    return {"id": id_, "system": system, "q": f"Question {id_}?", "context": legacy_context(**ctx),
            "usage": {"prompt_tokens": tokens, "completion_tokens": 20}}


def chars_of(id_="N1", **blocks) -> int:
    return len(prompt_of(f"Question {id_}?", **blocks))


# --- the prompt the recorded runs sent ------------------------------------------------------------------------------

def test_the_prompt_is_the_run_time_template_over_the_five_saved_blocks():
    assert check.run_prompt("Q?", legacy_context()) == prompt_of("Q?")


def test_a_context_that_does_not_split_is_not_guessed_at():
    assert check.run_prompt("Q?", "no headers here") is None
    assert check.run_prompt("Q?", check.RUN_HEADERS[0] + "x" + check.RUN_HEADERS[2]) is None       # a header missing
    assert check.run_prompt("Q?", legacy_context() + "\n\n=== TRAILING ===") is not None            # the last block is open


def test_the_embedded_template_and_headers_are_what_the_recorded_runs_used():
    """The runs of 2026-09-26 (before the M1b template of 13:11) were prompted with the 2026-07-03 template, which the
    script embeds so that it needs no git history. A change to the embedded text must be a deliberate one."""
    assert hashlib.sha256(check.RUN_TEMPLATE.encode("utf-8")).hexdigest() == check.RUN_TEMPLATE_SHA256
    assert check.RUN_TEMPLATE_SHA256 == "8f5d043b18263551c8b34efb8dcc06c04770f9f10c88ca92be8b2ceb4857aabc"
    assert len(check.RUN_TEMPLATE) == 833 and check.RUN_TEMPLATE.count("{") == 6
    from semigraph.retrieval.context_layout import LEGACY_CONTEXT_HEADERS
    assert check.RUN_HEADERS == LEGACY_CONTEXT_HEADERS


# --- the joins ------------------------------------------------------------------------------------------------------

def test_a_baseline_row_is_its_own_prompt_over_its_own_usage(tmp_path):
    report = check.build_report(processed(tmp_path, baseline=[base_row(tokens=300), base_row("N2", "vector", tokens=250)]))
    rows = {(r["id"], r["system"]): r for r in report["rows"] if r["source"] == "baseline"}
    assert rows[("N1", "hybrid")]["chars"] == chars_of("N1") and rows[("N1", "hybrid")]["model"] == SONNET
    assert rows[("N1", "hybrid")]["ratio"] == round(chars_of("N1") / 300, 4)
    assert rows[("N2", "vector")]["ratio"] == round(chars_of("N2") / 250, 4)
    assert {r["basis"] for r in rows.values()} == {"exact"}


def test_a_bakeoff_row_joins_the_hybrid_context_of_its_id_and_the_vector_one_never_joins(tmp_path):
    usage = {"prompt_tokens": 400, "completion_tokens": 9}
    bakeoff = [{"model": LUNA, "id": "N1", "usage": {"prompt_tokens": 200, "completion_tokens": 9}},
               {"model": OTHER, "id": "N1", "usage": usage},
               {"model": LUNA, "id": "GHOST", "usage": usage},          # no baseline row at all
               {"model": LUNA, "id": "V1", "usage": usage}]             # a baseline row, but a vector one: other prompt
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1"), base_row("V1", "vector", tokens=100)],
                                          bakeoff=bakeoff))
    rows = [r for r in report["rows"] if r["source"] == "bakeoff"]
    assert {(r["model"], r["id"]): r["ratio"] for r in rows} == {(LUNA, "N1"): round(chars_of("N1") / 200, 4),
                                                                 (OTHER, "N1"): round(chars_of("N1") / 400, 4)}
    assert report["unmatched_ids"]["bakeoff"] == ["GHOST", "V1"]


def test_the_deployed_join_is_approximate_and_never_decides_the_verdict(tmp_path):
    """The deployed runs saved usage and no prompt: the stand-in is the baseline prompt of the same id, from another day,
    a different graph, context layout and template. It is listed, marked approximate, and kept out of the verdict."""
    deployed = [{"id": "N1", "answered_by": LUNA, "escalated": False, "usage": {"prompt_tokens": 100, "completion_tokens": 5}},
                {"id": "N1", "answered_by": SONNET, "escalated": True, "usage": {"prompt_tokens": 900, "completion_tokens": 5}}]
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1", tokens=300)], deployed=deployed))
    rows = [r for r in report["rows"] if r["source"] == "deployed_v2e"]
    assert [(r["model"], r["basis"]) for r in rows] == [(LUNA, "approximate")]
    assert report["excluded"]["deployed_v2e"] == {"escalated (the usage sums two calls)": 1}
    assert LUNA not in report["verdict"] and SONNET in report["verdict"]       # Sonnet has its exact baseline row


def test_rows_without_provider_usage_are_excluded_and_counted(tmp_path):
    base = [base_row("N1"), {**base_row("N2"), "usage": {"prompt_tokens": 50, "completion_tokens": 5, "estimated": True}},
            {**base_row("N3"), "usage": None}]
    report = check.build_report(processed(tmp_path, baseline=base))
    assert [r["id"] for r in report["rows"]] == ["N1"]
    assert report["excluded"]["baseline"] == {"no provider-reported usage": 2}


# --- the verdict ----------------------------------------------------------------------------------------------------

def tokens_at(ratio: float, id_: str = "N1") -> int:
    """The billed prompt tokens that make the rebuilt prompt of ``id_`` ``ratio`` characters per token (to within a token)."""
    return round(chars_of(id_) / ratio)


def test_a_row_below_its_models_assumption_is_flagged_and_names_whether_the_model_is_one_the_service_calls(tmp_path):
    dense = base_row("N1", tokens=tokens_at(1.5))                        # Sonnet at 1.5 characters per token: below its 2.0
    sparse = base_row("N2", tokens=tokens_at(4.0, "N2"))                 # 4.0
    bakeoff = [{"model": OTHER, "id": "N2", "usage": {"prompt_tokens": chars_of("N2"), "completion_tokens": 1}}]   # 1.0
    report = check.build_report(processed(tmp_path, baseline=[dense, sparse], bakeoff=bakeoff))
    flags = {(f["source"], f["model"], f["id"]): f for f in report["flags"]}
    assert set(flags) == {("baseline", SONNET, "N1"), ("bakeoff", OTHER, "N2")}
    assert flags[("baseline", SONNET, "N1")]["production_model"] is True and flags[("bakeoff", OTHER, "N2")]["production_model"] is False
    assert flags[("baseline", SONNET, "N1")]["assumed"] == 2.0 and flags[("bakeoff", OTHER, "N2")]["assumed"] == 2.5
    assert all(f["ratio"] < f["assumed"] for f in report["flags"])
    verdict = report["verdict"][SONNET]
    assert verdict["min_ratio"] < 2.0 and verdict["assumed"] == 2.0 and verdict["holds"] is False and OTHER not in report["verdict"]


def test_each_model_is_judged_against_its_own_figure_sonnet_at_two_point_two_holds_where_the_old_single_2_5_failed(tmp_path):
    """The point of the per-model figure: a Sonnet row at 2.2 characters per token is below the old single 2.5 and above its
    own 2.0, so it holds; a Luna row at 2.4 is below ITS 2.5 and is flagged; a Luna row at 2.7 holds."""
    bakeoff = [{"model": LUNA, "id": "N2", "usage": {"prompt_tokens": tokens_at(2.4, "N2"), "completion_tokens": 1}},
               {"model": LUNA, "id": "N3", "usage": {"prompt_tokens": tokens_at(2.7, "N3"), "completion_tokens": 1}}]
    baseline = [base_row("N1", tokens=tokens_at(2.2)), base_row("N2"), base_row("N3")]
    report = check.build_report(processed(tmp_path, baseline=baseline, bakeoff=bakeoff))
    assert report["verdict"][SONNET]["holds"] is True and report["verdict"][SONNET]["below"] == 0
    assert 2.0 < report["verdict"][SONNET]["min_ratio"] < 2.5
    assert report["verdict"][LUNA]["holds"] is False and report["verdict"][LUNA]["below"] == 1 and report["verdict"][LUNA]["assumed"] == 2.5
    assert [(f["model"], f["id"]) for f in report["flags"] if f["production_model"]] == [(LUNA, "N2")]
    summary = {(s["source"], s["model"]): s for s in report["summary"]}
    assert summary[("baseline", SONNET)]["assumed"] == 2.0 and summary[("bakeoff", LUNA)]["assumed"] == 2.5
    assert summary[("bakeoff", LUNA)]["below"] == 1 and summary[("baseline", SONNET)]["below"] == 0
    assert report["conclusion"].startswith("the per-model assumptions do NOT all hold") and f"{LUNA}: lowest" in report["conclusion"]


def test_a_model_the_service_has_no_figure_for_is_judged_against_the_default(tmp_path):
    bakeoff = [{"model": OTHER, "id": "N1", "usage": {"prompt_tokens": tokens_at(2.4), "completion_tokens": 1}}]
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1", tokens=tokens_at(3.0))], bakeoff=bakeoff))
    [flag] = report["flags"]
    assert (flag["model"], flag["assumed"], flag["production_model"]) == (OTHER, 2.5, False)


def test_a_check_with_no_row_below_its_models_assumption_holds(tmp_path):
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1", tokens=chars_of("N1") // 3)]))
    assert report["flags"] == [] and report["verdict"][SONNET]["holds"] is True
    assert report["conclusion"].startswith("the per-model assumptions hold on the recorded prompts")


def test_the_report_states_the_per_model_assumption_it_checks_and_the_caveats(tmp_path):
    from decimal import Decimal

    from semigraph.serve.estimate import DEFAULT_CHARS_PER_TOKEN, chars_per_token
    report = check.build_report(processed(tmp_path, baseline=[base_row()]))
    assumption = report["assumption"]
    assert assumption["chars_per_token"] == {m: float(chars_per_token(m)) for m in check.PRODUCTION_MODELS} == {LUNA: 2.5, SONNET: 2.0}
    assert assumption["default"] == float(DEFAULT_CHARS_PER_TOKEN) == 2.5 and assumption["where"].endswith("chars_per_token(model)")
    assert not hasattr(check, "THRESHOLD") and Decimal(str(assumption["chars_per_token"][SONNET])) == Decimal("2.0")
    text = " ".join(report["caveats"]).lower()
    for must_say in ("join", "template", "never saved"):
        assert must_say in text, must_say
    assert any("planner" in m for m in report["missing"]) and any("workspace" in m for m in report["missing"])
    assert "quotes 3.03" not in text, "the estimate no longer quotes those figures: the caveat that discussed them is stale"


def test_the_input_files_are_fingerprinted_so_the_artifact_says_what_it_was_made_from(tmp_path):
    folder = processed(tmp_path, baseline=[base_row()])
    report = check.build_report(folder)
    entry = report["inputs"][check.BASELINE]
    assert entry == {"rows": 1, "sha256": hashlib.sha256((folder / check.BASELINE).read_bytes()).hexdigest()}


# --- the check on the rebuilt prompts -------------------------------------------------------------------------------
# The rebuilt prompt is only as good as the template and the layout it assumes. For Luna, whose tokenizer is o200k_base to
# within 0.2% on the recorded prompts, the rebuilt prompt can be tokenised offline and compared with the tokens billed.

def _encoder():
    pytest.importorskip("tiktoken")
    encoder = check.load_encoder()
    if encoder is None:
        pytest.skip("o200k_base is not available offline here")
    return encoder


def test_a_rebuilt_prompt_that_is_the_prompt_billed_matches_its_tokens(tmp_path):
    encoder = _encoder()
    billed = len(encoder.encode(prompt_of("Question N1?")))
    bakeoff = [{"model": LUNA, "id": "N1", "usage": {"prompt_tokens": billed, "completion_tokens": 3}}]
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1")], bakeoff=bakeoff), validate_template=True)
    validation = report["template_validation"]
    assert validation["encoding"] == "o200k_base" and validation["model"] == LUNA and validation["rows"] == 1
    assert (validation["min"], validation["mean"], validation["max"]) == (1.0, 1.0, 1.0)


def test_a_rebuilt_prompt_with_the_wrong_template_is_seen_in_the_comparison(tmp_path):
    encoder = _encoder()
    billed = len(encoder.encode(prompt_of("Question N1?"))) + 2800            # a template of some 9,000 characters more
    bakeoff = [{"model": LUNA, "id": "N1", "usage": {"prompt_tokens": billed, "completion_tokens": 3}}]
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1")], bakeoff=bakeoff), validate_template=True)
    assert report["template_validation"]["mean"] < 0.9


def test_without_the_flag_there_is_no_validation_and_no_tokenizer_is_loaded(tmp_path):
    report = check.build_report(processed(tmp_path, baseline=[base_row("N1")]))
    assert "template_validation" not in report


# --- the command line -----------------------------------------------------------------------------------------------

def test_main_writes_the_artifact_deterministically_and_exits_1_only_for_a_service_model_below_the_assumption(tmp_path, capsys):
    folder, out = processed(tmp_path, baseline=[base_row("N1", tokens=tokens_at(1.5))]), tmp_path / "out" / "check.json"
    assert check.main(["--processed", str(folder), "--out", str(out)]) == 1
    first = out.read_bytes()
    assert check.main(["--processed", str(folder), "--out", str(out)]) == 1 and out.read_bytes() == first   # no clock in it
    text = capsys.readouterr().out
    assert "BELOW" in text and SONNET in text
    (tmp_path / "ok").mkdir()
    ok = processed(tmp_path / "ok", baseline=[base_row("N1", tokens=chars_of("N1") // 3)])
    assert check.main(["--processed", str(ok), "--out", str(tmp_path / "ok.json")]) == 0


def test_main_exits_0_for_a_flagged_row_of_a_model_the_service_never_calls(tmp_path):
    folder = processed(tmp_path, baseline=[base_row("N1", tokens=chars_of("N1") // 3)],
                       bakeoff=[{"model": OTHER, "id": "N1", "usage": {"prompt_tokens": chars_of("N1"), "completion_tokens": 1}}])
    assert check.main(["--processed", str(folder), "--out", str(tmp_path / "o.json")]) == 0


def test_a_missing_input_file_is_a_clear_error_not_a_traceback(tmp_path, capsys):
    assert check.main(["--processed", str(tmp_path), "--out", str(tmp_path / "o.json")]) == 2
    assert check.BASELINE in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


# --- the committed artifact -----------------------------------------------------------------------------------------

def test_the_committed_artifact_states_its_caveats_and_names_what_it_could_not_see():
    report = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    from semigraph.serve.estimate import chars_per_token
    figures = report["assumption"]["chars_per_token"]
    # regenerate the artifact if an assumption moves: it must be judged against the figures the service uses NOW
    assert figures == {m: float(chars_per_token(m)) for m in check.PRODUCTION_MODELS} == {LUNA: 2.5, SONNET: 2.0}
    assert report["rows"] and report["summary"] and report["inputs"]
    text = " ".join(report["caveats"]).lower()
    assert "join" in text and "template" in text and "never saved" in text
    assert report["missing"], "what the offline data cannot show must be named"

    def assumed(model):
        return figures.get(model, report["assumption"]["default"])

    # internal consistency: a flag is exactly a row below ITS MODEL's assumption, and the verdict is read off the exact rows
    below = [r for r in report["rows"] if r["ratio"] < assumed(r["model"])]
    assert sorted((r["source"], r["model"], r["id"], r["system"]) for r in below) == sorted(
        (f["source"], f["model"], f["id"], f["system"]) for f in report["flags"])
    assert all(f["assumed"] == assumed(f["model"]) for f in report["flags"])
    for model, v in report["verdict"].items():
        exact = [r["ratio"] for r in report["rows"] if r["model"] == model and r["basis"] == "exact"]
        assert v["assumed"] == assumed(model)
        assert v["min_ratio"] == min(exact) and v["holds"] == (min(exact) >= assumed(model))


def test_the_committed_artifact_says_both_production_models_hold_against_their_own_figures():
    """Sonnet's lowest exact row is 2.0997 against its 2.0, Luna's 3.0224 against 2.5 (the old single 2.5 failed for
    Sonnet: 7 of its 20 hybrid rows were below it). The conclusion is read off the verdict, not typed."""
    report = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    verdict = report["verdict"]
    assert verdict[SONNET]["min_ratio"] == 2.0997 and verdict[SONNET]["rows"] == 40 and verdict[SONNET]["below"] == 0
    assert verdict[LUNA]["min_ratio"] == 3.0224 and verdict[LUNA]["rows"] == 20 and verdict[LUNA]["below"] == 0
    assert verdict[SONNET]["holds"] is True and verdict[LUNA]["holds"] is True
    assert report["conclusion"].startswith("the per-model assumptions hold on the recorded prompts (")
    assert "NOT" not in report["conclusion"]


def test_the_committed_artifact_shows_the_rebuilt_luna_prompts_match_the_tokens_billed():
    """The evidence that the embedded template is the one the runs used: Luna's tokenizer is o200k_base to within 0.2% on
    these prompts, and the rebuilt prompts tokenise to the billed count. A rebuild with today's template does not (it
    measured 1.10 to 1.52 times the billed tokens)."""
    validation = json.loads(ARTIFACT.read_text(encoding="utf-8"))["template_validation"]
    assert validation["rows"] == 20 and 0.99 <= validation["min"] <= validation["mean"] <= validation["max"] <= 1.01


@pytest.mark.skipif(not (PROCESSED / "eval_runs.v2-baseline.jsonl").exists(), reason="data/processed is git-ignored")
def test_the_committed_artifact_is_what_the_script_makes_of_the_recorded_runs(tmp_path):
    pytest.importorskip("tiktoken")
    if check.load_encoder() is None:
        pytest.skip("o200k_base is not available offline here")
    out = tmp_path / "again.json"
    check.main(["--processed", str(PROCESSED), "--out", str(out), "--validate-template"])
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(ARTIFACT.read_text(encoding="utf-8"))
