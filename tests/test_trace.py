"""The per-run debug trace (trace.py): stage records, exception chains, scrubbing, library
log records, and Strands agent conversations including reasoning."""

import io
import json
import logging
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

import httpx
from strands import Agent
from strands.models import Model

from pipeline import trace
from pipeline.search.base import BackendError
from pipeline.trace import MAX_TEXT, TRACE_FILE, tracing
from tests.support import OfflineTestCase, read_trace


def search_with_key():
    """Raises the way serpapi_backend does: a bare code, the httpx error hidden with `from None`."""
    try:
        raise httpx.ConnectError("cannot reach https://serpapi.com/search.json?engine=yandex_images&api_key=sk-live-1234567890")
    except httpx.HTTPError:
        raise BackendError("connection_error") from None


class ThinkingModel(Model):
    """Streams one reasoning block (with a signature) and one text block, then ends the turn."""

    config = {"model_id": "test-thinker"}

    def update_config(self, **config):
        self.config.update(config)

    def get_config(self):
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "The LinkedIn page names the same employer."}}}}
        yield {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "opaque-signature-blob"}}}}
        yield {"contentBlockStop": {}}
        yield {"contentBlockDelta": {"delta": {"text": "Same person."}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10}, "metrics": {"latencyMs": 2}}}


class TraceTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def events(self, name=None):
        return [line for line in read_trace(self.folder) if name is None or line["event"] == name]

    def test_nothing_is_written_or_printed_outside_tracing(self):
        err = io.StringIO()
        with redirect_stderr(err):
            trace.event("search", url="https://example.com/")
            trace.error("search_failed", RuntimeError("boom"))
        self.assertFalse((self.folder / TRACE_FILE).exists())
        self.assertEqual(err.getvalue(), "")

    def test_stage_records_and_logging_state_is_restored(self):
        root = logging.getLogger()
        level, handlers = root.level, list(root.handlers)
        with tracing(self.folder, 2, name="profiles and graph"):
            trace.event("jev_request", provider="fake", empty=None)
        self.assertEqual((root.level, root.handlers), (level, handlers))
        start, request, end = self.events()
        self.assertEqual((start["event"], start["stage"], start["level"], start["name"]),
                         ("stage_start", 2, "info", "profiles and graph"))
        self.assertEqual(request, {**request, "event": "jev_request", "stage": 2, "provider": "fake"})
        self.assertNotIn("empty", request)  # None fields are left out
        self.assertEqual((end["event"], end["outcome"]), ("stage_end", "ok"))
        self.assertIsInstance(end["duration_ms"], float)

    def test_a_failing_stage_records_its_exception_and_reraises(self):
        with self.assertRaises(KeyError), tracing(self.folder, 3):
            {}["missing"]
        [end] = self.events("stage_end")
        self.assertEqual((end["level"], end["outcome"]), ("error", "error"))
        [error] = end["error"]["chain"]
        self.assertEqual((error["type"], error["message"]), ("KeyError", "'missing'"))
        self.assertTrue(error["at"].startswith("tests/test_trace.py:"), error["at"])
        self.assertIn("KeyError: 'missing'", end["error"]["traceback"])

    def test_a_cause_hidden_with_from_none_is_recovered_without_its_secrets(self):
        with tracing(self.folder, 1):
            try:
                search_with_key()
            except BackendError as error:
                trace.error("search_failed", error, reason=error.reason)
        [line] = self.events("search_failed")
        outer, cause = line["error"]["chain"]
        self.assertEqual((outer["type"], outer["message"]), ("pipeline.search.base.BackendError", "connection_error"))
        self.assertEqual(cause["type"], "httpx.ConnectError")
        self.assertTrue(cause["message"].startswith("cannot reach https://serpapi.com/search.json?"))
        self.assertIn("in search_with_key", cause["at"])
        self.assertIn("httpx.ConnectError", line["error"]["traceback"])
        self.assertNotIn("sk-live-1234567890", (self.folder / TRACE_FILE).read_text())

    def test_secrets_are_scrubbed_everywhere(self):
        secrets = ["serp-secret-123", "sk-ant-abcdef123456", "AKIAABCDEFGHIJKLMNOP", "bearer-token-xyz",
                   "0f1e2d3c4b5a69", "0123456789abcdef0123456789abcdef", "sig-value"]
        with tracing(self.folder, 1):
            trace.event("search", url="https://serpapi.com/search.json?api_key=serp-secret-123&engine=yandex_images",
                        headers={"Authorization": "Bearer bearer-token-xyz", "x-api-key": "sk-ant-abcdef123456"},
                        note="AWS key AKIAABCDEFGHIJKLMNOP",
                        tunnel="0f1e2d3c4b5a69.lhr.life tunneled, https://0f1e2d3c4b5a69.lhr.life/0123456789abcdef0123456789abcdef.jpg",
                        content=[{"reasoningText": {"text": "thinking", "signature": "sig-value"}}])
            logging.getLogger("httpx").info("HTTP Request: GET %s", "https://serpapi.com/search.json?api_key=serp-secret-123")
        text = (self.folder / TRACE_FILE).read_text()
        for secret in secrets:
            self.assertNotIn(secret, text)
        [line] = self.events("search")
        self.assertEqual(line["content"][0]["reasoningText"]["text"], "thinking")
        self.assertIn("[REDACTED].lhr.life", line["tunnel"])

    def test_long_strings_are_cut(self):
        with tracing(self.folder, 2):
            trace.event("jev_request", state={"text": "x" * (MAX_TEXT + 5)})
        [line] = self.events("jev_request")
        self.assertEqual(line["state"]["text"], "x" * MAX_TEXT + "... [5 more characters]")

    def test_library_records_are_kept_and_library_warnings_still_print(self):
        err = io.StringIO()
        with redirect_stderr(err), tracing(self.folder, 1):
            logging.getLogger("strands.event_loop").info("cycle started")
            logging.getLogger("strands.models").warning("context window overflow")
            logging.getLogger("strands.models").debug("request=<...>")  # below INFO: dropped
            trace.error("image_agent_failed", RuntimeError("handled"), reason="agent_error")
        logs = self.events("log")
        self.assertEqual([(line["logger"], line["level"], line["message"]) for line in logs],
                         [("strands.event_loop", "info", "cycle started"),
                          ("strands.models", "warning", "context window overflow")])
        # Library warnings still reach stderr as before; the pipeline's own handled errors don't.
        self.assertEqual(err.getvalue(), "context window overflow\n")
        self.assertEqual(len(self.events("image_agent_failed")), 1)

    def test_agent_conversation_reasoning_and_usage(self):
        agent = Agent(model=ThinkingModel(), callback_handler=None, system_prompt="Compare two accounts.",
                      name="tester")
        trace.watch_agent(agent)
        with tracing(self.folder, 2):
            agent("Are these the same person?")
        start = self.events("agent_start")[0]
        self.assertEqual((start["agent"], start["model"], start["system_prompt"]),
                         ("tester", "test-thinker", "Compare two accounts."))
        prompt, reply = self.events("agent_message")
        self.assertEqual((prompt["role"], prompt["content"]), ("user", [{"text": "Are these the same person?"}]))
        reasoning, text = reply["content"]
        self.assertEqual(reasoning["reasoningContent"]["reasoningText"],
                         {"text": "The LinkedIn page names the same employer.", "signature": "[REDACTED]"})
        self.assertEqual(text, {"text": "Same person."})
        [call] = self.events("model_call")
        self.assertEqual(call["stop_reason"], "end_turn")
        [end] = self.events("agent_end")
        self.assertEqual((end["outcome"], end["stop_reason"], end["cycles"], end["usage"]["totalTokens"]),
                         ("ok", "end_turn", 1, 10))
        json.dumps(end)  # every line is plain JSON
