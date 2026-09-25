"""estimate_extraction_cost tests — prices come from Settings, not constants.

Numbers are chosen so nothing depends on rounding: 100,000 chunks x 1,200
tokens gives round Mtok counts, hence exact-to-the-cent dollar figures.
"""

import pandas as pd
import pytest

from semigraph.config import Settings
from semigraph.extraction import extractor
from semigraph.extraction.extractor import estimate_extraction_cost

N_CHUNKS = 100_000
TOKENS_PER_CHUNK = 1_200
EXPECTED_KEYS = {
    "n_chunks", "chunk_tokens", "extractor_in_usd", "extractor_out_usd",
    "critic_usd", "likely_usd", "worst_case_usd", "per_ticker",
}


def make_todo(n: int = N_CHUNKS, tokens: int = TOKENS_PER_CHUNK) -> dict[str, pd.DataFrame]:
    half = n // 2
    return {
        "NVDA": pd.DataFrame({"n_tokens": [tokens] * half}),
        "AMD": pd.DataFrame({"n_tokens": [tokens] * (n - half)}),
    }


def make_settings(**prices) -> Settings:
    return Settings(_env_file=None, **prices)


class TestSettingsFields:
    def test_critic_price_defaults_are_haiku_4_5_list_price(self):
        s = make_settings()
        assert s.critic_input_price_per_mtok == 1.0
        assert s.critic_output_price_per_mtok == 5.0

    def test_critic_prices_are_overridable_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("CRITIC_INPUT_PRICE_PER_MTOK", "0.5")
        monkeypatch.setenv("CRITIC_OUTPUT_PRICE_PER_MTOK", "2.5")
        s = Settings(_env_file=None)
        assert (s.critic_input_price_per_mtok, s.critic_output_price_per_mtok) == (0.5, 2.5)


class TestEstimateShape:
    def test_returned_keys_are_unchanged(self):
        est = estimate_extraction_cost(make_todo(), make_settings())
        assert set(est) == EXPECTED_KEYS
        assert est["n_chunks"] == N_CHUNKS
        assert est["chunk_tokens"] == N_CHUNKS * TOKENS_PER_CHUNK
        assert est["per_ticker"] == {"NVDA": N_CHUNKS // 2, "AMD": N_CHUNKS // 2}

    def test_empty_todo_costs_nothing(self):
        est = estimate_extraction_cost({"NVDA": pd.DataFrame({"n_tokens": []})}, make_settings())
        assert est["n_chunks"] == 0 and est["likely_usd"] == 0 and est["worst_case_usd"] == 0

    def test_likely_is_the_sum_and_worst_case_is_one_and_a_half_times(self):
        est = estimate_extraction_cost(make_todo(), make_settings())
        parts = est["extractor_in_usd"] + est["extractor_out_usd"] + est["critic_usd"]
        assert est["likely_usd"] == pytest.approx(parts, abs=0.011)
        assert est["worst_case_usd"] == pytest.approx(est["likely_usd"] * 1.5, abs=0.011)


class TestPricesFollowSettings:
    def test_default_settings_numbers(self):
        # extractor $2/$10, critic $1/$5 per Mtok (the Settings defaults)
        est = estimate_extraction_cost(make_todo(), make_settings())
        # (100k * 800 overhead + 120M chunk tokens) = 200 Mtok in  -> $400
        assert est["extractor_in_usd"] == pytest.approx(400.0)
        # 100k * 300 output tokens = 30 Mtok out -> $300
        assert est["extractor_out_usd"] == pytest.approx(300.0)
        # 25% of chunks: (100k*500 + 120M)=170 Mtok in @ $1 + 4 Mtok out @ $5 -> 0.25 * 190
        assert est["critic_usd"] == pytest.approx(47.5)

    def test_doubling_extractor_input_price_doubles_extractor_in_only(self):
        base = estimate_extraction_cost(make_todo(), make_settings())
        doubled = estimate_extraction_cost(
            make_todo(), make_settings(llm_input_price_per_mtok=4.0)
        )
        assert doubled["extractor_in_usd"] == pytest.approx(2 * base["extractor_in_usd"])
        assert doubled["extractor_out_usd"] == base["extractor_out_usd"]
        assert doubled["critic_usd"] == base["critic_usd"]

    def test_doubling_extractor_output_price_doubles_extractor_out_only(self):
        base = estimate_extraction_cost(make_todo(), make_settings())
        doubled = estimate_extraction_cost(
            make_todo(), make_settings(llm_output_price_per_mtok=20.0)
        )
        assert doubled["extractor_out_usd"] == pytest.approx(2 * base["extractor_out_usd"])
        assert doubled["extractor_in_usd"] == base["extractor_in_usd"]
        assert doubled["critic_usd"] == base["critic_usd"]

    def test_critic_prices_drive_the_critic_line_only(self):
        base = estimate_extraction_cost(make_todo(), make_settings())
        doubled = estimate_extraction_cost(
            make_todo(),
            make_settings(critic_input_price_per_mtok=2.0, critic_output_price_per_mtok=10.0),
        )
        assert doubled["critic_usd"] == pytest.approx(2 * base["critic_usd"])
        assert doubled["extractor_in_usd"] == base["extractor_in_usd"]
        assert doubled["extractor_out_usd"] == base["extractor_out_usd"]

    def test_zero_prices_give_zero_cost(self):
        est = estimate_extraction_cost(
            make_todo(),
            make_settings(
                llm_input_price_per_mtok=0.0, llm_output_price_per_mtok=0.0,
                critic_input_price_per_mtok=0.0, critic_output_price_per_mtok=0.0,
            ),
        )
        assert est["likely_usd"] == 0 and est["worst_case_usd"] == 0

    def test_no_settings_argument_falls_back_to_get_settings(self, monkeypatch):
        custom = make_settings(llm_input_price_per_mtok=6.0)
        monkeypatch.setattr(extractor, "get_settings", lambda: custom)
        est = estimate_extraction_cost(make_todo())
        assert est["extractor_in_usd"] == pytest.approx(1200.0)  # 200 Mtok x $6

    def test_inputs_are_not_mutated(self):
        todo = make_todo(n=10)
        snapshot = {t: df.copy() for t, df in todo.items()}
        estimate_extraction_cost(todo, make_settings())
        for t, df in todo.items():
            pd.testing.assert_frame_equal(df, snapshot[t])
