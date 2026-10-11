"""Per-image Strands agents with a scripted stub model (spec 1.3, 1.8).

ScriptedModel is a real strands Model that plays a fixed policy by reading the tool
results in the conversation. No provider is constructed and no external call is
made; the agents drive the real tools against a local mock internet.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from strands.models import Model

from pipeline.capture import load_capture
from pipeline.contracts import CaptureArtifact
from pipeline.image_agent import make_agent_runner, system_prompt
from pipeline.search.mock_backend import MockSearchBackend
from pipeline.stage1_collect import collect_evidence
from pipeline.trace import tracing
from tests.support import FIXTURE_CAPTURES, MockServerTestCase, read_trace

ARCHIVE = "https://archive.test/speakers/alex-morgan-2021"


def tool_results(messages):
    """Parsed JSON results from the latest user turn."""
    results = []
    for block in messages[-1].get("content", []) if messages and messages[-1]["role"] == "user" else []:
        if "toolResult" in block:
            for item in block["toolResult"]["content"]:
                try:
                    results.append(json.loads(item.get("text", "")))
                except ValueError:
                    pass
    return results


class ScriptedModel(Model):
    """Search, fetch every hit, follow depth-0 profiles, then judge every fetched page."""

    def __init__(self, *, exclude=(), bad_triage=False, crash=False, sneak_url=None, skip_search=False):
        self.exclude, self.bad_triage, self.crash, self.sneak_url = set(exclude), bad_triage, crash, sneak_url
        self.skip_search = skip_search
        self.phase = 0
        self.hit_urls: list[str] = []
        self.hits: list[dict] = []
        self.fetched: dict[str, dict] = {}
        self.sneak_result = None
        self.config = {}

    def update_config(self, **config):
        self.config.update(config)

    def get_config(self):
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("agents use the structured-output tool")
        yield  # pragma: no cover

    def next_calls(self, messages):
        for result in tool_results(messages):
            if "hits" in result:
                self.hits = result["hits"]
                self.hit_urls = [hit["url"] for hit in result["hits"]]
            for page in result.get("pages", [result]):
                if "url" in page and "kind" in page:
                    self.fetched[page["url"]] = page
            if self.sneak_url and result.get("error", "").startswith("not allowed"):
                self.sneak_result = result
        self.phase += 1
        if self.skip_search:
            return [("ImageTriage", {"judgments": []})]
        if self.phase == 1:
            return [("reverse_image_search", {})]
        if self.phase == 2:
            calls = [("fetch_page", {"url": url}) for url in self.hit_urls]
            return calls + ([("fetch_page", {"url": self.sneak_url})] if self.sneak_url else [])
        if self.phase == 3:
            profiles = [url for url, page in self.fetched.items() if page["kind"] == "profile" and page["depth"] == 0]
            if profiles:
                return [("follow_account_links", {"url": url}) for url in profiles]
            self.phase += 1
        if self.bad_triage:
            return [("ImageTriage", {"judgments": "not a list"})]
        judgments = [{"url": url, "relevant": url not in self.exclude,
                      "note": "unrelated speaker page" if url in self.exclude else "public page from image search"}
                     for url in self.fetched]
        return [("ImageTriage", {"judgments": judgments})]

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        if self.crash:
            raise RuntimeError("model unavailable")
        calls = self.next_calls(messages)
        yield {"messageStart": {"role": "assistant"}}
        for index, (name, args) in enumerate(calls):
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"call-{self.phase}-{index}", "name": name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}}


class ImageAgentTests(MockServerTestCase):
    def capture(self, name="alex"):
        loaded = load_capture(FIXTURE_CAPTURES / name)
        return CaptureArtifact(run_id=f"{loaded.bundle.session_id}-0000abcd", capture=loaded.bundle,
                               capture_dir=str(loaded.capture_dir), backend="mock", scenario="default",
                               llm_mode="anthropic", jev_mode="fake")

    def collect(self, models, name="alex"):
        queue = list(models)
        agent = make_agent_runner(lambda: queue.pop(0))
        bundle, store = asyncio.run(collect_evidence(self.capture(name), MockSearchBackend(self.mock_url), agent=agent))
        return bundle, store

    def test_one_isolated_agent_per_image_with_shared_fetches(self):
        models = [ScriptedModel(exclude={ARCHIVE}), ScriptedModel(), ScriptedModel(exclude={ARCHIVE})]
        bundle, store = self.collect(models)

        # X images -> X agents, each with its own model, each seeing only its own image's hits.
        self.assertEqual(len({id(model) for model in models}), 3)
        self.assertEqual([len(model.hit_urls) for model in models], [4, 4, 5])
        self.assertEqual([image.collection_method for image in bundle.images], ["agent"] * 3)
        self.assertEqual(self.mock_count("/api/reverse-image"), 3)

        # Three agents fetched overlapping pages, but each URL reached the server once.
        self.assertEqual(self.mock_count("/fetch"), store.fetch_count)
        self.assertEqual(store.fetch_count, 10)

        # The agent's judgment excludes the archive page without losing the observation.
        archive = bundle.pages[ARCHIVE]
        self.assertFalse(archive.relevant)
        self.assertIn("img-3 excluded: unrelated speaker page", archive.notes)
        self.assertNotIn(ARCHIVE, bundle.images[2].page_urls)
        self.assertTrue(bundle.pages["https://chatter.test/alexbuilds"].relevant)
        self.assertEqual(sum(page.page_kind == "profile" and page.relevant for page in bundle.pages.values()), 5)

    def test_face_search_hits_carry_face_scores(self):
        from pipeline.search.facecheck_backend import to_payload
        from tests.test_stage1 import FakeBackend, live_capture, page_html, yandex

        a, b = "https://github.com/pat", "https://lookalike.example/x"
        output = {"items": [{"score": 55, "url": {"value": b}}, {"score": 92, "url": {"value": a}}]}
        pages = {a: page_html(title="Pat"), b: page_html()}
        for payload, expected in ((to_payload("s1", output, demo=False), [(a, 92), (b, 55)]),
                                  (yandex(a, b), [(a, None), (b, None)])):
            with self.subTest(face_search=expected[0][1] is not None):
                model = ScriptedModel()
                asyncio.run(collect_evidence(live_capture("img-1"), FakeBackend({"img-1": payload}, pages),
                                             agent=make_agent_runner(lambda: model)))
                self.assertEqual([(hit["url"], hit.get("face_score")) for hit in model.hits], expected)
        from pipeline.search.facecheck_backend import FaceCheckBackend

        self.assertIn("under 70 as likely lookalikes", system_prompt(FaceCheckBackend))
        self.assertIn("No hit has a face_score", system_prompt(FakeBackend({}, {})))
        self.assertNotIn("{", system_prompt(FakeBackend({}, {})))

    def test_tools_refuse_urls_outside_the_image(self):
        other_image_hit = "https://photogram.test/alexbuilds"  # img-2's hit, not img-1's
        models = [ScriptedModel(sneak_url=other_image_hit), ScriptedModel(), ScriptedModel()]
        bundle, _ = self.collect(models)
        self.assertIsNotNone(models[0].sneak_result)
        img1 = bundle.images[0]
        self.assertNotIn(other_image_hit, img1.page_urls)
        self.assertNotIn("img-1", {via.image_id for via in bundle.pages[other_image_hit].found_via})

    def test_failures_fall_back_without_repeating_the_search(self):
        models = [ScriptedModel(skip_search=True), ScriptedModel(bad_triage=True), ScriptedModel(crash=True)]
        bundle, _ = self.collect(models)
        methods = {image.image_id: image.collection_method for image in bundle.images}
        self.assertEqual(methods, {"img-1": "fallback", "img-2": "fallback", "img-3": "fallback"})
        reasons = [image.search_extras["fallback_reason"] for image in bundle.images]
        self.assertEqual(reasons, ["no_search", "invalid_output", "agent_error"])
        # Deterministic triage over stored results still gives the full default evidence.
        self.assertEqual(self.mock_count("/api/reverse-image"), 3)
        self.assertEqual(len(bundle.pages), 9)
        self.assertTrue(all(page.relevant for page in bundle.pages.values()))

    def test_trace_records_each_conversation_and_why_agents_fell_back(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with tracing(folder, 1):
            self.collect([ScriptedModel(), ScriptedModel(crash=True), ScriptedModel(bad_triage=True)])
        lines = read_trace(folder)

        def of(agent, event):
            return [line for line in lines if line.get("agent") == agent and line["event"] == event]

        # img-1: prompts, every tool call and result, the structured triage, and usage.
        [start] = of("collector-img-1", "agent_start")
        self.assertEqual(start["tools"], ["reverse_image_search", "fetch_page", "follow_account_links"])
        self.assertIn("Call reverse_image_search once.", start["system_prompt"])
        prompt, call, result = of("collector-img-1", "agent_message")[:3]
        self.assertIn("Collect the public pages for image img-1", prompt["content"][0]["text"])
        self.assertEqual(call["content"][0]["toolUse"]["name"], "reverse_image_search")
        self.assertIn('"hits"', result["content"][0]["toolResult"]["content"][0]["text"])
        self.assertEqual({line["tool"] for line in of("collector-img-1", "tool_call")},
                         {"reverse_image_search", "fetch_page", "follow_account_links", "ImageTriage"})
        [end] = of("collector-img-1", "agent_end")
        self.assertTrue(end["structured_output"]["judgments"])
        self.assertGreater(end["usage"]["totalTokens"], 0)

        # img-2: the model error, and the fallback that names it.
        [failed] = of("collector-img-2", "model_call_failed")
        self.assertEqual(failed["error"]["chain"][0]["message"], "model unavailable")
        fallbacks = {line["image"]: line for line in lines if line["event"] == "image_agent_failed"}
        self.assertEqual({image: line["reason"] for image, line in fallbacks.items()},
                         {"img-2": "agent_error", "img-3": "invalid_output"})
        self.assertEqual(fallbacks["img-2"]["error"]["chain"][0]["type"], "RuntimeError")

        # img-3: the schema errors the model was shown.
        errors = [block["toolResult"]["content"][0]["text"] for line in of("collector-img-3", "agent_message")
                  for block in line["content"] if block.get("toolResult", {}).get("status") == "error"]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("Input should be a valid list" in error for error in errors))

        self.assertEqual({line["image"]: line["method"] for line in lines if line["event"] == "image_done"},
                         {"img-1": "agent", "img-2": "fallback", "img-3": "fallback"})
        self.assertEqual(len([line for line in lines if line["event"] == "search"]), 3)
