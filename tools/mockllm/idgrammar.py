"""The citation-id grammar of the mock: ``src/semigraph/retrieval/ids.py`` itself, loaded BY FILE PATH.

``import semigraph.retrieval.ids`` is not an option: the ``semigraph.retrieval`` package ``__init__`` imports the answerer
(LiteLLM, the retriever, ...), and the mock's image holds none of that. ``ids.py`` itself imports only ``re``, so it is
executed as a stand-alone module. That keeps ONE definition of what a citation id is, which is the whole point of the
mock: it must cite ids the real verifier accepts, and the verifier reads them with this very grammar.

Where the file is looked for: the ``MOCKLLM_IDS_PY`` environment variable, else ``<repository root>/src/semigraph/
retrieval/ids.py``. ``deploy/staging/Dockerfile.mockllm`` copies the file to that same relative place, so the lookup is
the same in a checkout and in the image.
"""

import importlib.util
import os
from pathlib import Path
from types import ModuleType

IDS_ENV = "MOCKLLM_IDS_PY"
_RELATIVE = Path("src") / "semigraph" / "retrieval" / "ids.py"


def locate_ids_file(env: dict[str, str] | None = None, root: Path | None = None) -> Path:
    """The ``ids.py`` to load. Raises ``FileNotFoundError`` naming every place it looked."""
    env = os.environ if env is None else env
    root = root if root is not None else Path(__file__).resolve().parents[2]
    candidates = ([Path(env[IDS_ENV])] if env.get(IDS_ENV) else []) + [root / _RELATIVE]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("the citation-id grammar (retrieval/ids.py) was not found; looked in: "
                            + ", ".join(str(p) for p in candidates))


def load_ids_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("mockllm_ids_grammar", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the citation-id grammar from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ids = load_ids_module(locate_ids_file())

CITE_RE = _ids.CITE_RE
CHUNK_ID_PATTERN = _ids.CHUNK_ID_PATTERN
DOC_ID_PATTERN = _ids.DOC_ID_PATTERN
classify_id = _ids.classify_id
