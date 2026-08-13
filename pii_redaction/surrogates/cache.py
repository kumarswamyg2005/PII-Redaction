"""
The consistency guarantee: one real entity always maps to one surrogate.

The brief requires that the same real entity is replaced by the same fake value
everywhere it appears. That is a stronger requirement than it first looks,
because it has to hold in both directions:

* **Same real value -> same surrogate.** Otherwise a reader can tell that two
  mentions of "Acme Limited" were originally different companies.
* **Different real values -> different surrogates.** Otherwise two distinct
  people merge into one, which silently corrupts the document's meaning. This
  is the collision case, and it is enforced rather than assumed.

Keys are the exact matched span, normalised only for surrounding whitespace and
case. No fuzzy matching: near-miss merging is how a redaction tool quietly
conflates two people who happen to share a surname.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .generators import generate

__all__ = ["SurrogateCache"]


class SurrogateCache:
    """Assigns and remembers one surrogate per distinct real entity."""

    #: Re-rolls attempted before falling back to a numeric discriminator.
    _RETRY_LIMIT = 64

    def __init__(self) -> None:
        self._by_original: Dict[str, str] = {}
        self._owner_of_fake: Dict[str, str] = {}
        #: Variants deliberately sharing another entry's surrogate.
        self._aliases: set[str] = set()

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(text.split()).lower()

    def surrogate_for(self, entity_type: str, original: str) -> str:
        """Return this entity's surrogate, creating it on first sight."""
        key = self._normalise(original)
        if key in self._by_original:
            return self._by_original[key]

        fake = self._claim_unique(generate(entity_type, original), entity_type, original)
        self._by_original[key] = fake
        self._owner_of_fake[fake] = key
        return fake

    def alias(self, variant: str, canonical: str) -> None:
        """
        Point a spelling variant at the canonical entity's surrogate.

        The mapping is what the rewriter applies, so a variant absent from it is
        simply left in the document. Registering the alias is what makes a
        shortened company name get replaced — with the *same* surrogate as its
        full form, keeping the two mentions recognisably one entity.
        """
        variant_key, canonical_key = self._normalise(variant), self._normalise(canonical)
        if variant_key == canonical_key or canonical_key not in self._by_original:
            return
        self._by_original[variant_key] = self._by_original[canonical_key]
        self._aliases.add(variant_key)

    def _claim_unique(self, candidate: str, entity_type: str, original: str) -> str:
        """
        Guarantee the surrogate is not already owned by a different entity.

        Generators are deterministic and draw from finite pools, so two distinct
        real entities can land on the same surrogate. Re-roll with a salted
        seed, and fall back to a numeric discriminator if that keeps colliding.

        The fallback matters: a document naming several hundred places will
        exhaust any fixed pool, and refusing to allocate would abort the whole
        run over a cosmetic detail. A slightly repetitive surrogate is a far
        better outcome than no redaction at all.
        """
        if candidate not in self._owner_of_fake:
            return candidate

        for attempt in range(1, self._RETRY_LIMIT):
            retry = generate(entity_type, f"{original}#{attempt}")
            if retry not in self._owner_of_fake:
                return retry

        suffix = 2
        while f"{candidate} {suffix}" in self._owner_of_fake:
            suffix += 1
        return f"{candidate} {suffix}"

    # -- reporting ---------------------------------------------------------

    @property
    def mapping(self) -> Dict[str, str]:
        """The full real -> surrogate mapping. This is a re-identification key."""
        return dict(self._by_original)

    def collisions(self) -> List[Tuple[str, List[str]]]:
        """
        Surrogates claimed by more than one *distinct* entity. Must be empty.

        Aliases are excluded: they share a surrogate on purpose, because they
        are spellings of one entity rather than two entities.
        """
        grouped: Dict[str, List[str]] = {}
        for original, fake in self._by_original.items():
            if original in self._aliases:
                continue
            grouped.setdefault(fake, []).append(original)
        return [(fake, origs) for fake, origs in grouped.items() if len(origs) > 1]

    def __len__(self) -> int:
        return len(self._by_original)
