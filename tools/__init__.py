"""Staging and load-test tooling that is not part of the shipped ``semigraph`` package (M5a I5, docs/v2/M5_PLAN.md section 6).

Nothing here is imported by the service. Each subpackage is a stand-alone program (``tools.mockllm`` is the mock LLM
provider the staging fleet is pointed at) and is run from the repository root.
"""
