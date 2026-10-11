"""Agent pipeline: capture bundle -> evidence -> profiles -> verification -> final profile.

The build spec is docs/agent-pipeline-spec.md. Search defaults to FaceCheck face search
(token from `python -m pipeline set-key facecheck`); SEARCH_BACKEND=serpapi uses Yandex
through SerpApi, and `--mock` uses the local mock internet instead.
Jev and the LLM default to fake/off, and `--models` is required for real model calls.
"""

import logging

# trace.py logs under "pipeline"; outside trace.tracing() its records go nowhere, not to stderr.
logging.getLogger("pipeline").addHandler(logging.NullHandler())
