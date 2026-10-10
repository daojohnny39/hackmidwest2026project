"""Section 2: account extraction, grouping, and the connection graph (spec 2a-2e).

Mock cases use tests/seed_evidence.py, which builds 01_raw_evidence bundles from the
mock seed the way Section 1's deterministic collector does; CollectorEndToEndTests
checks that the real collector, over a local mock server, gives the same result.
Real-Jev behavior runs through an injected responder, so no test reaches a provider.
"""

import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pipeline.__main__ import OK, REFUSED, main
from pipeline.artifacts import create_run, read_artifact, write_artifact
from pipeline.capture import load_capture
from pipeline.config import load_settings
from pipeline.contracts import (
    Account,
    CandidateProfile,
    CaptureArtifact,
    FoundVia,
    ImageEvidence,
    ImageHit,
    LinkRef,
    PageRecord,
    RawEvidenceBundle,
    validate_references,
)
from pipeline.jev import Jev
from pipeline.stage1_collect import run_stage1
from pipeline.stage2_profiles import (
    build_graphs,
    build_profiles,
    extract_accounts,
    format_accounts,
    format_profiles,
    group_accounts,
    report,
    role_from_text,
    run_stage2,
    title_fields,
)
from tests.seed_evidence import SEARCHED_AT, collect
from tests.support import FIXTURE_CAPTURES, MockServerTestCase, OfflineTestCase


def settings(mode="fake", **env):
    keys = {"typesafe": {"TYPESAFE_API_KEY": "ts-test-key"}}
    return load_settings(backend="mock", models=mode in keys, environ={"JEV_MODE": mode, **keys.get(mode, {}), **env},
                         dotenv_path=None)


def fake_jev():
    return Jev(settings("fake"))


def profiles_for(capture, scenario="default", jev="fake"):
    return build_profiles(collect(capture, scenario), jev=fake_jev() if jev == "fake" else jev)


def by_url(bundle):
    return {account.url: account for account in bundle.accounts.values()}


def profile_urls(bundle):
    return [sorted(bundle.accounts[a].url for a in profile.account_ids) for profile in bundle.profiles]


def incoming(bundle, profile_id):
    """Target URL -> (count, trust) for one profile's graph."""
    return {c.target_url: (c.count, c.trust) for c in bundle.graphs[profile_id].connections}


def account(account_id, platform, url, *, name=None, links=(), workplace=None, location=None, extraction="json_ld",
            handle=None):
    return Account(account_id=account_id, platform=platform, url=url, handle=handle, display_name=name,
                   workplace=workplace, location=location, links_to=sorted(links), depth=0, extraction=extraction)


class DiagramParityTests(OfflineTestCase):
    def test_diagram_trust_values(self):
        # LinkedIn links to Facebook and GitHub; Instagram links to Facebook and Twitter.
        li, fb, gh, tw, ig = ("https://linkedin.com/in/johndoe", "https://facebook.com/johnnyappleseed",
                              "https://github.com/janedoe", "https://x.com/johndoe", "https://instagram.com/johndoe")
        accounts = {
            "a1": account("a1", "linkedin", li, name="John Doe", links=[fb, gh]),
            "a2": account("a2", "instagram", ig, name="John Doe", links=[fb, tw]),
            "a3": account("a3", "facebook", fb, name="Johnny Appleseed"),
            "a4": account("a4", "github", gh, name="Jane Doe"),
        }
        profiles = [CandidateProfile(profile_id="p1", name="John Doe", account_ids=["a1", "a2"], pair_decisions=[]),
                    CandidateProfile(profile_id="p2", name="Johnny Appleseed", account_ids=["a3"], pair_decisions=[]),
                    CandidateProfile(profile_id="p3", name="Jane Doe", account_ids=["a4"], pair_decisions=[])]
        graph = build_graphs(accounts, profiles)["p1"]
        rows = {c.target_url: (c.target_platform, c.count, c.trust) for c in graph.connections}
        self.assertEqual(rows, {fb: ("facebook", 2, 95), gh: ("github", 1, 50), tw: ("x", 1, 50)})
        statuses = {c.target_url: (c.target_status, c.target_profile_id) for c in graph.connections}
        self.assertEqual(statuses[fb], ("other_profile", "p2"))
        self.assertEqual(statuses[tw], ("not_collected", None))
        # Facebook's trust 95 in another profile is evidence to inspect, not a merge.
        self.assertEqual(graph.flags, ["cross_profile_link"])
        self.assertEqual((graph.mean_link_trust, graph.internal_link_density), (0.0, 0.0))


class MockScenarioTests(OfflineTestCase):
    def test_alex_default(self):
        bundle = profiles_for("alex")
        self.assertEqual(format_accounts(bundle), [
            "LinkedIn: Alex Morgan", "X: Alex M.", "Instagram: Alex M.", "GitHub: Alex Morgan", "Blog: Alex Morgan",
        ])
        self.assertEqual(len(bundle.profiles), 1)
        profile = bundle.profiles[0]
        self.assertEqual((profile.profile_id, profile.name, len(profile.account_ids)), ("p1", "Alex Morgan", 5))
        graph = bundle.graphs["p1"]
        self.assertEqual(incoming(bundle, "p1"), {
            "https://linkup.test/in/alexbuilds": (2, 95), "https://chatter.test/alexbuilds": (2, 95),
            "https://codeforge.test/alexbuilds": (2, 95), "https://fieldnotes.test/@alexbuilds": (2, 95),
        })  # Instagram has no incoming links
        self.assertEqual((graph.mean_link_trust, graph.internal_link_density), (76.0, 0.4))
        self.assertTrue(all(c.target_status == "in_profile" for c in graph.connections))
        self.assertEqual(bundle.flags, [])
        self.assertEqual([(call.provider, call.error) for call in bundle.jev_calls], [("fake", None)])

    def test_alex_default_account_fields(self):
        linkedin = by_url(profiles_for("alex"))["https://linkup.test/in/alexbuilds"]
        self.assertEqual(
            (linkedin.handle, linkedin.role, linkedin.workplace, linkedin.location, linkedin.updated_at, linkedin.extraction),
            ("alexbuilds", "Software engineer", "Prairie Atlas Labs", "Kansas City, MO", "2026-10-01", "json_ld"),
        )
        self.assertEqual(linkedin.bio, "I build open-source transit maps and volunteer at the Riverbend maker club.")
        self.assertEqual(linkedin.links_to, ["https://chatter.test/alexbuilds", "https://codeforge.test/alexbuilds"])
        photogram = by_url(profiles_for("alex"))["https://photogram.test/alexbuilds"]
        self.assertEqual((photogram.workplace, photogram.updated_at), ("Old Mill Software", "2022-05-14"))

    def test_alex_ambiguous_keeps_two_alexes_apart(self):
        bundle = profiles_for("alex", "ambiguous")
        self.assertEqual([profile.name for profile in bundle.profiles], ["Alex Morgan", "Alex Morgan"])
        kc, portland = profile_urls(bundle)
        self.assertEqual(kc, sorted(f"https://{host}/{prefix}alexbuilds" for host, prefix in [
            ("linkup.test", "in/"), ("chatter.test", ""), ("photogram.test", ""), ("codeforge.test", ""), ("fieldnotes.test", "@")]))
        self.assertEqual(portland, sorted([
            "https://linkup.test/in/alexframes", "https://chatter.test/alexframes",
            "https://fieldnotes.test/@alexframes", "https://codeforge.test/alexframes"]))
        accounts = by_url(bundle)
        self.assertEqual(accounts["https://codeforge.test/alexframes"].depth, 1)
        self.assertEqual({accounts[url].location for url in kc}, {"Kansas City, MO"})
        self.assertEqual({accounts[url].location for url in portland}, {"Portland, OR"})
        # No pair across the two profiles is merged.
        for profile in bundle.profiles:
            for decision in profile.pair_decisions:
                handles = {bundle.accounts[decision.a].handle, bundle.accounts[decision.b].handle}
                self.assertEqual(len(handles), 1, decision)

        portland_id = bundle.profiles[1].profile_id
        self.assertEqual(incoming(bundle, portland_id), {
            "https://linkup.test/in/alexframes": (2, 95), "https://chatter.test/alexframes": (1, 50),
            "https://codeforge.test/alexframes": (2, 95), "https://fieldnotes.test/@alexframes": (1, 50),
        })
        graph = bundle.graphs[portland_id]
        self.assertEqual((graph.mean_link_trust, graph.internal_link_density), (72.5, 0.5))
        self.assertEqual(bundle.graphs["p1"].mean_link_trust, 76.0)
        # "Alex Morgan Photo" joins only through the mutual link and exact handle.
        chatter = accounts["https://chatter.test/alexframes"]
        self.assertEqual(chatter.display_name, "Alex Morgan Photo")
        joins = [(d.a, d.b, d.method) for d in bundle.profiles[1].pair_decisions]
        self.assertIn((chatter.account_id, accounts["https://linkup.test/in/alexframes"].account_id, "mutual_link"), joins)

    def test_sam_alias_case(self):
        bundle = profiles_for("sam")
        self.assertEqual(len(bundle.profiles), 1)
        names = {account.platform: account.display_name for account in bundle.accounts.values()}
        self.assertEqual(names, {"linkedin": "Sam Rivera", "github": "Sam Rivera", "blog": "Sam Rivera",
                                 "x": "Sammy Rivera", "instagram": "Sammy Rivera"})
        self.assertEqual(bundle.profiles[0].name, "Sam Rivera")
        graph = bundle.graphs["p1"]
        self.assertEqual((graph.mean_link_trust, graph.internal_link_density), (76.0, 0.4))
        followed = by_url(bundle)
        for url in ("https://codeforge.test/samdraws", "https://fieldnotes.test/@samdraws"):
            self.assertEqual((followed[url].depth, followed[url].direct_image_ids, followed[url].best_hit_position), (1, [], None))
        # The private album (403) stays an unattributed snippet mention, not an account.
        self.assertNotIn("https://photogram.test/samdraws/private-album", followed)
        album = next(m for m in bundle.mentions if m.url.endswith("private-album"))
        self.assertEqual((album.kind, album.subject_account_ids), ("article", []))

    def test_casey_sparse_footprint(self):
        bundle = profiles_for("casey")
        self.assertEqual(sorted(a.display_name for a in bundle.accounts.values()), ["C. Brooks", "Casey Brooks"])
        self.assertEqual(len(bundle.profiles), 1)
        self.assertEqual(bundle.profiles[0].name, "Casey Brooks")
        self.assertEqual(set(incoming(bundle, "p1").values()), {(1, 50)})
        graph = bundle.graphs["p1"]
        self.assertEqual((graph.mean_link_trust, graph.internal_link_density), (50.0, 1.0))

    def test_stale_and_duplicates_match_default(self):
        default = profiles_for("alex")
        for scenario in ("stale", "duplicates"):
            with self.subTest(scenario=scenario):
                bundle = profiles_for("alex", scenario)
                self.assertEqual(profile_urls(bundle), profile_urls(default))
                self.assertEqual(bundle.graphs["p1"].model_dump(), default.graphs["p1"].model_dump())
        # The archived 2021 bio has no account link: a mention with a name, never an account.
        stale = profiles_for("alex", "stale")
        archive = next(m for m in stale.mentions if m.url.startswith("https://archive.test/"))
        self.assertEqual((archive.names, archive.subject_account_ids, archive.links_to), (["Alex Morgan"], [], []))

    def test_interview_is_attributed_to_alex_only(self):
        bundle = profiles_for("alex")
        interview = next(m for m in bundle.mentions if m.url == "https://riverbend-news.test/people/alex-morgan-transit")
        linkedin = by_url(bundle)["https://linkup.test/in/alexbuilds"]
        self.assertEqual(interview.subject_account_ids, [linkedin.account_id])
        self.assertIn("https://linkup.test/in/samdraws", interview.links_to)  # linked, but articles aren't followed
        self.assertEqual(interview.names, ["Alex Morgan", "Sam Rivera"])
        self.assertNotIn("https://linkup.test/in/samdraws", by_url(bundle))
        self.assertEqual(interview.image_ids, ["img-1", "img-2", "img-3"])  # the mirror merged into it

    def test_attributed_mentions_add_direct_support(self):
        bundle = profiles_for("alex")
        chatter = by_url(bundle)["https://chatter.test/alexbuilds"]
        post = next(m for m in bundle.mentions if m.kind == "post")
        self.assertEqual((post.author_account_id, post.subject_account_ids), (chatter.account_id, [chatter.account_id]))
        # Chatter's own page came from img-1 and img-2; its post adds img-3.
        self.assertEqual(chatter.direct_image_ids, ["img-1", "img-2", "img-3"])
        linkedin = by_url(bundle)["https://linkup.test/in/alexbuilds"]
        self.assertEqual((linkedin.direct_image_ids, linkedin.best_hit_position), (["img-1", "img-2", "img-3"], 1))

    def test_no_evidence_gives_empty_bundle(self):
        for capture, scenario in (("unknown", "default"), ("alex", "empty")):
            with self.subTest(capture=capture, scenario=scenario):
                bundle = profiles_for(capture, scenario)
                self.assertEqual((bundle.accounts, bundle.profiles, bundle.graphs, bundle.jev_calls), ({}, [], {}, []))

    def test_every_bundle_agrees_with_its_evidence(self):
        for capture, scenario in (("alex", "default"), ("alex", "ambiguous"), ("alex", "stale"), ("sam", "default"),
                                  ("casey", "default"), ("unknown", "default")):
            with self.subTest(capture=capture, scenario=scenario):
                raw = collect(capture, scenario)
                validate_references(raw=raw, profiles=build_profiles(raw, jev=fake_jev()))

    def test_ids_follow_urls_not_page_order(self):
        raw = collect("alex", "ambiguous")
        shuffled = raw.model_copy(update={"pages": dict(reversed(list(raw.pages.items())))})
        self.assertEqual(build_profiles(shuffled, jev=fake_jev()).model_dump(),
                         build_profiles(raw, jev=fake_jev()).model_dump())
        accounts = build_profiles(raw, jev=fake_jev()).accounts
        self.assertEqual([accounts[f"a{n}"].url for n in range(1, 10)], sorted(a.url for a in accounts.values()))


class GroupingModeTests(OfflineTestCase):
    def test_off_mode_uses_rules_without_calls(self):
        for capture, scenario in (("alex", "default"), ("alex", "ambiguous"), ("sam", "default")):
            with self.subTest(capture=capture, scenario=scenario):
                off = build_profiles(collect(capture, scenario), jev=None)
                fake = profiles_for(capture, scenario)
                self.assertEqual(profile_urls(off), profile_urls(fake))
                self.assertEqual(off.jev_calls, [])
                for profile in off.profiles:
                    for decision in profile.pair_decisions:
                        self.assertIn((decision.method, decision.probability), [("mutual_link", None), ("rule", None)])

    def test_fake_answers_are_synthetic_rule_results(self):
        bundle = profiles_for("alex", "ambiguous")
        call = bundle.jev_calls[0]
        self.assertEqual((call.provider, call.model), ("fake", None))
        self.assertEqual({answer["noul"] for answer in call.answers.values()}, {0.95, 0.05})
        methods = {(d.method, d.probability) for p in bundle.profiles for d in p.pair_decisions}
        self.assertEqual(methods, {("mutual_link", None), ("rule", 0.95)})

    def test_strong_conflicts_are_never_asked(self):
        bundle = profiles_for("alex", "ambiguous")
        asked = set(bundle.jev_calls[0].answers)
        kc_linkedin = by_url(bundle)["https://linkup.test/in/alexbuilds"].account_id
        portland_linkedin = by_url(bundle)["https://linkup.test/in/alexframes"].account_id
        pair = sorted([kc_linkedin, portland_linkedin], key=lambda i: (len(i), i))
        self.assertNotIn(f"same_{pair[0]}_{pair[1]}", asked)

    def real_jev(self, responder):
        return Jev(settings("typesafe"), responder=responder)

    def test_real_jev_answers_drive_grouping(self):
        requests = []

        def responder(state, questions):
            requests.append((state, questions))
            return {"model": "jev-1.13.0", "answers": {qid: {"type": "noul", "noul": 0.9} for qid in questions}}

        bundle = profiles_for("alex", "default", jev=self.real_jev(responder))
        self.assertEqual(len(bundle.profiles), 1)
        methods = {d.method for d in bundle.profiles[0].pair_decisions}
        self.assertEqual(methods, {"mutual_link", "jev"})
        self.assertEqual(bundle.jev_calls[0].model, "jev-1.13.0")
        state, questions = requests[0]
        self.assertEqual(set(state["accounts"]), set(bundle.accounts))
        self.assertLess(len(json.dumps(state)), 20_000)  # summaries, never whole pages
        summary = state["accounts"]["a5"]
        self.assertEqual((summary["workplace"], summary["updated_at"]), ("Old Mill Software", "2022-05-14"))
        self.assertTrue(all(q["type"] == "noul" for q in questions.values()))

    def test_below_threshold_stays_separate(self):
        bundle = profiles_for("alex", "default", jev=self.real_jev(
            lambda s, q: {"model": "m", "answers": {qid: {"type": "noul", "noul": 0.69} for qid in q}}))
        # Only the supported mutual link (LinkUp-Chatter) joins; every 0.69 answer is below 0.70.
        sizes = sorted(len(profile.account_ids) for profile in bundle.profiles)
        self.assertEqual(sizes, [1, 1, 1, 2])

    def test_jev_failure_falls_back_to_rules(self):
        def outage(state, questions):
            raise ConnectionError("down")

        bundle = profiles_for("alex", "ambiguous", jev=self.real_jev(outage))
        self.assertIn("grouping_fallback", bundle.flags)
        self.assertEqual(bundle.jev_calls[0].error, "connection_error")
        self.assertEqual(profile_urls(bundle), profile_urls(profiles_for("alex", "ambiguous")))
        self.assertTrue(all(d.method in ("mutual_link", "rule") for p in bundle.profiles for d in p.pair_decisions))

    def test_real_low_answer_vetoes_a_bridge(self):
        # a1-a2 and a2-a3 look like the same person, but Jev says a1 and a3 are not.
        accounts = {
            "a1": account("a1", "linkedin", "https://linkedin.com/in/x1", name="Alex Morgan"),
            "a2": account("a2", "github", "https://github.com/x2", name="Alex Morgan"),
            "a3": account("a3", "x", "https://x.com/x3", name="Alex Morgan"),
        }
        answers = {"same_a1_a2": 0.95, "same_a2_a3": 0.9, "same_a1_a3": 0.2}
        jev = self.real_jev(lambda s, q: {"model": "m", "answers": {k: {"type": "noul", "noul": answers[k]} for k in q}})
        profiles, _, _ = group_accounts(accounts, jev)
        self.assertEqual([p.account_ids for p in profiles], [["a1", "a2"], ["a3"]])

        # A fake 0.05 is no rule support, not a veto.
        fake_profiles, _, _ = group_accounts(accounts, fake_jev())
        self.assertEqual([p.account_ids for p in fake_profiles], [["a1"], ["a2"], ["a3"]])

    def test_conflict_guard_blocks_weak_bridges(self):
        # a2 links to both; a1 and a3 state different workplaces and cities with no link between them.
        accounts = {
            "a1": account("a1", "linkedin", "https://linkedin.com/in/kc", name="Alex Morgan",
                          workplace="Prairie Atlas Labs", location="Kansas City, MO"),
            "a2": account("a2", "blog", "https://medium.com/@hub", name="Alex Morgan",
                          links=["https://linkedin.com/in/kc", "https://linkedin.com/in/pdx"]),
            "a3": account("a3", "linkedin", "https://linkedin.com/in/pdx", name="Alex Morgan",
                          workplace="Cedar Light Studio", location="Portland, OR"),
        }
        for jev in (None, fake_jev(), self.real_jev(
                lambda s, q: {"model": "m", "answers": {k: {"type": "noul", "noul": 0.9} for k in q}})):
            with self.subTest(jev=jev and jev.provider):
                profiles, _, _ = group_accounts(accounts, jev)
                self.assertEqual([p.account_ids for p in profiles], [["a1", "a2"], ["a3"]])

    def test_shared_workplace_rule(self):
        accounts = {
            "a1": account("a1", "linkedin", "https://linkedin.com/in/a", name="Alex Morgan", workplace="Prairie Atlas Labs"),
            "a2": account("a2", "github", "https://github.com/b", name="Alex M.", workplace="prairie atlas labs"),
            "a3": account("a3", "x", "https://x.com/c", name="Alex Morgan", location="Kansas City, MO"),
        }
        profiles, _, _ = group_accounts(accounts, None)
        self.assertEqual([p.account_ids for p in profiles], [["a1", "a2"], ["a3"]])  # city alone is too weak

    def test_profile_name_ties_prefer_linkedin(self):
        accounts = {
            "a1": account("a1", "x", "https://x.com/alex", name="Alex M.", handle="alex",
                          links=["https://linkedin.com/in/alex"]),
            "a2": account("a2", "linkedin", "https://linkedin.com/in/alex", name="Alex Morgan", handle="alex",
                          links=["https://x.com/alex"]),
        }
        profiles, calls, _ = group_accounts(accounts, fake_jev())
        self.assertEqual((profiles[0].name, calls), ("Alex Morgan", []))  # mutual link: nothing left to ask


class AdversarialGroupingTests(OfflineTestCase):
    """Inputs built to tempt a false merge (spec 2b); each pair must stay apart."""

    def assert_separate(self, accounts):
        profiles, _, _ = group_accounts(accounts, fake_jev())
        self.assertEqual(len(profiles), 2)
        self.assertEqual([p.account_ids for p in profiles], [["a1"], ["a2"]])

    def test_same_name_same_city_no_link_stays_separate(self):
        # 2b: a shared name or city alone is insufficient; no mutual link and no shared workplace.
        self.assert_separate({
            "a1": account("a1", "linkedin", "https://linkedin.com/in/jordanlee-acme", name="Jordan Lee",
                          handle="jordanlee-acme", workplace="Acme Labs", location="Austin, TX"),
            "a2": account("a2", "github", "https://github.com/jlee-globex", name="Jordan Lee",
                          handle="jlee-globex", workplace="Globex", location="Austin, TX"),
        })

    def test_mutual_link_with_conflicting_names_does_not_merge(self):
        # 2b: a link merges only when the names are compatible; Chris Parker -> Dana Wong is one directed link.
        self.assert_separate({
            "a1": account("a1", "linkedin", "https://linkedin.com/in/chrisparker", name="Chris Parker",
                          handle="chrisparker", links=["https://x.com/danawong"]),
            "a2": account("a2", "x", "https://x.com/danawong", name="Dana Wong", handle="danawong"),
        })

    def test_same_handle_different_names_does_not_merge(self):
        # 2b: a handle match still needs compatible names, and Michael and Maria are not.
        self.assert_separate({
            "a1": account("a1", "github", "https://github.com/mjones", name="Michael Jones", handle="mjones"),
            "a2": account("a2", "instagram", "https://instagram.com/mjones", name="Maria Jones", handle="mjones"),
        })


class RealJevOverconfidenceTests(OfflineTestCase):
    """The AdversarialGroupingTests pairs again, with a real Jev that answers every question 0.9.

    The join guard blocks conflicting full names unless a mutual link and matching handle
    bind the pair, so a confident but wrong 0.9 can't merge two differently named people.
    """

    def grouped(self, accounts):
        def responder(state, questions):
            return {"model": "jev-test", "answers": {qid: {"type": "noul", "noul": 0.9} for qid in questions},
                    "usage": {}}

        profiles, _, _ = group_accounts(accounts, Jev(settings("typesafe"), responder=responder))
        return [p.account_ids for p in profiles]

    def assert_separate(self, accounts):
        self.assertEqual(self.grouped(accounts), [["a1"], ["a2"]])

    def test_realjev_same_name_same_city_no_link(self):
        # Accepted boundary: the same name plus a confident Jev answer is trusted. A shared city keeps the
        # conflict guard off and the names agree, so no deterministic signal overrides Jev here.
        self.assertEqual(self.grouped({
            "a1": account("a1", "linkedin", "https://linkedin.com/in/jordanlee-acme", name="Jordan Lee",
                          handle="jordanlee-acme", workplace="Acme Labs", location="Austin, TX"),
            "a2": account("a2", "github", "https://github.com/jlee-globex", name="Jordan Lee",
                          handle="jlee-globex", workplace="Globex", location="Austin, TX"),
        }), [["a1", "a2"]])

    def test_realjev_conflicting_names_one_link(self):
        # One directed link plus Jev's 0.9 must not merge Chris Parker and Dana Wong: the names conflict.
        self.assert_separate({
            "a1": account("a1", "linkedin", "https://linkedin.com/in/chrisparker", name="Chris Parker",
                          handle="chrisparker", links=["https://x.com/danawong"]),
            "a2": account("a2", "x", "https://x.com/danawong", name="Dana Wong", handle="danawong"),
        })

    def test_realjev_same_handle_different_names(self):
        # A shared handle plus Jev's 0.9 must not merge Michael and Maria Jones without a mutual link.
        self.assert_separate({
            "a1": account("a1", "github", "https://github.com/mjones", name="Michael Jones", handle="mjones"),
            "a2": account("a2", "instagram", "https://instagram.com/mjones", name="Maria Jones", handle="mjones"),
        })


class ExtractionTests(OfflineTestCase):
    def test_title_patterns(self):
        self.assertEqual(title_fields("Alex Morgan | Linkup"), {"display_name": "Alex Morgan"})
        self.assertEqual(title_fields("Alex Morgan - Software Engineer - Prairie Atlas Labs | LinkedIn"),
                         {"display_name": "Alex Morgan", "role": "Software Engineer", "workplace": "Prairie Atlas Labs"})
        self.assertEqual(title_fields("Alex Morgan (@alexbuilds) / X"), {"display_name": "Alex Morgan"})
        self.assertEqual(title_fields("alexbuilds (Alex Morgan) · GitHub"), {"display_name": "Alex Morgan"})
        self.assertEqual(title_fields("Untitled"), {})

    def test_role_sentence(self):
        text = "Alex Morgan\nPrairie Atlas Labs\nSoftware engineer at Prairie Atlas Labs. I build maps."
        self.assertEqual(role_from_text(text, "Prairie Atlas Labs"), "Software engineer")
        self.assertIsNone(role_from_text(text, "Old Mill Software"))
        self.assertIsNone(role_from_text(text, None))

    def raw_with(self, *pages, retained=None):
        hits = [ImageHit(position=p.found_via[0].hit_position, title=p.title, url=p.url, fetch_url=p.url, source="x")
                for p in pages]
        evidence = ImageEvidence(image_id="img-1", angle="front", backend="mock", searched_at=SEARCHED_AT, hits=hits,
                                 page_urls=sorted(retained if retained is not None else [p.url for p in pages]),
                                 collection_method="deterministic")
        return RawEvidenceBundle(run_id="t-00000000", num_images=1, images=[evidence],
                                 pages={p.url: p for p in pages}, url_aliases={p.url: p.url for p in pages})

    def page(self, url, rank, **fields):
        kind = fields.pop("page_kind", "profile")
        return PageRecord(url=url, source_urls=[url], fetched_url=url, status_code=fields.pop("status_code", 200),
                          platform=fields.pop("platform", "linkedin"), page_kind=kind,
                          found_via=[FoundVia(image_id="img-1", hit_position=rank, depth=0)], **fields)

    def test_structured_sources_in_priority_order(self):
        live_person = {"@type": "Person", "name": "Jordan Lee", "jobTitle": "Researcher",
                       "worksFor": [{"@type": "Organization", "name": "Northlake"}],
                       "homeLocation": {"@type": "Place", "address": {"addressLocality": "Madison", "addressRegion": "WI"}},
                       "sameAs": ["https://github.com/jordanwater", "https://example.org/not-an-account"]}
        pages = [
            self.page("https://linkedin.com/in/jordanwater", 1, title="Jordan Lee | LinkedIn", json_ld=[live_person]),
            self.page("https://x.com/jordanwater", 2, platform="x", title="Jordan Lee (@jordanwater) / X",
                      og={"og:title": "J. Lee (@jordanwater) / X", "og:description": "Lake data."}),
            self.page("https://github.com/jordanwater", 3, platform="github", title="jordanwater (Jordan Lee) · GitHub"),
            self.page("https://instagram.com/jordanwater", 4, platform="instagram", title="Instagram"),
        ]
        accounts = by_url(build_profiles(self.raw_with(*pages)))
        linkedin = accounts["https://linkedin.com/in/jordanwater"]
        self.assertEqual((linkedin.extraction, linkedin.role, linkedin.workplace, linkedin.location),
                         ("json_ld", "Researcher", "Northlake", "Madison, WI"))
        self.assertEqual(linkedin.links_to, ["https://github.com/jordanwater"])
        x = accounts["https://x.com/jordanwater"]
        self.assertEqual((x.display_name, x.bio, x.extraction), ("J. Lee", "Lake data.", "meta"))
        self.assertEqual((accounts["https://github.com/jordanwater"].display_name,
                          accounts["https://github.com/jordanwater"].extraction), ("Jordan Lee", "title"))
        instagram = accounts["https://instagram.com/jordanwater"]
        self.assertEqual((instagram.display_name, instagram.handle, instagram.extraction), (None, "jordanwater", "partial"))
        self.assertEqual(format_accounts(build_profiles(self.raw_with(pages[3])))[0], "Instagram: @jordanwater")

    def test_snippet_accounts_have_no_edges(self):
        snippet = self.page("https://linkedin.com/in/walled", 1, title="Pat Doe - Analyst - Acme | LinkedIn",
                            text="Pat Doe - Analyst at Acme", extraction="snippet", status_code=403)
        bundle = build_profiles(self.raw_with(snippet))
        walled = bundle.accounts["a1"]
        self.assertEqual((walled.extraction, walled.display_name, walled.role, walled.workplace, walled.updated_at),
                         ("snippet", "Pat Doe", "Analyst", "Acme", None))
        self.assertEqual((walled.links_to, bundle.graphs["p1"].connections), ([], []))

    def test_excluded_observations_do_not_count(self):
        kept = self.page("https://linkedin.com/in/kept", 1, title="Kept Person | LinkedIn")
        dropped = self.page("https://x.com/dropped", 2, platform="x", title="Dropped (@dropped) / X", relevant=False)
        bundle = build_profiles(self.raw_with(kept, dropped, retained=[kept.url]))
        self.assertEqual([a.url for a in bundle.accounts.values()], [kept.url])

    def test_accounts_are_truncated_by_rank(self):
        pages = [self.page(f"https://github.com/user{n:02d}", n, platform="github", title=f"user{n:02d} · GitHub")
                 for n in range(1, 15)]
        accounts, _, flags = extract_accounts(self.raw_with(*pages))
        self.assertEqual(flags, ["accounts_truncated"])
        self.assertEqual(sorted(a.best_hit_position for a in accounts.values()), list(range(1, 13)))

    def test_llm_fills_only_supported_missing_fields(self):
        page = self.page("https://linkedin.com/in/pat", 1, title="LinkedIn",
                         text="Pat Doe\nData analyst at Acme Corp in Tulsa, OK.")
        calls = []

        def extractor(pages):
            calls.append(pages)
            return {"fields_by_url": {
                "https://linkedin.com/in/pat": {"display_name": "Pat Doe", "role": "Data analyst",
                                                "workplace": "Acme Corp", "location": "Tulsa, OK", "bio": "Loves hiking"},
                "https://invented.example/pat": {"display_name": "Pat Doe"},
            }}

        accounts, _, flags = extract_accounts(self.raw_with(page), llm_extract=extractor)
        pat = accounts["a1"]
        self.assertEqual((pat.display_name, pat.role, pat.workplace, pat.location, pat.bio, pat.extraction),
                         ("Pat Doe", "Data analyst", "Acme Corp", "Tulsa, OK", None, "llm"))
        self.assertEqual(flags, ["llm_rejected_url", "llm_rejected_value"])
        self.assertEqual([p["url"] for p in calls[0]], ["https://linkedin.com/in/pat"])
        self.assertLessEqual(len(calls[0][0]["text"]), 2000)

        def broken(pages):
            raise TimeoutError

        accounts, _, flags = extract_accounts(self.raw_with(page), llm_extract=broken)
        self.assertEqual((accounts["a1"].extraction, flags), ("partial", ["llm_extraction_failed"]))

    def test_llm_is_not_called_for_complete_accounts(self):
        def extractor(pages):
            raise AssertionError("complete accounts need no LLM call")

        accounts, _, flags = extract_accounts(collect("casey"), llm_extract=extractor)
        self.assertEqual(flags, [])
        self.assertEqual(len(accounts), 2)

    def test_duplicate_html_and_same_as_links_count_once(self):
        person = {"@type": "Person", "name": "Casey Brooks", "sameAs": ["https://x.com/caseykeys"]}
        source = self.page("https://linkedin.com/in/caseykeys", 1, title="Casey Brooks | LinkedIn", json_ld=[person],
                           outbound_links=[LinkRef(url="https://x.com/caseykeys", label="X"),
                                           LinkRef(url="https://x.com/caseykeys?utm_source=li", label="again")])
        target = self.page("https://x.com/caseykeys", 2, platform="x", title="C. Brooks (@caseykeys) / X")
        bundle = build_profiles(self.raw_with(source, target))
        self.assertEqual(len(bundle.profiles), 1)
        self.assertEqual(incoming(bundle, "p1"), {"https://x.com/caseykeys": (1, 50)})


class CollectorEndToEndTests(MockServerTestCase):
    """The real Section 1 collector over HTTP, then Section 2, for every mock-matrix row (spec 2e)."""

    def stage2_after_stage1(self, capture, scenario="default"):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        loaded = load_capture(FIXTURE_CAPTURES / capture)
        run_id, run_dir = create_run(Path(tmp), loaded.bundle.session_id)
        write_artifact(run_dir, 0, CaptureArtifact(
            run_id=run_id, capture=loaded.bundle, capture_dir=str(loaded.capture_dir), backend="mock",
            scenario=scenario, llm_mode="off", jev_mode="fake"))
        settings = load_settings(backend="mock", environ={"MOCK_INTERNET_URL": self.mock_url, "JEV_MODE": "fake"},
                                 dotenv_path=None)
        run_stage1(run_dir, settings)
        return run_stage2(run_dir, settings), run_id

    def test_matches_seed_built_evidence(self):
        for capture, scenario in (("alex", "default"), ("alex", "ambiguous"), ("alex", "stale"), ("alex", "duplicates"),
                                  ("alex", "empty"), ("sam", "default"), ("casey", "default"), ("unknown", "default")):
            with self.subTest(capture=capture, scenario=scenario):
                collected, run_id = self.stage2_after_stage1(capture, scenario)
                expected = build_profiles(collect(capture, scenario, run_id=run_id), jev=fake_jev())
                self.assertEqual(collected.model_dump(exclude={"jev_calls"}), expected.model_dump(exclude={"jev_calls"}))
                self.assertEqual([c.answers for c in collected.jev_calls], [c.answers for c in expected.jev_calls])

    def test_alex_targets_through_the_collector(self):
        bundle, _ = self.stage2_after_stage1("alex", "ambiguous")
        self.assertEqual([(p.name, len(p.account_ids)) for p in bundle.profiles], [("Alex Morgan", 5), ("Alex Morgan", 4)])
        self.assertEqual([(g.mean_link_trust, g.internal_link_density) for g in bundle.graphs.values()],
                         [(76.0, 0.4), (72.5, 0.5)])


class StageRunTests(OfflineTestCase):
    def make_run(self, tmp, capture="alex", scenario="default"):
        loaded = load_capture(FIXTURE_CAPTURES / capture)
        run_id, run_dir = create_run(Path(tmp), loaded.bundle.session_id)
        write_artifact(run_dir, 0, CaptureArtifact(
            run_id=run_id, capture=loaded.bundle, capture_dir=str(loaded.capture_dir), backend="mock",
            scenario=scenario, llm_mode="off", jev_mode="fake"))
        write_artifact(run_dir, 1, collect(capture, scenario, run_id=run_id))
        return run_dir

    def test_run_stage2_writes_a_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(tmp)
            (run_dir / "03_verification.json").write_text("{}")  # stale later output
            bundle = run_stage2(run_dir, settings("fake"))
            self.assertEqual(read_artifact(run_dir, 2), bundle)
            self.assertFalse((run_dir / "03_verification.json").exists())
            log = [json.loads(line) for line in (run_dir / "run_log.jsonl").read_text().splitlines()]
        self.assertEqual([(e["stage"], e["backend"], e["status"]) for e in log], [(2, "jev:fake", "ok")])
        self.assertNotIn("Alex", json.dumps(log))

    def test_off_mode_makes_no_jev_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(tmp, "sam")
            bundle = run_stage2(run_dir, settings("off"))
            self.assertFalse((run_dir / "run_log.jsonl").exists())
        self.assertEqual((len(bundle.profiles), bundle.jev_calls), (1, []))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_stage_2_replays_saved_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            self.assertEqual(self.cli("run", "--capture", str(FIXTURE_CAPTURES / "sam"), "--mock", "--until", "0",
                                      "--runs-dir", str(runs))[0], OK)
            [run_dir] = list(runs.iterdir())
            code, _, err = self.cli("stage", "2", "--run", str(run_dir), "--runs-dir", str(runs))
            self.assertEqual(code, REFUSED)
            self.assertIn("01_raw_evidence.json", err)

            write_artifact(run_dir, 1, collect("sam", run_id=run_dir.name))
            code, out, _ = self.cli("stage", "2", "--run", str(run_dir), "--runs-dir", str(runs))
            self.assertEqual(code, OK)
            self.assertIn("profile p1: Sam Rivera { LinkedIn: Sam Rivera, X: Sammy Rivera", out)
            self.assertIn("Stage 2 done.", out)
            self.assertEqual(len(read_artifact(run_dir, 2).profiles), 1)

            # Evidence from another capture doesn't match this run's images.
            write_artifact(run_dir, 1, collect("alex", run_id=run_dir.name))
            code, _, err = self.cli("stage", "2", "--run", str(run_dir), "--runs-dir", str(runs))
            self.assertEqual(code, REFUSED)
            self.assertIn("doesn't match", err)
            self.assertFalse((run_dir / "02_profiles.json").exists())

    def test_report_shows_the_three_views(self):
        text = report(profiles_for("alex", "ambiguous"))
        self.assertIn("Accounts:\n  LinkedIn: Alex Morgan", text)
        self.assertIn("profile p2: Alex Morgan { LinkedIn: Alex Morgan, X: Alex Morgan Photo, GitHub: Alex Morgan, "
                      "Blog: Alex Morgan }", text)
        self.assertIn("graph p2: Alex Morgan (mean link trust 72.5, internal link density 0.50)", text)
        self.assertIn("Instagram 0 connections, trust  0  in profile  https://photogram.test/alexbuilds", text)
        self.assertEqual(format_profiles(profiles_for("unknown")), [])
        self.assertIn("Accounts:\n  (none)", report(profiles_for("unknown")))
