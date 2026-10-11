"""Jev adapter (spec 2.0): question checks, answer validation, routes, and failures.

No test reaches a provider: real routes run against httpx/httpx2 mock transports
or an injected responder.
"""

import json
import tempfile
from pathlib import Path

import httpx
import httpx2

from pipeline.config import load_settings
from pipeline.jev import (
    Jev, JevAnswerError, KeyCheckFailed, KeyRejected, check_questions, check_typesafe_key, choice, noul, score,
    validate_answers,
)
from pipeline.trace import tracing
from tests.support import OfflineTestCase, read_trace

QUESTIONS = {
    "same_a1_a2": noul("Do accounts a1 and a2 belong to the same real person?",
                       {"true": "Same person.", "false": "Different people."}),
    "best": choice("Which candidate is the person?", {"p1": "Candidate p1", "none": "No candidate"}),
    "fit": score("How consistent are the accounts?", ["low", "medium", "high"]),
}
GOOD = {
    "same_a1_a2": {"type": "noul", "noul": 0.91},
    "best": {"type": "choice", "choice": "p1", "confidence": 0.8, "probabilities": {"p1": 0.9, "none": 0.1}},
    "fit": {"type": "score", "score": 1.7, "confidence": 0.6, "probabilities": {"0": 0.05, "1": 0.2, "2": 0.75},
            "legend": {"0": "low", "1": "medium", "2": "high"}},
}


def settings(mode="fake", **env):
    keys = {"typesafe": {"TYPESAFE_API_KEY": "ts-test-key"}, "openrouter": {"OPENROUTER_API_KEY": "or-test-key"}}
    environ = {"JEV_MODE": mode, **keys.get(mode, {}), **env}
    return load_settings(backend="mock", models=mode in keys, environ=environ, dotenv_path=None)


class QuestionAndAnswerValidationTests(OfflineTestCase):
    def test_builders_produce_wire_format(self):
        self.assertEqual(noul("Q?"), {"type": "noul", "instructions": "Q?"})
        self.assertEqual(QUESTIONS["fit"]["criteria"], ["low", "medium", "high"])
        check_questions(QUESTIONS)

    def test_malformed_questions_raise(self):
        for bad in ({}, {"q": {"type": "maybe", "instructions": "?"}}, {"q": noul("")},
                    {"q": choice("?", {"only": None})}, {"q": score("?", ["one"])},
                    {"q": {"type": "noul", "instructions": "?", "criteria": {"yes": "x"}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_questions(bad)

    def test_valid_answers_are_normalized(self):
        answers = validate_answers(QUESTIONS, GOOD)
        self.assertEqual(answers["same_a1_a2"], {"type": "noul", "noul": 0.91})
        self.assertEqual(answers["best"]["probabilities"], {"p1": 0.9, "none": 0.1})
        self.assertEqual(answers["fit"]["probabilities"], {"0": 0.05, "1": 0.2, "2": 0.75})
        self.assertNotIn("legend", answers["fit"])

    def test_missing_options_count_as_zero(self):
        answers = validate_answers({"best": QUESTIONS["best"]},
                                   {"best": {"type": "choice", "choice": "p1", "confidence": 1, "probabilities": {"p1": 1}}})
        self.assertEqual(answers["best"]["probabilities"], {"p1": 1.0, "none": 0.0})

    def test_bad_answers_are_rejected(self):
        def with_(qid, **changes):
            return {**GOOD, qid: {**GOOD[qid], **changes}}

        cases = {
            "missing": {k: v for k, v in GOOD.items() if k != "fit"},
            "extra": {**GOOD, "other": {"type": "noul", "noul": 0.5}},
            "wrong type": with_("same_a1_a2", type="choice"),
            "noul above 1": with_("same_a1_a2", noul=1.2),
            "noul NaN": with_("same_a1_a2", noul=float("nan")),
            "noul bool": with_("same_a1_a2", noul=True),
            "noul string": with_("same_a1_a2", noul="0.9"),
            "unknown choice": with_("best", choice="p9"),
            "probability for unknown option": with_("best", probabilities={"p1": 0.5, "p2": 0.5}),
            "probabilities don't sum to 1": with_("best", probabilities={"p1": 0.5, "none": 0.1}),
            "score out of range": with_("fit", score=2.5),
            "score level out of range": with_("fit", probabilities={"0": 0.5, "3": 0.5}),
            "confidence missing": {**GOOD, "best": {k: v for k, v in GOOD["best"].items() if k != "confidence"}},
            "not an object": {**GOOD, "best": "p1"},
        }
        for name, answers in cases.items():
            with self.subTest(name), self.assertRaises(JevAnswerError):
                validate_answers(QUESTIONS, answers)
        with self.assertRaises(JevAnswerError):
            validate_answers(QUESTIONS, ["not", "a", "dict"])


class JevClientTests(OfflineTestCase):
    def test_off_mode_has_no_client(self):
        with self.assertRaises(ValueError):
            Jev(settings("off"))

    def test_real_mode_needs_models_flag(self):
        plain = load_settings(backend="mock", environ={"JEV_MODE": "fake"}, dotenv_path=None)
        real = settings("typesafe")
        object.__setattr__(real, "models", False)  # simulate a caller that skipped the gate
        Jev(plain)
        with self.assertRaises(ValueError):
            Jev(real)

    def test_fake_answers_go_through_validation(self):
        jev = Jev(settings("fake"))
        self.assertFalse(jev.real)
        call = jev.ask({"accounts": {}}, {"same_a1_a2": QUESTIONS["same_a1_a2"]},
                       fake_answers={"same_a1_a2": {"type": "noul", "noul": 0.95}})
        self.assertIsNone(call.error)
        self.assertEqual((call.provider, call.model), ("fake", None))
        self.assertEqual(call.answers, {"same_a1_a2": {"type": "noul", "noul": 0.95}})

        bad = jev.ask({}, {"same_a1_a2": QUESTIONS["same_a1_a2"]}, fake_answers={})
        self.assertEqual((bad.error, bad.answers), ("invalid_response", {}))

    def test_responder_failures_become_error_codes(self):
        cases = [
            (lambda s, q: (_ for _ in ()).throw(TimeoutError()), "timeout"),
            (lambda s, q: (_ for _ in ()).throw(ConnectionError()), "connection_error"),
            (lambda s, q: (_ for _ in ()).throw(RuntimeError("boom")), "unavailable"),
            (lambda s, q: {"model": "jev-1", "answers": {"same_a1_a2": {"type": "noul", "noul": 7}}}, "invalid_response"),
            (lambda s, q: "not json", "invalid_response"),
        ]
        for responder, code in cases:
            with self.subTest(code=code):
                call = Jev(settings("typesafe"), responder=responder).ask({}, {"same_a1_a2": QUESTIONS["same_a1_a2"]})
                self.assertEqual(call.error, code)
                self.assertEqual(call.answers, {})

    def test_responder_success_keeps_model_and_usage(self):
        def responder(state, questions):
            return {"id": "req-1", "model": "jev-1.13.0-20260917", "answers": GOOD,
                    "usage": {"input_tokens": 400, "output_tokens": 70, "cost": 0.00002, "note": "dropped"}}

        jev = Jev(settings("typesafe"), responder=responder)
        self.assertTrue(jev.real)
        call = jev.ask({"x": 1}, QUESTIONS)
        self.assertIsNone(call.error)
        self.assertEqual((call.model, call.request_id), ("jev-1.13.0-20260917", "req-1"))
        self.assertEqual(call.usage, {"input_tokens": 400, "output_tokens": 70, "cost": 0.00002})

    def test_oversized_state_is_refused_before_any_call(self):
        def responder(state, questions):
            raise AssertionError("must not be called")

        call = Jev(settings("typesafe"), responder=responder).ask({"blob": "x" * 70_000}, QUESTIONS)
        self.assertEqual(call.error, "input_too_large")

    def test_calls_are_logged_without_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            jev = Jev(settings("typesafe"), run_dir=Path(tmp), stage=2,
                      responder=lambda s, q: {"model": "jev-1", "answers": GOOD, "usage": {"input_tokens": 5}})
            jev.ask({"secret_state": "Alex Morgan"}, QUESTIONS)
            line = (Path(tmp) / "run_log.jsonl").read_text()
        event = json.loads(line)
        self.assertEqual((event["stage"], event["backend"], event["status"], event["model"]), (2, "jev:typesafe", "ok", "jev-1"))
        self.assertNotIn("Alex Morgan", line)
        self.assertNotIn("ts-test-key", line)

    def test_trace_keeps_requests_answers_and_failure_details(self):
        bad = {**GOOD, "best": {**GOOD["best"], "choice": "p9"}}
        refused = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "unknown field 'fit'"}))
        with tempfile.TemporaryDirectory() as tmp, tracing(Path(tmp), 3):
            Jev(settings("typesafe"), responder=lambda s, q: {"model": "jev-1", "answers": GOOD}).ask(
                {"name": "Alex Morgan"}, QUESTIONS)
            Jev(settings("typesafe"), responder=lambda s, q: {"answers": bad}).ask({}, QUESTIONS)
            Jev(settings("openrouter"), transport=refused).ask({}, QUESTIONS)
            lines = read_trace(Path(tmp))
        requests = [line for line in lines if line["event"] == "jev_request"]
        self.assertEqual((len(requests), requests[0]["state"]), (3, {"name": "Alex Morgan"}))
        self.assertEqual(requests[0]["questions"]["fit"]["criteria"], ["low", "medium", "high"])
        [answers] = [line for line in lines if line["event"] == "jev_answers"]
        self.assertEqual((answers["model"], answers["answers"]["same_a1_a2"]), ("jev-1", {"type": "noul", "noul": 0.91}))
        invalid, http = [line for line in lines if line["event"] == "jev_failed"]
        self.assertEqual(invalid["error"]["chain"][0]["message"], "answer best chose 'p9', which is not an option")
        self.assertEqual(invalid["response"]["answers"]["best"]["choice"], "p9")
        self.assertEqual((http["code"], http["status"]), ("invalid_request", 400))
        [body] = [line for line in lines if line["event"] == "jev_http_error"]
        self.assertIn("unknown field 'fit'", body["body"])
        self.assertNotIn("or-test-key", json.dumps(lines))


class OpenRouterRouteTests(OfflineTestCase):
    def jev(self, handler, **kwargs):
        return Jev(settings("openrouter"), transport=httpx.MockTransport(handler), **kwargs)

    def test_request_shape_and_answer(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"model": "typesafe/jev-1.13-20260917", "answers": GOOD, "usage": {}},
                                  headers={"x-request-id": "or-1"})

        call = self.jev(handler).ask({"x": 1}, QUESTIONS)
        self.assertIsNone(call.error)
        self.assertEqual(seen["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(seen["auth"], "Bearer or-test-key")
        self.assertEqual(seen["body"]["model"], "typesafe/jev-1.13")
        self.assertEqual(set(seen["body"]), {"model", "state", "questions"})
        self.assertEqual((call.provider, call.request_id), ("openrouter", "or-1"))

    def test_retries_429_then_succeeds(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, json={"model": "m", "answers": GOOD})

        self.assertIsNone(self.jev(handler).ask({}, QUESTIONS).error)
        self.assertEqual(len(attempts), 2)

    def test_retry_budget_is_bounded(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(503, headers={"retry-after": "0"})

        call = self.jev(handler).ask({}, QUESTIONS)
        self.assertEqual(call.error, "server_error")
        self.assertEqual(len(attempts), 3)  # one try plus two retries

    def test_retry_after_beyond_deadline_stops(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(429, headers={"retry-after": "60"})

        call = self.jev(handler, timeout=2.0).ask({}, QUESTIONS)
        self.assertEqual(call.error, "rate_limited")
        self.assertEqual(len(attempts), 1)

    def test_errors_are_not_retried(self):
        for status, code in ((400, "invalid_request"), (401, "unauthorized"), (403, "forbidden")):
            attempts = []

            def handler(request, status=status):
                attempts.append(1)
                return httpx.Response(status, json={"error": "nope"})

            with self.subTest(status=status):
                self.assertEqual(self.jev(handler).ask({}, QUESTIONS).error, code)
                self.assertEqual(len(attempts), 1)

    def test_invalid_json_and_transport_errors(self):
        self.assertEqual(self.jev(lambda r: httpx.Response(200, content=b"<html>")).ask({}, QUESTIONS).error,
                         "invalid_response")

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        def refused(request):
            raise httpx.ConnectError("refused", request=request)

        self.assertEqual(self.jev(timeout).ask({}, QUESTIONS).error, "timeout")
        self.assertEqual(self.jev(refused).ask({}, QUESTIONS).error, "connection_error")


class TypeSafeRouteTests(OfflineTestCase):
    def jev(self, handler):
        return Jev(settings("typesafe"), transport=httpx2.MockTransport(handler))

    def test_sdk_request_and_answer(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, json={"model": "jev-1.13.0", "answers": GOOD,
                                              "usage": {"input_tokens": 12, "output_tokens": 3}})

        call = self.jev(handler).ask({"x": 1}, QUESTIONS)
        self.assertIsNone(call.error, call)
        self.assertTrue(seen["url"].startswith("https://api.typesafe.ai/"))
        self.assertEqual(seen["body"]["questions"]["best"]["criteria"], {"p1": "Candidate p1", "none": "No candidate"})
        self.assertEqual(call.model, "jev-1.13.0")
        self.assertEqual(call.answers["best"]["choice"], "p1")

    def test_key_check_lists_models_without_a_decision_request(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.headers["authorization"]))
            return httpx2.Response(200, json={"models": [
                {"name": "jev-1.13.0", "description": "Jev", "release_date": "2026-09-17"}]})

        self.assertEqual(check_typesafe_key("ts-key", transport=httpx2.MockTransport(handler)), ["jev-1.13.0"])
        self.assertEqual(seen, [("GET", "/v1/models", "Bearer ts-key")])

    def test_key_check_failures(self):
        def respond(status):
            return httpx2.MockTransport(lambda request: httpx2.Response(status, json={"error": {"message": "no"}}))

        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(KeyRejected):
                check_typesafe_key("bad", transport=respond(status))
        with self.assertRaises(KeyCheckFailed):
            check_typesafe_key("key", transport=respond(500))

        def refuse(request):
            raise httpx2.ConnectError("refused", request=request)

        with self.assertRaises(KeyCheckFailed) as caught:
            check_typesafe_key("secret-key", transport=httpx2.MockTransport(refuse))
        self.assertNotIn("secret-key", str(caught.exception))

    def test_sdk_errors_become_codes(self):
        for status, code in ((401, "unauthorized"), (400, "invalid_request")):
            with self.subTest(status=status):
                call = self.jev(lambda r, s=status: httpx2.Response(s, json={"error": {"message": "no"}})).ask({}, QUESTIONS)
                self.assertEqual(call.error, code)
                self.assertEqual(call.answers, {})
