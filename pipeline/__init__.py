"""Agent pipeline: capture bundle -> evidence -> profiles -> verification -> final profile.

The build spec is docs/agent-pipeline-spec.md. Search defaults to backend V3: SerpApi web
search with faces checked by Amazon Rekognition (keys from `python -m pipeline set-key serpapi`
and `set-key aws`). SEARCH_BACKEND=facecheck uses FaceCheck face search, SEARCH_BACKEND=serpapi
uses Yandex alone through SerpApi, and `--mock` uses the local mock internet instead.
Jev and the LLM default to fake/off, and `--models` is required for real model calls.
"""

import logging

# trace.py logs under "pipeline"; outside trace.tracing() its records go nowhere, not to stderr.
logging.getLogger("pipeline").addHandler(logging.NullHandler())
