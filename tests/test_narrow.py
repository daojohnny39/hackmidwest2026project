"""Narrowing search (pipeline/narrow.py): merging, scoring, face checks. Offline fakes only."""

import unittest

from pipeline.narrow import NarrowSession

ROWS_FRONT = [
    {"link": "https://www.instagram.com/jane.doe.kc/", "title": "Jane Doe (@jane.doe.kc)", "snippet": "KC life",
     "thumbnail": {"link": "https://img.example/jane1.jpg"}},
    {"link": "https://lookalike.example/post", "title": "Random person", "snippet": "meme",
     "thumbnail": "https://img.example/other.jpg"},
]
ROWS_LEFT = [{"link": "https://instagram.com/jane.doe.kc", "title": "Jane Doe", "snippet": ""}]  # same page, other URL form
NAME_ROWS = [
    {"link": "https://www.linkedin.com/in/janedoe", "title": "Jane Doe - Student - UMKC", "snippet": "Kansas City"},
    {"link": "https://example.org/jane-doe-chef", "title": "Jane Doe, chef in Paris", "snippet": "Paris restaurant"},
]
DETAIL_ROWS = [{"link": "https://news.umkc.example/jane", "title": "UMKC student Jane Doe wins award", "snippet": ""}]


def session(face=None, face_limit=10):
    calls = {"text": []}

    def text_search(query):
        calls["text"].append(query)
        return DETAIL_ROWS if "UMKC" in query else NAME_ROWS

    photos = iter([ROWS_FRONT, ROWS_LEFT])
    s = NarrowSession(image_search=lambda data: next(photos), text_search=text_search,
                      face_compare=face, face_limit=face_limit)
    return s, calls


def by_url(s):
    return {r.candidate.url: r for r in s.ranked()}


class NarrowTests(unittest.TestCase):
    def test_same_page_from_two_angles_is_merged_and_boosted(self):
        s, _ = session()
        s.add_photos([("left", b"L"), ("front", b"F")])
        ranked = s.ranked()
        self.assertEqual(len(ranked), 2)
        top = ranked[0]
        self.assertEqual(top.candidate.angles, {"front", "left"})
        self.assertEqual(top.score, 3)
        self.assertEqual(s.front_photo, b"F")
        self.assertEqual(s.searches, 2)

    def test_name_boosts_image_hits_and_adds_pages(self):
        s, calls = session()
        s.add_photos([("front", b"F"), ("left", b"L")])
        s.add_name("Jane Doe")
        self.assertEqual(calls["text"], ['"Jane Doe"'])
        r = by_url(s)
        insta = next(v for k, v in r.items() if "instagram" in k)
        self.assertEqual(insta.score, 2 + 1 + 3)  # two angles + full name
        self.assertIn("full name in the page", insta.reasons)
        self.assertEqual(insta.level, "likely")

    def test_detail_narrows_toward_the_right_person(self):
        s, calls = session()
        s.add_photos([("front", b"F")])
        s.add_name("Jane Doe")
        s.add_detail("UMKC")
        self.assertEqual(calls["text"][-1], '"Jane Doe" UMKC')
        r = by_url(s)
        linkedin = next(v for k, v in r.items() if "linkedin" in k)
        chef = next(v for k, v in r.items() if "chef" in k)
        self.assertGreater(linkedin.score, chef.score)
        self.assertIn("mentions UMKC", linkedin.reasons)
        self.assertEqual(linkedin.level, "likely")
        self.assertEqual(chef.level, "possible")  # same-name stranger drops out of risk scoring
        self.assertIn("matches none of the details", chef.reasons)

    def test_face_match_confirms_and_different_face_drops_lookalike(self):
        sims = {"https://img.example/jane1.jpg": 97.0, "https://img.example/other.jpg": 12.0}
        seen = []

        def face(front, url):
            seen.append((front, url))
            return sims[url]

        s, _ = session(face=face)
        s.add_photos([("front", b"F"), ("left", b"L")])
        s.add_name("Jane Doe")
        r = by_url(s)
        insta = next(v for k, v in r.items() if "instagram" in k)
        look = next(v for k, v in r.items() if "lookalike" in k)
        self.assertEqual(insta.level, "confirmed")
        self.assertIn("face matches (97)", insta.reasons)
        self.assertEqual(look.level, "noise")
        self.assertTrue(all(front == b"F" for front, _ in seen))
        self.assertEqual(len(seen), 2)  # each picture checked once, not again after the name step

    def test_face_limit_is_respected(self):
        s, _ = session(face=lambda f, u: 50.0, face_limit=1)
        s.add_photos([("front", b"F")])
        self.assertEqual(s.face_checks, 1)

    def test_bad_links_are_skipped(self):
        s = NarrowSession(image_search=lambda d: [{"link": "javascript:alert(1)"}, {"title": "no link"}],
                          text_search=lambda q: [])
        s.add_photos([("front", b"F")])
        self.assertEqual(s.candidates, {})


if __name__ == "__main__":
    unittest.main()
