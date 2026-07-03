"""semigraph — AI Semiconductor Risk Intelligence GraphRAG SDK.

Refactored from notebooks 01-14 (M0-M6, benchmark-verified). The notebooks
remain the historical record; this package is the supported interface.

Typical use:

    from semigraph.config import get_settings
    from semigraph.embeddings import Embedder
    from semigraph.graph.client import get_driver
    from semigraph.retrieval.answerer import answer
"""

__version__ = "0.1.0"
