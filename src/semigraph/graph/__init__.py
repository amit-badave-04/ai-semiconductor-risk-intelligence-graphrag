"""Neo4j graph layer: client, schema DDL, loaders, bitemporal closure.

``loaders`` and ``temporal`` pull in pandas (pipeline-only); they are resolved
lazily so the web service image — which only needs ``client`` and ``schema`` —
does not have to ship pandas/pyarrow.
"""

from importlib import import_module

from .client import get_driver, run_cypher
from .schema import apply_schema

__all__ = ["apply_schema", "get_driver", "loaders", "run_cypher", "temporal"]


def __getattr__(name):
    if name in ("loaders", "temporal"):
        return import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
