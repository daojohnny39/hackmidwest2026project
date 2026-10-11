"""Per-source findings and their confidence levels (findings.py)."""

from pipeline.contracts import RawEvidenceBundle, Subject
from pipeline.findings import build_findings
from tests.seed_evidence import collect
from tests.support import OfflineTestCase

INTERVIEW = "https://riverbend-news.test/people/alex-morgan-transit"
OLD_PROJECT = "https://fieldnotes.test/@alexbuilds/old-project"  # a 404: snippet only
MIRROR = "https://syndicated.test/transit/alex-morgan"


def with_scores(raw, backend, score_for):
    """The bundle as if backend had returned the same hits, each scored score_for(image_id, url)."""
    data = raw.model_dump()
    for image in data["images"]:
        image["backend"] = backend
        for hit in image["hits"]:
            hit["face_score"] = score_for(image["image_id"], hit["url"])
    return RawEvidenceBundle.model_validate(data)


def without_search(raw, image_ids):
    """The bundle with these images' searches failed."""
    data = raw.model_dump()
    for image in data["images"]:
        if image["image_id"] in image_ids:
            image.update(hits=[], page_urls=[], request_id=None,
                         errors=[{"url": None, "status_code": 429, "reason": "rate_limited"}])
    for page in data["pages"].values():
        page["found_via"] = [via for via in page["found_via"] if via["image_id"] not in image_ids]
    data["pages"] = {key: page for key, page in data["pages"].items() if page["found_via"]}
    data["url_aliases"] = {url: key for url, key in data["url_aliases"].items() if key in data["pages"]}
    for page in data["pages"].values():
        page["relevant"] = any(page["url"] in image["page_urls"] for image in data["images"])
    return RawEvidenceBundle.model_validate(data)


class FindingsTests(OfflineTestCase):
    def test_one_finding_per_page_across_images(self):
        raw = collect("alex")
        findings = build_findings(raw)
        keys = {raw.page_key(hit.url) or hit.url for image in raw.images for hit in image.hits}
        self.assertEqual(sorted(candidate["sourceUrl"] for candidate in findings.candidates), sorted(keys))
        self.assertNotIn(MIRROR, {candidate["sourceUrl"] for candidate in findings.candidates})  # merged into INTERVIEW
        self.assertEqual(len({candidate["id"] for candidate in findings.candidates}), len(keys))
        self.assertFalse(findings.failed)

    def test_mock_hits_have_no_confidence_score(self):
        findings = build_findings(collect("alex"))
        self.assertEqual({candidate["confidence"]["level"] for candidate in findings.candidates}, {"unknown"})
        self.assertTrue(all("score" not in candidate["confidence"] for candidate in findings.candidates))
        self.assertTrue(all("threat" not in candidate for candidate in findings.candidates))

    def test_rekognition_similarity_sets_the_level_and_order(self):
        raw = collect("alex")
        urls = sorted({hit.url for image in raw.images for hit in image.hits} - {MIRROR})
        scores = dict(zip(urls, [99, 96, 92, 85, 81, 80, 80, 80, 80]))
        scores[MIRROR] = 80
        findings = build_findings(with_scores(raw, "rekognition", lambda _, url: scores[url]))
        confidence = [candidate["confidence"] for candidate in findings.candidates]
        self.assertEqual([item["score"] for item in confidence[:5]], [99, 96, 92, 85, 81])
        self.assertEqual([item["level"] for item in confidence[:4]], ["strong", "likely", "possible", "weak"])
        self.assertEqual(confidence[0]["reasons"][0], "Face similarity 99/100 from Amazon Rekognition (strong match).")

    def test_best_score_across_images_wins_and_counts_photos(self):
        raw = collect("alex")
        shared = next(url for url in {hit.url for hit in raw.images[0].hits}
                      if url in {hit.url for hit in raw.images[1].hits})
        findings = build_findings(with_scores(
            raw, "rekognition", lambda image_id, url: 97 if (image_id, url) == ("img-2", shared) else 83))
        top = findings.candidates[0]
        self.assertEqual((top["sourceUrl"], top["confidence"]["score"]), (raw.page_key(shared), 97))
        self.assertIn("Found from 2 of 3 photos.", top["confidence"]["reasons"])

    def test_other_backends_scores_are_not_banded(self):
        findings = build_findings(with_scores(collect("alex"), "facecheck", lambda *_: 95))
        self.assertEqual({candidate["confidence"]["level"] for candidate in findings.candidates}, {"unknown"})

    def test_agent_notes_are_context(self):
        raw = collect("alex")
        data = raw.model_dump()
        data["pages"][INTERVIEW]["notes"] = ["img-1: News interview about the participant", "img-2: Same article"]
        findings = build_findings(RawEvidenceBundle.model_validate(data))
        interview = next(candidate for candidate in findings.candidates if candidate["sourceUrl"] == INTERVIEW)
        self.assertEqual(interview["confidence"]["reasons"][-1], "Agent note: News interview about the participant")
        self.assertEqual(interview["sourceType"], "News article")

    def test_failed_searches(self):
        raw = collect("alex")
        some = build_findings(without_search(raw, {"img-1"}))
        self.assertFalse(some.failed)
        self.assertIn("search_failed", some.warnings)
        every = build_findings(without_search(raw, {"img-1", "img-2", "img-3"}))
        self.assertTrue(every.failed)
        self.assertEqual(every.candidates, [])

    def test_failed_page_fetch_is_a_warning(self):
        findings = build_findings(collect("alex"))  # one hit is a 404 page
        self.assertEqual(findings.warnings, ["page_unavailable"])
        self.assertTrue(findings.incomplete)


class SubjectReasonTests(OfflineTestCase):
    def reasons(self, subject, url):
        findings = build_findings(collect("alex"), subject)
        return next(c["confidence"]["reasons"] for c in findings.candidates if c["sourceUrl"] == url)

    def test_name_and_city_on_the_page(self):
        reasons = self.reasons(Subject(first_name="Alex", last_name="Morgan", city="Riverbend"), INTERVIEW)
        self.assertIn("Your name appears on this page.", reasons)
        self.assertIn("Your city appears on this page.", reasons)

    def test_a_fetched_page_without_the_name_says_so(self):
        reasons = self.reasons(Subject(first_name="Jordan", last_name="Lee", city="Lawrence"), INTERVIEW)
        self.assertIn("Your name doesn't appear on this page.", reasons)
        self.assertFalse(any("city" in reason for reason in reasons))  # a missing city isn't worth saying

    def test_a_search_result_alone_only_reports_matches(self):
        self.assertFalse(any("name" in reason for reason in self.reasons(Subject(first_name="Jordan"), OLD_PROJECT)))

    def test_no_details_add_no_reasons(self):
        for subject in (None, Subject()):
            reasons = self.reasons(subject, INTERVIEW)
            self.assertFalse(any(reason.startswith("Your") for reason in reasons))

    def test_matching_ignores_case_accents_and_punctuation_but_not_partial_words(self):
        self.assertIn("Your name appears on this page.", self.reasons(Subject(first_name="ALEX", last_name="morgán"), INTERVIEW))
        self.assertIn("Your name doesn't appear on this page.", self.reasons(Subject(first_name="Morg"), INTERVIEW))
