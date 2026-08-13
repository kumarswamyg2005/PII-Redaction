"""
Identifier recognizers: pattern, then proof.

Every recognizer here follows the same shape — a deliberately loose regex
proposes candidates, and a validator from :mod:`.validators` decides which of
them are real. The regex alone is never trusted, which is the whole point: on a
financial document, twelve-digit runs are everywhere (folio numbers, share
counts, transaction references), and only a checksum can separate an Aadhaar
number from an invoice number.

Presidio's contract makes this clean. ``validate_result`` returning ``True``
promotes a match to certainty, ``False`` discards it outright, and ``None``
leaves the pattern's own score in place. So candidates start at a low score and
are *earned* up to 1.0 by passing validation — a match that fails its checksum
never reaches the output at all.

Adding an identifier type is one entry in :data:`IDENTIFIER_SPECS` plus one
validator; no other file needs to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from presidio_analyzer import Pattern, PatternRecognizer, RecognizerResult, EntityRecognizer

from . import validators

__all__ = ["IDENTIFIER_SPECS", "build_identifier_recognizers", "DateOfBirthRecognizer"]


@dataclass(frozen=True)
class IdentifierSpec:
    """One identifier type: how to find candidates and how to prove them."""

    entity: str
    #: (name, regex, base score) — score stays low until the validator agrees.
    patterns: Sequence[Tuple[str, str, float]]
    #: Nearby words that raise confidence via Presidio's context enhancer.
    context: Sequence[str] = field(default_factory=tuple)
    validator: Optional[Callable[[str], bool]] = None


IDENTIFIER_SPECS: List[IdentifierSpec] = [
    IdentifierSpec(
        entity="IN_AADHAAR",
        patterns=[("aadhaar_12_digit", r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b", 0.30)],
        context=["aadhaar", "aadhar", "uid", "uidai", "unique identification"],
        validator=validators.is_valid_aadhaar,
    ),
    IdentifierSpec(
        entity="IN_PAN",
        patterns=[("pan_10_char", r"\b[A-Z]{5}\d{4}[A-Z]\b", 0.40)],
        context=["pan", "permanent account number", "income tax"],
        validator=validators.is_valid_pan,
    ),
    IdentifierSpec(
        entity="IN_GSTIN",
        patterns=[("gstin_15_char", r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b", 0.40)],
        context=["gst", "gstin", "goods and services"],
        validator=validators.is_valid_gstin,
    ),
    IdentifierSpec(
        entity="IN_IFSC",
        patterns=[("ifsc_11_char", r"\b[A-Z]{4}0[0-9A-Z]{6}\b", 0.35)],
        context=["ifsc", "bank", "branch", "neft", "rtgs"],
        validator=validators.is_valid_ifsc,
    ),
    IdentifierSpec(
        entity="IN_PASSPORT",
        patterns=[("passport_8_char", r"\b[A-PR-WY]\d{7}\b", 0.20)],
        # Passport numbers are only eight characters and collide readily with
        # internal references, so this type leans hard on its context words.
        context=["passport", "travel document", "issued at"],
        validator=validators.is_valid_indian_passport,
    ),
    IdentifierSpec(
        entity="IN_VOTER_ID",
        patterns=[("voter_epic", r"\b[A-Z]{3}\d{7}\b", 0.25)],
        context=["voter", "epic", "election", "electoral"],
        validator=validators.is_valid_voter_id,
    ),
    IdentifierSpec(
        entity="CREDIT_CARD",
        patterns=[("card_13_19_digit", r"\b(?:\d[ -]?){12,18}\d\b", 0.20)],
        context=["card", "credit", "debit", "visa", "mastercard", "cvv"],
        validator=validators.is_valid_luhn,
    ),
    IdentifierSpec(
        entity="CORPORATE_ID",
        # A CIN identifies a company rather than a person. It is detected so
        # that policy can decide: it is redacted only when it belongs to an
        # entity that is itself being redacted.
        patterns=[("cin_21_char", r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b", 0.60)],
        context=["cin", "corporate identity", "registration number"],
    ),
]


class _ValidatedPatternRecognizer(PatternRecognizer):
    """A pattern recognizer whose matches must satisfy a validator."""

    def __init__(self, spec: IdentifierSpec) -> None:
        super().__init__(
            supported_entity=spec.entity,
            patterns=[Pattern(name, regex, score) for name, regex, score in spec.patterns],
            context=list(spec.context),
            supported_language="en",
            name=f"{spec.entity}Recognizer",
        )
        self._validator = spec.validator

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        if self._validator is None:
            return None  # keep the pattern score; nothing to prove against
        return self._validator(pattern_text)


def build_identifier_recognizers() -> List[PatternRecognizer]:
    """Instantiate every recognizer described by :data:`IDENTIFIER_SPECS`."""
    return [_ValidatedPatternRecognizer(spec) for spec in IDENTIFIER_SPECS]


class DateOfBirthRecognizer(EntityRecognizer):
    """
    Dates that are birth dates, and only those.

    A prospectus is saturated with dates — board meeting dates, filing dates,
    financial year ends — and redacting them would destroy the document while
    protecting nobody. A date is therefore only treated as PII when a birth-date
    cue appears close enough to bind to it.
    """

    #: Characters either side of the date that are searched for a cue word.
    CONTEXT_WINDOW = 60

    _DATE_PATTERNS = (
        re.compile(
            r"\b(?:January|February|March|April|May|June|July|August|September"
            r"|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b"),
        re.compile(r"\b\d{4}[/\-]\d{1,2}[/\-]\d{1,2}\b"),
    )
    _CUE = re.compile(
        r"\b(?:date of birth|dob|d\.o\.b|born on|born in|birth date|birthday"
        r"|जन्म\s*तिथि|जन्म\s*की\s*तारीख)\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        super().__init__(
            supported_entities=["DATE_OF_BIRTH"],
            supported_language="en",
            name="DateOfBirthRecognizer",
        )

    def load(self) -> None:  # required by the EntityRecognizer interface
        pass

    def analyze(self, text: str, entities, nlp_artifacts=None) -> List[RecognizerResult]:
        results: List[RecognizerResult] = []
        for pattern in self._DATE_PATTERNS:
            for match in pattern.finditer(text):
                window = text[
                    max(0, match.start() - self.CONTEXT_WINDOW):
                    match.end() + self.CONTEXT_WINDOW
                ]
                if self._CUE.search(window):
                    results.append(
                        RecognizerResult(
                            entity_type="DATE_OF_BIRTH",
                            start=match.start(),
                            end=match.end(),
                            score=0.95,
                        )
                    )
        return results
