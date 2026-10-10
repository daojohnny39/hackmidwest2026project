"""Section 3 tests (spec 3.4): signals, fallback formula, Jev confidence, and the decision rule.

Sections 1 and 2 aren't built yet, so `build_case` turns a mock-seed case (hits per
image, followed links, accounts and their links, profiles) into valid 01/02
artifacts. The cases mirror spec 9's matrix rows; the graphs use the 2c trust table.
"""

import json
import math
import tempfile
from pathlib import Path

from pipeline import stage3_verify
from pipeline.artifacts import ArtifactError, read_artifact, write_artifact
from pipeline.config import TRUST_TABLE, load_settings
from pipeline.contracts import (
    CaptureArtifact,
    ProfilesBundle,
    RawEvidenceBundle,
    VerificationNotes,
    validate_references,
)
from pipeline.jev import Jev
from pipeline.platforms import platform_for
from pipeline.stage3_verify import apply_notes, compute_signals, fallback_confidence, verify
from tests import test_contracts as contract_fixture
from tests.support import OfflineTestCase

WHEN = "2026-10-10T14:03:00Z"
MODEL = "jev-1.13.0-test"


def local(url):
    return "http://127.0.0.1:8765/web/" + url.split("://", 1)[1]


def trust(count):
    return TRUST_TABLE[min(count, max(TRUST_TABLE))]


def account(url, name, workplace=None, links=(), updated_at="2026-10-01", location=None):
    return {"url": url, "name": name, "workplace": workplace, "links": list(links), "updated_at": updated_at,
            "location": location}


def build_case(slug, *, hits, accounts, profiles, follows=None, mentions=(), aliases=None, snippets=None,
               search_failed=None, drop=None, flags=(), backend="mock"):
    """Valid (RawEvidenceBundle, ProfilesBundle) for one case.

    hits: image_id -> hit URLs in rank order. follows: URL -> parent profile URL it was
    followed from (depth 1). aliases: original URL -> canonical page URL. snippets: URL ->
    HTTP status of a failed fetch (kept as a snippet record). search_failed: image_id ->
    (status, reason). drop: image_id -> URLs that image's triage didn't keep.
    profiles: (name, [account URLs]) in profile ID order.
    """
    run_id = f"mock-{slug}-0123abcd"
    follows, aliases, snippets = follows or {}, aliases or {}, snippets or {}
    search_failed, drop = search_failed or {}, drop or {}
    by_url = {spec["url"]: spec for spec in accounts}
    mention_of = {mention["url"]: mention for mention in mentions}

    found: dict[str, list[dict]] = {}  # page key -> found_via
    reached: dict[str, list[str]] = {image_id: [] for image_id in hits}
    for image_id, urls in hits.items():
        for rank, url in enumerate(urls, start=1):
            key = aliases.get(url, url)
            found.setdefault(key, []).append({"image_id": image_id, "hit_position": rank, "depth": 0})
            if key not in reached[image_id]:
                reached[image_id].append(key)
    for url, parent in follows.items():
        direct = {via["image_id"] for via in found.get(url, []) if via["depth"] == 0}
        for image_id in dict.fromkeys(via["image_id"] for via in found[parent] if via["depth"] == 0):
            if image_id not in direct:
                found.setdefault(url, []).append(
                    {"image_id": image_id, "hit_position": None, "depth": 1, "parent_url": parent})
                reached[image_id].append(url)
    kept = {image_id: [url for url in urls if url not in drop.get(image_id, ())] for image_id, urls in reached.items()}
    retained = {url for urls in kept.values() for url in urls}

    pages = {}
    for key, via in found.items():
        kind = "profile" if key in by_url else mention_of[key]["kind"] if key in mention_of else "article"
        links = by_url[key]["links"] if key in by_url else mention_of.get(key, {}).get("links", [])
        snippet = key in snippets
        pages[key] = {
            "url": key, "source_urls": [key, *[alias for alias, target in aliases.items() if target == key]],
            "fetched_url": local(key), "status_code": snippets.get(key, 200), "platform": platform_for(key),
            "page_kind": kind, "title": by_url.get(key, {}).get("name") or "", "found_via": via,
            "outbound_links": [] if snippet else [{"url": link, "label": ""} for link in links],
            "relevant": key in retained, "extraction": "snippet" if snippet else "html",
        }
    images = []
    for image_id, urls in hits.items():
        failed = search_failed.get(image_id)
        errors = [{"url": None, "status_code": failed[0], "reason": failed[1]}] if failed else [
            {"url": url, "status_code": snippets[url], "reason": "not_found" if snippets[url] == 404 else "forbidden"}
            for url in dict.fromkeys(aliases.get(u, u) for u in urls) if url in snippets]
        images.append({
            "image_id": image_id, "angle": "front", "backend": backend, "searched_at": WHEN,
            "request_id": None if failed else f"mock-{image_id}", "collection_method": "deterministic",
            "hits": [{"position": rank, "title": url, "url": url, "fetch_url": local(url), "source": url.split("/")[2]}
                     for rank, url in enumerate(urls, start=1)],
            "page_urls": kept[image_id], "errors": errors,
        })
    raw = RawEvidenceBundle.model_validate({
        "run_id": run_id, "num_images": len(hits), "images": images, "pages": pages,
        "url_aliases": {**{key: key for key in pages}, **{a: t for a, t in aliases.items() if t in pages}},
    })

    ids = {spec["url"]: f"a{index}" for index, spec in enumerate(accounts, start=1)}
    attributed: dict[str, list[dict]] = {}  # account URL -> found_via of mentions about it
    for mention in mentions:
        for subject in mention.get("subjects", []):
            attributed.setdefault(subject, []).extend(found[mention["url"]])
    account_records = {}
    for spec in accounts:
        url = spec["url"]
        via = found[url]
        direct = [v for v in via + attributed.get(url, []) if v["depth"] == 0]
        account_records[ids[url]] = {
            "account_id": ids[url], "platform": platform_for(url), "url": url, "display_name": spec["name"],
            "workplace": spec["workplace"], "location": spec["location"], "updated_at": spec["updated_at"],
            "links_to": spec["links"], "depth": min(v["depth"] for v in via),
            "image_ids": sorted({v["image_id"] for v in via + attributed.get(url, [])}),
            "direct_image_ids": sorted({v["image_id"] for v in direct}),
            "best_hit_position": min((v["hit_position"] for v in direct), default=None),
            "extraction": "snippet" if url in snippets else "json_ld",
        }
    mention_records = [{
        "url": mention["url"], "kind": mention["kind"],
        "author_account_id": ids.get(mention.get("author")),
        "subject_account_ids": [ids[subject] for subject in mention.get("subjects", [])],
        "links_to": mention.get("links", []), "image_ids": sorted({v["image_id"] for v in found[mention["url"]]}),
    } for mention in mentions]

    profile_of = {url: f"p{index}" for index, (_, urls) in enumerate(profiles, start=1) for url in urls}
    profile_records, graphs = [], {}
    for index, (name, urls) in enumerate(profiles, start=1):
        profile_id = f"p{index}"
        profile_records.append({"profile_id": profile_id, "name": name, "account_ids": [ids[url] for url in urls],
                                "pair_decisions": []})
        sources: dict[str, list[str]] = {}
        for url in urls:
            if url in snippets:
                continue  # snippet-only accounts contribute no edges (2c)
            for target in by_url[url]["links"]:
                if target != url:
                    sources.setdefault(target, []).append(ids[url])
        connections = []
        for target, linked_from in sources.items():
            owner = profile_of.get(target)
            status = "not_collected" if owner is None else "in_profile" if owner == profile_id else "other_profile"
            connections.append({
                "target_url": target, "target_platform": platform_for(target), "count": len(linked_from),
                "sources": linked_from, "trust": trust(len(linked_from)), "target_status": status,
                "target_profile_id": owner if status == "other_profile" else None,
            })
        incoming = {c["target_url"]: c["trust"] for c in connections}
        internal = sum(c["count"] for c in connections if c["target_status"] == "in_profile")
        cross = any(c["target_status"] == "other_profile" and c["trust"] >= 95 for c in connections)
        graphs[profile_id] = {
            "profile_id": profile_id, "connections": connections,
            "mean_link_trust": sum(incoming.get(url, 0) for url in urls) / len(urls),
            "internal_link_density": internal / (len(urls) * (len(urls) - 1)) if len(urls) > 1 else 0.0,
            "flags": ["cross_profile_link"] if cross else [],
        }
    grouped = ProfilesBundle.model_validate({
        "run_id": run_id, "accounts": account_records, "mentions": mention_records,
        "profiles": profile_records, "graphs": graphs, "jev_calls": [], "flags": list(flags),
    })
    validate_references(raw=raw, profiles=grouped)
    return raw, grouped


# The mock seed (mock_internet/seed.py): Kansas City Alex, Portland Alex, Sam, Casey.
LINKUP, CHATTER = "https://linkup.test/in/alexbuilds", "https://chatter.test/alexbuilds"
PHOTOGRAM, CODEFORGE = "https://photogram.test/alexbuilds", "https://codeforge.test/alexbuilds"
FIELDNOTES = "https://fieldnotes.test/@alexbuilds"
INTERVIEW = "https://riverbend-news.test/people/alex-morgan-transit"
MIRROR = "https://syndicated.test/transit/alex-morgan"
POST = "https://chatter.test/alexbuilds/status/101"
ARCHIVE = "https://archive.test/speakers/alex-morgan-2021"
OLD_PROJECT = "https://fieldnotes.test/@alexbuilds/old-project"
SAM_LINKUP = "https://linkup.test/in/samdraws"

PL_LINKUP, PL_CHATTER = "https://linkup.test/in/alexframes", "https://chatter.test/alexframes"
PL_FIELDNOTES, PL_CODEFORGE = "https://fieldnotes.test/@alexframes", "https://codeforge.test/alexframes"

ALEX_HITS = {
    "img-1": [LINKUP, CHATTER, INTERVIEW, CODEFORGE],
    "img-2": [PHOTOGRAM, FIELDNOTES, CHATTER, MIRROR],
    "img-3": [INTERVIEW, CODEFORGE, POST, ARCHIVE, OLD_PROJECT],
}
KC, ATLAS = "Kansas City, MO", "Prairie Atlas Labs"
ALEX_ACCOUNTS = [
    account(LINKUP, "Alex Morgan", ATLAS, [CHATTER, CODEFORGE], location=KC),
    account(CHATTER, "Alex M.", ATLAS, [LINKUP, FIELDNOTES], location=KC),
    account(PHOTOGRAM, "Alex M.", "Old Mill Software", [CHATTER, FIELDNOTES], "2022-05-14", KC),
    account(CODEFORGE, "Alex Morgan", ATLAS, location=KC),
    account(FIELDNOTES, "Alex Morgan", ATLAS, [LINKUP, CODEFORGE], location=KC),
]
ALEX_MENTIONS = [
    {"url": INTERVIEW, "kind": "article", "subjects": [LINKUP], "links": [LINKUP, SAM_LINKUP]},
    {"url": POST, "kind": "post", "author": CHATTER, "subjects": [CHATTER], "links": [CHATTER]},
    {"url": ARCHIVE, "kind": "article"},
    {"url": OLD_PROJECT, "kind": "article"},
]
PORTLAND_ACCOUNTS = [
    account(PL_LINKUP, "Alex Morgan", "Cedar Light Studio", [PL_CHATTER, PL_CODEFORGE], location="Portland, OR"),
    account(PL_CHATTER, "Alex Morgan Photo", "Cedar Light Studio", [PL_LINKUP, PL_FIELDNOTES], location="Portland, OR"),
    account(PL_FIELDNOTES, "Alex Morgan", "Cedar Light Studio", [PL_LINKUP, PL_CODEFORGE], location="Portland, OR"),
    account(PL_CODEFORGE, "Alex Morgan", "Cedar Light Studio", location="Portland, OR"),
]


def alex(scenario="default", **changes):
    hits = {image_id: list(urls) for image_id, urls in ALEX_HITS.items()}
    accounts, profiles = list(ALEX_ACCOUNTS), [("Alex Morgan", [spec["url"] for spec in ALEX_ACCOUNTS])]
    follows = {FIELDNOTES: CHATTER}
    if scenario == "ambiguous":  # the photographer's pages follow Alex's on every angle
        for urls in hits.values():
            urls += [PL_LINKUP, PL_CHATTER, PL_FIELDNOTES]
        follows[PL_CODEFORGE] = PL_LINKUP
        accounts += PORTLAND_ACCOUNTS
        profiles.append(("Alex Morgan", [spec["url"] for spec in PORTLAND_ACCOUNTS]))
    elif scenario == "stale":  # every angle also returns the 2021 archive and the deleted post
        for urls in hits.values():
            urls += [ARCHIVE, OLD_PROJECT]
    elif scenario == "duplicates":
        for urls in hits.values():
            urls[:] = [urls[0], *urls] + [MIRROR]
    elif scenario in ("empty", "rate_limited", "server_error"):
        hits = {image_id: [] for image_id in hits}
        failed = {"rate_limited": (429, "rate_limited"), "server_error": (503, "server_error")}.get(scenario)
        failure = {image_id: failed for image_id in hits} if failed else None
        return build_case(f"alex-{scenario}", hits=hits, accounts=[], profiles=[], search_failed=failure)
    options = {"follows": follows, "mentions": ALEX_MENTIONS, "aliases": {MIRROR: INTERVIEW},
               "snippets": {OLD_PROJECT: 404}, **changes}
    return build_case(f"alex-{scenario}", hits=hits, accounts=accounts, profiles=profiles, **options)


def sam():
    linkup, chatter, photogram = SAM_LINKUP, "https://chatter.test/samdraws", "https://photogram.test/samdraws"
    codeforge, fieldnotes = "https://codeforge.test/samdraws", "https://fieldnotes.test/@samdraws"
    private = "https://photogram.test/samdraws/private-album"
    mosaic = "Mosaic Kite Design"
    accounts = [
        account(linkup, "Sam Rivera", mosaic, [chatter, codeforge], location=KC),
        account(chatter, "Sammy Rivera", mosaic, [linkup, fieldnotes], location=KC),
        account(photogram, "Sammy Rivera", mosaic, [chatter, fieldnotes], location=KC),
        account(codeforge, "Sam Rivera", mosaic, location=KC),
        account(fieldnotes, "Sam Rivera", mosaic, [linkup, codeforge], location=KC),
    ]
    return build_case("sam", hits={"img-1": [linkup, chatter, photogram, private]}, accounts=accounts,
                      profiles=[("Sam Rivera", [spec["url"] for spec in accounts])],
                      follows={codeforge: linkup, fieldnotes: chatter}, snippets={private: 403})


CASEY_LINKUP, CASEY_CHATTER = "https://linkup.test/in/caseykeys", "https://chatter.test/caseykeys"


def casey(names=("Casey Brooks", "C. Brooks"), snippets=None, profile_name="Casey Brooks"):
    accounts = [
        account(CASEY_LINKUP, names[0], "Blue Finch Music School", [CASEY_CHATTER], location="Tulsa, OK"),
        account(CASEY_CHATTER, names[1], "Blue Finch Music School", [CASEY_LINKUP], location="Tulsa, OK"),
    ]
    return build_case("casey", hits={"img-1": [CASEY_LINKUP, CASEY_CHATTER]}, accounts=accounts,
                      profiles=[(profile_name, [CASEY_LINKUP, CASEY_CHATTER])], snippets=snippets)


def unknown():
    return build_case("unknown", hits={"img-1": []}, accounts=[], profiles=[])


def formula(image_support, top_hit_share, trust, platforms, consistency, collision=False):
    z = (-4 + 2 * image_support + 3 * top_hit_share + 1.5 * trust / 100 + 0.3 * min(platforms, 5)
         + consistency - 0.5 * collision)
    return 1 / (1 + math.exp(-z))


def jev_settings():
    return load_settings(backend="mock", models=True, environ={"JEV_MODE": "typesafe", "TYPESAFE_API_KEY": "x"},
                         dotenv_path=None)


def fake_settings():
    return load_settings(backend="mock", environ={}, dotenv_path=None)


def reply(nouls, best, none=0.0):
    """A raw provider response: one noul per profile and a `best` choice."""
    probabilities = {profile_id: 0.0 for profile_id in nouls} | {"none": none}
    if best == "none":
        probabilities["none"] = 1.0
    else:
        probabilities[best] = 1.0 - none
    return {
        "model": MODEL, "usage": {"input_tokens": 512, "output_tokens": 12},
        "answers": {**{f"match_{p}": {"type": "noul", "noul": value} for p, value in nouls.items()},
                    "best": {"type": "choice", "choice": best, "confidence": 0.8, "probabilities": probabilities}},
    }


def real_jev(responder, **options):
    return Jev(jev_settings(), responder=responder, **options)


class NoCallJev:
    """Stands in for a client that must not be called."""

    real = True
    provider = "typesafe"

    def ask(self, *args, **kwargs):
        raise AssertionError("Jev must not be called")


class Stage3TestCase(OfflineTestCase):
    def assertConfidence(self, verdict, expected, display):
        self.assertAlmostEqual(verdict.confidence, expected, places=12)
        self.assertEqual(round(verdict.confidence, 2), display)

    def verdicts(self, result):
        return {verdict.profile_id: verdict for verdict in result.verdicts}


class MockMatrixTests(Stage3TestCase):
    """Spec 3.4 and the Section 3 column of the spec 9 matrix, with the default fake Jev."""

    def run_case(self, case, **options):
        raw, grouped = case
        result = verify(raw, grouped, jev=Jev(fake_settings()), **options)
        validate_references(raw=raw, profiles=grouped, verification=result)
        return result

    def test_alex_default_accepts_kansas_city_alex(self):
        result = self.run_case(alex())
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        verdict = result.verdicts[0]
        self.assertConfidence(verdict, formula(1, 1, 76, 5, 0.8), 0.99)
        self.assertEqual(verdict.method, "fallback")
        self.assertEqual(verdict.signals.newest_update, "2026-10-01")
        self.assertFalse(verdict.signals.name_collision)

    def test_matches_the_contract_fixture(self):
        raw = RawEvidenceBundle.model_validate(contract_fixture.raw())
        grouped = ProfilesBundle.model_validate(contract_fixture.profiles())
        result = verify(raw, grouped)
        self.assertEqual(result.verdicts[0].confidence, contract_fixture.CONFIDENCE)
        self.assertEqual(result.verdicts[0].signals.model_dump(), contract_fixture.verdict()["signals"])

    def test_alex_ambiguous_accepts_kansas_city_and_rejects_portland(self):
        result = self.run_case(alex("ambiguous"))
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        kc, portland = result.verdicts
        self.assertEqual((kc.profile_id, portland.profile_id), ("p1", "p2"))
        self.assertConfidence(kc, formula(1, 1, 76, 5, 0.8, collision=True), 0.98)
        self.assertConfidence(portland, formula(1, 0, 72.5, 4, 1.0, collision=True), 0.69)
        self.assertEqual(portland.decision, "rejected")
        # Retrieval alone scores the namesakes alike; the rank-1 share separates them.
        self.assertEqual((kc.signals.image_support, portland.signals.image_support), (1.0, 1.0))
        self.assertEqual((kc.signals.top_hit_share, portland.signals.top_hit_share), (1.0, 0.0))
        self.assertTrue(kc.signals.name_collision and portland.signals.name_collision)

    def test_alex_stale_accepts_one_candidate(self):
        result = self.run_case(alex("stale"))
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        self.assertEqual(len(result.verdicts), 1)  # the archived 2021 bio is not a candidate
        self.assertConfidence(result.verdicts[0], formula(1, 1, 76, 5, 0.8), 0.99)

    def test_alex_duplicates_do_not_multiply_support(self):
        result = self.run_case(alex("duplicates"))
        self.assertEqual(result.reason, "accepted")
        self.assertConfidence(result.verdicts[0], formula(1, 1, 76, 5, 0.8), 0.99)

    def test_sam_accepts_both_name_forms_as_one_candidate(self):
        raw, grouped = sam()
        result = self.run_case((raw, grouped))
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        self.assertConfidence(result.verdicts[0], formula(1, 1, 76, 5, 1.0), 0.99)
        names = {grouped.accounts[a].display_name for a in grouped.profiles[0].account_ids}
        self.assertEqual(names, {"Sam Rivera", "Sammy Rivera"})

    def test_casey_is_accepted_with_a_sparse_flag(self):
        result = self.run_case(casey())
        self.assertEqual(result.reason, "accepted")
        self.assertConfidence(result.verdicts[0], formula(1, 1, 50, 2, 1.0), 0.97)
        self.assertIn("sparse_footprint", result.verdicts[0].flags)

    def test_unknown_has_no_candidates_and_calls_no_model(self):
        raw, grouped = unknown()
        result = verify(raw, grouped, jev=NoCallJev())
        self.assertEqual((result.reason, result.verdicts, result.jev_calls), ("no_candidates", [], []))

    def test_empty_search_has_no_candidates(self):
        raw, grouped = alex("empty")
        self.assertEqual(verify(raw, grouped, jev=NoCallJev()).reason, "no_candidates")

    def test_failed_searches_make_evidence_unavailable(self):
        for scenario in ("rate_limited", "server_error"):
            with self.subTest(scenario=scenario):
                raw, grouped = alex(scenario)
                result = verify(raw, grouped, jev=NoCallJev())
                self.assertEqual((result.reason, result.jev_calls), ("evidence_unavailable", []))

    def test_fake_jev_exercises_typed_parsing_without_scoring(self):
        result = self.run_case(alex("ambiguous"))
        [call] = result.jev_calls
        self.assertEqual((call.provider, call.error, call.model), ("fake", None, None))
        self.assertEqual(set(call.answers), {"match_p1", "match_p2", "best"})
        self.assertEqual(call.answers["best"]["choice"], "p1")
        self.assertTrue(all(verdict.method == "fallback" for verdict in result.verdicts))

    def test_jev_off_uses_the_formula_without_a_call(self):
        raw, grouped = alex()
        result = verify(raw, grouped, jev=None)
        self.assertEqual((result.reason, result.jev_calls), ("accepted", []))


class SignalTests(Stage3TestCase):
    def test_partial_search_failure_lowers_support_but_keeps_x(self):
        hits = {**ALEX_HITS, "img-3": []}  # img-3's search was rate limited
        raw, grouped = build_case(
            "alex-partial", hits=hits, accounts=ALEX_ACCOUNTS, profiles=[("Alex Morgan", [s["url"] for s in ALEX_ACCOUNTS])],
            follows={FIELDNOTES: CHATTER}, mentions=ALEX_MENTIONS[:1], aliases={MIRROR: INTERVIEW},
            search_failed={"img-3": (429, "rate_limited")})
        signals = compute_signals(raw, grouped)["p1"]
        self.assertEqual(raw.num_images, 3)
        self.assertAlmostEqual(signals.image_support, 2 / 3)
        self.assertAlmostEqual(signals.top_hit_share, 2 / 3)
        result = verify(raw, grouped)
        self.assertIn("partial_search_failure", result.verdicts[0].flags)

    def test_top_hit_counts_only_when_the_image_kept_the_page(self):
        raw, grouped = alex(drop={"img-3": [INTERVIEW]})  # img-3's rank-1 hit, dropped by its triage
        self.assertAlmostEqual(compute_signals(raw, grouped)["p1"].top_hit_share, 2 / 3)

    def test_followed_links_add_no_direct_support(self):
        raw, grouped = alex()
        fieldnotes = next(a for a in grouped.accounts.values() if a.url == FIELDNOTES)
        self.assertEqual(fieldnotes.direct_image_ids, ["img-2"])  # img-1 reached it only through Chatter
        self.assertEqual(compute_signals(raw, grouped)["p1"].image_support, 1.0)

    def test_consistency_counts_missing_workplaces_against_the_profile(self):
        self.assertEqual(stage3_verify._consistency(["Atlas", "atlas.", None, "Old Mill"]), 0.5)
        self.assertEqual(stage3_verify._consistency([None, ""]), 0.0)
        self.assertEqual(stage3_verify._consistency(["B Co", "A Co"]), 0.5)

    def test_newest_update_ignores_invalid_dates(self):
        newest = stage3_verify._newest
        self.assertEqual(newest(["not a date", "2022-05-14", None]), "2022-05-14")
        self.assertEqual(newest(["2026-10-01", "2026-10-01T09:00:00Z", "2026-10-01T05:00:00-05:00"]),
                         "2026-10-01T05:00:00-05:00")
        self.assertIsNone(newest([None, "soon"]))

    def test_fallback_formula_matches_the_spec(self):
        raw, grouped = alex()
        signals = compute_signals(raw, grouped)["p1"]
        self.assertEqual(signals.model_dump(), {
            "image_support": 1.0, "top_hit_share": 1.0, "mean_link_trust": 76.0, "platform_count": 5,
            "consistency": 0.8, "name_collision": False, "newest_update": "2026-10-01"})
        self.assertAlmostEqual(fallback_confidence(signals), formula(1, 1, 76, 5, 0.8))
        capped = signals.model_copy(update={"platform_count": 9})
        self.assertAlmostEqual(fallback_confidence(capped), fallback_confidence(signals))


class JevConfidenceTests(Stage3TestCase):
    def test_valid_jev_answers_become_the_confidence(self):
        raw, grouped = alex("ambiguous")
        result = verify(raw, grouped, jev=real_jev(lambda s, q: reply({"p1": 0.94, "p2": 0.20}, "p1")))
        p1, p2 = result.verdicts
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        self.assertEqual((p1.method, p1.confidence, p1.jev_model), ("jev", 0.94, MODEL))
        self.assertAlmostEqual(p1.fallback_confidence, formula(1, 1, 76, 5, 0.8, collision=True))
        self.assertEqual((p2.confidence, p2.decision), (0.20, "rejected"))
        self.assertIn("jev_fallback_disagree", p2.flags)  # 0.20 vs 0.69
        self.assertNotIn("jev_fallback_disagree", p1.flags)
        self.assertEqual(result.jev_calls[0].model, MODEL)

    def test_request_holds_every_candidate_and_x(self):
        raw, grouped = alex("ambiguous")
        seen = {}

        def responder(state, questions):
            seen.update(state=state, questions=questions)
            return reply({"p1": 0.9, "p2": 0.2}, "p1")

        verify(raw, grouped, jev=real_jev(responder))
        self.assertEqual(seen["state"]["num_images"], 3)
        self.assertEqual(set(seen["state"]["candidates"]), {"p1", "p2"})
        self.assertEqual(len(seen["state"]["candidates"]["p2"]["accounts"]), 4)
        self.assertEqual(set(seen["questions"]), {"match_p1", "match_p2", "best"})
        self.assertEqual(seen["questions"]["match_p1"]["type"], "noul")
        self.assertEqual(set(seen["questions"]["best"]["criteria"]), {"p1", "p2", "none"})
        self.assertLess(len(json.dumps(seen)), 20_000)

    def test_best_none_accepts_nobody(self):
        raw, grouped = alex("ambiguous")
        for best, none in (("none", 1.0), ("p1", 0.5)):
            with self.subTest(best=best, none=none):
                result = verify(raw, grouped, jev=real_jev(lambda s, q: reply({"p1": 0.95, "p2": 0.1}, best, none)))
                self.assertEqual((result.reason, result.accepted_profile_id), ("jev_none", None))
                self.assertTrue(all(v.decision == "rejected" for v in result.verdicts))

    def test_best_disagreeing_with_the_top_noul_accepts_nobody(self):
        raw, grouped = alex("ambiguous")
        result = verify(raw, grouped, jev=real_jev(lambda s, q: reply({"p1": 0.95, "p2": 0.1}, "p2")))
        self.assertEqual((result.reason, result.accepted_profile_id), ("jev_disagreement", None))

    def test_jev_outage_falls_back_for_the_whole_stage(self):
        def outage(state, questions):
            raise ConnectionError("down")

        raw, grouped = alex()
        result = verify(raw, grouped, jev=real_jev(outage))
        verdict = result.verdicts[0]
        self.assertEqual((result.reason, verdict.method), ("accepted", "fallback"))
        self.assertEqual(verdict.confidence, verdict.fallback_confidence)
        self.assertIn("verification_fallback", verdict.flags)
        self.assertEqual(result.jev_calls[0].error, "connection_error")

    def test_malformed_answers_fall_back_without_mixing_scores(self):
        missing = reply({"p1": 0.9, "p2": 0.2}, "p1")
        del missing["answers"]["match_p2"]
        out_of_range = reply({"p1": 1.5, "p2": 0.2}, "p1")
        foreign_choice = reply({"p1": 0.9, "p2": 0.2}, "p1")
        foreign_choice["answers"]["best"]["choice"] = "p9"
        raw, grouped = alex("ambiguous")
        for label, response in (("missing", missing), ("range", out_of_range), ("choice", foreign_choice)):
            with self.subTest(label):
                result = verify(raw, grouped, jev=real_jev(lambda s, q, r=response: r))
                self.assertEqual({v.method for v in result.verdicts}, {"fallback"})
                self.assertEqual(result.jev_calls[0].error, "invalid_response")
                self.assertEqual(result.reason, "accepted")


class DecisionRuleTests(Stage3TestCase):
    def decide(self, case, nouls, best=None):
        raw, grouped = case
        best = best or max(nouls, key=nouls.get)
        return verify(raw, grouped, jev=real_jev(lambda s, q: reply(nouls, best)))

    def test_confidence_threshold_is_inclusive_at_0_80(self):
        self.assertEqual(self.decide(casey(), {"p1": 0.80}).reason, "accepted")
        self.assertEqual(self.decide(casey(), {"p1": 0.7999}).reason, "below_threshold")

    def test_single_candidate_leads_a_runner_up_of_zero(self):
        result = self.decide(casey(), {"p1": 0.81})
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))

    def test_lead_threshold_is_inclusive_at_0_20(self):
        self.assertEqual(self.decide(alex("ambiguous"), {"p1": 0.85, "p2": 0.65}).reason, "accepted")
        self.assertEqual(self.decide(alex("ambiguous"), {"p1": 0.85, "p2": 0.66}).reason, "too_close")

    def test_thresholds_use_unrounded_values(self):
        self.assertEqual(self.decide(casey(), {"p1": 0.796}).reason, "below_threshold")  # displays as 0.80

    def test_a_tie_is_too_close(self):
        result = self.decide(alex("ambiguous"), {"p1": 0.9, "p2": 0.9}, best="p2")
        self.assertEqual((result.reason, [v.profile_id for v in result.verdicts]), ("too_close", ["p1", "p2"]))

    def test_snippets_alone_cannot_match(self):
        raw, grouped = casey(snippets={CASEY_LINKUP: 403, CASEY_CHATTER: 403})
        result = verify(raw, grouped)
        self.assertGreater(result.verdicts[0].confidence, 0.8)
        self.assertEqual((result.reason, result.accepted_profile_id), ("insufficient_evidence", None))

    def test_unnamed_candidate_is_insufficient(self):
        result = verify(*casey(names=(None, None), profile_name=None))
        self.assertEqual(result.reason, "insufficient_evidence")

    def test_candidate_without_direct_image_support_is_insufficient(self):
        other = "https://codeforge.test/jordanwater"
        accounts = [account(CASEY_LINKUP, "Casey Brooks", links=[CASEY_CHATTER, other]),
                    account(CASEY_CHATTER, "C. Brooks", links=[CASEY_LINKUP]),
                    account(other, "Jordan Lee")]
        case = build_case("follow-only", hits={"img-1": [CASEY_LINKUP, CASEY_CHATTER]}, accounts=accounts,
                          follows={other: CASEY_LINKUP},
                          profiles=[("Casey Brooks", [CASEY_LINKUP, CASEY_CHATTER]), ("Jordan Lee", [other])])
        result = self.decide(case, {"p1": 0.3, "p2": 0.95})
        self.assertEqual(result.verdicts[0].signals.image_support, 0.0)
        self.assertEqual(result.reason, "insufficient_evidence")

    def test_grouping_and_graph_flags_carry_to_verdicts(self):
        raw, grouped = alex()
        grouped = grouped.model_copy(update={"flags": ["grouping_fallback"]})
        self.assertIn("grouping_fallback", verify(raw, grouped).verdicts[0].flags)


class LivePolicyTests(Stage3TestCase):
    def test_live_fallback_requires_review(self):
        def outage(state, questions):
            raise TimeoutError

        raw, grouped = alex()
        for jev in (real_jev(outage), Jev(fake_settings()), None):
            with self.subTest(provider=getattr(jev, "provider", "off")):
                result = verify(raw, grouped, jev=jev, live=True)
                self.assertEqual((result.reason, result.accepted_profile_id), ("review_required", None))
                self.assertEqual(result.verdicts[0].decision, "rejected")

    def test_live_run_with_valid_jev_can_accept(self):
        raw, grouped = alex()
        result = verify(raw, grouped, jev=real_jev(lambda s, q: reply({"p1": 0.93}, "p1")), live=True)
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))

    def test_live_run_without_candidates_keeps_its_reason(self):
        raw, grouped = alex("rate_limited")
        self.assertEqual(verify(raw, grouped, live=True).reason, "evidence_unavailable")


class NotesTests(Stage3TestCase):
    def test_notes_replace_rationales_only(self):
        raw, grouped = alex("ambiguous")
        result = verify(raw, grouped)
        noted = apply_notes(result, VerificationNotes(rationales={"p2": "A namesake photographer in Portland."}))
        self.assertEqual(self.verdicts(noted)["p2"].rationale, "A namesake photographer in Portland.")
        self.assertEqual(self.verdicts(noted)["p1"].rationale, result.verdicts[0].rationale)
        strip = lambda r: [v.model_dump(exclude={"rationale"}) for v in r.verdicts]
        self.assertEqual(strip(noted), strip(result))

    def test_bad_notes_are_rejected(self):
        result = verify(*alex())
        for rationales in ({"p9": "Unknown."}, {"p1": "One. Two."}, {"p1": "  "}, {"p1": "x" * 301}):
            with self.subTest(rationales=rationales), self.assertRaises(ValueError):
                apply_notes(result, VerificationNotes(rationales=rationales))

    def test_code_rationale_describes_the_signals(self):
        rationale = verify(*alex("ambiguous")).verdicts[1].rationale
        self.assertIn("Found by 3 of 3 images and the top result for 0", rationale)
        self.assertIn("compatible name", rationale)


class StageRunnerTests(Stage3TestCase):
    def write_run(self, case, backend="mock"):
        raw, grouped = case
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        run_dir = Path(root.name) / raw.run_id
        run_dir.mkdir()
        session = raw.run_id.rsplit("-", 1)[0]
        images = [{"image_id": image.image_id, "angle": image.angle,
                   **({"file": f"{image.image_id}.jpg"} if backend == "serpapi" else {"mock_fixture": "alex-front"})}
                  for image in raw.images]
        capture = CaptureArtifact.model_validate({
            "run_id": raw.run_id, "capture_dir": "captures/x", "backend": backend, "scenario": "default",
            "llm_mode": "off", "jev_mode": "fake",
            "capture": {"session_id": session, "captured_at": WHEN, "images": images,
                        "consent": {"confirmed": True, "participant": "Fictional", "confirmed_at": WHEN,
                                    "external_processing": True}},
        })
        for stage, artifact in ((0, capture), (1, raw), (2, grouped)):
            write_artifact(run_dir, stage, artifact)
        return run_dir

    def test_runner_writes_the_verification_artifact(self):
        run_dir = self.write_run(alex())
        view = stage3_verify.run(run_dir, fake_settings(), False)
        result = read_artifact(run_dir, 3)
        self.assertEqual((result.reason, result.accepted_profile_id), ("accepted", "p1"))
        self.assertIn("Verification: accepted p1", view)
        self.assertIn("p1  Alex Morgan   99% fallback  accepted", view)

    def test_live_policy_follows_the_recorded_backend(self):
        run_dir = self.write_run(alex(backend="serpapi"), backend="serpapi")
        result = stage3_verify.run_stage(run_dir, fake_settings())  # replay settings are mock
        self.assertEqual(result.reason, "review_required")

    def test_real_jev_calls_are_logged_under_stage_3(self):
        run_dir = self.write_run(alex())
        jev = Jev(jev_settings(), run_dir=run_dir, stage=3, responder=lambda s, q: reply({"p1": 0.9}, "p1"))
        raw, grouped = read_artifact(run_dir, 1), read_artifact(run_dir, 2)
        verify(raw, grouped, jev=jev)
        [line] = (run_dir / "run_log.jsonl").read_text().splitlines()
        event = json.loads(line)
        self.assertEqual((event["stage"], event["status"], event["model"]), (3, "ok", MODEL))

    def test_missing_prerequisite_is_an_artifact_error(self):
        run_dir = self.write_run(alex())
        (run_dir / "02_profiles.json").unlink()
        with self.assertRaises(ArtifactError):
            stage3_verify.run_stage(run_dir, fake_settings())

    def test_report_for_no_candidates(self):
        view = stage3_verify.report(verify(*unknown()))
        self.assertIn("no accepted candidate (no_candidates)", view)
