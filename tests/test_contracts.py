"""Contract tests (spec 0.5/0.6): full artifacts round-trip through ARTIFACT_ADAPTERS,
and every validator and cross-artifact reference rule rejects a focused violation.

The fixtures follow the mock `alex` default case: 3 images, Alex's 5 accounts in one
profile, an accepted fallback verdict, and a FinalProfile. The `unknown` case and a
live-style `review_required` variant cover NoMatch.
"""

import json
import math

from pydantic import ValidationError

from pipeline.contracts import (
    ARTIFACT_ADAPTERS,
    FinalProfile,
    ImageTriage,
    NoMatch,
    validate_references,
)
from tests.support import OfflineTestCase

RUN_ID = "mock-alex-0123abcd"
WHEN = "2026-10-10T14:03:00Z"
STAGE = {"capture": 0, "raw": 1, "profiles": 2, "verification": 3, "final": 4}

LINKUP = "https://linkup.test/in/alexbuilds"
CHATTER = "https://chatter.test/alexbuilds"
PHOTOGRAM = "https://photogram.test/alexbuilds"
CODEFORGE = "https://codeforge.test/alexbuilds"
FIELDNOTES = "https://fieldnotes.test/@alexbuilds"
INTERVIEW = "https://riverbend-news.test/people/alex-morgan-transit"
MIRROR = "https://syndicated.test/transit/alex-morgan"
POST = "https://chatter.test/alexbuilds/status/101"
ARCHIVE = "https://archive.test/speakers/alex-morgan-2021"
OLD_PROJECT = "https://fieldnotes.test/@alexbuilds/old-project"
SAM_LINKUP = "https://linkup.test/in/samdraws"

HITS = {  # alex-front, alex-left, alex-right, in rank order: 4 + 4 + 5 = 13 hits
    "img-1": [LINKUP, CHATTER, INTERVIEW, CODEFORGE],
    "img-2": [PHOTOGRAM, FIELDNOTES, CHATTER, MIRROR],
    "img-3": [INTERVIEW, CODEFORGE, POST, ARCHIVE, OLD_PROJECT],
}
PAGE_URLS = {  # after the mirror merges into the interview; img-1 also follows Chatter to FieldNotes
    "img-1": [LINKUP, CHATTER, INTERVIEW, CODEFORGE, FIELDNOTES],
    "img-2": [PHOTOGRAM, FIELDNOTES, CHATTER, INTERVIEW],
    "img-3": [INTERVIEW, CODEFORGE, POST, ARCHIVE, OLD_PROJECT],
}
BIO = "I build open-source transit maps and volunteer at the Riverbend maker club."

# Fallback formula (3.2) for Alex: image_support 1, top_hit_share 1, trust 76, 5 platforms, consistency 0.8.
CONFIDENCE = 1 / (1 + math.exp(-(-4 + 2 + 3 + 1.5 * 0.76 + 0.3 * 5 + 0.8)))


def local(url):
    return "http://127.0.0.1:8765/web/" + url.split("://", 1)[1]


def hit(image_id, position):
    return {"image_id": image_id, "hit_position": position, "depth": 0}


def page(url, platform, kind, found_via, links=(), **extra):
    return {
        "url": url, "source_urls": [url], "fetched_url": local(url), "status_code": 200,
        "platform": platform, "page_kind": kind, "found_via": found_via,
        "outbound_links": [{"url": target, "label": label} for target, label in links],
        "published_at": "2026-10-01", **extra,
    }


def conn(url, platform, sources, status="in_profile", trust=95, profile_id=None):
    return {"target_url": url, "target_platform": platform, "count": len(sources), "sources": sources,
            "trust": trust, "target_status": status, "target_profile_id": profile_id}


def capture():
    images = {"img-1": ("front", "alex-front"), "img-2": ("left", "alex-left"), "img-3": ("right", "alex-right")}
    return {
        "run_id": RUN_ID,
        "capture": {
            "session_id": "mock-alex",
            "captured_at": "2026-10-10T14:02:11Z",
            "consent": {"confirmed": True, "confirmed_at": "2026-10-10T14:01:50Z", "external_processing": True},
            "subject": {"first_name": "Alex", "last_name": "Morgan", "city": "", "age": None},
            "images": [{"image_id": image_id, "angle": angle, "mock_fixture": fixture}
                       for image_id, (angle, fixture) in images.items()],
        },
        "capture_dir": "tests/fixtures/captures/alex",
        "backend": "mock", "scenario": "default", "llm_mode": "off", "jev_mode": "fake",
    }


def raw():
    person = {"@type": "Person", "name": "Alex Morgan", "worksFor": {"@type": "Organization", "name": "Prairie Atlas Labs"},
              "homeLocation": "Kansas City, MO", "sameAs": [CHATTER, CODEFORGE]}
    pages = [
        page(LINKUP, "linkedin", "profile", [hit("img-1", 1)], [(CHATTER, "Social profile"), (CODEFORGE, "Projects")],
             title="Alex Morgan | Linkup", json_ld=[person], text=f"Software engineer at Prairie Atlas Labs. {BIO}"),
        page(CHATTER, "x", "profile", [hit("img-1", 2), hit("img-2", 3)],
             [(LINKUP, "Work profile"), (FIELDNOTES, "My writing"), (POST, "Recent post 1")], title="Alex M. | Chatter"),
        page(PHOTOGRAM, "instagram", "profile", [hit("img-2", 1)], [(CHATTER, "Updates"), (FIELDNOTES, "Read my blog")],
             title="Alex M. | Photogram", published_at="2022-05-14"),
        page(CODEFORGE, "github", "profile", [hit("img-1", 4), hit("img-3", 2)], title="Alex Morgan | Codeforge"),
        page(FIELDNOTES, "blog", "profile",
             [hit("img-2", 2), {"image_id": "img-1", "hit_position": None, "depth": 1, "parent_url": CHATTER}],
             [(LINKUP, "About the author"), (CODEFORGE, "Source code"), (OLD_PROJECT, "My old project")],
             title="Alex Morgan | Fieldnotes"),
        page(INTERVIEW, "news", "article", [hit("img-1", 3), hit("img-2", 4), hit("img-3", 1)],
             [(LINKUP, "Alex Morgan"), (SAM_LINKUP, "Sam Rivera")], source_urls=[INTERVIEW, MIRROR],
             canonical_url=INTERVIEW, title="Alex Morgan helps neighbors navigate Riverbend transit"),
        page(POST, "x", "post", [hit("img-3", 3)], [(CHATTER, "Author")], published_at="2026-10-03"),
        page(ARCHIVE, "other", "article", [hit("img-3", 4)], published_at="2021-06-12"),
        page(OLD_PROJECT, "blog", "article", [hit("img-3", 5)], status_code=404, extraction="snippet",
             title="Alex Morgan: an old transit project", text="An old transit project.", published_at=None),
    ]
    angles = {"img-1": "front", "img-2": "left", "img-3": "right"}
    images = [
        {
            "image_id": image_id, "angle": angles[image_id], "backend": "mock", "request_id": f"mock-{image_id}",
            "searched_at": WHEN, "page_urls": list(PAGE_URLS[image_id]), "collection_method": "deterministic",
            "search_extras": {"engine": "yandex_images", "tab": "about"},
            "hits": [{"position": rank, "title": f"hit {rank}", "url": url, "fetch_url": local(url),
                      "source": url.split("/")[2], "snippet": "Alex Morgan"}
                     for rank, url in enumerate(urls, start=1)],
            "errors": [{"url": OLD_PROJECT, "status_code": 404, "reason": "not_found"}] if image_id == "img-3" else [],
        }
        for image_id, urls in HITS.items()
    ]
    return {
        "run_id": RUN_ID, "num_images": 3, "images": images,
        "pages": {record["url"]: record for record in pages},
        "url_aliases": {**{record["url"]: record["url"] for record in pages}, MIRROR: INTERVIEW},
    }


def profiles():
    alex = {"handle": "alexbuilds", "bio": BIO, "role": "Software engineer", "workplace": "Prairie Atlas Labs",
            "location": "Kansas City, MO", "updated_at": "2026-10-01", "depth": 0, "extraction": "json_ld"}
    accounts = [
        {**alex, "account_id": "a1", "platform": "x", "url": CHATTER, "display_name": "Alex M.",
         "links_to": [LINKUP, FIELDNOTES], "image_ids": ["img-1", "img-2", "img-3"],
         "direct_image_ids": ["img-1", "img-2", "img-3"], "best_hit_position": 2},
        {**alex, "account_id": "a2", "platform": "github", "url": CODEFORGE, "display_name": "Alex Morgan",
         "image_ids": ["img-1", "img-3"], "direct_image_ids": ["img-1", "img-3"], "best_hit_position": 2},
        {**alex, "account_id": "a3", "platform": "blog", "url": FIELDNOTES, "display_name": "Alex Morgan",
         "links_to": [LINKUP, CODEFORGE], "image_ids": ["img-1", "img-2"], "direct_image_ids": ["img-2"],
         "best_hit_position": 2},
        {**alex, "account_id": "a4", "platform": "linkedin", "url": LINKUP, "display_name": "Alex Morgan",
         "links_to": [CHATTER, CODEFORGE], "image_ids": ["img-1", "img-2", "img-3"],
         "direct_image_ids": ["img-1", "img-2", "img-3"], "best_hit_position": 1},
        {**alex, "account_id": "a5", "platform": "instagram", "url": PHOTOGRAM, "display_name": "Alex M.",
         "workplace": "Old Mill Software", "updated_at": "2022-05-14", "links_to": [CHATTER, FIELDNOTES],
         "image_ids": ["img-2"], "direct_image_ids": ["img-2"], "best_hit_position": 1},
    ]
    pairs = [("a1", "a4", None, "mutual_link"), ("a1", "a3", 0.95, "rule"), ("a1", "a5", 0.95, "rule"),
             ("a2", "a4", 0.95, "rule"), ("a3", "a4", 0.95, "rule")]
    return {
        "run_id": RUN_ID,
        "accounts": {account["account_id"]: account for account in accounts},
        "mentions": [
            {"url": INTERVIEW, "kind": "article", "subject_account_ids": ["a4"], "names": ["Alex Morgan", "Sam Rivera"],
             "links_to": [LINKUP, SAM_LINKUP], "image_ids": ["img-1", "img-2", "img-3"], "published_at": "2026-10-01"},
            {"url": POST, "kind": "post", "author_account_id": "a1", "subject_account_ids": ["a1"],
             "names": ["Alex M."], "links_to": [CHATTER], "image_ids": ["img-3"], "published_at": "2026-10-03"},
            {"url": ARCHIVE, "kind": "article", "names": ["Alex Morgan"], "image_ids": ["img-3"],
             "published_at": "2021-06-12"},
            {"url": OLD_PROJECT, "kind": "article", "image_ids": ["img-3"]},
        ],
        "profiles": [{
            "profile_id": "p1", "name": "Alex Morgan", "account_ids": ["a1", "a2", "a3", "a4", "a5"],
            "pair_decisions": [{"a": a, "b": b, "probability": p, "method": m} for a, b, p, m in pairs],
        }],
        "graphs": {"p1": {
            "profile_id": "p1", "mean_link_trust": 76.0, "internal_link_density": 0.4,
            "connections": [conn(LINKUP, "linkedin", ["a1", "a3"]), conn(CHATTER, "x", ["a4", "a5"]),
                            conn(CODEFORGE, "github", ["a3", "a4"]), conn(FIELDNOTES, "blog", ["a1", "a5"])],
        }},
        "jev_calls": [{"provider": "fake", "answers": {f"same_{a}_{b}": {"noul": p} for a, b, p, _ in pairs if p},
                       "usage": {}}],
    }


def split_photogram(data, profile_id="p2"):
    """Move Photogram (a5) into its own profile; its links become cross-profile edges."""
    p1 = data["profiles"][0]
    p1["account_ids"].remove("a5")
    p1["pair_decisions"] = [pair for pair in p1["pair_decisions"] if "a5" not in (pair["a"], pair["b"])]
    data["profiles"].append({"profile_id": profile_id, "name": "Alex M.", "account_ids": ["a5"], "pair_decisions": []})
    data["graphs"]["p1"].update(mean_link_trust=72.5, internal_link_density=0.5, connections=[
        conn(LINKUP, "linkedin", ["a1", "a3"]), conn(CHATTER, "x", ["a4"], trust=50),
        conn(CODEFORGE, "github", ["a3", "a4"]), conn(FIELDNOTES, "blog", ["a1"], trust=50)])
    data["graphs"][profile_id] = {
        "profile_id": profile_id, "mean_link_trust": 0.0, "internal_link_density": 0.0, "flags": [],
        "connections": [conn(CHATTER, "x", ["a5"], "other_profile", 50, "p1"),
                        conn(FIELDNOTES, "blog", ["a5"], "other_profile", 50, "p1")],
    }
    return data


def verdict(profile_id="p1", confidence=CONFIDENCE, decision="accepted", name="Alex Morgan"):
    return {
        "profile_id": profile_id, "name": name, "confidence": confidence, "fallback_confidence": confidence,
        "method": "fallback", "decision": decision, "rationale": "Three images retrieved linked accounts.",
        "signals": {"image_support": 1.0, "top_hit_share": 1.0, "mean_link_trust": 76.0, "platform_count": 5,
                    "consistency": 0.8, "name_collision": False, "newest_update": "2026-10-01"},
    }


def verification():
    return {"run_id": RUN_ID, "verdicts": [verdict()], "accepted_profile_id": "p1", "reason": "accepted", "jev_calls": []}


def verification_review():
    """Live fallback policy (3.2): the same candidate, but no automatic match."""
    return {**verification(), "verdicts": [verdict(decision="rejected")], "accepted_profile_id": None,
            "reason": "review_required"}


def final():
    linkup = [LINKUP]
    return {
        "status": "match", "run_id": RUN_ID,
        "name": {"value": "Alex Morgan", "sources": linkup},
        "role": {"value": "Software engineer", "sources": linkup},
        "workplace": {"value": "Prairie Atlas Labs", "sources": [LINKUP, INTERVIEW]},
        "location": {"value": "Kansas City, MO", "sources": linkup},
        "bio": {"value": BIO, "sources": linkup},
        "accounts": [{"platform": platform, "url": url, "display_name": name, "trust": trust}
                     for platform, url, name, trust in (
                         ("linkedin", LINKUP, "Alex Morgan", 95), ("x", CHATTER, "Alex M.", 95),
                         ("instagram", PHOTOGRAM, "Alex M.", 0), ("github", CODEFORGE, "Alex Morgan", 95),
                         ("blog", FIELDNOTES, "Alex Morgan", 95))],
        "confidence": CONFIDENCE, "method": "fallback", "flags": [], "generated_at": WHEN,
    }


def no_match_review():
    return {"status": "no_match", "run_id": RUN_ID, "reason": "review_required", "generated_at": WHEN,
            "top_candidates": [{"profile_id": "p1", "name": "Alex Morgan", "confidence": CONFIDENCE,
                                "method": "fallback"}]}


def unknown_case():
    """The `unknown` fixture: one image, zero hits, no candidates, NoMatch."""
    run_id = "mock-unknown-89abcdef"
    return {
        "capture": {**capture(), "run_id": run_id, "capture_dir": "tests/fixtures/captures/unknown", "capture": {
            **capture()["capture"], "session_id": "mock-unknown",
            "images": [{"image_id": "img-1", "angle": "front", "mock_fixture": "unknown"}]}},
        "raw": {"run_id": run_id, "num_images": 1, "pages": {}, "url_aliases": {}, "images": [{
            "image_id": "img-1", "angle": "front", "backend": "mock", "request_id": "mock-unknown",
            "searched_at": WHEN, "hits": [], "page_urls": [], "collection_method": "deterministic"}]},
        "profiles": {"run_id": run_id, "accounts": {}, "mentions": [], "profiles": [], "graphs": {}, "jev_calls": []},
        "verification": {"run_id": run_id, "verdicts": [], "accepted_profile_id": None, "reason": "no_candidates",
                         "jev_calls": []},
        "final": {"status": "no_match", "run_id": run_id, "reason": "no_candidates", "top_candidates": [],
                  "generated_at": WHEN},
    }


def load(name, data):
    return ARTIFACT_ADAPTERS[STAGE[name]].validate_python(data)


def alex(**changed):
    """Validated alex artifacts keyed like validate_references; `changed` replaces raw dicts."""
    data = {"capture": capture(), "raw": raw(), "profiles": profiles(), "verification": verification(), "final": final()}
    data.update(changed)
    return {name: load(name, value) for name, value in data.items()}


class ContractTestCase(OfflineTestCase):
    def assertRejected(self, name, data, fragment):
        with self.assertRaises(ValidationError) as caught:
            load(name, data)
        self.assertIn(fragment, str(caught.exception))

    def assertDisagree(self, fragment, **artifacts):
        with self.assertRaises(ValueError) as caught:
            validate_references(**artifacts)
        self.assertIn(fragment, str(caught.exception))


class RoundTripTests(ContractTestCase):
    def assertRoundTrips(self, name, data):
        adapter = ARTIFACT_ADAPTERS[STAGE[name]]
        artifact = adapter.validate_python(data)
        self.assertEqual(adapter.validate_json(adapter.dump_json(artifact)), artifact)

    def test_alex_artifacts_round_trip(self):
        data = {"capture": capture(), "raw": raw(), "profiles": profiles(), "verification": verification(),
                "final": final()}
        for name, value in data.items():
            with self.subTest(stage=name):
                self.assertRoundTrips(name, value)

    def test_unknown_artifacts_round_trip(self):
        for name, value in unknown_case().items():
            with self.subTest(stage=name):
                self.assertRoundTrips(name, value)

    def test_review_required_artifacts_round_trip(self):
        self.assertRoundTrips("verification", verification_review())
        self.assertRoundTrips("final", no_match_review())

    def test_every_stage_has_an_adapter(self):
        self.assertEqual(sorted(ARTIFACT_ADAPTERS), [0, 1, 2, 3, 4])

    def test_final_output_dispatches_on_status(self):
        self.assertIsInstance(load("final", final()), FinalProfile)
        self.assertIsInstance(load("final", no_match_review()), NoMatch)

    def test_alex_counts_match_the_spec(self):
        evidence, grouped = load("raw", raw()), load("profiles", profiles())
        self.assertEqual(sum(len(image.hits) for image in evidence.images), 13)
        self.assertEqual(len({hit.url for image in evidence.images for hit in image.hits}), 10)
        self.assertEqual(len(evidence.pages), 9)
        self.assertEqual(round(CONFIDENCE, 2), 0.99)
        graph = grouped.graphs["p1"]
        trust = {connection.target_url: connection.trust for connection in graph.connections}
        mean = sum(trust.get(account.url, 0) for account in grouped.accounts.values()) / len(grouped.accounts)
        self.assertEqual(mean, graph.mean_link_trust)


class ReferenceAgreementTests(ContractTestCase):
    def test_alex_artifacts_agree(self):
        validate_references(**alex())

    def test_unknown_artifacts_agree(self):
        validate_references(**{name: load(name, value) for name, value in unknown_case().items()})

    def test_review_required_artifacts_agree(self):
        validate_references(**alex(verification=verification_review(), final=no_match_review()))

    def test_partial_sets_check_only_given_pairs(self):
        artifacts = alex()
        validate_references()
        validate_references(raw=artifacts["raw"], final=artifacts["final"])
        validate_references(capture=artifacts["capture"], profiles=artifacts["profiles"])

    def test_final_source_resolves_through_url_aliases(self):
        data = final()
        data["workplace"]["sources"] = [LINKUP, MIRROR]
        validate_references(**alex(final=data))

    def test_cross_profile_connections_validate(self):
        grouped = load("profiles", split_photogram(profiles()))
        self.assertEqual(grouped.graphs["p2"].connections[0].target_profile_id, "p1")


class FieldRuleTests(ContractTestCase):
    def test_unknown_fields_are_rejected(self):
        data = raw()
        data["images"][0]["unexpected"] = 1
        self.assertRejected("raw", data, "Extra inputs are not permitted")

    def test_run_id_must_be_a_safe_path_component(self):
        self.assertRejected("verification", {**verification(), "run_id": "../escape"}, "safe path component")

    def test_probability_is_at_most_one(self):
        data = profiles()
        data["profiles"][0]["pair_decisions"][1]["probability"] = 1.5
        self.assertRejected("profiles", data, "less than or equal to 1")

    def test_trust_is_at_most_100(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0]["trust"] = 101
        self.assertRejected("profiles", data, "less than or equal to 100")

    def test_mean_link_trust_is_at_most_100(self):
        data = profiles()
        data["graphs"]["p1"]["mean_link_trust"] = 100.5
        self.assertRejected("profiles", data, "less than or equal to 100")

    def test_depth_is_zero_or_one(self):
        data = profiles()
        data["accounts"]["a1"]["depth"] = 2
        self.assertRejected("profiles", data, "Input should be 0 or 1")

    def test_datetimes_need_a_timezone(self):
        data = raw()
        data["images"][0]["searched_at"] = "2026-10-10T14:03:00"
        self.assertRejected("raw", data, "timezone")

    def test_floats_reject_nan(self):
        data = verification()
        data["verdicts"][0]["signals"]["consistency"] = float("nan")
        self.assertRejected("verification", data, "finite number")

    def test_free_form_json_rejects_nan_from_disk(self):
        data = profiles()
        data["jev_calls"][0]["usage"] = {"tokens": float("nan")}
        with self.assertRaises(ValidationError) as caught:
            ARTIFACT_ADAPTERS[2].validate_json(json.dumps(data))
        self.assertIn("NaN and infinity are not allowed", str(caught.exception))

    def test_hit_positions_start_at_one(self):
        data = raw()
        data["images"][0]["hits"][0]["position"] = 0
        self.assertRejected("raw", data, "greater than or equal to 1")

    def test_http_status_is_bounded(self):
        data = raw()
        data["pages"][ARCHIVE]["status_code"] = 42
        self.assertRejected("raw", data, "greater than or equal to 100")

    def test_page_text_is_capped_at_4000_characters(self):
        data = raw()
        data["pages"][ARCHIVE]["text"] = "x" * 4001
        self.assertRejected("raw", data, "at most 4000 characters")

    def test_connection_count_is_not_negative(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0]["count"] = -1
        self.assertRejected("profiles", data, "greater than or equal to 0")

    def test_sourced_needs_a_source(self):
        data = final()
        data["bio"]["sources"] = []
        self.assertRejected("final", data, "at least 1 item")

    def test_no_match_lists_at_most_three_candidates(self):
        data = no_match_review()
        data["top_candidates"] = [{**data["top_candidates"][0], "profile_id": f"p{n}"} for n in range(1, 5)]
        self.assertRejected("final", data, "at most 3 items")

    def test_final_output_needs_a_known_status(self):
        self.assertRejected("final", {**final(), "status": "maybe"}, "does not match any of the expected tags")

    def test_triage_output_is_bounded_and_strict(self):
        judgment = {"url": LINKUP, "relevant": True}
        with self.assertRaises(ValidationError):
            ImageTriage.model_validate({"judgments": [judgment] * 51})
        with self.assertRaises(ValidationError):
            ImageTriage.model_validate({"judgments": [{**judgment, "relevant": "yes"}]})


class RawEvidenceRuleTests(ContractTestCase):
    def test_num_images_matches_images(self):
        self.assertRejected("raw", {**raw(), "num_images": 2}, "num_images is 2")

    def test_image_ids_are_unique(self):
        data = raw()
        data["images"][1]["image_id"] = "img-1"
        self.assertRejected("raw", data, "duplicate image IDs: img-1")

    def test_aliases_point_at_pages(self):
        data = raw()
        data["url_aliases"]["https://elsewhere.test/a"] = "https://elsewhere.test/b"
        self.assertRejected("raw", data, "which is not a pages key")

    def test_pages_keys_match_record_urls(self):
        data = raw()
        data["pages"][ARCHIVE]["url"] = ARCHIVE + "/copy"
        self.assertRejected("raw", data, f"pages key {ARCHIVE} holds the record")

    def test_source_urls_alias_to_their_page(self):
        data = raw()
        del data["url_aliases"][MIRROR]
        self.assertRejected("raw", data, f"source URL {MIRROR} of page {INTERVIEW} must alias to it")

    def test_found_via_names_a_bundle_image(self):
        data = raw()
        data["pages"][ARCHIVE]["found_via"][0]["image_id"] = "img-9"
        self.assertRejected("raw", data, "found via unknown image img-9")

    def test_found_via_position_matches_a_hit(self):
        data = raw()
        data["pages"][ARCHIVE]["found_via"][0]["hit_position"] = 1
        self.assertRejected("raw", data, "has no hit for it at position 1")

    def test_search_hit_needs_a_position(self):
        data = raw()
        data["pages"][ARCHIVE]["found_via"][0]["hit_position"] = None
        self.assertRejected("raw", data, "a depth-0 search hit needs a hit_position")

    def test_followed_link_needs_a_parent_and_no_position(self):
        data = raw()
        data["pages"][FIELDNOTES]["found_via"][1]["hit_position"] = 3
        self.assertRejected("raw", data, "a depth-1 followed link needs a parent_url")

    def test_parent_url_was_collected(self):
        data = raw()
        data["pages"][FIELDNOTES]["found_via"][1]["parent_url"] = "https://chatter.test/nobody"
        self.assertRejected("raw", data, "is not a collected page")

    def test_page_urls_are_pages_keys(self):
        data = raw()
        data["images"][0]["page_urls"].append(MIRROR)
        self.assertRejected("raw", data, f"page_urls entry {MIRROR} is not a pages key")

    def test_page_urls_need_provenance_for_that_image(self):
        data = raw()
        data["images"][0]["page_urls"].append(ARCHIVE)
        self.assertRejected("raw", data, f"image img-1 lists page {ARCHIVE} but is not in its found_via")

    def test_relevant_matches_retention(self):
        data = raw()
        data["pages"][ARCHIVE]["relevant"] = False
        self.assertRejected("raw", data, "relevant must be true exactly when")

    def test_snippet_pages_carry_no_links(self):
        data = raw()
        data["pages"][OLD_PROJECT]["outbound_links"] = [{"url": FIELDNOTES}]
        self.assertRejected("raw", data, "cannot carry links or JSON-LD")

    def test_failed_search_has_no_hits(self):
        data = raw()
        data["images"][0]["errors"].append({"url": None, "status_code": 429, "reason": "rate_limited"})
        self.assertRejected("raw", data, "a failed search has no hits")


class ProfilesRuleTests(ContractTestCase):
    def test_accounts_keys_match_account_ids(self):
        data = profiles()
        data["accounts"]["a2"]["account_id"] = "a9"
        self.assertRejected("profiles", data, "accounts key a2 holds account a9")

    def test_account_urls_are_unique(self):
        data = profiles()
        data["accounts"]["a2"]["url"] = CHATTER
        self.assertRejected("profiles", data, "accounts share a URL")

    def test_direct_images_are_a_subset(self):
        data = profiles()
        data["accounts"]["a3"]["direct_image_ids"] = ["img-3"]
        self.assertRejected("profiles", data, "direct_image_ids must be a subset of image_ids")

    def test_best_hit_position_follows_direct_images(self):
        data = profiles()
        data["accounts"]["a3"]["best_hit_position"] = None
        self.assertRejected("profiles", data, "best_hit_position is set exactly when")

    def test_mention_author_exists(self):
        data = profiles()
        data["mentions"][1]["author_account_id"] = "a9"
        self.assertRejected("profiles", data, "names unknown account a9")

    def test_mention_subjects_exist(self):
        data = profiles()
        data["mentions"][0]["subject_account_ids"] = ["a9"]
        self.assertRejected("profiles", data, "names unknown account a9")

    def test_profile_ids_are_unique(self):
        self.assertRejected("profiles", split_photogram(profiles(), "p1"), "duplicate profile IDs: p1")

    def test_profile_accounts_exist(self):
        data = profiles()
        data["profiles"][0]["account_ids"].append("a9")
        self.assertRejected("profiles", data, "profile p1 names unknown account a9")

    def test_account_is_in_one_profile(self):
        data = split_photogram(profiles())
        data["profiles"][0]["account_ids"].append("a5")
        self.assertRejected("profiles", data, "account a5 is in profiles p1 and p2")

    def test_profile_lists_an_account_once(self):
        data = profiles()
        data["profiles"][0]["account_ids"].append("a1")
        self.assertRejected("profiles", data, "lists accounts twice: a1")

    def test_pair_decisions_stay_inside_the_profile(self):
        data = profiles()
        data["profiles"][0]["pair_decisions"][1]["b"] = "a9"
        self.assertRejected("profiles", data, "names an account outside it")

    def test_pair_decision_compares_two_accounts(self):
        data = profiles()
        data["profiles"][0]["pair_decisions"][1]["b"] = "a1"
        self.assertRejected("profiles", data, "compares a1 with itself")

    def test_jev_pair_decision_has_a_probability(self):
        data = profiles()
        data["profiles"][0]["pair_decisions"][0]["method"] = "jev"
        self.assertRejected("profiles", data, "needs a probability")

    def test_mutual_link_pair_decision_has_no_probability(self):
        data = profiles()
        data["profiles"][0]["pair_decisions"][0]["probability"] = 0.9
        self.assertRejected("profiles", data, "has no probability")

    def test_graphs_keys_are_the_profile_ids(self):
        self.assertRejected("profiles", {**profiles(), "graphs": {}}, "must be exactly the profile IDs")

    def test_graph_profile_id_matches_its_key(self):
        data = profiles()
        data["graphs"]["p1"]["profile_id"] = "p2"
        self.assertRejected("profiles", data, "graphs key p1 holds the graph for p2")

    def test_graph_targets_are_unique(self):
        data = profiles()
        data["graphs"]["p1"]["connections"].append(conn(LINKUP, "linkedin", ["a1"], trust=50))
        self.assertRejected("profiles", data, "repeats targets")

    def test_count_matches_sources(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0]["count"] = 3
        self.assertRejected("profiles", data, "count 3 but 2 sources")

    def test_sources_are_unique(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0].update(sources=["a1", "a1"])
        self.assertRejected("profiles", data, "repeats sources: a1")

    def test_sources_are_accounts_of_the_profile(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0].update(sources=["a1", "a9"])
        self.assertRejected("profiles", data, "source a9 of")

    def test_snippet_accounts_are_not_sources(self):
        data = profiles()
        data["accounts"]["a1"]["extraction"] = "snippet"
        self.assertRejected("profiles", data, "snippet-only account a1 cannot be a source")

    def test_self_links_are_excluded(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0].update(sources=["a1", "a3", "a4"], count=3, trust=99)
        self.assertRejected("profiles", data, "account a4 links to itself")

    def test_target_profile_id_only_for_other_profile(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][0]["target_profile_id"] = "p1"
        self.assertRejected("profiles", data, "target_profile_id is set exactly for other_profile targets")

    def test_other_profile_target_exists(self):
        data = split_photogram(profiles())
        data["graphs"]["p2"]["connections"][0]["target_profile_id"] = "p9"
        self.assertRejected("profiles", data, "needs target_profile_id naming another existing profile")

    def test_target_status_matches_where_the_url_lives(self):
        data = profiles()
        data["graphs"]["p1"]["connections"][1]["target_status"] = "not_collected"
        self.assertRejected("profiles", data, f"{CHATTER} is labeled not_collected but belongs to p1")


class VerificationRuleTests(ContractTestCase):
    def two_verdicts(self, first, second, accepted_id, reason="accepted"):
        return {**verification(), "verdicts": [first, second], "accepted_profile_id": accepted_id, "reason": reason}

    def test_at_most_one_accepted_verdict(self):
        data = self.two_verdicts(verdict(), verdict("p2", 0.3), "p1")
        self.assertRejected("verification", data, "at most one verdict can be accepted")

    def test_accepted_reason_needs_accepted_profile_id(self):
        self.assertRejected("verification", {**verification(), "accepted_profile_id": None},
                            "needs accepted_profile_id equal to the accepted verdict")

    def test_accepted_profile_id_matches_the_accepted_verdict(self):
        data = self.two_verdicts(verdict(), verdict("p2", 0.3, "rejected"), "p2")
        self.assertRejected("verification", data, "needs accepted_profile_id equal to the accepted verdict")

    def test_other_reasons_have_no_accepted_profile_id(self):
        self.assertRejected("verification", {**verification_review(), "accepted_profile_id": "p1"},
                            "allows no accepted verdict or accepted_profile_id")

    def test_other_reasons_have_no_accepted_verdict(self):
        self.assertRejected("verification", {**verification(), "accepted_profile_id": None, "reason": "too_close"},
                            "allows no accepted verdict or accepted_profile_id")

    def test_verdicts_are_sorted_by_confidence(self):
        data = self.two_verdicts(verdict("p2", 0.3, "rejected"), verdict(), "p1")
        self.assertRejected("verification", data, "sorted by confidence, highest first")

    def test_only_the_top_verdict_is_accepted(self):
        data = self.two_verdicts(verdict(decision="rejected"), verdict("p2", 0.3), "p2")
        self.assertRejected("verification", data, "only the highest-confidence verdict can be accepted")

    def test_verdict_profile_ids_are_unique(self):
        data = self.two_verdicts(verdict(), verdict(confidence=0.3, decision="rejected"), "p1")
        self.assertRejected("verification", data, "duplicate verdict profile IDs: p1")

    def test_no_candidate_reasons_match_empty_verdicts(self):
        self.assertRejected("verification", {**verification_review(), "reason": "no_candidates"},
                            "does not match 1 verdicts")

    def test_fallback_confidence_is_the_confidence(self):
        data = verification()
        data["verdicts"][0]["fallback_confidence"] = 0.5
        self.assertRejected("verification", data, "a fallback verdict's confidence is its fallback_confidence")


class FinalRuleTests(ContractTestCase):
    def test_no_match_reason_is_not_accepted(self):
        self.assertRejected("final", {**no_match_review(), "reason": "accepted"}, "cannot be 'accepted'")

    def test_no_match_candidates_are_sorted(self):
        data = no_match_review()
        data["top_candidates"].append({**data["top_candidates"][0], "profile_id": "p2", "confidence": 1.0})
        self.assertRejected("final", data, "sorted by confidence, highest first")

    def test_no_match_candidates_are_unique(self):
        data = no_match_review()
        data["top_candidates"].append(dict(data["top_candidates"][0]))
        self.assertRejected("final", data, "duplicate candidate profile IDs: p1")

    def test_no_candidate_reasons_list_no_candidates(self):
        self.assertRejected("final", {**no_match_review(), "reason": "no_candidates"},
                            "does not match 1 top_candidates")

    def test_final_accounts_are_unique(self):
        data = final()
        data["accounts"].append(dict(data["accounts"][0]))
        self.assertRejected("final", data, "final accounts repeat URLs")


class CrossReferenceTests(ContractTestCase):
    def test_run_ids_match(self):
        self.assertDisagree("run_id differs", **alex(verification={**verification(), "run_id": "mock-alex-ffffffff"}))

    def test_num_images_matches_the_capture(self):
        data = capture()
        data["capture"]["images"].pop()
        artifacts = alex(capture=data)
        self.assertDisagree("num_images=3 but the capture has 2 images", capture=artifacts["capture"], raw=artifacts["raw"])

    def test_raw_image_ids_come_from_the_capture(self):
        data = capture()
        data["capture"]["images"][2]["image_id"] = "img-9"
        artifacts = alex(capture=data)
        self.assertDisagree("raw image img-3 is not in the capture", capture=artifacts["capture"], raw=artifacts["raw"])

    def test_raw_angles_match_the_capture(self):
        data = capture()
        data["capture"]["images"][1]["angle"] = "up"
        artifacts = alex(capture=data)
        self.assertDisagree("raw image img-2 has angle left, capture says up",
                            capture=artifacts["capture"], raw=artifacts["raw"])

    def test_raw_backend_matches_the_run(self):
        artifacts = alex(capture={**capture(), "backend": "serpapi"})
        self.assertDisagree("used backend mock, run uses serpapi", capture=artifacts["capture"], raw=artifacts["raw"])

    def test_account_urls_resolve_to_raw_pages(self):
        data = profiles()
        data["accounts"]["a5"]["url"] = "https://photogram.test/someone-else"
        artifacts = alex(profiles=data)
        self.assertDisagree("account a5 URL https://photogram.test/someone-else is not a page",
                            raw=artifacts["raw"], profiles=artifacts["profiles"])

    def test_accounts_come_from_profile_pages(self):
        data = profiles()
        data["accounts"]["a5"]["url"] = POST
        artifacts = alex(profiles=data)
        self.assertDisagree(f"account a5 page {POST} is not a relevant profile page",
                            raw=artifacts["raw"], profiles=artifacts["profiles"])

    def test_account_extraction_matches_its_page(self):
        data = profiles()
        data["accounts"]["a2"]["extraction"] = "snippet"
        artifacts = alex(profiles=data)
        self.assertDisagree("account a2 extraction snippet disagrees with its page",
                            raw=artifacts["raw"], profiles=artifacts["profiles"])

    def test_account_images_come_from_raw(self):
        data = profiles()
        data["accounts"]["a2"]["image_ids"].append("img-9")
        artifacts = alex(profiles=data)
        self.assertDisagree("account a2 cites unknown images img-9", raw=artifacts["raw"], profiles=artifacts["profiles"])

    def test_mention_urls_resolve_to_raw_pages(self):
        data = profiles()
        data["mentions"].append({"url": "https://elsewhere.test/story", "kind": "article"})
        artifacts = alex(profiles=data)
        self.assertDisagree("mention https://elsewhere.test/story is not a page",
                            raw=artifacts["raw"], profiles=artifacts["profiles"])

    def test_mention_kind_matches_its_page(self):
        data = profiles()
        data["mentions"][1]["kind"] = "article"
        artifacts = alex(profiles=data)
        self.assertDisagree(f"mention {POST} has kind article but its page kind is post",
                            raw=artifacts["raw"], profiles=artifacts["profiles"])

    def test_verdicts_cover_exactly_the_profiles(self):
        data = verification_review()
        data["verdicts"][0]["profile_id"] = "p2"
        artifacts = alex(verification=data)
        self.assertDisagree("verdicts cover profiles ['p2'] but the profiles are ['p1']",
                            profiles=artifacts["profiles"], verification=artifacts["verification"])

    def test_accepted_profile_id_is_a_profile(self):
        data = verification()
        data["verdicts"][0]["profile_id"] = "p2"
        data["accepted_profile_id"] = "p2"
        artifacts = alex(verification=data)
        self.assertDisagree("accepted_profile_id p2 is not a profile",
                            profiles=artifacts["profiles"], verification=artifacts["verification"])

    def test_final_sources_are_raw_pages(self):
        data = final()
        data["bio"]["sources"] = ["https://elsewhere.test/invented"]
        artifacts = alex(final=data)
        self.assertDisagree("final source https://elsewhere.test/invented is not a page in the raw evidence",
                            raw=artifacts["raw"], final=artifacts["final"])

    def test_final_sources_belong_to_the_accepted_profile(self):
        data = final()
        data["workplace"]["sources"] = [ARCHIVE]
        self.assertDisagree(f"final source {ARCHIVE} is not an account or attributed mention",
                            **alex(final=data))

    def test_final_accounts_belong_to_the_accepted_profile(self):
        data = final()
        data["accounts"].append({"platform": "linkedin", "url": SAM_LINKUP, "trust": 50})
        self.assertDisagree(f"final account {SAM_LINKUP} is not in accepted profile p1", **alex(final=data))

    def test_final_confidence_is_the_accepted_verdicts(self):
        self.assertDisagree("differs from the accepted verdict's", **alex(final={**final(), "confidence": 0.5}))

    def test_final_profile_needs_an_accepted_verdict(self):
        artifacts = alex(verification=verification_review())
        self.assertDisagree("a final profile needs an accepted verdict",
                            verification=artifacts["verification"], final=artifacts["final"])

    def test_no_match_reason_matches_verification(self):
        artifacts = alex(verification=verification_review(), final={**no_match_review(), "reason": "below_threshold"})
        self.assertDisagree("no-match reason below_threshold differs from verification reason review_required",
                            verification=artifacts["verification"], final=artifacts["final"])

    def test_no_match_candidates_copy_the_verdicts(self):
        data = no_match_review()
        data["top_candidates"][0]["confidence"] = 0.5
        artifacts = alex(verification=verification_review(), final=data)
        self.assertDisagree("top_candidates must copy the first three verdicts",
                            verification=artifacts["verification"], final=artifacts["final"])

    def test_no_match_candidates_are_profiles(self):
        data = no_match_review()
        data["top_candidates"][0]["profile_id"] = "p9"
        artifacts = alex(final=data)
        self.assertDisagree("no-match candidates p9 are not profiles",
                            profiles=artifacts["profiles"], final=artifacts["final"])
