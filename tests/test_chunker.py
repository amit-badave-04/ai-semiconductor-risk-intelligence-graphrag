"""Tests for the pure chunking core (semigraph.parsing.chunker).

Zero network, zero LLM, zero Neo4j — synthetic section rows only.
(tiktoken's cl100k_base BPE is read from the local on-disk cache.)
"""

import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from semigraph.config import Settings
from semigraph.ingestion.edgar import edgar_dir, manifest_path
from semigraph.parsing.chunker import (
    CHUNK_COLUMNS,
    MAX_TOKENS,
    SECTION_TEXT_COLUMNS,
    SEP,
    TARGET_TOKENS,
    chunk_filing_elements,
    chunk_filings,
    chunks_path_for,
    n_tokens,
    section_texts_path_for,
    split_oversized,
)
from semigraph.parsing.segmentation import sections_dir

META = {
    "ticker": "TEST",
    "cik": 1234567,
    "form": "10-K",
    "filing_date": "2024-06-30",
    "accession_no": "0001234567-24-000001",
    "source_url": "https://www.sec.gov/Archives/test.htm",
}


def make_elements(rows, section_id="I.1A", section_title="Item 1A. Risk Factors"):
    """Build a segmentation-output DataFrame from (element_type, text) pairs."""
    return pd.DataFrame(
        [
            {
                "element_index": i,
                "section_id": section_id,
                "section_title": section_title,
                "element_type": et,
                "text": text,
            }
            for i, (et, text) in enumerate(rows)
        ]
    )


def sentence(i: int) -> str:
    return f"Risk paragraph {i} describes supply chain exposure in some detail."


def para(i: int, n_sentences: int = 5) -> str:
    return " ".join(sentence(i * 100 + j) for j in range(n_sentences))


def section_text_of(st_rows, section_id="I.1A"):
    return next(r["text"] for r in st_rows if r["section_id"] == section_id)


# ---------------------------------------------------------------- packing

class TestParagraphPacking:
    def test_small_paragraphs_pack_into_one_chunk(self):
        paras = [para(i) for i in range(3)]  # well under TARGET_TOKENS total
        st, chunks = chunk_filing_elements(
            META, make_elements([("TextElement", p) for p in paras])
        )
        assert len(chunks) == 1
        assert chunks[0]["kind"] == "prose"
        assert chunks[0]["text"] == SEP.join(paras)  # whole elements, SEP-joined

    def test_packing_respects_target_then_starts_new_chunk(self):
        paras = [para(i, n_sentences=8) for i in range(30)]
        st, chunks = chunk_filing_elements(
            META, make_elements([("TextElement", p) for p in paras])
        )
        assert len(chunks) > 1
        # every non-final chunk was flushed because it reached the target
        for c in chunks[:-1]:
            assert c["n_tokens"] >= TARGET_TOKENS
        # nothing lost: chunks tile the section text in order
        full = section_text_of(st)
        assert chunks[0]["char_start"] == 0
        assert chunks[-1]["char_end"] == len(full)

    def test_chunks_never_cross_section_boundaries(self):
        df = pd.concat(
            [
                make_elements(
                    [("TextElement", para(i, 8)) for i in range(10)],
                    section_id="I.1",
                    section_title="Item 1. Business",
                ),
                make_elements(
                    [("TextElement", para(50 + i, 8)) for i in range(10)],
                    section_id="I.1A",
                ),
            ],
            ignore_index=True,
        )
        df["element_index"] = range(len(df))
        st, chunks = chunk_filing_elements(META, df)
        assert {c["section_id"] for c in chunks} == {"I.1", "I.1A"}
        for c in chunks:
            assert ":" + c["section_id"] + ":" in c["chunk_id"]


# ------------------------------------------------------------- tables

class TestTableAwareness:
    def test_table_is_standalone_chunk(self):
        table = "Region | Revenue\nAmericas | 100\nEMEA | 50"
        st, chunks = chunk_filing_elements(
            META,
            make_elements(
                [
                    ("TextElement", para(1)),
                    ("TableElement", table),
                    ("TextElement", para(2)),
                ]
            ),
        )
        kinds = [c["kind"] for c in chunks]
        assert kinds == ["prose", "table", "prose"]
        table_chunk = chunks[1]
        assert table_chunk["text"] == table  # never merged, never split
        full = section_text_of(st)
        assert full[table_chunk["char_start"] : table_chunk["char_end"]] == table

    def test_table_keeps_its_heading_as_sub_heading(self):
        """A table is not severed from its header: the preceding TitleElement
        rides along as the table chunk's sub_heading metadata."""
        st, chunks = chunk_filing_elements(
            META,
            make_elements(
                [
                    ("TextElement", para(1)),
                    ("TitleElement", "Purchase Obligations by Fiscal Year"),
                    ("TableElement", "FY25 | $10B\nFY26 | $12B"),
                ]
            ),
        )
        table_chunk = next(c for c in chunks if c["kind"] == "table")
        assert table_chunk["sub_heading"] == "Purchase Obligations by Fiscal Year"
        # headings are metadata, never baked into chunk text
        assert "Purchase Obligations" not in table_chunk["text"]

    def test_sub_heading_applies_to_following_prose(self):
        st, chunks = chunk_filing_elements(
            META,
            make_elements(
                [
                    ("TitleElement", "Dependence on Third-Party Foundries"),
                    ("TextElement", para(1)),
                ]
            ),
        )
        assert chunks[0]["sub_heading"] == "Dependence on Third-Party Foundries"


# ------------------------------------------------------------ token limits

class TestTokenLimits:
    def test_oversized_element_is_sentence_split(self):
        big = " ".join(sentence(i) for i in range(200))  # far over MAX_TOKENS
        assert n_tokens(big) > MAX_TOKENS
        st, chunks = chunk_filing_elements(
            META, make_elements([("TextElement", big)])
        )
        assert len(chunks) > 1
        for c in chunks:
            assert c["n_tokens"] <= MAX_TOKENS + 50  # notebook 04's assertion

    def test_split_oversized_offsets_are_exact(self):
        text = " ".join(sentence(i) for i in range(150))
        abs_start = 37
        pieces = split_oversized(text, abs_start)
        assert len(pieces) > 1
        for piece, ps, pe in pieces:
            assert n_tokens(piece) <= MAX_TOKENS
            # offsets are absolute and index the piece within the element
            assert text[ps - abs_start : pe - abs_start] == piece

    def test_all_prose_chunks_under_hard_cap(self):
        rows = [("TextElement", para(i, 12)) for i in range(20)]
        rows.insert(5, ("TextElement", " ".join(sentence(i) for i in range(180))))
        st, chunks = chunk_filing_elements(META, make_elements(rows))
        for c in chunks:
            if c["kind"] == "prose":
                assert c["n_tokens"] <= MAX_TOKENS + 50


# ------------------------------------------------------------- provenance

class TestCharOffsets:
    def test_every_chunk_is_exact_substring_at_offsets(self):
        rows = [
            ("TitleElement", "Supply Constraints"),
            ("TextElement", para(1, 10)),
            ("TableElement", "A | 1\nB | 2"),
            ("TextElement", " ".join(sentence(i) for i in range(170))),  # oversized
            ("TextElement", para(2, 6)),
        ]
        st, chunks = chunk_filing_elements(META, make_elements(rows))
        full = section_text_of(st)
        assert chunks, "expected chunks"
        for c in chunks:
            assert full[c["char_start"] : c["char_end"]] == c["text"]

    def test_section_text_is_sep_joined_elements(self):
        paras = [para(1), para(2)]
        st, _ = chunk_filing_elements(
            META, make_elements([("TextElement", p) for p in paras])
        )
        assert section_text_of(st) == SEP.join(paras)
        assert st[0]["n_chars"] == len(SEP.join(paras))


# ---------------------------------------------------------- ids & schema

class TestChunkIds:
    def test_chunk_id_format_and_determinism(self):
        rows = [("TextElement", para(i, 8)) for i in range(15)]
        df = make_elements(rows)
        _, first = chunk_filing_elements(META, df)
        _, second = chunk_filing_elements(META, df)
        ids = [c["chunk_id"] for c in first]
        assert ids == [c["chunk_id"] for c in second]  # deterministic
        assert len(ids) == len(set(ids))  # unique
        for i, cid in enumerate(ids):
            assert cid == f"{META['accession_no']}:I.1A:{i:04d}"  # per-section seq

    def test_seq_resets_per_section(self):
        df = pd.concat(
            [
                make_elements([("TextElement", para(1))], section_id="I.1",
                              section_title="Item 1. Business"),
                make_elements([("TextElement", para(2))], section_id="I.1A"),
            ],
            ignore_index=True,
        )
        df["element_index"] = range(len(df))
        _, chunks = chunk_filing_elements(META, df)
        assert [c["chunk_id"] for c in chunks] == [
            f"{META['accession_no']}:I.1:0000",
            f"{META['accession_no']}:I.1A:0000",
        ]

    def test_chunk_rows_match_parquet_schema(self):
        _, chunks = chunk_filing_elements(
            META, make_elements([("TextElement", para(1))])
        )
        assert list(chunks[0].keys()) == CHUNK_COLUMNS

    def test_non_keep_sections_are_dropped(self):
        df = make_elements(
            [("TextElement", para(1))], section_id="II.9A",
            section_title="Item 9A. Controls",
        )
        st, chunks = chunk_filing_elements(META, df)
        assert st == [] and chunks == []

    def test_empty_input_never_crashes(self):
        st, chunks = chunk_filing_elements(META, pd.DataFrame())
        assert st == [] and chunks == []


# ------------------------------------------------ chunk_filings: append-only

OLD_ACC = "0001234567-23-000009"   # 10-K already chunked in the (legacy) data lake
NEW_10Q = "0001234567-24-000050"   # 10-Q that lands later
NEW_10K = "0001234567-24-000090"   # another later filing


def make_lake(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data")


def write_sections(
    settings: Settings, accession: str, sections: list[tuple[str, int]]
) -> None:
    """Write a filing's interim segmentation parquet. ``sections`` is a list
    of ``(section_id, n_paragraphs)``."""
    frames = [
        make_elements(
            [("TextElement", para(k, 8)) for k in range(n_paras)],
            section_id=section_id, section_title=f"title {section_id}",
        )
        for section_id, n_paras in sections
    ]
    df = pd.concat(frames, ignore_index=True)
    df["element_index"] = range(len(df))
    sections_dir(settings).mkdir(parents=True, exist_ok=True)
    df.to_parquet(sections_dir(settings) / f"{accession}.parquet", index=False)


def add_filing(
    settings: Settings,
    ticker: str,
    accession: str,
    form: str,
    filing_date: str,
    sections: list[tuple[str, int]] | None,
) -> None:
    """Register a filing in the manifest and (unless ``sections`` is None)
    write its interim segmentation parquet — the state the real pipeline has
    after ``download_filings`` + ``segment_filings``."""
    path = manifest_path(settings)
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    manifest.setdefault(ticker, []).append({
        "ticker": ticker, "cik": 1234567, "form": form,
        "filing_date": filing_date, "accession_no": accession,
        "source_url": f"https://www.sec.gov/Archives/{accession}.htm",
    })
    edgar_dir(settings).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if sections is not None:
        write_sections(settings, accession, sections)


def seed_legacy_chunks(settings: Settings, ticker: str) -> pd.DataFrame:
    """A pre-existing chunk parquet with notebook-04 style ids: the seq is the
    GLOBAL row index (offset here, never restarting per section), so it does
    not match what ``chunk_filing_elements`` would regenerate."""
    rows = []
    for gidx, (section_id, sub) in enumerate(
        [("I.1", None), ("I.1", "Overview"), ("I.1A", None), ("I.1A", "Supply"), ("II.7", None)]
    ):
        text = f"Legacy chunk {gidx} text about foundry supply."
        rows.append({
            "chunk_id": f"{OLD_ACC}:{section_id}:{gidx + 17:04d}",
            "ticker": ticker, "cik": 1234567, "form": "10-K",
            "filing_date": "2023-02-24", "accession_no": OLD_ACC,
            "section_id": section_id, "section_title": f"title {section_id}",
            "sub_heading": sub, "kind": "prose", "text": text,
            "char_start": gidx * 10, "char_end": gidx * 10 + len(text),
            "n_tokens": n_tokens(text), "source_url": "https://www.sec.gov/legacy.htm",
        })
    df = pd.DataFrame(rows, columns=CHUNK_COLUMNS)
    path = chunks_path_for(settings, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df


def read_chunks(settings: Settings, ticker: str) -> pd.DataFrame:
    return pd.read_parquet(chunks_path_for(settings, ticker))


class TestChunkFilingsFirstRun:
    def test_ticker_with_no_parquet_yet_is_chunked_from_scratch(self, tmp_path):
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24",
                   [("I.1", 12), ("I.1A", 12)])

        summary = chunk_filings(settings, ["TEST"])["TEST"]

        chunks = read_chunks(settings, "TEST")
        assert list(chunks.columns) == CHUNK_COLUMNS
        assert summary["chunks"] == len(chunks) > 2
        assert summary["new_chunks"] == len(chunks)
        assert summary["new_accessions"] == [OLD_ACC]
        assert summary["cached"] is False
        assert summary["sections"] == 2
        assert summary["tokens"] == int(chunks["n_tokens"].sum())
        assert summary["warnings"] == []
        st = pd.read_parquet(section_texts_path_for(settings, "TEST"))
        assert list(st.columns) == SECTION_TEXT_COLUMNS and len(st) == 2

    def test_new_ids_use_accession_section_seq_with_four_digits(self, tmp_path):
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24",
                   [("I.1", 12), ("I.1A", 12)])
        chunk_filings(settings, ["TEST"])
        ids = read_chunks(settings, "TEST")["chunk_id"].tolist()
        assert len(ids) == len(set(ids))
        for section_id in ("I.1", "I.1A"):
            seqs = [i for i in ids if f":{section_id}:" in i]
            assert seqs == [f"{OLD_ACC}:{section_id}:{k:04d}" for k in range(len(seqs))]
            assert len(seqs) > 1  # the seq really counts per section

    def test_no_manifest_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="manifest"):
            chunk_filings(make_lake(tmp_path), ["TEST"])

    def test_existing_but_empty_chunks_parquet_is_rebuilt(self, tmp_path):
        """A filer whose earlier run yielded no chunks (0-row parquet) must be
        chunked once segmentation output exists — old behaviour preserved."""
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24", [("I.1A", 12)])
        chunks_path_for(settings, "TEST").parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=CHUNK_COLUMNS).to_parquet(
            chunks_path_for(settings, "TEST"), index=False
        )
        summary = chunk_filings(settings, ["TEST"])["TEST"]
        assert summary["new_accessions"] == [OLD_ACC]
        assert len(read_chunks(settings, "TEST")) == summary["chunks"] > 0


class TestAppendOnlyChunking:
    def _seeded(self, tmp_path):
        """A ticker chunked once (10-K), then a new 10-Q arrives."""
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24",
                   [("I.1", 12), ("I.1A", 12)])
        chunk_filings(settings, ["TEST"])
        add_filing(settings, "TEST", NEW_10Q, "10-Q", "2024-05-20",
                   [("I.2", 12), ("II.1A", 12)])
        return settings

    def test_new_filing_is_appended_and_existing_rows_are_identical(self, tmp_path):
        settings = self._seeded(tmp_path)
        before = read_chunks(settings, "TEST")
        before_st = pd.read_parquet(section_texts_path_for(settings, "TEST"))

        summary = chunk_filings(settings, ["TEST"])["TEST"]

        after = read_chunks(settings, "TEST")
        n_old = len(before)
        # every pre-existing row — every column, dtype and position — unchanged
        pd.testing.assert_frame_equal(after.iloc[:n_old].reset_index(drop=True), before)
        assert after["chunk_id"].iloc[:n_old].tolist() == before["chunk_id"].tolist()
        # new rows come strictly after, all from the new accession
        new_rows = after.iloc[n_old:]
        assert len(new_rows) > 0
        assert set(new_rows["accession_no"]) == {NEW_10Q}
        assert new_rows["chunk_id"].is_unique
        assert not set(new_rows["chunk_id"]) & set(before["chunk_id"])
        assert list(after.columns) == CHUNK_COLUMNS
        # section texts appended the same way
        after_st = pd.read_parquet(section_texts_path_for(settings, "TEST"))
        pd.testing.assert_frame_equal(
            after_st.iloc[: len(before_st)].reset_index(drop=True), before_st
        )
        assert set(after_st["accession_no"].iloc[len(before_st):]) == {NEW_10Q}
        # summary
        assert summary["chunks"] == len(after)
        assert summary["new_chunks"] == len(new_rows)
        assert summary["new_accessions"] == [NEW_10Q]
        assert summary["sections"] == len(after_st) == 4
        assert summary["tokens"] == int(after["n_tokens"].sum())
        assert summary["cached"] is False

    def test_appended_chunks_are_exact_substrings_of_their_section_text(self, tmp_path):
        settings = self._seeded(tmp_path)
        chunk_filings(settings, ["TEST"])
        chunks = read_chunks(settings, "TEST")
        st = pd.read_parquet(section_texts_path_for(settings, "TEST"))
        lookup = {(r.accession_no, r.section_id): r.text for r in st.itertuples()}
        new = chunks[chunks["accession_no"] == NEW_10Q]
        assert len(new) > 0
        for r in new.itertuples():
            assert lookup[(r.accession_no, r.section_id)][r.char_start : r.char_end] == r.text

    def test_rerun_is_idempotent_and_does_not_touch_files(self, tmp_path):
        settings = self._seeded(tmp_path)
        chunk_filings(settings, ["TEST"])
        chunks_file = chunks_path_for(settings, "TEST")
        st_file = section_texts_path_for(settings, "TEST")
        chunk_bytes, st_bytes = chunks_file.read_bytes(), st_file.read_bytes()

        summary = chunk_filings(settings, ["TEST"])["TEST"]

        assert summary["cached"] is True
        assert summary["new_chunks"] == 0
        assert summary["new_accessions"] == []
        assert summary["chunks"] == len(read_chunks(settings, "TEST"))
        assert chunks_file.read_bytes() == chunk_bytes
        assert st_file.read_bytes() == st_bytes

    def test_several_new_accessions_append_in_manifest_order(self, tmp_path):
        settings = self._seeded(tmp_path)
        add_filing(settings, "TEST", NEW_10K, "10-K", "2024-06-30", [("I.1A", 12)])
        summary = chunk_filings(settings, ["TEST"])["TEST"]
        assert summary["new_accessions"] == [NEW_10Q, NEW_10K]
        order = read_chunks(settings, "TEST")["accession_no"].drop_duplicates().tolist()
        assert order == [OLD_ACC, NEW_10Q, NEW_10K]

    def test_two_step_growth_keeps_first_step_rows_identical(self, tmp_path):
        settings = self._seeded(tmp_path)
        chunk_filings(settings, ["TEST"])
        step1 = read_chunks(settings, "TEST")
        add_filing(settings, "TEST", NEW_10K, "10-K", "2024-06-30", [("I.1A", 12)])
        chunk_filings(settings, ["TEST"])
        step2 = read_chunks(settings, "TEST")
        pd.testing.assert_frame_equal(step2.iloc[: len(step1)].reset_index(drop=True), step1)
        assert len(step2) > len(step1)

    def test_no_temp_files_left_behind(self, tmp_path):
        settings = self._seeded(tmp_path)
        chunk_filings(settings, ["TEST"])
        leftovers = [
            p for d in (settings.chunks_dir, settings.interim_dir / "section_texts")
            for p in d.iterdir() if p.suffix != ".parquet"
        ]
        assert leftovers == []


class TestLegacyIdPreservation:
    def test_nvda_legacy_ids_survive_appending_a_new_filing(self, tmp_path):
        """nvda_chunks.parquet carries legacy ids (they do not follow the
        per-section seq); seeded citations and cached benchmark answers key on
        them, so they must never change."""
        settings = make_lake(tmp_path)
        legacy = seed_legacy_chunks(settings, "NVDA")
        assert legacy["chunk_id"].tolist()[0].endswith(":I.1:0017")  # not per-section seq
        add_filing(settings, "NVDA", OLD_ACC, "10-K", "2023-02-24", [("I.1", 12)])
        add_filing(settings, "NVDA", NEW_10Q, "10-Q", "2024-05-20", [("II.1A", 12)])
        legacy_file = chunks_path_for(settings, "NVDA")
        assert legacy_file.name == "nvda_chunks.parquet"

        summary = chunk_filings(settings, ["NVDA"])["NVDA"]

        after = pd.read_parquet(legacy_file)
        pd.testing.assert_frame_equal(after.iloc[: len(legacy)].reset_index(drop=True), legacy)
        # the already-chunked 10-K is NOT re-chunked under the new convention
        assert summary["new_accessions"] == [NEW_10Q]
        assert set(after.iloc[len(legacy):]["accession_no"]) == {NEW_10Q}
        assert set(after["accession_no"]) == {OLD_ACC, NEW_10Q}
        assert after["chunk_id"].is_unique

    def test_legacy_column_dtypes_are_preserved(self, tmp_path):
        settings = make_lake(tmp_path)
        legacy = seed_legacy_chunks(settings, "NVDA")
        legacy = legacy.astype({"n_tokens": "int32", "char_start": "int32"})
        legacy.to_parquet(chunks_path_for(settings, "NVDA"), index=False)
        add_filing(settings, "NVDA", NEW_10Q, "10-Q", "2024-05-20", [("II.1A", 12)])

        chunk_filings(settings, ["NVDA"])

        after = read_chunks(settings, "NVDA")
        assert after["n_tokens"].dtype == "int32" and after["char_start"].dtype == "int32"
        pd.testing.assert_frame_equal(after.iloc[: len(legacy)].reset_index(drop=True), legacy)

    def test_legacy_ticker_without_new_filings_is_left_untouched(self, tmp_path):
        settings = make_lake(tmp_path)
        seed_legacy_chunks(settings, "NVDA")
        add_filing(settings, "NVDA", OLD_ACC, "10-K", "2023-02-24", [("I.1", 12)])
        before = chunks_path_for(settings, "NVDA").read_bytes()

        summary = chunk_filings(settings, ["NVDA"])["NVDA"]

        assert summary["cached"] is True and summary["new_chunks"] == 0
        assert summary["chunks"] == 5
        assert chunks_path_for(settings, "NVDA").read_bytes() == before

    def test_existing_parquet_with_wrong_schema_fails_loudly(self, tmp_path):
        settings = make_lake(tmp_path)
        chunks_path_for(settings, "TEST").parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"chunk_id": ["x"], "text": ["y"]}).to_parquet(
            chunks_path_for(settings, "TEST"), index=False
        )
        add_filing(settings, "TEST", NEW_10Q, "10-Q", "2024-05-20", [("II.1A", 12)])
        with pytest.raises(ValueError, match="columns"):
            chunk_filings(settings, ["TEST"])


class TestAccessionsThatYieldNothing:
    def test_no_keep_sections_warns_and_adds_nothing(self, tmp_path, caplog):
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24", [("I.1A", 12)])
        chunk_filings(settings, ["TEST"])
        before = read_chunks(settings, "TEST")
        # ASML-style: segmentation produced only a non-keep section
        add_filing(settings, "TEST", NEW_10Q, "10-Q", "2024-05-20", [("II.9A", 5)])

        with caplog.at_level(logging.WARNING, logger="semigraph.parsing.chunker"):
            summary = chunk_filings(settings, ["TEST"])["TEST"]

        assert summary["new_chunks"] == 0 and summary["new_accessions"] == []
        assert summary["cached"] is True
        assert len(summary["warnings"]) == 1 and NEW_10Q in summary["warnings"][0]
        assert any("no keep-sections" in r.message for r in caplog.records)
        pd.testing.assert_frame_equal(read_chunks(settings, "TEST"), before)

    def test_empty_accession_reruns_cleanly_without_looping(self, tmp_path):
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24", [("II.9A", 5)])
        first = chunk_filings(settings, ["TEST"])["TEST"]
        second = chunk_filings(settings, ["TEST"])["TEST"]
        for summary in (first, second):
            assert summary["chunks"] == 0 and summary["new_chunks"] == 0
            assert len(summary["warnings"]) == 1
        assert len(read_chunks(settings, "TEST")) == 0

    def test_one_empty_accession_does_not_block_the_others(self, tmp_path):
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24", [("I.1A", 12)])
        chunk_filings(settings, ["TEST"])
        add_filing(settings, "TEST", NEW_10Q, "10-Q", "2024-05-20", [("II.9A", 5)])
        add_filing(settings, "TEST", NEW_10K, "10-K", "2024-06-30", [("I.1A", 12)])
        summary = chunk_filings(settings, ["TEST"])["TEST"]
        assert summary["new_accessions"] == [NEW_10K]
        assert len(summary["warnings"]) == 1

    def test_missing_sections_parquet_warns_then_chunks_once_segmented(self, tmp_path):
        settings = make_lake(tmp_path)
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24", [("I.1A", 12)])
        chunk_filings(settings, ["TEST"])
        add_filing(settings, "TEST", NEW_10Q, "10-Q", "2024-05-20", None)  # not segmented yet

        pending = chunk_filings(settings, ["TEST"])["TEST"]
        assert pending["new_chunks"] == 0
        assert any("segment_filings" in w for w in pending["warnings"])

        write_sections(settings, NEW_10Q, [("II.1A", 12)])  # segmentation lands
        done = chunk_filings(settings, ["TEST"])["TEST"]
        assert done["new_accessions"] == [NEW_10Q]
        assert done["warnings"] == []

    def test_section_texts_parquet_missing_for_existing_chunks_still_appends(self, tmp_path):
        settings = make_lake(tmp_path)
        legacy = seed_legacy_chunks(settings, "TEST")
        add_filing(settings, "TEST", OLD_ACC, "10-K", "2023-02-24", [("I.1", 12)])
        add_filing(settings, "TEST", NEW_10Q, "10-Q", "2024-05-20", [("II.1A", 12)])
        assert not section_texts_path_for(settings, "TEST").exists()

        summary = chunk_filings(settings, ["TEST"])["TEST"]

        assert summary["new_accessions"] == [NEW_10Q]
        assert len(read_chunks(settings, "TEST")) == len(legacy) + summary["new_chunks"]
        st = pd.read_parquet(section_texts_path_for(settings, "TEST"))
        assert set(st["accession_no"]) == {NEW_10Q}  # only what we actually chunked


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
