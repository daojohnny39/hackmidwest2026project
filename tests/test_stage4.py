"""Section 4 tests (spec 4.3): summarization into FinalProfile or NoMatch.

Sections 1-3 aren't built yet, so `Case` hand-builds their artifacts from the mock
seed's facts (mock_internet/seed.py): pages and hits for 01, accounts, mentions,
profiles and trust graphs for 02, and verdicts for 03. Every case goes through
contracts.validate_references before stage 4 sees it.

The synthesis agent is replaced by stub agents, plus one real Strands Agent driven
by a scripted model, so no test makes a model call.
"""

import asyncio
import dataclasses
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pipeline.artifacts import ArtifactError, read_artifact, write_artifact
from pipeline.config import TRUST_TABLE, load_settings
from pipeline.contracts import (
    ARTIFACT_ADAPTERS,
    FinalProfile,
    NoMatch,
    SummaryDraft,
    validate_references,
)
from pipeline import stage4_summarize as stage4
from tests.support import OfflineTestCase

NOW = datetime(2026, 10, 10, 14, 5, tzinfo=timezone.utc)
SEARCHED = "2026-10-10T14:03:00Z"

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

ALEX_BIO = "I build open-source transit maps and volunteer at the Riverbend maker club."
ALEX = dict(role="Software engineer", workplace="Prairie Atlas Labs", location="Kansas City, MO", bio=ALEX_BIO)
ALEX_HITS = {  # alex-front, alex-left, alex-right (seed fixtures)
    "img-1": [LINKUP, CHATTER, INTERVIEW, CODEFORGE],
    "img-2": [PHOTOGRAM, FIELDNOTES, CHATTER, MIRROR],
    "img-3": [INTERVIEW, CODEFORGE, POST, ARCHIVE, OLD_PROJECT],
}


def local(url):
    return "http://127.0.0.1:8765/web/" + url.split("://", 1)[1]


class Case:
    """Builds consistent 01/02/03 artifacts for one run from a compact description."""

    def __init__(self, run_id, hits):
        self.run_id, self.hits = run_id, hits
        self.pages, self.accounts, self.mentions, self.profiles = {}, {}, [], []
        self.errors = {}

    def _via(self, url, aliases, followed):
        keys = {url, *aliases}
        via = [{"image_id": image_id, "hit_position": rank, "depth": 0}
               for image_id, urls in self.hits.items() for rank, hit in enumerate(urls, start=1) if hit in keys]
        via += [{"image_id": image_id, "hit_position": None, "depth": 1, "parent_url": parent}
                for image_id, parent in followed]
        assert via, f"{url} was neither a hit nor followed"
        return via

    def page(self, url, platform, kind, *, aliases=(), followed=(), links=(), snippet=False, status=200,
             title="", published_at="2026-10-01"):
        self.pages[url] = {
            "url": url, "source_urls": [url, *aliases], "canonical_url": url if aliases else None,
            "fetched_url": local(url), "status_code": status, "platform": platform, "page_kind": kind,
            "title": title, "found_via": self._via(url, aliases, followed),
            "outbound_links": [] if snippet else [{"url": target, "label": ""} for target in links],
            "extraction": "snippet" if snippet else "html", "published_at": published_at,
        }
        return url

    def account(self, platform, url, name, *, links=(), followed=(), updated_at="2026-10-01", snippet=False, **facts):
        self.page(url, platform, "profile", followed=followed, links=links, snippet=snippet,
                  title=f"{name} | {platform}")
        via = self.pages[url]["found_via"]
        direct = sorted({v["image_id"] for v in via if v["depth"] == 0})
        account_id = f"a{len(self.accounts) + 1}"
        self.accounts[url] = {
            "account_id": account_id, "platform": platform, "url": url, "handle": url.rstrip("/").split("/")[-1],
            "display_name": name, "updated_at": updated_at, "links_to": [] if snippet else list(links),
            "image_ids": sorted({v["image_id"] for v in via}), "direct_image_ids": direct,
            "best_hit_position": min((v["hit_position"] for v in via if v["depth"] == 0), default=None),
            "depth": 0 if direct else 1, "extraction": "snippet" if snippet else "json_ld",
            **{key: facts.get(key) for key in ("role", "workplace", "location", "bio")},
        }
        return url

    def mention(self, url, kind, *, subjects=(), names=(), links=(), aliases=(), published_at="2026-10-01",
                title="", snippet=False, status=200):
        self.page(url, "news" if kind == "article" else "x", kind, aliases=aliases, links=links, title=title,
                  published_at=published_at, snippet=snippet, status=status)
        self.mentions.append({"url": url, "kind": kind, "subjects": list(subjects), "names": list(names),
                              "links_to": list(links), "image_ids": sorted({v["image_id"] for v in
                                                                            self.pages[url]["found_via"]}),
                              "published_at": published_at})
        return url

    def profile(self, name, urls):
        self.profiles.append((f"p{len(self.profiles) + 1}", name, list(urls)))
        return self.profiles[-1][0]

    def build(self, verdicts, reason, errors=None):
        """verdicts: [(profile_id, confidence, decision, flags)], highest confidence first."""
        pages = self.pages
        aliases = {source: key for key, record in pages.items() for source in record["source_urls"]}
        images = [{
            "image_id": image_id, "angle": ("front", "left", "right")[index % 3], "backend": "mock",
            "request_id": f"mock-{image_id}", "searched_at": SEARCHED, "collection_method": "deterministic",
            "hits": [{"position": rank, "title": f"hit {rank}", "url": url, "fetch_url": local(url),
                      "source": url.split("/")[2]} for rank, url in enumerate(urls, start=1)],
            "page_urls": [key for key, record in pages.items()
                          if any(v["image_id"] == image_id for v in record["found_via"])],
            "errors": (errors or {}).get(image_id, []),
        } for index, (image_id, urls) in enumerate(self.hits.items())]
        raw = {"run_id": self.run_id, "num_images": len(images), "images": images, "pages": pages,
               "url_aliases": aliases}

        ids = {url: account["account_id"] for url, account in self.accounts.items()}
        owner = {url: profile_id for profile_id, _, urls in self.profiles for url in urls}
        graphs = {}
        for profile_id, _, urls in self.profiles:
            sources_by_target = {}
            for url in urls:
                for target in self.accounts[url]["links_to"]:
                    if target != url:
                        sources_by_target.setdefault(target, []).append(ids[url])
            connections = []
            for target, sources in sources_by_target.items():
                status = ("in_profile" if owner.get(target) == profile_id
                          else "other_profile" if target in owner else "not_collected")
                connections.append({
                    "target_url": target, "target_platform": pages[target]["platform"] if target in pages else "other",
                    "count": len(sources), "sources": sources, "trust": TRUST_TABLE[min(len(sources), 3)],
                    "target_status": status, "target_profile_id": owner[target] if status == "other_profile" else None,
                })
            incoming = {c["target_url"]: c["trust"] for c in connections if c["target_status"] == "in_profile"}
            internal = sum(c["count"] for c in connections if c["target_status"] == "in_profile")
            n = len(urls)
            graphs[profile_id] = {
                "profile_id": profile_id, "connections": connections,
                "mean_link_trust": sum(incoming.get(url, 0) for url in urls) / n,
                "internal_link_density": internal / (n * (n - 1)) if n > 1 else 0.0,
            }
        profiles = {
            "run_id": self.run_id, "accounts": {a["account_id"]: a for a in self.accounts.values()},
            "mentions": [{"url": m["url"], "kind": m["kind"], "subject_account_ids": [ids[s] for s in m["subjects"]],
                          "author_account_id": ids[m["subjects"][0]] if m["kind"] == "post" and m["subjects"] else None,
                          "names": m["names"], "links_to": m["links_to"], "image_ids": m["image_ids"],
                          "published_at": m["published_at"]} for m in self.mentions],
            "profiles": [{"profile_id": profile_id, "name": name, "account_ids": [ids[url] for url in urls],
                          "pair_decisions": []} for profile_id, name, urls in self.profiles],
            "graphs": graphs, "jev_calls": [],
        }
        names = {profile_id: name for profile_id, name, _ in self.profiles}
        verification = {
            "run_id": self.run_id, "reason": reason, "jev_calls": [],
            "accepted_profile_id": next((p for p, _, decision, _ in verdicts if decision == "accepted"), None),
            "verdicts": [{
                "profile_id": profile_id, "name": names[profile_id], "confidence": confidence,
                "fallback_confidence": confidence, "method": "fallback", "decision": decision, "flags": list(flags),
                "rationale": "Code template rationale.",
                "signals": {"image_support": 1.0, "top_hit_share": 1.0, "mean_link_trust": 76.0, "platform_count": 5,
                            "consistency": 0.8, "name_collision": False, "newest_update": "2026-10-01"},
            } for profile_id, confidence, decision, flags in verdicts],
        }
        artifacts = tuple(ARTIFACT_ADAPTERS[stage].validate_python(data)
                          for stage, data in ((1, raw), (2, profiles), (3, verification)))
        validate_references(raw=artifacts[0], profiles=artifacts[1], verification=artifacts[2])
        return artifacts


def alex_case(*, stale=False, accepted=True, run_id="mock-alex-0123abcd", edit=None):
    """Alex (alexbuilds) on the default or stale scenario: 5 accounts in one profile."""
    hits = {image_id: list(urls) for image_id, urls in ALEX_HITS.items()}
    if stale:  # stale adds archive and deleted-post hits to every image; they repeat existing pages
        hits["img-1"] += [ARCHIVE, OLD_PROJECT]
        hits["img-2"] += [ARCHIVE, OLD_PROJECT]
        hits["img-3"] += [MIRROR, PHOTOGRAM]
    case = Case(run_id, hits)
    facts = {url: dict(ALEX) for url in (LINKUP, CHATTER, PHOTOGRAM, CODEFORGE, FIELDNOTES)}
    facts[PHOTOGRAM].update(workplace="Old Mill Software", updated_at="2022-05-14")
    if edit:
        edit(facts)
    case.account("linkedin", LINKUP, "Alex Morgan", links=[CHATTER, CODEFORGE], **facts[LINKUP])
    case.account("x", CHATTER, "Alex M.", links=[LINKUP, FIELDNOTES, POST], **facts[CHATTER])
    case.account("instagram", PHOTOGRAM, "Alex M.", links=[CHATTER, FIELDNOTES], **facts[PHOTOGRAM])
    case.account("github", CODEFORGE, "Alex Morgan", **facts[CODEFORGE])
    case.account("blog", FIELDNOTES, "Alex Morgan", links=[LINKUP, CODEFORGE, OLD_PROJECT],
                 followed=[("img-1", CHATTER)], **facts[FIELDNOTES])
    case.mention(INTERVIEW, "article", aliases=[MIRROR], subjects=[LINKUP], names=["Alex Morgan", "Sam Rivera"],
                 links=[LINKUP, SAM_LINKUP], title="Alex Morgan helps neighbors navigate Riverbend transit")
    case.mention(POST, "post", subjects=[CHATTER], names=["Alex M."], links=[CHATTER], published_at="2026-10-03")
    case.mention(ARCHIVE, "article", names=["Alex Morgan"], published_at="2021-06-12",
                 title="Alex Morgan — 2021 Riverbend speaker")
    case.mention(OLD_PROJECT, "article", snippet=True, status=404, published_at=None)
    case.profile("Alex Morgan", [LINKUP, CHATTER, PHOTOGRAM, CODEFORGE, FIELDNOTES])
    decision, reason = ("accepted", "accepted") if accepted else ("rejected", "below_threshold")
    return case.build([("p1", 0.99, decision, [])], reason,
                      errors={"img-3": [{"url": OLD_PROJECT, "status_code": 404, "reason": "not_found"}]})


def sam_case():
    """Sam Rivera / Sammy Rivera: CodeForge and FieldNotes are followed at depth 1; the album is a 403."""
    linkup, chatter, photogram = SAM_LINKUP, "https://chatter.test/samdraws", "https://photogram.test/samdraws"
    codeforge, fieldnotes = "https://codeforge.test/samdraws", "https://fieldnotes.test/@samdraws"
    album = "https://photogram.test/samdraws/private-album"
    case = Case("mock-sam-0123abcd", {"img-1": [linkup, chatter, photogram, album]})
    sam = dict(role="Product designer", workplace="Mosaic Kite Design", location="Kansas City, MO",
               bio="Designing accessible civic tools. Sketchbook enthusiast and community mentor.")
    case.account("linkedin", linkup, "Sam Rivera", links=[chatter, codeforge], **sam)
    case.account("x", chatter, "Sammy Rivera", links=[linkup, fieldnotes], **sam)
    case.account("instagram", photogram, "Sammy Rivera", links=[chatter, fieldnotes, album], **sam)
    case.account("github", codeforge, "Sam Rivera", followed=[("img-1", linkup)], **sam)
    case.account("blog", fieldnotes, "Sam Rivera", links=[linkup, codeforge], followed=[("img-1", chatter)], **sam)
    case.mention(album, "article", snippet=True, status=403, title="Sam Rivera: workshop photos")
    case.profile("Sam Rivera", [linkup, chatter, photogram, codeforge, fieldnotes])
    return case.build([("p1", 0.99, "accepted", [])], "accepted",
                      errors={"img-1": [{"url": album, "status_code": 403, "reason": "forbidden"}]})


def casey_case():
    linkup, chatter = "https://linkup.test/in/caseykeys", "https://chatter.test/caseykeys"
    case = Case("mock-casey-0123abcd", {"img-1": [linkup, chatter]})
    casey = dict(role="Piano teacher", workplace="Blue Finch Music School", location="Tulsa, OK",
                 bio="Piano lessons, small jazz ensembles, and beginner music theory.")
    case.account("linkedin", linkup, "Casey Brooks", links=[chatter], **casey)
    case.account("x", chatter, "C. Brooks", links=[linkup], **casey)
    case.profile("Casey Brooks", [linkup, chatter])
    return case.build([("p1", 0.97, "accepted", ["sparse_footprint"])], "accepted")


def ambiguous_case():
    """Two Alex Morgans: Kansas City (accepted) and Portland (rejected)."""
    frames = {platform: url for platform, url in (
        ("linkedin", "https://linkup.test/in/alexframes"), ("x", "https://chatter.test/alexframes"),
        ("blog", "https://fieldnotes.test/@alexframes"), ("github", "https://codeforge.test/alexframes"))}
    hits = {image_id: urls + [frames["linkedin"], frames["x"], frames["blog"]] for image_id, urls in ALEX_HITS.items()}
    case = Case("mock-alex-amb-0123abcd", hits)
    for platform, url, name, links in (
        ("linkedin", LINKUP, "Alex Morgan", [CHATTER, CODEFORGE]), ("x", CHATTER, "Alex M.", [LINKUP, FIELDNOTES]),
        ("instagram", PHOTOGRAM, "Alex M.", [CHATTER]), ("github", CODEFORGE, "Alex Morgan", []),
        ("blog", FIELDNOTES, "Alex Morgan", [LINKUP, CODEFORGE]),
    ):
        case.account(platform, url, name, links=links, **ALEX)
    photo = dict(role="Landscape photographer", workplace="Cedar Light Studio", location="Portland, OR",
                 bio="Film cameras, coastal trails, and photography workshops in the Pacific Northwest.")
    case.account("linkedin", frames["linkedin"], "Alex Morgan", links=[frames["x"], frames["github"]], **photo)
    case.account("x", frames["x"], "Alex Morgan Photo", links=[frames["linkedin"], frames["blog"]], **photo)
    case.account("blog", frames["blog"], "Alex Morgan", links=[frames["linkedin"], frames["github"]], **photo)
    case.account("github", frames["github"], "Alex Morgan", followed=[("img-1", frames["linkedin"])], **photo)
    case.profile("Alex Morgan", [LINKUP, CHATTER, PHOTOGRAM, CODEFORGE, FIELDNOTES])
    case.profile("Alex Morgan", list(frames.values()))
    return case.build([("p1", 0.98, "accepted", []), ("p2", 0.69, "rejected", [])], "accepted"), frames


def unknown_case():
    case = Case("mock-unknown-0123abcd", {"img-1": []})
    return case.build([], "no_candidates")


def offline_settings(**changes):
    return dataclasses.replace(load_settings(backend="mock", dotenv_path=None), **changes)


def llm_settings():
    return offline_settings(llm_mode="anthropic", llm_model="claude-sonnet-5-5", models=True)


class Stage4TestCase(OfflineTestCase):
    def summarize(self, artifacts, settings=None, **kwargs):
        return stage4.summarize(*artifacts, settings or offline_settings(), now=NOW, **kwargs)

    def assertSourcesCollected(self, final, raw):
        for url in final.cited_urls():
            self.assertIsNotNone(raw.page_key(url), url)


class TemplateTests(Stage4TestCase):
    def test_alex_default_uses_the_newest_workplace(self):
        artifacts = alex_case()
        final = self.summarize(artifacts).final
        self.assertIsInstance(final, FinalProfile)
        self.assertEqual(final.name.value, "Alex Morgan")
        self.assertEqual(final.name.sources, [LINKUP, CODEFORGE, FIELDNOTES])
        self.assertEqual(final.workplace.value, "Prairie Atlas Labs")
        self.assertNotIn(PHOTOGRAM, final.workplace.sources)
        self.assertEqual(final.role.value, "Software engineer")
        self.assertEqual(final.location.value, "Kansas City, MO")
        self.assertEqual(final.bio.value, ALEX_BIO)
        self.assertEqual(final.bio.sources, [LINKUP])
        self.assertIn(stage4.WORKPLACE_CONFLICT, final.flags)  # the stale Photogram employer is kept as a flag
        self.assertSourcesCollected(final, artifacts[0])

    def test_alex_default_copies_confidence_and_trust(self):
        raw, profiles, verification = alex_case()
        final = self.summarize((raw, profiles, verification)).final
        verdict = verification.verdicts[0]
        self.assertEqual((final.confidence, final.method), (verdict.confidence, verdict.method))
        self.assertEqual({a.url: a.trust for a in final.accounts},
                         {LINKUP: 95, CHATTER: 95, CODEFORGE: 95, FIELDNOTES: 95, PHOTOGRAM: 0})
        self.assertEqual(final.accounts[-1].url, PHOTOGRAM)  # highest trust first
        self.assertEqual(final.generated_at, NOW)
        self.assertEqual(final.run_id, raw.run_id)

    def test_alex_stale_still_gives_prairie_atlas_labs(self):
        artifacts = alex_case(stale=True)
        final = self.summarize(artifacts).final
        self.assertEqual(final.workplace.value, "Prairie Atlas Labs")
        self.assertNotIn("Old Mill Software", json.dumps(final.model_dump(mode="json")))
        self.assertNotIn(ARCHIVE, final.cited_urls())  # the unlinked 2021 archive is never a source

    def test_sam(self):
        artifacts = sam_case()
        final = self.summarize(artifacts).final
        self.assertEqual(final.name.value, "Sam Rivera")
        self.assertEqual(final.workplace.value, "Mosaic Kite Design")
        self.assertEqual(len(final.accounts), 5)
        self.assertEqual(final.bio.value, "Designing accessible civic tools. Sketchbook enthusiast and community mentor.")
        self.assertNotIn("https://photogram.test/samdraws/private-album", final.cited_urls())
        self.assertEqual(final.flags, [])
        self.assertSourcesCollected(final, artifacts[0])

    def test_casey_keeps_the_sparse_flag(self):
        final = self.summarize(casey_case()).final
        self.assertEqual((final.name.value, final.workplace.value), ("Casey Brooks", "Blue Finch Music School"))
        self.assertEqual({a.trust for a in final.accounts}, {50})
        self.assertEqual(final.flags, ["sparse_footprint"])

    def test_ambiguous_uses_only_the_accepted_candidate(self):
        artifacts, frames = ambiguous_case()
        final = self.summarize(artifacts).final
        self.assertEqual(final.workplace.value, "Prairie Atlas Labs")
        self.assertEqual(final.location.value, "Kansas City, MO")
        urls = {a.url for a in final.accounts} | set(final.cited_urls())
        self.assertFalse(urls & set(frames.values()))

    def test_template_makes_no_agent(self):
        def factory(*_):
            raise AssertionError("LLM_MODE=off must not build an agent")
        outcome = self.summarize(alex_case(), agent_factory=factory)
        self.assertEqual((outcome.bio_method, outcome.fallback), ("template", None))


class NoMatchTests(Stage4TestCase):
    def test_unknown_gives_no_match(self):
        final = self.summarize(unknown_case()).final
        self.assertIsInstance(final, NoMatch)
        self.assertEqual((final.reason, final.top_candidates), ("no_candidates", []))

    def test_every_candidate_rejected(self):
        final = self.summarize(alex_case(accepted=False)).final
        self.assertIsInstance(final, NoMatch)
        self.assertEqual(final.reason, "below_threshold")
        self.assertEqual([(c.profile_id, c.name, c.confidence) for c in final.top_candidates],
                         [("p1", "Alex Morgan", 0.99)])

    def test_review_required_and_evidence_unavailable_stay_distinct(self):
        raw, profiles, verification = alex_case(accepted=False)
        review = verification.model_copy(update={"reason": "review_required"})
        self.assertEqual(self.summarize((raw, profiles, review)).final.reason, "review_required")

        case = Case("mock-alex-0123abcd", {"img-1": [], "img-2": [], "img-3": []})
        failed = {image_id: [{"url": None, "status_code": 429, "reason": "rate_limited"}]
                  for image_id in ("img-1", "img-2", "img-3")}
        final = self.summarize(case.build([], "evidence_unavailable", errors=failed)).final
        self.assertEqual((final.reason, final.top_candidates), ("evidence_unavailable", []))

    def test_no_match_lists_the_top_three(self):
        case = Case("mock-many-0123abcd", {"img-1": [f"https://linkup.test/in/person{i}" for i in range(4)]})
        for i in range(4):
            case.account("linkedin", f"https://linkup.test/in/person{i}", f"Person {i}")
            case.profile(f"Person {i}", [f"https://linkup.test/in/person{i}"])
        artifacts = case.build([(f"p{i + 1}", 0.6 - i / 10, "rejected", []) for i in range(4)], "below_threshold")
        final = self.summarize(artifacts).final
        self.assertEqual([c.profile_id for c in final.top_candidates], ["p1", "p2", "p3"])

    def test_no_match_never_builds_an_agent(self):
        def factory(*_):
            raise AssertionError("no summary model for NoMatch")
        final = self.summarize(alex_case(accepted=False), llm_settings(), agent_factory=factory).final
        self.assertIsInstance(final, NoMatch)


class FieldRuleTests(Stage4TestCase):
    def test_date_tie_with_different_employers_leaves_workplace_null(self):
        def tie(facts):
            facts[CODEFORGE]["workplace"] = "Other Labs"
        final = self.summarize(alex_case(edit=tie)).final
        self.assertIsNone(final.workplace)
        self.assertIn(stage4.WORKPLACE_CONFLICT, final.flags)

    def test_undated_accounts_cannot_beat_a_dated_one(self):
        def undated(facts):
            for url in (LINKUP, CHATTER, CODEFORGE, FIELDNOTES):
                facts[url]["updated_at"] = None
        final = self.summarize(alex_case(edit=undated)).final
        self.assertEqual(final.workplace.value, "Old Mill Software")
        self.assertEqual(final.workplace.sources, [PHOTOGRAM])

    def test_undated_accounts_that_disagree_leave_workplace_null(self):
        def undated(facts):
            for facts_ in facts.values():
                facts_["updated_at"] = "not a date"
        self.assertIsNone(self.summarize(alex_case(edit=undated)).final.workplace)

    def test_datetimes_compare_by_day(self):
        def newer(facts):
            facts[PHOTOGRAM]["updated_at"] = "2026-10-02T01:00:00+05:00"  # 2026-10-01 20:00 UTC: a tie
        final = self.summarize(alex_case(edit=newer)).final
        self.assertIsNone(final.workplace)

    def test_role_follows_the_same_rule(self):
        def newer(facts):
            facts[PHOTOGRAM].update(role="Staff engineer", updated_at="2026-10-05")
        final = self.summarize(alex_case(edit=newer)).final
        self.assertEqual((final.role.value, final.role.sources), ("Staff engineer", [PHOTOGRAM]))
        self.assertIn(stage4.ROLE_CONFLICT, final.flags)

    def test_missing_bio_stays_null(self):
        def no_bio(facts):
            for facts_ in facts.values():
                facts_["bio"] = None
        self.assertIsNone(self.summarize(alex_case(edit=no_bio)).final.bio)

    def test_bio_keeps_three_linkedin_sentences(self):
        def long_bio(facts):
            facts[LINKUP]["bio"] = "One. Two! Three? Four."
            facts[FIELDNOTES]["bio"] = "A much longer bio than the LinkedIn one, which still loses to LinkedIn."
        bio = self.summarize(alex_case(edit=long_bio)).final.bio
        self.assertEqual((bio.value, bio.sources), ("One. Two! Three?", [LINKUP]))

    def test_bio_falls_back_to_the_longest(self):
        def no_linkedin_bio(facts):
            for url in (LINKUP, PHOTOGRAM, CODEFORGE):
                facts[url]["bio"] = None
            facts[CHATTER]["bio"] = "Short."
            facts[FIELDNOTES]["bio"] = "Writing about transit maps. Weekend tinkerer."
        bio = self.summarize(alex_case(edit=no_linkedin_bio)).final.bio
        self.assertEqual((bio.value, bio.sources), ("Writing about transit maps. Weekend tinkerer.", [FIELDNOTES]))

    def test_bio_drops_private_sentences(self):
        def private(facts):
            for facts_ in facts.values():
                facts_["bio"] = None
            facts[LINKUP]["bio"] = "Proud dad of two. Born in 1990. I build transit maps. Call 816-555-0134."
        bio = self.summarize(alex_case(edit=private)).final.bio
        self.assertEqual(bio.value, "I build transit maps.")

    def test_bio_that_is_all_private_is_omitted(self):
        def private(facts):
            for facts_ in facts.values():
                facts_["bio"] = "Married to my best friend. Active in my church."
        self.assertIsNone(self.summarize(alex_case(edit=private)).final.bio)

    def test_location_is_city_level(self):
        def street(facts):
            for facts_ in facts.values():
                facts_["location"] = "1200 Main St, Kansas City, MO 64105"
        self.assertIsNone(self.summarize(alex_case(edit=street)).final.location)

    def test_location_tie_is_null(self):
        def split(facts):
            for url in (LINKUP, CHATTER):
                facts[url]["location"] = "Lawrence, KS"
            facts[PHOTOGRAM]["location"] = None
        self.assertIsNone(self.summarize(alex_case(edit=split)).final.location)

    def test_snippet_accounts_supply_no_facts(self):
        case = Case("mock-snip-0123abcd", {"img-1": [LINKUP, CHATTER]})
        case.account("linkedin", LINKUP, "Alex Morgan", **ALEX)
        case.account("x", CHATTER, "Alex Morgan", snippet=True, workplace="Snippet Corp", updated_at="2026-10-09",
                     bio="Snippet text.")
        case.profile("Alex Morgan", [LINKUP, CHATTER])
        final = self.summarize(case.build([("p1", 0.9, "accepted", [])], "accepted")).final
        self.assertEqual(final.workplace.value, "Prairie Atlas Labs")
        self.assertEqual(final.bio.sources, [LINKUP])
        self.assertEqual(len(final.accounts), 2)

    def test_sentences_keep_abbreviations(self):
        self.assertEqual(stage4.sentences("Junior dev in St. Louis. Loves maps."),
                         ["Junior dev in St. Louis.", "Loves maps."])

    def test_disagreeing_artifacts_are_refused(self):
        raw, profiles, verification = alex_case()
        other = verification.model_copy(update={"run_id": "mock-alex-ffffffff"})
        with self.assertRaises(ArtifactError):
            self.summarize((raw, profiles, other))

    def test_accepted_profile_needs_a_sourced_name(self):
        raw, profiles, verification = alex_case()
        profiles.profiles[0].name = "Somebody Else"
        with self.assertRaises(stage4.SummaryError):
            self.summarize((raw, profiles, verification))


class StubAgent:
    """Returns scripted SummaryDraft results (or raises) for each invoke_async call."""

    def __init__(self, *results, delay=0.0):
        self.results, self.delay, self.prompts, self.tools = list(results), delay, [], []

    async def invoke_async(self, prompt, *, structured_output_model, limits):
        assert structured_output_model is SummaryDraft and limits == {"turns": stage4.SUMMARY_TURNS}
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return type("Result", (), {"structured_output": result})()

    def factory(self, system_prompt, tools):
        self.system_prompt, self.tools = system_prompt, tools
        return self


def draft(value, *sources):
    return SummaryDraft.model_validate({"bio": {"value": value, "sources": list(sources)}})


class AgentTests(Stage4TestCase):
    def run_agent(self, agent, artifacts=None, **kwargs):
        artifacts = artifacts or alex_case()
        return self.summarize(artifacts, llm_settings(), agent_factory=agent.factory, **kwargs)

    def assertFellBack(self, outcome, code):
        self.assertEqual((outcome.bio_method, outcome.fallback), ("template", code))
        self.assertEqual(outcome.final.bio.value, ALEX_BIO)
        self.assertIn(stage4.SUMMARY_FALLBACK, outcome.final.flags)

    def test_valid_draft_is_used_with_canonical_sources(self):
        agent = StubAgent(draft(ALEX_BIO, CHATTER, MIRROR))  # the mirror aliases to the attributed interview
        outcome = self.run_agent(agent)
        self.assertEqual((outcome.bio_method, outcome.fallback), ("agent", None))
        self.assertEqual(outcome.final.bio.sources, [CHATTER, INTERVIEW])
        self.assertNotIn(stage4.SUMMARY_FALLBACK, outcome.final.flags)
        self.assertEqual(len(agent.prompts), 1)

    def test_prompt_holds_only_accepted_evidence(self):
        artifacts, frames = ambiguous_case()
        agent = StubAgent(SummaryDraft())
        self.run_agent(agent, artifacts)
        evidence = json.loads(agent.prompts[0])
        self.assertEqual({a["url"] for a in evidence["accounts"]}, {LINKUP, CHATTER, PHOTOGRAM, CODEFORGE, FIELDNOTES})
        self.assertFalse(set(frames.values()) & set(agent.prompts[0].split('"')))

    def test_agent_may_omit_the_bio(self):
        outcome = self.run_agent(StubAgent(SummaryDraft()))
        self.assertEqual(outcome.bio_method, "agent")
        self.assertIsNone(outcome.final.bio)

    def test_one_correction_is_allowed(self):
        agent = StubAgent(draft(ALEX_BIO, "https://invented.test/alex"), draft(ALEX_BIO, LINKUP))
        outcome = self.run_agent(agent)
        self.assertEqual(outcome.bio_method, "agent")
        self.assertEqual(len(agent.prompts), 2)
        self.assertIn("was not collected", agent.prompts[1])

    def test_rejected_twice_falls_back_to_the_template(self):
        bad = draft(ALEX_BIO, "https://invented.test/alex")
        self.assertFellBack(self.run_agent(StubAgent(bad, bad)), "summary_rejected")

    def test_unrelated_collected_source_is_rejected(self):
        bad = draft(ALEX_BIO, ARCHIVE)  # collected, but not attributed to Alex
        agent = StubAgent(bad, bad)
        self.assertFellBack(self.run_agent(agent), "summary_rejected")
        self.assertIn("not an account or attributed mention", agent.prompts[1])

    def test_rejected_candidate_source_is_rejected(self):
        artifacts, frames = ambiguous_case()
        photo_bio = "Film cameras, coastal trails, and photography workshops in the Pacific Northwest."
        bad = draft(photo_bio, frames["linkedin"])
        outcome = self.run_agent(StubAgent(bad, bad), artifacts)
        self.assertEqual(outcome.fallback, "summary_rejected")
        self.assertEqual(outcome.final.bio.value, ALEX_BIO)

    def test_unsupported_employer_is_rejected(self):
        bad = draft("Alex works at Old Mill Software.", LINKUP)
        agent = StubAgent(bad, bad)
        self.assertFellBack(self.run_agent(agent), "summary_rejected")
        self.assertIn("not copied exactly", agent.prompts[1])

    def test_interview_alone_does_not_support_a_bio(self):
        bad = draft(ALEX_BIO, INTERVIEW)  # an allowed source, but it has no account bio
        self.assertFellBack(self.run_agent(StubAgent(bad, bad)), "summary_rejected")

    def test_more_than_three_sentences_is_rejected(self):
        def long_bio(facts):
            facts[LINKUP]["bio"] = "One. Two. Three. Four."
        bad = draft("One. Two. Three. Four.", LINKUP)
        outcome = self.run_agent(StubAgent(bad, bad), alex_case(edit=long_bio))
        self.assertEqual(outcome.fallback, "summary_rejected")
        self.assertEqual(outcome.final.bio.value, "One. Two. Three.")

    def test_private_sentence_is_rejected(self):
        def private(facts):
            facts[LINKUP]["bio"] = f"{ALEX_BIO} Proud mom of two."
        bad = draft("Proud mom of two.", LINKUP)
        outcome = self.run_agent(StubAgent(bad, bad), alex_case(edit=private))
        self.assertEqual(outcome.fallback, "summary_rejected")
        self.assertEqual(outcome.final.bio.value, ALEX_BIO)

    def test_extra_fields_such_as_confidence_are_rejected(self):
        class Tampered(SummaryDraft):
            model_config = {"extra": "allow"}
        tampered = Tampered.model_validate({"bio": None, "confidence": 0.5})
        outcome = self.run_agent(StubAgent(tampered, tampered))
        self.assertEqual(outcome.fallback, "summary_rejected")
        self.assertEqual(outcome.final.confidence, 0.99)

    def test_agent_error_falls_back_without_retry(self):
        agent = StubAgent(RuntimeError("provider down"))
        self.assertFellBack(self.run_agent(agent), "summary_error")
        self.assertEqual(len(agent.prompts), 1)

    def test_timeout_falls_back(self):
        agent = StubAgent(draft(ALEX_BIO, LINKUP), delay=1.0)
        self.assertFellBack(self.run_agent(agent, timeout=0.05), "summary_timeout")

    def test_read_verification_tool_is_read_only(self):
        agent = StubAgent(SummaryDraft())
        raw, profiles, verification = alex_case()
        self.run_agent(agent, (raw, profiles, verification))
        [read] = agent.tools
        view = read()
        self.assertEqual((view["reason"], view["accepted_profile_id"]), ("accepted", "p1"))
        view["verdicts"][0]["confidence"] = 0.1
        self.assertEqual(read()["verdicts"][0]["confidence"], 0.99)
        self.assertEqual(verification.verdicts[0].confidence, 0.99)


class StrandsAgentTests(Stage4TestCase):
    """A real Strands Agent with a scripted model: checks the tool and structured-output wiring offline."""

    def test_scripted_model_through_strands(self):
        from strands.models.model import Model

        class ScriptedModel(Model):
            def __init__(self, turns):
                self.turns, self.tool_names = list(turns), []

            def update_config(self, **config):
                pass

            def get_config(self):
                return {}

            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                raise NotImplementedError
                yield

            async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
                self.tool_names.append(sorted(spec["name"] for spec in tool_specs or []))
                name, payload = self.turns.pop(0)
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{len(self.turns)}", "name": name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}

        model = ScriptedModel([
            ("read_verification", {}),
            ("SummaryDraft", {"bio": {"value": ALEX_BIO, "sources": [FIELDNOTES]}}),
        ])
        outcome = self.summarize(alex_case(), llm_settings(),
                                 agent_factory=lambda prompt, tools: stage4.strands_agent(prompt, tools, model))
        self.assertEqual((outcome.bio_method, outcome.fallback), ("agent", None))
        self.assertEqual(outcome.final.bio.sources, [FIELDNOTES])
        self.assertEqual(model.tool_names[0], ["SummaryDraft", "read_verification"])
        self.assertEqual(model.turns, [])


class RunStageTests(Stage4TestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def run_dir(self, artifacts):
        run_dir = self.root / artifacts[0].run_id
        run_dir.mkdir()
        for stage, artifact in enumerate(artifacts, start=1):
            write_artifact(run_dir, stage, artifact)
        return run_dir

    def test_writes_final_and_logs(self):
        run_dir = self.run_dir(alex_case())
        view = stage4.run_stage(run_dir, offline_settings(), False)
        final = read_artifact(run_dir, 4)
        self.assertIsInstance(final, FinalProfile)
        self.assertIn("Workplace: Prairie Atlas Labs", view)
        log = [json.loads(line) for line in (run_dir / "run_log.jsonl").read_text().splitlines()]
        self.assertEqual((log[-1]["stage"], log[-1]["status"]), (4, "ok"))
        self.assertNotIn("model", log[-1])

    def test_no_match_view(self):
        run_dir = self.run_dir(alex_case(accepted=False))
        view = stage4.run_stage(run_dir, offline_settings(), False)
        self.assertIsInstance(read_artifact(run_dir, 4), NoMatch)
        self.assertIn("No confident match (below_threshold)", view)
        self.assertIn("p1 Alex Morgan: 99%", view)

    def test_missing_prerequisite_is_refused(self):
        raw, profiles, _ = alex_case()
        run_dir = self.root / raw.run_id
        run_dir.mkdir()
        write_artifact(run_dir, 1, raw)
        write_artifact(run_dir, 2, profiles)
        with self.assertRaises(ArtifactError):
            stage4.run_stage(run_dir, offline_settings(), False)
        self.assertFalse((run_dir / "04_final.json").exists())
