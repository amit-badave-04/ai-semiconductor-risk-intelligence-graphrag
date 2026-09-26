"""Two tiny text helpers shared by the answer checks (verify.py) and the removal-claim detector (removal_claims.py)."""

# Straight and typographic apostrophes: "doesn't" / "doesn’t" / "doesnʼt".
A = "['’‘ʼ]"


def plain_spaces(text: str) -> str:
    """A no-break space (U+00A0) or narrow no-break space (U+202F) reads as a plain one: "$5<nbsp>billion" is an amount."""
    return text.replace("\u00a0", " ").replace("\u202f", " ")
