"""The staging mock LLM provider (OpenAI-compatible) and its calibration.

* ``server``   FastAPI app: chat completions (stream and not), models, metrics, knobs
* ``answers``  drafts the real verifier accepts, built from the prompt
* ``reply``    what one request is answered with (text or tool call, tokens, timings)
* ``planner``  tool-calling mode for the agent planner
* ``profile``  timing and length distributions (``profiles.json``)
* ``calibrate`` writes ``profiles.json`` from recorded runs (v2e eval rows, the S12 smoke, agent runs)

Run it: ``python -m tools.mockllm`` (see ``deploy/staging/Dockerfile.mockllm``). Importing the package imports nothing heavy.
"""
