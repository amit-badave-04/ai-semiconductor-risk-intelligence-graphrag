"""Tests for the pure chunking core (semigraph.parsing.chunker).

Zero network, zero LLM, zero Neo4j — synthetic section rows only.
(tiktoken's cl100k_base BPE is read from the local on-disk cache.)
"""

import pandas as pd
import pytest

from semigraph.parsing.chunker import (
    CHUNK_COLUMNS,
    MAX_TOKENS,
    SEP,
    TARGET_TOKENS,
    chunk_filing_elements,
    n_tokens,
    split_oversized,
)

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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
