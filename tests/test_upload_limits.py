"""I4a / R18 + OD11: the upload limits and the production validators (docs/specs/m2-search.md, decision 009).

Provisional values (marked in config.py) are fixed after the $0 re-measure; these tests pin the values in force now and the
validator boundaries, which do not depend on the measurement.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_serve_config_production import EMBED_TIMEOUT, live, named

from semigraph import config
from semigraph.config import Settings

MIB = 1024 * 1024


def test_defaults_follow_od11():
    s = Settings(_env_file=None)
    # HIGH 2: the gated values stay at today's until the sandbox measurements and body streaming land (OD11).
    assert s.upload_max_bytes == 15 * MIB
    assert (s.upload_max_pages, s.upload_max_workspace_pages) == (30, 120)
    assert s.upload_max_tokens == 160_000
    assert s.upload_max_chunks == config.UPLOAD_MAX_CHUNKS_PROVISIONAL == 600
    assert s.upload_guaranteed_embed_tokens == 19_900
    assert s.upload_embed_timeout_s == 3600
    assert s.upload_embed_jobs_per_day == 23
    assert s.max_uploads_per_day == 40
    assert s.upload_max_workspace_tokens == 48_000
    assert s.upload_max_documents == 3
    assert s.uploads_per_hour == 10
    assert s.upload_min_mem_available_kb == 1_250_000


@pytest.mark.parametrize("workspace_pages,boots", [(300, True), (299, False), (120, False)])
def test_workspace_pages_must_cover_the_pdf_page_limit(workspace_pages, boots):
    """R18: the workspace page limit is at least the PDF page limit, in every environment."""
    kwargs = {"_env_file": None, "environment": "development", "upload_max_pages": 300,
              "upload_max_workspace_pages": workspace_pages}
    if boots:
        assert Settings(**kwargs).upload_max_workspace_pages == workspace_pages
        return
    with pytest.raises(ValidationError) as refused:
        Settings(**kwargs)
    assert "UPLOAD_MAX_WORKSPACE_PAGES" in str(refused.value) and str(workspace_pages) not in str(refused.value)


def test_live_defaults_boot_in_production():
    s = live()
    assert s.upload_guaranteed_embed_tokens <= int(s.upload_embed_timeout_s * config.PRODUCTION_WORST_EMBED_TOKENS_PER_S)
    assert s.upload_embed_timeout_s * s.upload_embed_jobs_per_day <= config.SECONDS_PER_DAY


@pytest.mark.parametrize("tokens,boots", [(19_944, True), (19_945, False), (19_900, True), (30_000, False)])
def test_guaranteed_tokens_within_ceiling_times_floor(tokens, boots):
    """floor(3,600 x 5.54) = 19,944: the production validator binds the guaranteed figure, not upload_max_tokens."""
    if boots:
        assert live(upload_guaranteed_embed_tokens=tokens).upload_guaranteed_embed_tokens == tokens
        return
    with pytest.raises(ValidationError) as refused:
        live(upload_guaranteed_embed_tokens=tokens)
    assert named(refused) == [EMBED_TIMEOUT]


def test_upload_max_tokens_is_no_longer_bound_by_the_ceiling():
    assert live(upload_max_tokens=160_000).upload_max_tokens == 160_000
    assert live(upload_max_tokens=1_000_000).upload_max_tokens == 1_000_000


@pytest.mark.parametrize("jobs,boots", [(23, True), (24, True), (25, False)])
def test_day_rule_counts_embed_jobs(jobs, boots):
    """86,400 / 3,600 = 24: the strict rule passes at 24 with zero margin; the default of 23 keeps the overrun margin."""
    if boots:
        assert live(upload_embed_jobs_per_day=jobs).upload_embed_jobs_per_day == jobs
        return
    with pytest.raises(ValidationError) as refused:
        live(upload_embed_jobs_per_day=jobs)
    assert named(refused) == [EMBED_TIMEOUT]


def test_uploads_per_day_no_longer_enters_the_day_rule():
    assert live(max_uploads_per_day=500).max_uploads_per_day == 500


def test_the_ceiling_floor_still_applies():
    with pytest.raises(ValidationError) as refused:
        live(upload_embed_timeout_s=599)
    assert named(refused) == [EMBED_TIMEOUT]


def test_refusal_prints_no_value():
    with pytest.raises(ValidationError) as refused:
        live(upload_guaranteed_embed_tokens=31_337)
    text = str(refused.value)
    assert "input_value" not in text and "31337" not in text and "31,337" not in text


def test_outside_production_anything_boots():
    s = Settings(_env_file=None, environment="development", upload_guaranteed_embed_tokens=10**7,
                 upload_embed_jobs_per_day=10**6)
    assert s.upload_guaranteed_embed_tokens == 10**7


def test_docx_uncompressed_cap():
    """A regression pin: OD11 leaves the DOCX zip caps unchanged (they are raised only if a measured real filing exceeds them)."""
    from semigraph.uploads import gate

    assert (gate.DOCX_MAX_UNCOMPRESSED_BYTES, gate.DOCX_MAX_MEMBERS, gate.DOCX_MAX_COMPRESSION_RATIO) == (60 * MIB, 200, 120.0)


def test_the_memory_guard_and_the_parse_sandbox_are_unchanged():
    from semigraph.uploads import sandbox

    assert Settings(_env_file=None).upload_min_mem_available_kb == 1_250_000
    assert Settings(_env_file=None).upload_parse_timeout_s == 90
    assert sandbox._RLIMIT_CPU_SECONDS == 120
    assert config.PRODUCTION_UPLOAD_MIN_MEM_AVAILABLE_KB == (500_000, 2_000_000)


def test_the_gate_still_refuses_a_zip_bomb():
    import io
    import zipfile

    from semigraph.uploads import gate

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"0" * (5 * MIB))          # compresses far past the 120x ratio cap
    with pytest.raises(gate.GateError) as bomb:
        gate.check_docx_zip(buffer.getvalue())
    assert bomb.value.code == "zip_bomb"
