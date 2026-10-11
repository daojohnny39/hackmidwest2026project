"""Bind Member 3 as one Strands tool, with backend-owned dependencies."""

from pydantic import ValidationError

from .contracts import CandidateBatch
from .pipeline import analyze_candidates


def create_candidate_tool(*, store, scope: str, analyzer=None, provider=None, deadline_seconds=120.0):
    """Create an async tool for Member 1's existing agent; starts no agent/model.

    Scope, credentials and storage never appear in the model's tool arguments.
    The caller owns the lifecycle of any injected provider.
    """
    from strands import tool

    schema = CandidateBatch.model_json_schema(by_alias=True)
    definitions = schema.pop("$defs", {})

    @tool(name="analyze_candidates", inputSchema={"json": {
        "type": "object", "properties": {"request": schema}, "required": ["request"],
        "additionalProperties": False, "$defs": definitions,
    }})
    async def candidate_tool(request: dict) -> dict:
        """Analyze authorized candidates and rank contextual relevance, preserving review items.

        Args:
            request: Version 1 candidate batch from Member 2; image asset IDs must already be stored.

        Returns:
            Structured BatchResult JSON, or a sanitized tool error for an invalid batch.
            Contextual labels do not establish facial identity or source trustworthiness.
        """
        try:
            result = await analyze_candidates(request, store=store, scope=scope, analyzer=analyzer,
                                              provider=provider, deadline_seconds=deadline_seconds)
        except ValidationError:
            return {"status": "error", "content": [{"json": {
                "code": "invalid_candidate_batch", "message": "Candidate input does not satisfy the version 1 contract.",
            }}]}
        except Exception:
            return {"status": "error", "content": [{"json": {
                "code": "candidate_pipeline_failed", "message": "Candidate processing could not be completed.",
            }}]}
        return {"status": "success", "content": [{"json": result}]}

    return candidate_tool
