"""Name-compatibility rules for grouping (spec 2b)."""

from pipeline.names import (
    differing_facts,
    looks_like_person_name,
    name_in_text,
    name_tokens,
    names_compatible,
    same_fact,
)
from tests.support import OfflineTestCase


class NameCompatibilityTests(OfflineTestCase):
    def test_spec_examples_are_compatible(self):
        for a, b in [
            ("Alex M.", "Alex Morgan"),
            ("Sam Rivera", "Sammy Rivera"),
            ("C. Brooks", "Casey Brooks"),
            ("alex morgan", "Alex Morgan"),
            ("Alex J. Morgan", "Alex Morgan"),
        ]:
            with self.subTest(a=a, b=b):
                self.assertTrue(names_compatible(a, b))
                self.assertTrue(names_compatible(b, a))

    def test_incompatible_names(self):
        for a, b in [
            ("Alex Morgan Photo", "Alex Morgan"),  # no dropping arbitrary surname tokens
            ("Alex Morgan", "Jordan Morgan"),
            ("Alex Morgan", "Alex Rivera"),
            ("Alexa Morgan", "Alex Morgan"),  # no arbitrary prefixes
            ("Sam Rivera", "Casey Rivera"),
            ("B. Brooks", "Casey Brooks"),
            ("Alex", "Alex Morgan"),  # a single name can't support a merge
            (None, "Alex Morgan"),
            ("", ""),
        ]:
            with self.subTest(a=a, b=b):
                self.assertFalse(names_compatible(a, b))

    def test_tokens_drop_case_accents_and_punctuation(self):
        self.assertEqual(name_tokens("José O'Brien-Smith"), ["jose", "obriensmith"])
        self.assertEqual(name_tokens("C. Brooks"), ["c", "brooks"])

    def test_link_labels_that_read_as_names(self):
        for label in ("Sam Rivera", "Alex Morgan", "C. Brooks"):
            self.assertTrue(looks_like_person_name(label), label)
        for label in ("Social profile", "Work profile", "About the author", "Alex's notes", "Author",
                      "Read my blog", "Original article", "Source code", "Projects", "Read More"):
            self.assertFalse(looks_like_person_name(label), label)

    def test_name_in_title_is_a_whole_phrase(self):
        title = "Alex Morgan helps neighbors navigate Riverbend transit"
        self.assertTrue(name_in_text("Alex Morgan", title))
        self.assertFalse(name_in_text("Alex M.", title))
        self.assertFalse(name_in_text("Sam Rivera", title))
        self.assertFalse(name_in_text("Alex", title))  # not a full name
        self.assertFalse(name_in_text("Alex Morga", title))

    def test_fact_comparison(self):
        self.assertTrue(same_fact("Prairie Atlas Labs", "prairie atlas labs."))
        self.assertFalse(same_fact(None, None))
        self.assertTrue(differing_facts("Kansas City, MO", "Portland, OR"))
        self.assertFalse(differing_facts("Kansas City, MO", None))
        self.assertFalse(differing_facts("Kansas City, MO", "kansas city mo"))
