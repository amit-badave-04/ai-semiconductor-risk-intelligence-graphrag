"""content_hash — the frozen content fingerprint for EvidenceSpan text.

The spec is frozen: any change invalidates every stored hash, so these tests
pin exact digests, not just properties.
"""

import hashlib
import unicodedata

from semigraph.hashing import content_hash


def test_returns_sha256_hex_of_the_normalised_text():
    assert content_hash("Export controls") == hashlib.sha256(b"Export controls").hexdigest()


def test_digest_is_pinned_so_the_spec_cannot_drift_silently():
    # sha256("hello world") — computed independently of the implementation
    assert content_hash("  hello \n\t world\r\n") == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_deterministic_across_calls():
    assert content_hash("same text") == content_hash("same text")


def test_whitespace_runs_collapse_and_ends_are_stripped():
    assert content_hash("a  b") == content_hash("a b")
    assert content_hash("a\n\n\tb") == content_hash("a b")
    assert content_hash("  a b  ") == content_hash("a b")
    assert content_hash("a b") == content_hash("a b")  # NBSP is whitespace after NFKC


def test_unicode_composition_form_does_not_matter():
    composed = unicodedata.normalize("NFC", "café")
    decomposed = unicodedata.normalize("NFD", "café")
    assert composed != decomposed
    assert content_hash(composed) == content_hash(decomposed)


def test_nfkc_folds_compatibility_characters():
    assert content_hash("ﬁnance") == content_hash("finance")  # fi ligature
    assert content_hash("ＡＢ") == content_hash("AB")      # fullwidth letters


def test_case_is_preserved():
    assert content_hash("Entity List") != content_hash("entity list")


def test_different_text_gives_different_hash():
    assert content_hash("H100") != content_hash("H200")


def test_empty_and_blank_text_hash_like_the_empty_string():
    assert content_hash("") == hashlib.sha256(b"").hexdigest()
    assert content_hash("  \n\t ") == content_hash("")
