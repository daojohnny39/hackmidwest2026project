"""Section 3: real Section 2 output, spec 9 targets, and verification edge cases.

Seed collection stays in memory. Jev responders exercise the real adapter without
HTTP. Only boundary tests alter artifacts or provider answers.
"""

import io
import json
import math
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline import stage3_verify, stage4_summarize
from pipeline.__main__ import OK, REFUSED, main
from pipeline.artifacts import ArtifactError, read_artifact, write_artifact
from pipeline.config import load_settings
from pipeline.contracts import (
    CandidateProfile,
    CandidateSignals,
    CaptureArtifact,
    FetchError,
    ProfilesBundle,
    RawEvidenceBundle,
    VerificationNotes,
    VerificationResult,
    validate_references,
)
from pipeline.jev import Jev
from pipeline.search import facecheck_backend, rekognition_backend
from pipeline.stage2_profiles import build_graphs, build_profiles
from pipeline.stage3_verify import FACE_SCORE_RANGE, apply_notes, compute_signals, face_match, fallback_confidence, verify
from tests.seed_evidence import collect
from tests.support import FIXTURE_CAPTURES, MockServerTestCase, OfflineTestCase

WHEN = "2026-10-10T14:03:00Z"
MODEL = "jev-1.13.0-test"
LINKUP = "https://linkup.test/in/alexbuilds"
INTERVIEW = "https://riverbend-news.test/people/alex-morgan-transit"
MIRROR = "https://syndicated.test/transit/alex-morgan"


def settings(mode="fake"):
    env = {"JEV_MODE": mode, "LLM_MODE": "off", "TYPESAFE_API_KEY": "test", "OPENROUTER_API_KEY": "test"}
    return load_settings(backend="mock", models=mode in ("typesafe", "openrouter"), environ=env, dotenv_path=None)


def client(mode):
    return None if mode == "off" else Jev(settings(mode))


def seed_case(capture="alex", scenario="default", mode="off"):
    # collect() models seed hits but not server.fault(). Inject those terminal search errors here.
    status = {"rate_limited": 429, "server_error": 503}.get(scenario)
    raw = collect(capture, "empty" if status else scenario)
    if status:
        for image in raw.images:
            image.request_id = None
            image.errors = [FetchError(url=None, status_code=status, reason=scenario)]
    return raw, build_profiles(raw, jev=client(mode))


def reply(nouls, best, none=0.0):
    probabilities = dict.fromkeys(nouls, 0.0) | {"none": none}
    probabilities[best] = 1.0 if best == "none" else 1.0 - none
    return {
        "model": MODEL, "usage": {"input_tokens": 512, "output_tokens": 12},
        "answers": {**{f"match_{p}": {"type": "noul", "noul": value} for p, value in nouls.items()},
                    "best": {"type": "choice", "choice": best, "confidence": 0.8, "probabilities": probabilities}},
    }


def real_jev(responder, mode="typesafe", **options):
    return Jev(settings(mode), responder=responder, **options)


def rename_profiles(profiles, ids):
    data = profiles.model_dump()
    mapping = dict(zip((p.profile_id for p in profiles.profiles), ids))
    for profile in data["profiles"]:
        profile["profile_id"] = mapping[profile["profile_id"]]
    data["graphs"] = {mapping[key]: {**graph, "profile_id": mapping[key]} for key, graph in data["graphs"].items()}
    for graph in data["graphs"].values():
        for connection in graph["connections"]:
            target = connection["target_profile_id"]
            if target is not None:
                connection["target_profile_id"] = mapping[target]
    return ProfilesBundle.model_validate(data)


def face_scored(raw, backend, score_for):
    """The bundle as if a face search backend had returned the same hits, each scored score_for(url)."""
    data = raw.model_dump()
    for image in data["images"]:
        image["backend"] = backend
        for hit in image["hits"]:
            hit["face_score"] = score_for(hit["url"])
    return RawEvidenceBundle.model_validate(data)


def remove_search(raw, image_index, *, failed):
    data = raw.model_dump()
    image = data["images"][image_index]
    image.update(hits=[], page_urls=[], errors=[], request_id=None)
    if failed:
        image["errors"] = [{"url": None, "status_code": 429, "reason": "rate_limited"}]
    for page in data["pages"].values():
        page["found_via"] = [v for v in page["found_via"] if v["image_id"] != image["image_id"]]
    data["pages"] = {key: page for key, page in data["pages"].items() if page["found_via"]}
    data["url_aliases"] = {url: key for url, key in data["url_aliases"].items() if key in data["pages"]}
    return RawEvidenceBundle.model_validate(data)


class NoCallJev:
    real = True
    provider = "typesafe"

    def ask(self, *args, **kwargs):
        raise AssertionError("Jev must not be called")


class MockMatrixTests(OfflineTestCase):
    def test_section2_to_section3_matrix_in_fake_and_off_modes(self):
        rows = [
            ("alex", "default", "accepted", [("alexbuilds", "accepted", 0.99)]),
            ("alex", "ambiguous", "accepted", [("alexbuilds", "accepted", 0.98), ("alexframes", "rejected", 0.69)]),
            ("alex", "stale", "accepted", [("alexbuilds", "accepted", 0.99)]),
            ("alex", "duplicates", "accepted", [("alexbuilds", "accepted", 0.99)]),
            ("alex", "empty", "no_candidates", []),
            ("alex", "rate_limited", "evidence_unavailable", []),
            ("alex", "server_error", "evidence_unavailable", []),
            ("sam", "default", "accepted", [("samdraws", "accepted", 0.99)]),
            ("casey", "default", "accepted", [("caseykeys", "accepted", 0.97)]),
            ("unknown", "default", "no_candidates", []),
        ]
        for mode in ("fake", "off"):
            for capture, scenario, reason, expected in rows:
                with self.subTest(mode=mode, capture=capture, scenario=scenario):
                    raw, profiles = seed_case(capture, scenario, mode)
                    result = verify(raw, profiles, jev=client(mode))
                    validate_references(raw=raw, profiles=profiles, verification=result)
                    self.assertEqual(VerificationResult.model_validate_json(result.model_dump_json()), result)
                    handles = {p.profile_id: {profiles.accounts[a].handle for a in p.account_ids}
                               for p in profiles.profiles}
                    actual = [(handles[v.profile_id], v.decision, round(v.confidence, 2)) for v in result.verdicts]
                    self.assertEqual(actual, [({handle}, decision, confidence) for handle, decision, confidence in expected])
                    self.assertEqual(result.reason, reason)
                    self.assertEqual(result.accepted_profile_id, result.verdicts[0].profile_id if expected else None)
                    self.assertTrue(all(v.method == "fallback" and v.confidence == v.fallback_confidence
                                        for v in result.verdicts))
                    self.assertEqual(len(result.jev_calls), int(mode == "fake" and bool(expected)))
                    self.assertTrue(all(call.error is None for call in result.jev_calls))
                    for verdict in result.verdicts:
                        self.assertNotIn("partial_search_failure", verdict.flags)
                        if mode == "fake":
                            answer = result.jev_calls[0].answers[f"match_{verdict.profile_id}"]
                            self.assertEqual(answer["noul"], verdict.fallback_confidence)
                    if capture == "casey":
                        self.assertIn("sparse_footprint", result.verdicts[0].flags)
                    # Section 4 must preserve confidence or the exact no-match reason.
                    final = stage4_summarize.summarize(raw, profiles, result, settings(mode)).final
                    if expected:
                        self.assertEqual((final.confidence, final.method), (result.verdicts[0].confidence, "fallback"))
                    else:
                        self.assertEqual((final.reason, final.top_candidates), (reason, []))

    def test_empty_candidates_never_call_jev(self):
        for capture, scenario in (("unknown", "default"), ("alex", "empty"),
                                  ("alex", "rate_limited"), ("alex", "server_error")):
            with self.subTest(capture=capture, scenario=scenario):
                self.assertEqual(verify(*seed_case(capture, scenario), jev=NoCallJev()).jev_calls, [])

    def test_no_accounts_after_access_error_is_evidence_unavailable(self):
        raw = collect("alex")
        for page in raw.pages.values():
            if page.page_kind == "profile":
                page.relevant = False
        for image in raw.images:
            image.page_urls = [url for url in image.page_urls if raw.pages[url].relevant]
        raw = RawEvidenceBundle.model_validate(raw.model_dump())
        # The retained deleted article has a 404; none of the remaining pages forms an account.
        result = verify(raw, build_profiles(raw), jev=NoCallJev())
        self.assertEqual(result.reason, "evidence_unavailable")


class SignalTests(OfflineTestCase):
    def test_real_section2_signals(self):
        raw, profiles = seed_case(scenario="ambiguous")
        signals = compute_signals(raw, profiles)
        self.assertEqual(signals["p1"].model_dump(), {
            "image_support": 1.0, "top_hit_share": 1.0, "mean_link_trust": 76.0, "platform_count": 5,
            "consistency": 0.8, "name_collision": True, "newest_update": "2026-10-01",
            "face_score": None, "face_match": None})
        self.assertEqual(signals["p2"].model_dump(), {
            "image_support": 1.0, "top_hit_share": 0.0, "mean_link_trust": 72.5, "platform_count": 4,
            "consistency": 1.0, "name_collision": True, "newest_update": "2026-10-01",
            "face_score": None, "face_match": None})

    def test_failed_and_empty_searches_stay_in_the_denominator(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                raw = remove_search(collect("alex"), 2, failed=failed)
                result = verify(raw, build_profiles(raw))
                verdict = result.verdicts[0]
                self.assertEqual((verdict.signals.image_support, verdict.signals.top_hit_share), (2 / 3, 2 / 3))
                self.assertEqual("partial_search_failure" in verdict.flags, failed)

    def test_support_and_platform_count_use_distinct_values(self):
        raw, profiles = seed_case("casey")
        for account in profiles.accounts.values():
            account.platform = "linkedin"
            account.direct_image_ids *= 2
        signals = compute_signals(raw, profiles)["p1"]
        self.assertEqual((signals.image_support, signals.platform_count), (1, 1))

    def test_top_hit_must_be_rank_one_even_when_hits_are_out_of_order(self):
        raw = collect("casey")
        raw.images[0].hits.reverse()
        self.assertEqual(compute_signals(raw, build_profiles(raw))["p1"].top_hit_share, 1)
        for hit in raw.images[0].hits:
            hit.position += 1
        for page in raw.pages.values():
            for via in page.found_via:
                via.hit_position += 1
        raw = RawEvidenceBundle.model_validate(raw.model_dump())
        self.assertEqual(compute_signals(raw, build_profiles(raw))["p1"].top_hit_share, 0)

    def test_triage_removes_only_that_images_support(self):
        raw = collect("alex")
        raw.images[2].page_urls.remove(INTERVIEW)
        profiles = build_profiles(raw)
        self.assertEqual(compute_signals(raw, profiles)["p1"].top_hit_share, 2 / 3)
        self.assertTrue(raw.pages[INTERVIEW].relevant)

    def test_rank_one_mirror_resolves_to_its_attributed_mention(self):
        raw = collect("alex")
        image = raw.images[1]
        mirror = next(hit for hit in image.hits if hit.url == MIRROR)
        old_rank = mirror.position
        for hit in image.hits:
            hit.position = {1: old_rank, old_rank: 1}.get(hit.position, hit.position)
        for page in raw.pages.values():
            for via in page.found_via:
                if via.image_id == image.image_id and via.depth == 0:
                    via.hit_position = {1: old_rank, old_rank: 1}.get(via.hit_position, via.hit_position)
        raw = RawEvidenceBundle.model_validate(raw.model_dump())
        self.assertEqual(compute_signals(raw, build_profiles(raw))["p1"].top_hit_share, 1)

    def test_only_subject_attribution_carries_mention_support(self):
        raw = collect("alex")
        profiles = build_profiles(raw)
        mention = next(m for m in profiles.mentions if m.url == INTERVIEW)
        self.assertEqual([profiles.accounts[a].url for a in mention.subject_account_ids], [LINKUP])
        raw.pages[INTERVIEW].title = "Transit interview with a designer"
        profiles = build_profiles(raw)
        mention = next(m for m in profiles.mentions if m.url == INTERVIEW)
        self.assertTrue(mention.links_to)
        self.assertEqual(mention.subject_account_ids, [])
        self.assertEqual(compute_signals(raw, profiles)["p1"].top_hit_share, 2 / 3)

    def test_followed_accounts_do_not_gain_direct_support(self):
        raw, profiles = seed_case("sam")
        followed = [a for a in profiles.accounts.values() if a.depth == 1]
        self.assertEqual(len(followed), 2)
        for account in followed:
            self.assertEqual((account.image_ids, account.direct_image_ids, account.best_hit_position), (["img-1"], [], None))
        split = followed[0]
        profiles.profiles = [
            CandidateProfile(profile_id="p1", name="Sam Rivera", pair_decisions=[],
                             account_ids=[a for a in profiles.accounts if a != split.account_id]),
            CandidateProfile(profile_id="p2", name=split.display_name, pair_decisions=[], account_ids=[split.account_id]),
        ]
        profiles.graphs = build_graphs(profiles.accounts, profiles.profiles)
        result = verify(raw, profiles, jev=real_jev(lambda s, q: reply({"p1": 0.1, "p2": 0.95}, "p2")))
        self.assertEqual((result.reason, result.verdicts[0].signals.image_support), ("insufficient_evidence", 0))

    def test_workplaces_count_missing_values_and_normalize_case_and_punctuation(self):
        raw, profiles = seed_case()
        for workplaces, expected in ((["Atlas", "atlas.", None, "Old Mill", ""], 0.4),
                                     ([None] * 5, 0), (["B Co", "A Co", None, None, None], 0.2),
                                     (["東京研究所"] * 5, 1)):
            with self.subTest(workplaces=workplaces):
                for account, workplace in zip(profiles.accounts.values(), workplaces):
                    account.workplace = workplace
                self.assertEqual(compute_signals(raw, profiles)["p1"].consistency, expected)

    def test_name_collisions_use_section2_compatibility(self):
        raw, profiles = seed_case(scenario="ambiguous")
        for names, expected in ((("Sam Rivera", "Sammy Rivera"), True), (("Alex M.", "Alex Morgan"), True),
                                (("Alex", "Alex"), False), ((None, None), False),
                                (("Alex Morgan", "Alex Morgan Photo"), False)):
            with self.subTest(names=names):
                for profile, name in zip(profiles.profiles, names):
                    profile.name = name
                self.assertEqual([s.name_collision for s in compute_signals(raw, profiles).values()], [expected] * 2)

    def test_newest_update_ignores_invalid_dates_and_uses_offsets(self):
        raw, profiles = seed_case()
        for dates, expected in ((["not a date", "2022-05-14", None, "", "soon"], "2022-05-14"),
                                (["2026-10-01", "2026-10-01T09:00:00Z", "2026-10-01T05:00:00-05:00", None, None],
                                 "2026-10-01T05:00:00-05:00"), ([None] * 5, None)):
            with self.subTest(dates=dates):
                for account, date in zip(profiles.accounts.values(), dates):
                    account.updated_at = date
                self.assertEqual(compute_signals(raw, profiles)["p1"].newest_update, expected)

    def test_fallback_formula_anchor_and_platform_cap(self):
        signals = CandidateSignals(image_support=1, top_hit_share=0.5, mean_link_trust=0, platform_count=0,
                                   consistency=0.5, name_collision=False)
        self.assertEqual(fallback_confidence(signals), 0.5)  # spec formula gives z = 0
        signals.platform_count = 5
        self.assertAlmostEqual(fallback_confidence(signals), 0.8175744761936437)
        signals.platform_count = 9
        self.assertAlmostEqual(fallback_confidence(signals), 0.8175744761936437)


class FaceSignalTests(OfflineTestCase):
    def test_face_match_follows_each_backends_bands(self):
        for backend, score, expected in (("rekognition", 80, 0), ("rekognition", 90, 0), ("rekognition", 94, 4 / 9),
                                         ("rekognition", 99, 1), ("rekognition", 100, 1), ("facecheck", 69, 0),
                                         ("facecheck", 80, 0.5), ("facecheck", 90, 1), ("facecheck", 99, 1)):
            with self.subTest(backend=backend, score=score):
                self.assertAlmostEqual(face_match(score, backend), expected)
        for backend, module in (("rekognition", rekognition_backend), ("facecheck", facecheck_backend)):
            with self.subTest(backend=backend):
                low, high = FACE_SCORE_RANGE[backend]
                strongest = module.SCORE_BANDS[0][1]
                self.assertEqual((module.score_band(high), module.score_band(high - 1) != strongest), (strongest, True))
                self.assertEqual((module.score_band(low - 1), module.score_band(low) != "weak match"), ("weak match", True))

    def test_face_score_and_face_match_are_set_together(self):
        with self.assertRaises(ValueError):
            CandidateSignals(image_support=1, top_hit_share=0, mean_link_trust=0, platform_count=1, consistency=0,
                             name_collision=False, face_score=99)

    def test_signals_take_the_best_face_score_of_kept_hits_and_attributed_mentions(self):
        raw = face_scored(collect("alex"), "rekognition", lambda url: 85)
        next(hit for hit in raw.images[2].hits if hit.url == INTERVIEW).face_score = 99  # a mention of p1
        signals = compute_signals(raw, build_profiles(raw))["p1"]
        self.assertEqual((signals.face_score, signals.face_match), (99, 1))
        raw.images[2].page_urls.remove(INTERVIEW)
        signals = compute_signals(raw, build_profiles(raw))["p1"]
        self.assertEqual((signals.face_score, signals.face_match), (85, 0))

    def test_face_match_moves_the_formula_around_its_midpoint(self):
        signals = CandidateSignals(image_support=1, top_hit_share=0.5, mean_link_trust=0, platform_count=0,
                                   consistency=0.5, name_collision=False)  # z = 0 without a face
        for score, match, z in ((None, None, 0), (94, 0.5, 0), (99, 1, 2), (80, 0, -2)):
            with self.subTest(match=match):
                signals = signals.model_copy(update={"face_score": score, "face_match": match})
                self.assertAlmostEqual(fallback_confidence(signals), 1 / (1 + math.exp(-z)))

    def test_a_closer_face_match_raises_confidence(self):
        raw = collect("casey")
        without = verify(raw, build_profiles(raw)).verdicts[0]
        verdicts = []
        for score in (85, 95, 99):
            scored = face_scored(raw, "rekognition", lambda url: score)
            verdicts.append(verify(scored, build_profiles(scored)).verdicts[0])
        confidences = [verdict.confidence for verdict in verdicts]
        self.assertEqual(confidences, sorted(set(confidences)))
        self.assertLess(confidences[0], without.confidence)
        self.assertGreater(confidences[-1], without.confidence)
        self.assertIn("Best face score 99/100.", verdicts[-1].rationale)
        self.assertNotIn("face", without.rationale)

    def test_a_strong_face_match_outranks_a_weak_top_hit(self):
        # Live V3 runs ranked a rank-1 hit at similarity 80 above a rank-2 hit at 99.
        raw = face_scored(collect("alex", "ambiguous"), "rekognition", lambda url: 99 if "alexframes" in url else 82)
        result = verify(raw, build_profiles(raw))
        self.assertEqual([(v.profile_id, v.signals.top_hit_share, v.signals.face_score) for v in result.verdicts],
                         [("p2", 0, 99), ("p1", 1, 82)])
        self.assertEqual(result.reason, "too_close")

    def test_jev_sees_face_match_only_when_the_search_compared_faces(self):
        raw, profiles = seed_case(scenario="ambiguous")
        state, questions = stage3_verify.jev_request(raw, profiles, compute_signals(raw, profiles))
        self.assertIn("no face comparison", state["task"])
        self.assertNotIn("face_match", json.dumps([state, questions]))
        raw = face_scored(raw, "rekognition", lambda url: 99 if "alexframes" in url else 94)
        profiles = build_profiles(raw)
        state, questions = stage3_verify.jev_request(raw, profiles, compute_signals(raw, profiles))
        self.assertNotIn("no face comparison", state["task"])
        self.assertIn("face_match", state["signal_notes"])
        self.assertEqual({key: c["signals"]["face_match"] for key, c in state["candidates"].items()}, {"p1": 0.44, "p2": 1})
        self.assertNotIn("face_score", json.dumps(state))
        self.assertIn("only face_match", questions["match_p1"]["instructions"])


class JevTests(OfflineTestCase):
    def test_valid_answers_from_either_route_supply_all_confidences(self):
        raw, profiles = seed_case(scenario="ambiguous")
        for mode in ("typesafe", "openrouter"):
            with self.subTest(mode=mode):
                result = verify(raw, profiles, jev=real_jev(lambda s, q: reply({"p1": 0.94, "p2": 0.20}, "p1"), mode))
                self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
                self.assertEqual([(v.method, v.confidence, v.jev_model) for v in result.verdicts],
                                 [("jev", 0.94, MODEL), ("jev", 0.20, MODEL)])
                self.assertEqual([round(v.fallback_confidence, 2) for v in result.verdicts], [0.98, 0.69])
                self.assertNotIn("jev_fallback_disagree", result.verdicts[0].flags)
                self.assertIn("jev_fallback_disagree", result.verdicts[1].flags)
                self.assertEqual(len(result.jev_calls), 1)

    def test_request_has_one_noul_per_candidate_and_a_best_choice(self):
        raw, profiles = seed_case(scenario="ambiguous")
        seen = []

        def responder(state, questions):
            seen.append((state, questions))
            return reply({"p1": 0.9, "p2": 0.2}, "p1")

        verify(raw, profiles, jev=real_jev(responder))
        [(state, questions)] = seen
        self.assertEqual(state["num_images"], 3)
        self.assertEqual(set(state["candidates"]), {"p1", "p2"})
        self.assertEqual(set(questions), {"match_p1", "match_p2", "best"})
        self.assertEqual([questions[key]["type"] for key in ("match_p1", "match_p2", "best")], ["noul", "noul", "choice"])
        self.assertEqual(set(questions["best"]["criteria"]), {"p1", "p2", "none"})
        self.assertIn("untrusted", state["task"])
        self.assertNotIn("Alex Morgan", json.dumps(questions))
        for profile in profiles.profiles:
            summary = state["candidates"][profile.profile_id]
            self.assertEqual(summary["signals"], compute_signals(raw, profiles)[profile.profile_id].model_dump(
                exclude={"face_score", "face_match"}))
            self.assertEqual(len(summary["accounts"]), len(profile.account_ids))
            self.assertTrue(all("url" not in account and "text" not in account for account in summary["accounts"]))

    def test_maximum_normal_request_is_bounded_despite_long_scraped_fields(self):
        raw, profiles = seed_case()
        # Twelve singleton profiles maximize repeated candidate/question overhead.
        template = profiles.accounts["a1"]
        profiles.accounts = {f"a{i}": template.model_copy(update={"account_id": f"a{i}", "url": f"https://test.invalid/{i}"})
                             for i in range(12)}
        profiles.profiles = [CandidateProfile(profile_id=f"p{i}", name="x" * 100_000,
                                              account_ids=[f"a{i}"], pair_decisions=[]) for i in range(12)]
        for account in profiles.accounts.values():
            for field in ("platform", "display_name", "handle", "role", "workplace", "location", "updated_at", "bio"):
                setattr(account, field, "x" * 100_000)
            account.links_to = []
        profiles.graphs = build_graphs(profiles.accounts, profiles.profiles)
        signals = compute_signals(raw, profiles)
        # A face match on every candidate adds a note and a field each, and must still fit.
        with_faces = {key: value.model_copy(update={"face_score": 94, "face_match": 4 / 9}) for key, value in signals.items()}
        for candidate_signals in (signals, with_faces):
            state, questions = stage3_verify.jev_request(raw, profiles, candidate_signals)
            size = len(json.dumps({"state": state, "questions": questions}).encode())
            self.assertLess(size, 24_000)
            self.assertEqual(len(state["candidates"]), 12)

    def test_oversized_request_falls_back_before_contacting_provider(self):
        raw, profiles = seed_case()
        profiles = rename_profiles(profiles, ["p" + "界" * 10_000])
        result = verify(raw, profiles, jev=NoCallJev())
        self.assertEqual((result.reason, result.jev_calls[0].error), ("accepted", "input_too_large"))
        self.assertIn("verification_fallback", result.verdicts[0].flags)
        self.assertEqual(result.verdicts[0].method, "fallback")

    def test_fake_answers_are_parsed_and_recorded_but_never_used_to_score(self):
        raw, profiles = seed_case(scenario="ambiguous", mode="fake")
        fake = reply({"p1": 0.05, "p2": 0.99}, "p2")["answers"]
        with patch.object(stage3_verify, "fake_answers", return_value=fake):
            result = verify(raw, profiles, jev=client("fake"))
        [call] = result.jev_calls
        self.assertEqual((call.provider, call.error, call.model), ("fake", None, None))
        self.assertEqual(call.answers["best"]["choice"], "p2")
        self.assertEqual((result.accepted_profile_id, round(result.verdicts[0].confidence, 2)), ("p1", 0.98))
        self.assertTrue(all(v.method == "fallback" and "verification_fallback" not in v.flags for v in result.verdicts))

    def test_malformed_answers_fall_back_for_the_whole_stage(self):
        bad_responses = []
        for value in (True, -0.1, 1.1, float("nan"), float("inf"), "0.9"):
            bad_responses.append(reply({"p1": value, "p2": 0.2}, "p1"))
        for mutation in (
            lambda a: a.pop("match_p2"),
            lambda a: a.update(extra={"type": "noul", "noul": 0.5}),
            lambda a: a["match_p2"].update(type="score"),
            lambda a: a["best"].update(choice="foreign"),
            lambda a: a["best"].pop("confidence"),
            lambda a: a["best"]["probabilities"].update(p2=-0.1),
            lambda a: a["best"]["probabilities"].update(foreign=0.0),
            lambda a: a["best"]["probabilities"].update(p1=0.1),
        ):
            response = reply({"p1": 0.9, "p2": 0.2}, "p1")
            mutation(response["answers"])
            bad_responses.append(response)
        raw, profiles = seed_case(scenario="ambiguous")
        for index, response in enumerate(bad_responses):
            with self.subTest(response=index):
                result = verify(raw, profiles, jev=real_jev(lambda s, q: response))
                self.assertEqual((result.reason, result.jev_calls[0].error), ("accepted", "invalid_response"))
                self.assertEqual(result.jev_calls[0].answers, {})
                for verdict in result.verdicts:
                    self.assertEqual((verdict.method, verdict.confidence, verdict.jev_model),
                                     ("fallback", verdict.fallback_confidence, None))
                    self.assertIn("verification_fallback", verdict.flags)

    def test_jev_failure_and_grouping_flags_are_carried_to_every_verdict(self):
        def outage(state, questions):
            raise ConnectionError("down")

        raw = collect("alex", "ambiguous")
        profiles = build_profiles(raw, jev=real_jev(outage))
        self.assertIn("grouping_fallback", profiles.flags)
        profiles.flags.append("accounts_truncated")
        profiles.graphs["p2"].flags.append("cross_profile_link")
        result = verify(raw, profiles, jev=real_jev(outage))
        self.assertEqual(result.jev_calls[0].error, "connection_error")
        for verdict in result.verdicts:
            self.assertTrue({"grouping_fallback", "accounts_truncated", "verification_fallback"} <= set(verdict.flags))
        self.assertIn("cross_profile_link", result.verdicts[1].flags)

    def test_disagreement_flag_requires_more_than_point_four(self):
        for confidence, flagged in ((0.9, False), (0.9001, True)):
            with self.subTest(confidence=confidence), patch.object(stage3_verify, "fallback_confidence", return_value=0.5):
                result = verify(*seed_case("casey"), jev=real_jev(lambda s, q: reply({"p1": confidence}, "p1")))
                self.assertEqual("jev_fallback_disagree" in result.verdicts[0].flags, flagged)


class DecisionTests(OfflineTestCase):
    def decide(self, case, nouls, best=None, none=0.0, live=False):
        best = best if best is not None else max(nouls, key=nouls.get)
        result = verify(*case, jev=real_jev(lambda s, q: reply(nouls, best, none)), live=live)
        self.assertEqual(VerificationResult.model_validate_json(result.model_dump_json()), result)
        return result

    def test_confidence_threshold_uses_unrounded_values_without_tolerance(self):
        for value, reason in ((0.8, "accepted"), (math.nextafter(0.8, 0), "below_threshold"),
                              (0.796, "below_threshold")):
            with self.subTest(confidence=value):
                self.assertEqual(self.decide(seed_case("casey"), {"p1": value}).reason, reason)

    def test_lead_has_only_the_spec_float_tolerance(self):
        for runner_up, reason in ((0.65, "accepted"), (0.6500000005, "accepted"),
                                  (0.650000002, "too_close"), (0.654, "too_close")):
            with self.subTest(runner_up=runner_up):
                self.assertEqual(self.decide(seed_case(scenario="ambiguous"), {"p1": 0.85, "p2": runner_up}).reason, reason)

    def test_ties_accept_neither_candidate_whichever_one_best_selects(self):
        for best in ("p1", "p2"):
            result = self.decide(seed_case(scenario="ambiguous"), {"p1": 0.9, "p2": 0.9}, best)
            self.assertEqual((result.reason, result.accepted_profile_id), ("too_close", None))
            self.assertEqual([v.profile_id for v in result.verdicts], ["p1", "p2"])

    def test_none_and_disagreement_precede_evidence_and_threshold_checks(self):
        raw, profiles = seed_case(scenario="ambiguous")
        profiles.profiles[0].name = None
        for best, none, reason in (("none", 1.0, "jev_none"), ("p1", 0.5, "jev_none"),
                                   ("p2", 0.0, "jev_disagreement"), ("p1", 0.499, "insufficient_evidence")):
            with self.subTest(best=best, none=none):
                result = self.decide((raw, profiles), {"p1": 0.4, "p2": 0.2}, best, none)
                self.assertEqual((result.reason, result.accepted_profile_id), (reason, None))

    def test_best_none_below_half_is_disagreement_per_the_spec_order(self):
        response = reply({"p1": 0.95, "p2": 0.1}, "none")
        response["answers"]["best"]["probabilities"] = {"p1": 0.3, "p2": 0.3, "none": 0.4}
        result = verify(*seed_case(scenario="ambiguous"), jev=real_jev(lambda s, q: response))
        self.assertEqual(result.reason, "jev_disagreement")

    def test_snippets_alone_cannot_match(self):
        raw = collect("casey")
        for page in raw.pages.values():
            page.extraction, page.status_code = "snippet", 403
            page.json_ld, page.outbound_links = [], []
        profiles = build_profiles(raw)
        scores = {p.profile_id: 0.95 if i == 0 else 0.1 for i, p in enumerate(profiles.profiles)}
        result = self.decide((raw, profiles), scores)
        self.assertEqual((result.reason, result.accepted_profile_id), ("insufficient_evidence", None))

    def test_name_must_be_nonempty_and_sourced(self):
        raw, profiles = seed_case("casey")
        for name in (None, " ", "Somebody Else"):
            with self.subTest(name=name):
                profiles.profiles[0].name = name
                self.assertEqual(self.decide((raw, profiles), {"p1": 0.95}).reason, "insufficient_evidence")

    def test_unicode_sourced_name_reaches_section4(self):
        raw, profiles = seed_case("casey")
        profiles.profiles[0].name = "李 明"
        profiles.accounts[profiles.profiles[0].account_ids[0]].display_name = "李 明"
        result = verify(raw, profiles)
        self.assertEqual(result.reason, "accepted")
        final = stage4_summarize.summarize(raw, profiles, result, settings()).final
        self.assertEqual(final.name.value, "李 明")

    def test_unusual_ids_sort_stably_and_keep_exact_question_keys(self):
        raw, profiles = seed_case(scenario="ambiguous")
        for ids in (("p10", "p2"), ("²", "match_best/東京")):
            with self.subTest(ids=ids):
                renamed = rename_profiles(profiles, ids)
                result = self.decide((raw, renamed), dict.fromkeys(ids, 0.9), best=ids[1])
                self.assertEqual(result.reason, "too_close")
                self.assertEqual([v.profile_id for v in result.verdicts], sorted(ids))
                self.assertEqual(set(result.jev_calls[0].answers), {*(f"match_{p}" for p in ids), "best"})

    def test_reserved_none_id_is_rejected_before_the_request(self):
        raw, profiles = seed_case("casey")
        with self.assertRaisesRegex(ArtifactError, "reserved"):
            verify(raw, rename_profiles(profiles, ["none"]), jev=NoCallJev())

    def test_live_run_requires_review_when_real_jev_fails(self):
        def outage(state, questions):
            raise TimeoutError

        for name in ("Casey Brooks", None):
            with self.subTest(name=name):
                raw, profiles = seed_case("casey")
                profiles.profiles[0].name = name
                result = verify(raw, profiles, jev=real_jev(outage), live=True)
                self.assertEqual((result.reason, result.accepted_profile_id), ("review_required", None))
                self.assertTrue(all(v.decision == "rejected" for v in result.verdicts))
        self.assertEqual(verify(*seed_case("alex", "rate_limited"), live=True).reason, "evidence_unavailable")
        self.assertEqual(self.decide(seed_case("casey"), {"p1": 0.93}, live=True).reason, "accepted")

    def test_live_run_without_real_jev_uses_the_accept_rule(self):
        for jev in (client("fake"), None):
            for name, reason in (("Casey Brooks", "accepted"), (None, "insufficient_evidence")):
                with self.subTest(provider=getattr(jev, "provider", "off"), name=name):
                    raw, profiles = seed_case("casey")
                    profiles.profiles[0].name = name
                    result = verify(raw, profiles, jev=jev, live=True)
                    self.assertEqual(result.reason, reason)
                    self.assertEqual(result.accepted_profile_id, "p1" if reason == "accepted" else None)
                    self.assertTrue(all(v.method == "fallback" for v in result.verdicts))
            with self.subTest(provider=getattr(jev, "provider", "off"), scenario="ambiguous"):
                self.assertEqual(verify(*seed_case(scenario="ambiguous"), jev=jev, live=True).reason,
                                 verify(*seed_case(scenario="ambiguous"), jev=jev).reason)


class NotesTests(OfflineTestCase):
    def test_notes_change_only_rationales(self):
        result = verify(*seed_case(scenario="ambiguous"))
        noted = apply_notes(result, VerificationNotes(rationales={"p2": "  A namesake photographer in Portland.  "}))
        self.assertEqual(noted.verdicts[1].rationale, "A namesake photographer in Portland.")
        expected = result.model_dump()
        expected["verdicts"][1]["rationale"] = "A namesake photographer in Portland."
        self.assertEqual(noted.model_dump(), expected)
        self.assertNotEqual(result.verdicts[1].rationale, noted.verdicts[1].rationale)

    def test_bad_notes_are_rejected(self):
        result = verify(*seed_case())
        for rationales in ({"p9": "Unknown."}, {"p1": "One. Two."}, {"p1": "  "}, {"p1": "x" * 301}):
            with self.subTest(rationales=rationales), self.assertRaises(ValueError):
                apply_notes(result, VerificationNotes(rationales=rationales))


class StageRunnerTests(OfflineTestCase):
    def write_run(self, backend="mock"):
        raw, profiles = seed_case()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        run_dir = Path(root.name) / raw.run_id
        run_dir.mkdir()
        for image in raw.images:
            image.backend = backend
        images = [{"image_id": image.image_id, "angle": image.angle,
                   **({"file": f"{image.image_id}.jpg"} if backend == "serpapi" else {"mock_fixture": "alex-front"})}
                  for image in raw.images]
        capture = CaptureArtifact.model_validate({
            "run_id": raw.run_id, "capture_dir": "captures/x", "backend": backend, "scenario": "default",
            "llm_mode": "off", "jev_mode": "fake",
            "capture": {"session_id": raw.run_id.rsplit("-", 1)[0], "captured_at": WHEN, "images": images,
                        "consent": {"confirmed": True, "participant": "Fictional", "confirmed_at": WHEN,
                                    "external_processing": True}},
        })
        for stage, artifact in ((0, capture), (1, raw), (2, profiles)):
            write_artifact(run_dir, stage, artifact)
        return run_dir

    def test_runner_writes_a_valid_verification_artifact_and_report(self):
        run_dir = self.write_run()
        view = stage3_verify.run(run_dir, settings(), False)
        result = read_artifact(run_dir, 3)
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        self.assertIn("Verification: accepted p1", view)
        self.assertIn("99% fallback", view)
        self.assertIn("uncalibrated", view)

    def test_live_policy_uses_recorded_backend_with_mock_replay_settings(self):
        def outage(state, questions):
            raise TimeoutError

        failing = lambda settings, run_dir, stage: real_jev(outage, run_dir=run_dir, stage=stage)
        for backend, reason in (("serpapi", "review_required"), ("mock", "accepted")):
            with self.subTest(backend=backend), patch.object(stage3_verify, "Jev", failing):
                result = stage3_verify.run_stage(self.write_run(backend), settings())
                self.assertEqual(result.reason, reason)
        for mode in ("fake", "off"):
            with self.subTest(mode=mode):
                result = stage3_verify.run_stage(self.write_run("serpapi"), settings(mode))
                self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))

    def test_rerun_invalidates_old_outputs_even_if_a_prerequisite_is_missing(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                run_dir = self.write_run()
                stage3_verify.run(run_dir, settings())
                final = stage4_summarize.summarize(
                    read_artifact(run_dir, 1), read_artifact(run_dir, 2), read_artifact(run_dir, 3), settings()).final
                write_artifact(run_dir, 4, final)
                if missing:
                    (run_dir / "02_profiles.json").unlink()
                    with self.assertRaises(ArtifactError):
                        stage3_verify.run(run_dir, settings())
                else:
                    stage3_verify.run(run_dir, settings())
                self.assertEqual((run_dir / "03_verification.json").exists(), not missing)
                self.assertFalse((run_dir / "04_final.json").exists())

    def test_real_jev_calls_log_only_metadata_under_stage3(self):
        run_dir = self.write_run()
        jev = real_jev(lambda s, q: reply({"p1": 0.9}, "p1"), run_dir=run_dir, stage=3)
        result = verify(read_artifact(run_dir, 1), read_artifact(run_dir, 2), jev=jev)
        [line] = (run_dir / "run_log.jsonl").read_text().splitlines()
        event = json.loads(line)
        self.assertEqual((event["stage"], event["status"], event["model"]), (3, "ok", MODEL))
        self.assertNotIn("Alex", line)
        self.assertIn("90% jev (fallback 99%)", stage3_verify.report(result))

    def test_missing_or_mismatched_prerequisites_are_artifact_errors(self):
        run_dir = self.write_run()
        raw = read_artifact(run_dir, 1)
        raw.images[0].backend = "serpapi"
        write_artifact(run_dir, 1, raw)
        with self.assertRaisesRegex(ArtifactError, "backend|run uses"):
            stage3_verify.run_stage(run_dir, settings())
        (run_dir / "02_profiles.json").unlink()
        with self.assertRaises(ArtifactError):
            stage3_verify.run_stage(run_dir, settings())

    def test_verify_rejects_mismatched_run_ids_before_a_call(self):
        raw, profiles = seed_case()
        profiles.run_id = "other-0123abcd"
        with self.assertRaisesRegex(ValueError, "run_id differs"):
            verify(raw, profiles, jev=NoCallJev())

    def test_no_candidates_report_keeps_the_reason(self):
        self.assertIn("no accepted candidate (no_candidates)", stage3_verify.report(verify(*seed_case("unknown"))))


class CliTests(MockServerTestCase):
    """`run` and `stage 3` through the CLI, with stage 1 searching a per-class mock internet."""

    def setUp(self):
        super().setUp()
        os.environ["MOCK_INTERNET_URL"] = self.mock_url
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, "--runs-dir", str(self.runs)])
        return code, out.getvalue(), err.getvalue()

    def run_capture(self, capture, *options):
        code, out, err = self.cli("run", "--capture", str(FIXTURE_CAPTURES / capture), "--mock", *options)
        self.assertEqual(code, OK, err)
        [run_dir] = list(self.runs.iterdir())
        return run_dir, out

    def test_full_run_verifies_and_summarizes(self):
        run_dir, out = self.run_capture("alex")
        result = read_artifact(run_dir, 3)
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        self.assertEqual(round(result.verdicts[0].confidence, 2), 0.99)
        self.assertIn("Verification: accepted p1", out)
        self.assertIn("Stage 3 done.", out)
        self.assertEqual(read_artifact(run_dir, 4).status, "match")
        self.assertIn("Stage 4 done.", out)

    def test_ambiguous_run_rejects_the_namesake(self):
        run_dir, out = self.run_capture("alex", "--scenario", "ambiguous", "--until", "3")
        result = read_artifact(run_dir, 3)
        self.assertEqual([(v.profile_id, v.decision, round(v.confidence, 2)) for v in result.verdicts],
                         [("p1", "accepted", 0.98), ("p2", "rejected", 0.69)])
        self.assertFalse((run_dir / "04_final.json").exists())

    def test_unknown_run_ends_without_a_match(self):
        run_dir, out = self.run_capture("unknown")
        self.assertEqual(read_artifact(run_dir, 3).reason, "no_candidates")
        self.assertEqual(read_artifact(run_dir, 4).status, "no_match")
        self.assertIn("no accepted candidate (no_candidates)", out)

    def test_stage_3_replays_saved_profiles_and_refuses_mismatches(self):
        run_dir, _ = self.run_capture("sam", "--until", "2")
        code, out, _ = self.cli("stage", "3", "--run", str(run_dir))
        self.assertEqual(code, OK)
        self.assertEqual(read_artifact(run_dir, 3).accepted_profile_id, "p1")
        self.assertIn("Stage 3 done.", out)

        # Profiles built from another capture's evidence don't belong to this run's pages.
        write_artifact(run_dir, 2, build_profiles(collect("casey", run_id=run_dir.name), jev=None))
        code, _, err = self.cli("stage", "3", "--run", str(run_dir))
        self.assertEqual(code, REFUSED)
        self.assertIn("Refused (stage 3)", err)
        self.assertFalse((run_dir / "03_verification.json").exists())
