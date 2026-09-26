"""The provenance sidecar of the align-items tables and what ``require_alignment`` (so ``build-graph``) refuses because of it.

Synthetic lake, fake model and embedder, nothing paid. The tables are written under ``tmp_path`` only.
"""

import hashlib
import json
import re

import pytest
from embedfix import make_embed
from test_align_replay import BELOW_ON, RELAXED, FullLLM, full_lake, wipe_tables  # noqa: F401  (full_lake is a fixture)
from test_items_pipeline import ZZZ

import lakefix
from semigraph.graph import adjudicate as adj
from semigraph.graph import alignment_provenance as prov
from semigraph.graph import items
from semigraph.graph import passage_adjudicate as pad


def sidecar(settings):
    return json.loads((items.alignment_dir(settings) / prov.PROVENANCE_NAME).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def buy(settings, **kwargs):
    return items.run_align_items(settings, [ZZZ], adjudicate=True, adjudicate_passages=True, passage_params=BELOW_ON, llm=FullLLM(),
                                 embed=make_embed(), pas_params=RELAXED, max_usd=5.0, **kwargs)


def append_record(directory, name, key="k|k|pas-v3|m"):
    with (directory / name).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"key": key, "verdict": "different", "zone": "band", "model": "m"}) + "\n")


# --------------------------------------------------------------------------- what is written

def test_the_sidecar_names_the_flags_the_replayed_versions_the_checkpoint_digests_and_the_input_digests(full_lake):
    run = buy(full_lake)
    entry = sidecar(full_lake)["tickers"][ZZZ]
    directory = items.alignment_dir(full_lake)
    assert run.provenance == directory / prov.PROVENANCE_NAME and run.written[-1] == run.provenance
    assert entry["flags"] == {"adjudicate": True, "adjudicate_passages": True, "adjudicate_all_absent": True}
    assert entry["model"] == full_lake.adjudication_model
    assert entry["item_adjudication"] == {"prompt_version": adj.PROMPT_VERSION, "verdicts_applied": run.item_verdicts_used}
    assert entry["passage_adjudication"] == {"prompt_version": pad.PROMPT_VERSION, "below_zone": True,
                                             "verdicts_applied": run.passage_verdicts_used}
    for name in (adj.CHECKPOINT_NAME, pad.CHECKPOINT_NAME):
        lines = (directory / name).read_text(encoding="utf-8").splitlines()
        assert entry["checkpoints"][name] == {"sha256": sha(directory / name), "records": len(lines)}
    inputs = items.ticker_input_paths(full_lake, ZZZ)
    assert entry["inputs"] == {name: sha(path) for name, path in sorted(inputs.items())} and set(inputs) == {
        "risk_items", "section_texts", "chunks", "quality"}


def test_a_lake_without_checkpoints_records_null_digests_and_no_version(tmp_path):
    lake = lakefix.build_lake(tmp_path)
    items.run_align_items(lake, [ZZZ])
    entry = sidecar(lake)["tickers"][ZZZ]
    assert entry["checkpoints"] == {adj.CHECKPOINT_NAME: None, pad.CHECKPOINT_NAME: None}
    assert entry["passage_adjudication"]["prompt_version"] is None and entry["flags"]["adjudicate"] is False


def test_the_sidecar_has_no_timestamp_and_no_absolute_path_so_the_same_run_writes_the_same_bytes(full_lake):
    buy(full_lake)
    first = (items.alignment_dir(full_lake) / prov.PROVENANCE_NAME).read_bytes()
    items.run_align_items(full_lake, [ZZZ], adjudicate=True, adjudicate_passages=True, passage_params=BELOW_ON, llm=FullLLM(),
                          embed=make_embed(), pas_params=RELAXED, max_usd=5.0)
    assert (items.alignment_dir(full_lake) / prov.PROVENANCE_NAME).read_bytes() == first
    text = first.decode("utf-8")
    assert str(full_lake.data_dir) not in text and "\\" not in text and not re.search(r"\d{4}-\d{2}-\d{2}", text)
    assert json.loads(text)["schema"] == prov.SCHEMA_VERSION and first.endswith(b"\n")


def test_a_plain_replay_records_the_same_digests_and_versions_as_the_buying_run_and_only_its_flags_differ(full_lake):
    buy(full_lake)
    bought = sidecar(full_lake)["tickers"][ZZZ]
    items.run_align_items(full_lake, [ZZZ], pas_params=RELAXED)
    plain = sidecar(full_lake)["tickers"][ZZZ]
    assert plain["flags"] == {"adjudicate": False, "adjudicate_passages": False, "adjudicate_all_absent": False}
    assert {k: v for k, v in plain.items() if k != "flags"} == {k: v for k, v in bought.items() if k != "flags"}


def test_the_digest_is_of_the_bytes_the_tables_were_built_from_not_of_a_later_file_state(full_lake, monkeypatch):
    """A paid job appending after the checkpoint was read must not make the tables claim answers they do not contain."""
    buy(full_lake)
    directory = items.alignment_dir(full_lake)
    real = items.replay_context
    seen = {}

    def spying(passage_cp, *args, **kwargs):
        seen["sha"] = passage_cp.sha256
        append_record(directory, pad.CHECKPOINT_NAME, "late|late|pas-v3|" + full_lake.adjudication_model)     # arrives after the read
        return real(passage_cp, *args, **kwargs)

    monkeypatch.setattr(items, "replay_context", spying)
    items.run_align_items(full_lake, [ZZZ], pas_params=RELAXED)
    assert sidecar(full_lake)["tickers"][ZZZ]["checkpoints"][pad.CHECKPOINT_NAME]["sha256"] == seen["sha"] != sha(directory / pad.CHECKPOINT_NAME)
    with pytest.raises(items.AlignItemsError, match="run `semigraph align-items`"):
        items.require_alignment(full_lake, [ZZZ])


def test_a_dry_run_and_a_refused_run_write_no_sidecar(full_lake):
    items.run_align_items(full_lake, [ZZZ], dry_run=True)
    assert not items.alignment_dir(full_lake).exists()
    with pytest.raises(pad.BudgetExceeded):
        items.run_align_items(full_lake, [ZZZ], adjudicate=True, max_usd=1e-9, llm=FullLLM())
    assert not (items.alignment_dir(full_lake) / prov.PROVENANCE_NAME).exists()


def test_a_run_for_one_ticker_leaves_the_other_tickers_entries_alone(tmp_path):
    lake = lakefix.build_lake(tmp_path, ticker="AAA")
    lakefix.build_lake(tmp_path, ticker="BBB")
    items.run_align_items(lake, ["AAA", "BBB"])
    before = json.loads((items.alignment_dir(lake) / prov.PROVENANCE_NAME).read_text(encoding="utf-8"))
    append_record(items.alignment_dir(lake), pad.CHECKPOINT_NAME)                       # the checkpoint grows, then only AAA is redone
    items.run_align_items(lake, ["AAA"])
    after = json.loads((items.alignment_dir(lake) / prov.PROVENANCE_NAME).read_text(encoding="utf-8"))
    assert set(after["tickers"]) == {"AAA", "BBB"} and after["tickers"]["BBB"] == before["tickers"]["BBB"]
    assert after["tickers"]["AAA"]["checkpoints"][pad.CHECKPOINT_NAME]["records"] == 1
    assert prov.stale_checkpoints(items.alignment_dir(lake), ["AAA", "BBB"]) == {"BBB": [pad.CHECKPOINT_NAME]}
    with pytest.raises(items.AlignItemsError, match=r"\['BBB'\]"):
        items.require_alignment(lake, ["AAA", "BBB"])
    items.require_alignment(lake, ["AAA"])


# --------------------------------------------------------------------------- what require_alignment refuses

def test_fresh_tables_pass_and_so_do_tables_built_by_a_plain_replay_of_the_same_checkpoints(full_lake):
    buy(full_lake)
    items.require_alignment(full_lake, [ZZZ])
    items.run_align_items(full_lake, [ZZZ], pas_params=RELAXED)
    items.require_alignment(full_lake, [ZZZ])


@pytest.mark.parametrize("name", [adj.CHECKPOINT_NAME, pad.CHECKPOINT_NAME])
def test_tables_built_before_a_checkpoint_gained_answers_are_refused_with_the_command_to_rerun(full_lake, name):
    buy(full_lake)
    items.require_alignment(full_lake, [ZZZ])
    append_record(items.alignment_dir(full_lake), name)                                # the paid job wrote one more answer
    with pytest.raises(items.AlignItemsError) as err:
        items.require_alignment(full_lake, [ZZZ])
    message = str(err.value)
    assert "run `semigraph align-items`" in message and name in message and "free" in message and "['ZZZ']" in message
    items.run_align_items(full_lake, [ZZZ], pas_params=RELAXED)                        # the free rerun repairs it
    items.require_alignment(full_lake, [ZZZ])


def test_tables_with_no_provenance_are_refused_while_a_checkpoint_exists(full_lake):
    buy(full_lake)
    (items.alignment_dir(full_lake) / prov.PROVENANCE_NAME).unlink()                   # tables written by the code before this change
    with pytest.raises(items.AlignItemsError, match="carry no provenance.*run `semigraph align-items`"):
        items.require_alignment(full_lake, [ZZZ])


def test_a_corrupt_sidecar_is_no_provenance(full_lake):
    buy(full_lake)
    (items.alignment_dir(full_lake) / prov.PROVENANCE_NAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(items.AlignItemsError, match="run `semigraph align-items`"):
        items.require_alignment(full_lake, [ZZZ])


def test_a_checkpoint_deleted_after_the_tables_were_built_makes_them_unreproducible_and_refused(full_lake):
    buy(full_lake)
    (items.alignment_dir(full_lake) / adj.CHECKPOINT_NAME).unlink()
    with pytest.raises(items.AlignItemsError, match=adj.CHECKPOINT_NAME):
        items.require_alignment(full_lake, [ZZZ])


def test_tables_with_no_provenance_pass_when_no_checkpoint_exists_because_nothing_can_be_missing_from_them(tmp_path):
    lake = lakefix.build_lake(tmp_path)
    items.run_align_items(lake, [ZZZ])
    (items.alignment_dir(lake) / prov.PROVENANCE_NAME).unlink()
    items.require_alignment(lake, [ZZZ])


def test_the_id_consistency_check_still_speaks_first(full_lake):
    buy(full_lake)
    import pandas as pd

    path = full_lake.interim_dir / "risk_items" / "ZZZ_risk_items.parquet"
    frame = pd.read_parquet(path)
    frame[frame["item_id"] != f"{lakefix.ACC['24']}:I.1A:i002"].to_parquet(path, index=False)
    append_record(items.alignment_dir(full_lake), pad.CHECKPOINT_NAME)
    with pytest.raises(items.AlignItemsError, match="do not match the risk-item file"):
        items.require_alignment(full_lake, [ZZZ])


def test_an_alignment_directory_argument_is_honoured_for_the_sidecar_too(full_lake, tmp_path):
    out = tmp_path / "elsewhere"
    items.run_align_items(full_lake, [ZZZ], adjudicate=True, llm=FullLLM(), out_dir=out, pas_params=RELAXED, max_usd=5.0)
    items.require_alignment(full_lake, [ZZZ], out)
    append_record(out, adj.CHECKPOINT_NAME)
    with pytest.raises(items.AlignItemsError, match="run `semigraph align-items`"):
        items.require_alignment(full_lake, [ZZZ], out)


def test_wiping_the_tables_is_still_reported_as_missing_not_as_stale(full_lake):
    buy(full_lake)
    wipe_tables(full_lake)
    with pytest.raises(items.AlignItemsError, match="no risk-item alignment"):
        items.require_alignment(full_lake, [ZZZ])


# --------------------------------------------------------------------------- the pure helpers

def test_a_checkpoint_appended_to_after_it_was_read_cannot_be_digested(tmp_path):
    cp = adj.Checkpoint(tmp_path / "c.jsonl")
    assert prov.checkpoint_digest(cp) is None                                            # no file: no digest
    cp.add({"key": "k1", "verdict": "removed"})
    with pytest.raises(ValueError, match="changed after it was read"):
        prov.checkpoint_digest(cp)
    fresh = adj.Checkpoint(tmp_path / "c.jsonl")
    assert prov.checkpoint_digest(fresh) == {"sha256": sha(tmp_path / "c.jsonl"), "records": 1} and not fresh.stale


def test_a_file_that_appears_after_the_checkpoint_was_read_is_recorded_as_absent_not_as_a_crash(tmp_path):
    """The paid job may start writing between our read and the digest: the tables did not use those answers, so they say 'no file'."""
    path = tmp_path / adj.CHECKPOINT_NAME
    cp = adj.Checkpoint(path)
    path.write_text('{"key": "k1"}\n', encoding="utf-8")                              # the job's first answer arrives
    assert prov.checkpoint_digest(cp) is None and cp.records == {}
    prov.write_provenance(tmp_path, {"ZZZ": {"checkpoints": {adj.CHECKPOINT_NAME: prov.checkpoint_digest(cp)}}})
    assert prov.stale_checkpoints(tmp_path, ["ZZZ"]) == {"ZZZ": [adj.CHECKPOINT_NAME]}        # so the tables are stale, as they should be


def test_write_provenance_merges_tickers_and_ignores_a_sidecar_of_another_schema(tmp_path):
    prov.write_provenance(tmp_path, {"AAA": {"x": 1}})
    prov.write_provenance(tmp_path, {"BBB": {"x": 2}})
    assert prov.read_provenance(tmp_path) == {"AAA": {"x": 1}, "BBB": {"x": 2}}
    (tmp_path / prov.PROVENANCE_NAME).write_text(json.dumps({"schema": 99, "tickers": {"AAA": {}}}), encoding="utf-8")
    assert prov.read_provenance(tmp_path) == {}
    assert not list(tmp_path.glob("*.tmp"))
