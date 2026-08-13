"""
Applying a real -> surrogate mapping to arbitrary text.

Detection runs once over the document's joined text; rewriting happens later,
against a completely different coordinate system (XML text nodes). Character
offsets cannot survive that trip, so the mapping is re-applied by literal
match instead. That indirection is deliberate: it is also what makes the
consistency guarantee hold, because every occurrence of an entity is rewritten
from the same table regardless of whether the detector found that particular
occurrence.

Two rules make the literal pass safe:

* **Longest first** — ``Example Cables Limited`` must be consumed before
  ``KSH International``, or the longer name is left half-redacted.
* **Token boundaries** — an entity that begins and ends with an alphanumeric
  is only matched at word boundaries, so a short entity cannot fire inside an
  unrelated word.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Tuple

__all__ = ["EntityMatcher", "Replacement"]

#: One rewrite on a piece of text: half-open span plus the text to insert.
Replacement = Tuple[int, int, str]


class EntityMatcher:
    """Finds every occurrence of the mapped entities in a piece of text."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        self._mapping: Dict[str, str] = dict(mapping)
        self._pattern = self._compile(self._mapping)

    @staticmethod
    def _compile(mapping: Mapping[str, str]) -> "re.Pattern | None":
        if not mapping:
            return None

        alternatives: List[str] = []
        # Longest first so the alternation prefers the most specific entity;
        # Python's `|` is first-match-wins, not longest-match-wins.
        for original in sorted(mapping, key=len, reverse=True):
            body = re.escape(original.strip())
            # Whitespace in the source may be any run of whitespace in the
            # document, including a line break inside a table cell.
            body = re.sub(r"(?:\\[ ])+", r"\\s+", body)
            prefix = r"(?<![A-Za-z0-9])" if original[:1].isalnum() else ""
            suffix = r"(?![A-Za-z0-9])" if original[-1:].isalnum() else ""
            alternatives.append(f"{prefix}(?:{body}){suffix}")

        return re.compile("|".join(alternatives), re.IGNORECASE)

    def find(self, text: str) -> List[Replacement]:
        """
        Return non-overlapping replacements for ``text``, in document order.

        ``re.finditer`` already yields non-overlapping matches, and the
        longest-first alternation means the match taken at each position is the
        most specific one available there.
        """
        if not self._pattern or not text:
            return []

        found: List[Replacement] = []
        for match in self._pattern.finditer(text):
            if self._inside_address_token(text, match.start(), match.end()):
                continue
            key = " ".join(match.group(0).split()).lower()
            surrogate = self._mapping.get(key)
            if surrogate is None:
                # The span matched with flexible whitespace; recover the entry
                # by normalising the configured keys the same way.
                surrogate = self._lookup_normalised(key)
            if surrogate is not None:
                found.append((match.start(), match.end(), surrogate))
        return found

    @staticmethod
    def _inside_address_token(text: str, start: int, end: int) -> bool:
        """
        True when the match is only part of an e-mail address.

        A company short name can be the local part of an address:
        ``ksh@portal.registrar.example.com``. Replacing that fragment produced
        ``Crestview LLP 2@portal.registrar.example.com`` — a broken address still exposing
        the real domain. Addresses are entities in their own right and are
        replaced whole, so a match touching an ``@`` from either side is one
        that should not fire.

        A span containing its own ``@`` is a complete address and is allowed
        through; the check is only for fragments.
        """
        if "@" in text[start:end]:
            return False
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        return before == "@" or after == "@"

    def _lookup_normalised(self, key: str) -> "str | None":
        for original, surrogate in self._mapping.items():
            if " ".join(original.split()).lower() == key:
                return surrogate
        return None

    def apply(self, text: str) -> Tuple[str, int]:
        """Rewrite ``text`` in full. Returns the new text and the match count."""
        replacements = self.find(text)
        for start, end, surrogate in reversed(replacements):
            text = text[:start] + surrogate + text[end:]
        return text, len(replacements)

    def __len__(self) -> int:
        return len(self._mapping)
