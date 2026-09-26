"""graph/sentence_embed.py: the per-section sentence embedding cache and the cosine neighbours of a sentence (M1b semantic candidates).

The embedder is a deterministic fake (``embedfix``); nothing here loads a model or touches the lake.
"""

import json

import numpy as np
import pytest
from embedfix import DIM, make_embed
from test_passage_bands import BELOW, BELOW2, DISTRACTORS
from test_passages import P, X, Y

from semigraph.config import get_settings
from semigraph.graph import sentence_embed as se
from semigraph.graph.align_text import split_sentences
from semigraph.hashing import content_hash

OLDER = " ".join([P[0], BELOW[0], BELOW2[0], X[3], P[1]])
NEWER = " ".join([P[0], BELOW[1], BELOW2[1], Y[0], DISTRACTORS[0], P[1]])
ACC = "0000000000-25-000001"


def unit_rows(matrix):
    return np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)


# --------------------------------------------------------------------------- embedding texts

class TestEmbedTexts:
    def test_rows_are_float32_unit_length_and_batched_in_order(self, monkeypatch):
        monkeypatch.setattr(se, "EMBED_BATCH", 2)
        embed = make_embed()
        texts = [P[0], P[1], P[2], X[0], Y[0]]
        out = se.embed_texts(embed, texts)
        assert out.shape == (5, DIM) and out.dtype == np.float32 and unit_rows(out)
        assert [len(b) for b in embed.calls] == [2, 2, 1] and embed.embedded() == texts

    def test_an_unnormalised_embedder_is_normalised_here_so_cosine_is_a_dot_product(self):
        out = se.embed_texts(lambda texts: np.full((len(texts), 4), 3.0), ["a", "b"])
        assert unit_rows(out)

    def test_a_wrong_number_of_rows_is_refused(self):
        with pytest.raises(ValueError, match="rows"):
            se.embed_texts(lambda texts: np.ones((1, 4)), ["a", "b"])

    def test_an_all_zero_vector_stays_zero_instead_of_becoming_nan(self):
        out = se.embed_texts(lambda texts: np.zeros((len(texts), 4)), ["a"])
        assert not np.isnan(out).any()

    def test_no_text_is_no_call(self):
        embed = make_embed()
        assert se.embed_texts(embed, []).shape[0] == 0 and embed.calls == []

    def test_a_very_long_sentence_is_cut_before_it_is_embedded(self):
        embed = make_embed()
        se.embed_texts(embed, ["word " * 2000])
        assert len(embed.embedded()[0]) == se.MAX_EMBED_CHARS


# --------------------------------------------------------------------------- the section matrix and its cache

class TestSectionCache:
    def test_every_sentence_span_gets_a_unit_row_in_text_order(self):
        embed = make_embed()
        m = se.section_matrix(None, ACC, OLDER, embed)
        assert list(m.spans) == split_sentences(OLDER) and m.vectors.shape == (len(m.spans), DIM) and unit_rows(m.vectors)
        assert embed.embedded() == [OLDER[a:b] for a, b in m.spans]
        assert m.row_of(m.spans[2]) == 2 and m.row_of((1, 2)) is None

    def test_the_cache_holds_an_npy_and_a_small_json_index_named_by_accession_and_section_hash(self, tmp_path):
        m = se.section_matrix(tmp_path, ACC, OLDER, make_embed(name="model-a"))
        stem = f"{ACC}_{content_hash(OLDER)[:8]}"
        assert sorted(p.name for p in tmp_path.iterdir()) == [f"{stem}.json", f"{stem}.npy"]
        index = json.loads((tmp_path / f"{stem}.json").read_text(encoding="utf-8"))
        assert index["embedder"] == "model-a" and index["section_sha"] == content_hash(OLDER) and index["n"] == len(m.spans)
        assert index["dim"] == DIM and index["spans"] == [list(s) for s in m.spans]
        assert np.load(tmp_path / f"{stem}.npy").shape == (len(m.spans), DIM)

    def test_a_second_call_reads_the_cache_and_never_embeds_again(self, tmp_path):
        first = se.section_matrix(tmp_path, ACC, OLDER, make_embed())
        embed = make_embed()
        again = se.section_matrix(tmp_path, ACC, OLDER, embed)
        assert embed.calls == [] and np.array_equal(first.vectors, again.vectors) and first.spans == again.spans

    def test_a_changed_section_text_is_embedded_afresh_under_another_file_name(self, tmp_path):
        se.section_matrix(tmp_path, ACC, OLDER, make_embed())
        embed = make_embed()
        changed = OLDER + " " + Y[1]
        m = se.section_matrix(tmp_path, ACC, changed, embed)
        assert embed.calls and len(m.spans) == len(split_sentences(changed))
        assert len(list(tmp_path.glob("*.npy"))) == 2                              # the old file is left alone, never reused
        again = make_embed()
        se.section_matrix(tmp_path, ACC, changed, again)
        assert again.calls == []

    def test_another_embedder_invalidates_the_cache_and_rewrites_it(self, tmp_path):
        se.section_matrix(tmp_path, ACC, OLDER, make_embed(name="model-a"))
        embed = make_embed(name="model-b")
        se.section_matrix(tmp_path, ACC, OLDER, embed)
        assert embed.calls
        later = make_embed(name="model-b")
        se.section_matrix(tmp_path, ACC, OLDER, later)
        assert later.calls == []

    @pytest.mark.parametrize("damage", ["truncate_npy", "garbage_json", "wrong_spans", "wrong_dim", "delete_json", "missing_npy"])
    def test_a_damaged_or_inconsistent_cache_is_rebuilt_never_trusted(self, tmp_path, damage):
        se.section_matrix(tmp_path, ACC, OLDER, make_embed())
        stem = f"{ACC}_{content_hash(OLDER)[:8]}"
        npy, js = tmp_path / f"{stem}.npy", tmp_path / f"{stem}.json"
        index = json.loads(js.read_text(encoding="utf-8"))
        if damage == "truncate_npy":
            npy.write_bytes(npy.read_bytes()[:40])
        elif damage == "garbage_json":
            js.write_text("{not json", encoding="utf-8")
        elif damage == "wrong_spans":                                              # e.g. the sentence splitter changed since
            index["spans"][0][1] += 1
            js.write_text(json.dumps(index), encoding="utf-8")
        elif damage == "wrong_dim":
            index["dim"] = DIM + 1
            js.write_text(json.dumps(index), encoding="utf-8")
        elif damage == "delete_json":                                              # the index is the commit marker: written last
            js.unlink()
        else:
            npy.unlink()
        embed = make_embed()
        m = se.section_matrix(tmp_path, ACC, OLDER, embed)
        assert embed.calls and m.vectors.shape[1] == DIM
        healed = make_embed()
        se.section_matrix(tmp_path, ACC, OLDER, healed)
        assert healed.calls == []

    def test_no_temp_files_are_left_behind(self, tmp_path):
        se.section_matrix(tmp_path, ACC, OLDER, make_embed())
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_an_empty_section_has_no_rows_and_calls_nothing(self, tmp_path):
        embed = make_embed()
        m = se.section_matrix(tmp_path, ACC, "   ", embed)
        assert m.vectors.shape[0] == 0 and m.spans == () and embed.calls == []

    def test_a_cache_directory_that_does_not_exist_yet_is_created(self, tmp_path):
        target = tmp_path / "a" / "b"
        se.section_matrix(target, ACC, OLDER, make_embed())
        assert any(target.glob("*.npy"))


# --------------------------------------------------------------------------- ranking

class TestRank:
    def matrix(self):
        return se.section_matrix(None, ACC, NEWER, make_embed())

    def test_the_paraphrase_outranks_unrelated_sentences_and_cosines_descend(self):
        m, embed = self.matrix(), make_embed()
        query = se.embed_texts(embed, [BELOW[0]])[0]
        ranked = m.rank(query, 4, min_chars=40)
        assert NEWER[ranked[0].start:ranked[0].end] == BELOW[1]
        assert [n.cosine for n in ranked] == sorted((n.cosine for n in ranked), reverse=True) and len(ranked) == 4

    def test_short_sentences_are_not_neighbours(self):
        text = "Hong Kong. " + NEWER
        m = se.section_matrix(None, ACC, text, make_embed())
        assert (0, 10) in m.spans                                                  # indexed ...
        ranked = m.rank(se.embed_texts(make_embed(), ["Hong Kong."])[0], 50, min_chars=40)
        assert all(n.end - n.start >= 40 for n in ranked)                          # ... but never ranked

    def test_ties_go_to_the_earlier_sentence_and_the_result_is_deterministic(self):
        m = se.section_matrix(None, ACC, P[0] + "\n" + P[0] + "\n" + Y[0], make_embed())          # the same sentence twice
        query = se.embed_texts(make_embed(), [P[0]])[0]
        a, b = m.rank(query, 3, min_chars=40), m.rank(query, 3, min_chars=40)
        assert a == b and a[0].start < a[1].start and a[0].cosine == a[1].cosine

    def test_count_is_capped_by_the_number_of_sentences_and_zero_rows_give_nothing(self):
        m = self.matrix()
        query = se.embed_texts(make_embed(), [X[0]])[0]
        assert len(m.rank(query, 1000, min_chars=40)) == len([s for s in m.spans if s[1] - s[0] >= 40])
        empty = se.section_matrix(None, ACC, "", make_embed())
        assert empty.rank(query, 5, min_chars=40) == []


# --------------------------------------------------------------------------- neighbours across a pair

class TestPairNeighbours:
    def make(self, tmp_path, embed=None):
        embed = embed or make_embed()
        return se.PairNeighbours(("ACC-OLD", OLDER), ("ACC-NEW", NEWER), embed, tmp_path, min_chars=40), embed

    def span(self, text, sentence):
        start = text.index(sentence)
        return start, start + len(sentence)

    def test_nothing_is_embedded_until_a_neighbour_is_asked_for(self, tmp_path):
        _, embed = self.make(tmp_path)
        assert embed.calls == [] and list(tmp_path.iterdir()) == []

    def test_the_neighbours_of_an_older_sentence_are_sentences_of_the_newer_section_best_first(self, tmp_path):
        nb, _ = self.make(tmp_path)
        a, b = self.span(OLDER, BELOW[0])
        got = nb.neighbours("older", a, b, BELOW[0], 3)
        assert len(got) == 3 and NEWER[got[0].start:got[0].end] == BELOW[1]
        assert all(isinstance(g, se.Neighbour) for g in got)

    def test_the_neighbours_of_a_newer_sentence_are_sentences_of_the_older_section(self, tmp_path):
        nb, _ = self.make(tmp_path)
        a, b = self.span(NEWER, BELOW2[1])
        got = nb.neighbours("newer", a, b, BELOW2[1], 2)
        assert OLDER[got[0].start:got[0].end] == BELOW2[0]

    def test_the_query_vector_comes_from_the_own_sections_rows_so_no_sentence_is_embedded_twice(self, tmp_path):
        nb, embed = self.make(tmp_path)
        a, b = self.span(OLDER, BELOW[0])
        nb.neighbours("older", a, b, BELOW[0], 3)
        flat = embed.embedded()
        assert flat.count(BELOW[0]) == 1 and len(flat) == len(split_sentences(OLDER)) + len(split_sentences(NEWER))

    def test_a_sentence_that_is_not_a_section_span_is_embedded_on_the_fly_once(self, tmp_path):
        nb, embed = self.make(tmp_path)
        odd = BELOW[0][:-1]                                                            # not one of the section's exact spans
        a = OLDER.index(BELOW[0])
        first = nb.neighbours("older", a, a + len(odd), odd, 3)
        n_before = len(embed.embedded())
        again = nb.neighbours("older", a, a + len(odd), odd, 3)
        assert embed.embedded().count(odd) == 1 and len(embed.embedded()) == n_before and first == again
        assert NEWER[first[0].start:first[0].end] == BELOW[1]

    def test_a_second_pair_object_reads_the_disk_cache_and_embeds_nothing_for_a_cached_sentence(self, tmp_path):
        nb, _ = self.make(tmp_path)
        a, b = self.span(OLDER, BELOW[0])
        first = nb.neighbours("older", a, b, BELOW[0], 3)
        nb2, embed2 = self.make(tmp_path)
        assert nb2.neighbours("older", a, b, BELOW[0], 3) == first and embed2.calls == []

    def test_the_sections_are_cached_under_their_own_accession_and_hash(self, tmp_path):
        nb, _ = self.make(tmp_path)
        a, b = self.span(OLDER, BELOW[0])
        nb.neighbours("older", a, b, BELOW[0], 1)
        names = sorted(p.name for p in tmp_path.glob("*.npy"))
        assert names == sorted([f"ACC-OLD_{content_hash(OLDER)[:8]}.npy", f"ACC-NEW_{content_hash(NEWER)[:8]}.npy"])

    def test_an_unknown_side_is_refused(self, tmp_path):
        nb, _ = self.make(tmp_path)
        with pytest.raises(ValueError, match="side"):
            nb.neighbours("middle", 0, 5, "text", 1)


# --------------------------------------------------------------------------- the adapter over the project embedder

class TestSentenceEmbedder:
    def test_it_is_lazy_names_the_configured_model_and_uses_passage_side_encoding(self, monkeypatch):
        seen = {}

        class FakeEmbedder:
            def __init__(self, model_name=None, backend=None):
                seen["init"] = (model_name, backend)

            def encode_passages(self, texts, batch_size=8, show_progress=False):
                seen["encode"] = (list(texts), batch_size)
                return np.ones((len(texts), 4), dtype=np.float32)

            def encode_query(self, question):
                raise AssertionError("sentences are encoded as passages, never with the query instruction")

        monkeypatch.setattr("semigraph.embeddings.Embedder", FakeEmbedder)
        adapter = se.SentenceEmbedder(backend="local", batch_size=16)
        assert seen == {} and adapter.name == f"local:{get_settings().embedding_model}"      # naming the model does not load it
        assert se.SentenceEmbedder(backend="ONNX").name.startswith("onnx:") and se.SentenceEmbedder()._batch_size == 8
        out = adapter(["a sentence", "another"])
        assert out.shape == (2, 4) and seen["init"] == (None, "local") and seen["encode"] == (["a sentence", "another"], 16)
        adapter(["third"])
        assert seen["encode"][0] == ["third"]

    def test_it_plugs_into_the_section_matrix_and_its_cache_is_named_by_the_configured_model(self, tmp_path):
        fake = make_embed()

        class Stub:
            def encode_passages(self, texts, batch_size=8, show_progress=False):
                return fake(list(texts))

        adapter = se.SentenceEmbedder(embedder_factory=Stub)
        m = se.section_matrix(tmp_path, ACC, OLDER, adapter)
        assert m.vectors.shape[0] == len(split_sentences(OLDER))
        (index,) = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*.json")]
        assert index["embedder"] == adapter.name
