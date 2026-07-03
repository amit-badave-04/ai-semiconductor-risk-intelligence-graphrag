"""Packaged artifacts: graph schema DDL, canonical entity dictionary,
gold benchmark, prompt templates. Loaded via importlib.resources so they
work from an installed wheel, not just a source checkout."""

import json
from importlib.resources import files


def _root():
    return files(__name__)


def read_schema_cypher() -> str:
    return _root().joinpath("schema.cypher").read_text(encoding="utf-8")


def load_canonical_entities() -> dict:
    return json.loads(
        _root().joinpath("canonical_entities.json").read_text(encoding="utf-8")
    )


def load_benchmark() -> list[dict]:
    return json.loads(
        _root().joinpath("benchmark.json").read_text(encoding="utf-8")
    )


def read_prompt(name: str) -> str:
    """Read a prompt template by stem, e.g. read_prompt("extractor")."""
    return _root().joinpath("prompts", f"{name}.txt").read_text(encoding="utf-8")
