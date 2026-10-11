"""Risk scoring (pipeline/risk.py) and the Rekognition wrapper (pipeline/rekognition.py).

No test reaches a network: Jev uses an injected responder, Rekognition a fake client.
"""

import io
import unittest

from PIL import Image

from pipeline.config import load_settings
from pipeline.jev import Jev, check_questions
from pipeline.rekognition import recognize
from pipeline.risk import PresenceItem, risk_questions, rule_floor, score_presence


def jev(mode="fake", responder=None):
    env = {"JEV_MODE": mode}
    if mode == "typesafe":
        env["TYPESAFE_API_KEY"] = "ts-test-key"
    return Jev(load_settings(backend="mock", models=mode == "typesafe", environ=env, dotenv_path=None),
               responder=responder)


ITEMS = [
    PresenceItem("p1", "https://www.linkedin.com/in/someone", "Someone - Engineer", "Engineer in KC."),
    PresenceItem("p2", "https://instagram.com/someone", "someone", "Photos with friends."),
    PresenceItem("p3", "https://www.whitepages.com/name/someone", "Someone, KC", "Lives at 12 Elm Street. Call 816-555-0100."),
]


def answer_all(levels: dict[str, dict[str, float]], kinds: dict[str, tuple[str, float]] | None = None):
    """A responder that gives the probabilities we choose for each page."""
    def responder(state, questions):
        answers = {}
        for qid, q in questions.items():
            item = qid.split("_", 1)[1]
            if q["type"] == "score":
                probs = levels[item]
                answers[qid] = {"type": "score", "score": sum(int(k) * v for k, v in probs.items()),
                                "confidence": max(probs.values()), "probabilities": probs}
            else:
                kind, p = (kinds or {}).get(item, ("social", 1.0))
                rest = (1 - p) / (len(q["criteria"]) - 1)
                answers[qid] = {"type": "choice", "choice": kind, "confidence": p,
                                "probabilities": {k: (p if k == kind else rest) for k in q["criteria"]}}
        return {"model": "jev-test", "answers": answers}
    return responder


LOW = {"0": 0.9, "1": 0.08, "2": 0.02}
MED = {"0": 0.1, "1": 0.8, "2": 0.1}


class RuleTests(unittest.TestCase):
    def test_questions_are_valid(self):
        check_questions(risk_questions(ITEMS))

    def test_people_search_with_phone_is_high_floor(self):
        floor, _, reasons = rule_floor(ITEMS[2])
        self.assertEqual(floor, 2)
        self.assertIn("shows phone number", reasons)
        self.assertIn("shows street address", reasons)

    def test_subdomain_matches_category(self):
        floor, _, _ = rule_floor(PresenceItem("x", "https://en.wikipedia.org/wiki/X", "X", "Actor."))
        self.assertEqual(floor, 0)

    def test_rules_only_and_fake_agree(self):
        for client in (None, jev("fake")):
            report = score_presence("Someone", ITEMS, client)
            self.assertEqual([r.level for r in report.items], ["low", "medium", "high"])
            self.assertEqual(report.overall_level, "high")
            self.assertEqual(report.overall_score, 5 + 15 + 40)


class SizeTests(unittest.TestCase):
    def test_full_batch_of_long_pages_fits_jev_limit(self):
        import json
        from pipeline.jev import MAX_INPUT_CHARS
        from pipeline.risk import MAX_ITEMS_PER_CALL, risk_state
        items = [PresenceItem(f"p{i}", f"https://example.com/{'x' * 150}/{i}", "t" * 300, "w " * 2000)
                 for i in range(MAX_ITEMS_PER_CALL)]
        size = len(json.dumps({"state": risk_state("Someone", items), "questions": risk_questions(items)}))
        self.assertLess(size, MAX_INPUT_CHARS)


class RealJevTests(unittest.TestCase):
    def test_jev_cannot_lower_a_proved_high(self):
        client = jev("typesafe", answer_all({"p1": LOW, "p2": LOW, "p3": LOW}))
        report = score_presence("Someone", ITEMS, client)
        self.assertEqual(report.items[2].level, "high")
        self.assertEqual(report.items[2].method, "jev")

    def test_jev_can_raise_a_social_page(self):
        client = jev("typesafe", answer_all({"p1": LOW, "p2": {"0": 0.3, "1": 0.3, "2": 0.4}, "p3": LOW}))
        report = score_presence("Someone", ITEMS, client)
        self.assertEqual(report.items[1].level, "high")  # P(high)=0.4 >= alert
        self.assertIn("Jev rated the page content higher than the rules", report.items[1].reasons)

    def test_not_about_person_is_excluded(self):
        client = jev("typesafe", answer_all({"p1": LOW, "p2": MED, "p3": LOW}, {"p2": ("not_about_person", 0.9)}))
        report = score_presence("Someone", ITEMS, client)
        self.assertTrue(report.items[1].excluded)
        self.assertEqual(report.overall_score, 5 + 40)

    def test_jev_failure_falls_back_to_rules(self):
        def broken(state, questions):
            raise ConnectionError("down")
        report = score_presence("Someone", ITEMS, jev("typesafe", broken))
        self.assertEqual(report.jev_errors, ["connection_error"])
        self.assertEqual({r.method for r in report.items}, {"rules"})


class FakeRekognition:
    def __init__(self, response):
        self.response, self.calls = response, []

    def recognize_celebrities(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def png(size=(200, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


class RekognitionTests(unittest.TestCase):
    def test_parses_celebrities_and_picks_best(self):
        client = FakeRekognition({
            "CelebrityFaces": [
                {"Name": "Low Match", "Id": "a", "MatchConfidence": 70.0, "Urls": [], "Face": {}},
                {"Name": "Star", "Id": "b", "MatchConfidence": 99.2, "Urls": ["www.imdb.com/name/nm1"],
                 "Face": {"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.3, "Height": 0.4}}},
            ],
            "UnrecognizedFaces": [{"BoundingBox": {}, "Confidence": 99.0}],
        })
        result = recognize(png(), client)
        self.assertEqual(result.best().name, "Star")
        self.assertEqual(len(result.unrecognized), 1)
        self.assertIn("Bytes", client.calls[0]["Image"])

    def test_no_match_returns_none(self):
        result = recognize(png(), FakeRekognition({"CelebrityFaces": [], "UnrecognizedFaces": [{}]}))
        self.assertIsNone(result.best())

    def test_big_image_is_shrunk(self):
        client = FakeRekognition({})
        result = recognize(png((4000, 3000)), client)
        self.assertLessEqual(max(result.image_size), 1920)


class FakeCollectionClient:
    class exceptions:
        class InvalidParameterException(Exception):
            pass

    def __init__(self, matches=None, no_face=False):
        self.matches, self.no_face, self.calls = matches or [], no_face, []

    def index_faces(self, **kwargs):
        self.calls.append(("index", kwargs))
        return {"FaceRecords": [] if self.no_face else [{"Face": {"FaceId": "f-1"}}]}

    def search_faces_by_image(self, **kwargs):
        self.calls.append(("search", kwargs))
        if self.no_face:
            raise self.exceptions.InvalidParameterException("There are no faces in the image.")
        return {"FaceMatches": self.matches}


def match(person, similarity, face="f"):
    return {"Similarity": similarity, "Face": {"FaceId": f"{face}-{person}-{similarity}", "ExternalImageId": person}}


class CollectionTests(unittest.TestCase):
    def test_index_cleans_external_id(self):
        from pipeline.rekognition import index_face
        client = FakeCollectionClient()
        self.assertEqual(index_face(client, "c", png(), "Jean Dupont/é"), "f-1")
        self.assertEqual(client.calls[0][1]["ExternalImageId"], "Jean_Dupont__")

    def test_index_without_face_returns_none(self):
        from pipeline.rekognition import index_face
        self.assertIsNone(index_face(FakeCollectionClient(no_face=True), "c", png(), "p"))

    def test_search_best_first_one_row_per_person(self):
        from pipeline.rekognition import best_per_person, search_face
        client = FakeCollectionClient([match("a", 40.0), match("b", 95.5), match("a", 70.0)])
        result = best_per_person(search_face(client, "c", png()))
        self.assertEqual([(m.person_id, m.similarity) for m in result], [("b", 95.5), ("a", 70.0)])
        self.assertEqual(client.calls[0][1]["FaceMatchThreshold"], 0.0)  # lookalikes allowed

    def test_search_without_face_is_empty(self):
        from pipeline.rekognition import search_face
        self.assertEqual(search_face(FakeCollectionClient(no_face=True), "c", png()), [])


if __name__ == "__main__":
    unittest.main()
