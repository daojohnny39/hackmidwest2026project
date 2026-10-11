"""Person-name normalization and compatibility rules for grouping (spec 2b).

Two display names are compatible when their first names match, one is an initial,
or they form an explicit alias pair (Sam/Sammy), and their last names match or one
is an initial. Middle tokens are ignored. Nothing else is inferred: "Alex Morgan
Photo" is not compatible with "Alex Morgan", because its last token is "Photo".
"""

from __future__ import annotations

import re
import unicodedata

# Explicit nickname groups. A name is only an alias of names in its own group.
ALIAS_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(group.split())
    for group in (
        "sam sammy samuel samantha",
        "alex alexander alexandra alexis",
        "casey case",
        "chris christopher christina christine",
        "dan danny daniel",
        "dave david",
        "jim jimmy james",
        "joe joey joseph",
        "john johnny jon jonathan",
        "kate katie katherine kathryn",
        "liz beth elizabeth",
        "matt matthew",
        "mike michael",
        "nick nicholas",
        "rob robert bob bobby",
        "steve steven stephen",
        "tom tommy thomas",
        "will william bill",
    )
)

# Words that mark a link label as navigation text rather than a person's name.
_NAV_WORDS = frozenset(
    "about all article author blog code contact follow home link me more my notes of on original "
    "page profile projects read see site social source the updates view website work".split()
)
_NAME_TOKEN = re.compile(r"^(?:[A-Z][a-z'’\-]+|[A-Z]\.?)$")


def name_tokens(name: str | None) -> list[str]:
    """Lowercase ASCII tokens with punctuation removed: "C. Brooks" -> ["c", "brooks"]."""
    if not name:
        return []
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’\-]", "", text)
    return re.findall(r"[a-z0-9]+", text)


def normalize_name(name: str | None) -> str:
    return " ".join(name_tokens(name))


def _same_first(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return any(a in group and b in group for group in ALIAS_GROUPS)


def _same_last(a: str, b: str) -> bool:
    if a == b:
        return True
    return (len(a) == 1 or len(b) == 1) and a[0] == b[0]


def names_compatible(a: str | None, b: str | None) -> bool:
    """True when both names have a first and last name and the two agree (see module doc)."""
    left, right = name_tokens(a), name_tokens(b)
    if len(left) < 2 or len(right) < 2:
        return False
    return _same_first(left[0], right[0]) and _same_last(left[-1], right[-1])


def looks_like_person_name(label: str) -> bool:
    """Whether a link label reads as a person's name ("Sam Rivera"), not navigation ("Social profile")."""
    words = label.split()
    if not 2 <= len(words) <= 4:
        return False
    if any(word.lower().strip(".") in _NAV_WORDS for word in words):
        return False
    return all(_NAME_TOKEN.match(word) for word in words)


def name_in_text(name: str | None, text: str) -> bool:
    """Whether the full name appears as a whole-word phrase in text, ignoring case and punctuation."""
    tokens = name_tokens(name)
    if len(tokens) < 2:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(' '.join(tokens))}(?![a-z0-9])", normalize_name(text)) is not None


def same_fact(a: str | None, b: str | None) -> bool:
    """Case- and punctuation-insensitive equality for workplaces and locations."""
    return bool(a and b) and normalize_name(a) == normalize_name(b)


def differing_facts(a: str | None, b: str | None) -> bool:
    """Both values are stated and they differ. A missing value is never a conflict."""
    return bool(normalize_name(a)) and bool(normalize_name(b)) and normalize_name(a) != normalize_name(b)
