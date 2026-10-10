"""Agent pipeline: capture bundle -> evidence -> profiles -> verification -> final profile.

The build spec is docs/agent-pipeline-spec.md. Defaults are offline: mock search,
fake Jev, and no LLM. `--live` and `--models` are the only switches that allow
external calls.
"""
