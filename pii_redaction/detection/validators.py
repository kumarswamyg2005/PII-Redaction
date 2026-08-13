"""
Checksum and structural validators for identifier-style PII.

A regex alone cannot tell an Aadhaar number from an order reference: the naive
pattern ``[2-9][0-9]{11}`` matches ten billion values, most of which are not
Aadhaar numbers at all. Every identifier here carries either a checksum or
enough internal structure to be verified, so each recognizer pairs its pattern
with a validator and only reports a match the validator accepts.

That pairing is what keeps precision high on noisy documents, and it is why
this module contains no document-specific knowledge whatsoever — the rules come
from the identifier specifications, not from the file being redacted.

Every validator takes the raw matched text and returns True/False. They are
tolerant of the separators people actually type (spaces, hyphens) and are
deliberately cheap: each runs in well under a millisecond.
"""

from __future__ import annotations

import re
from typing import Callable, Dict

__all__ = [
    "is_valid_aadhaar",
    "is_valid_pan",
    "is_valid_gstin",
    "is_valid_luhn",
    "is_valid_ifsc",
    "is_valid_indian_passport",
    "is_valid_voter_id",
    "is_valid_indian_mobile",
    "VALIDATORS",
]


# ---------------------------------------------------------------------------
# Verhoeff — the checksum UIDAI chose for Aadhaar
# ---------------------------------------------------------------------------

# Multiplication table over the dihedral group D5.
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)

# Permutation table, applied cyclically by digit position.
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def verhoeff_checksum(number: str) -> int:
    """Return the Verhoeff checksum of a digit string (0 means valid)."""
    checksum = 0
    for position, digit in enumerate(reversed(number)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[position % 8][int(digit)]]
    return checksum


#: Inverse table over D5, used to derive a check digit rather than test one.
_VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_check_digit(number_without_check: str) -> str:
    """
    Return the check digit that makes ``number_without_check`` a valid Aadhaar.

    Used by the surrogate generator so replacement Aadhaar numbers are
    well-formed: a fake that failed its own checksum would be trivially
    identifiable as redacted, and would break any downstream validation.
    """
    return str(_VERHOEFF_INV[verhoeff_checksum(number_without_check + "0")])


def is_valid_aadhaar(text: str) -> bool:
    """
    True for a 12-digit Aadhaar number whose Verhoeff check digit agrees.

    Aadhaar numbers never begin with 0 or 1, which the UIDAI reserves. Together
    with the checksum this rejects the overwhelming majority of incidental
    12-digit runs (invoice numbers, concatenated dates, phone numbers with a
    country code).
    """
    number = _digits(text)
    if len(number) != 12 or number[0] in "01":
        return False
    return verhoeff_checksum(number) == 0


# ---------------------------------------------------------------------------
# PAN — Permanent Account Number
# ---------------------------------------------------------------------------

# Fourth character encodes the holder type; anything else is not a real PAN.
#   P individual   C company    H HUF          F firm    A association of persons
#   T trust        B body of individuals       L local authority
#   J artificial juridical person              G government   K Krish (trust)
_PAN_HOLDER_TYPES = set("PCHFATBLJGK")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def is_valid_pan(text: str) -> bool:
    """True for a structurally valid PAN (AAAAA9999A with a legal holder type)."""
    candidate = text.strip().upper().replace(" ", "")
    if not _PAN_RE.match(candidate):
        return False
    return candidate[3] in _PAN_HOLDER_TYPES


# ---------------------------------------------------------------------------
# GSTIN — 2-digit state + PAN + entity code + 'Z' + base-36 check character
# ---------------------------------------------------------------------------

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def is_valid_gstin(text: str) -> bool:
    """True for a GSTIN whose trailing base-36 checksum character agrees."""
    candidate = text.strip().upper().replace(" ", "")
    if not _GSTIN_RE.match(candidate):
        return False
    if not 1 <= int(candidate[:2]) <= 38:  # state codes currently run 01..38
        return False

    total = 0
    for index, char in enumerate(candidate[:14]):
        value = _GSTIN_ALPHABET.index(char) * (2 if index % 2 else 1)
        total += value // 36 + value % 36
    expected = _GSTIN_ALPHABET[(36 - total % 36) % 36]
    return expected == candidate[14]


# ---------------------------------------------------------------------------
# Luhn — payment cards
# ---------------------------------------------------------------------------

def is_valid_luhn(text: str) -> bool:
    """True for a 13-19 digit number satisfying the Luhn check."""
    number = _digits(text)
    if not 13 <= len(number) <= 19:
        return False

    total = 0
    for index, digit in enumerate(reversed(number)):
        value = int(digit)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Structural-only identifiers
# ---------------------------------------------------------------------------

_IFSC_RE = re.compile(r"^[A-Z]{4}0[0-9A-Z]{6}$")
_PASSPORT_RE = re.compile(r"^[A-PR-WY][0-9]{7}$")
_VOTER_ID_RE = re.compile(r"^[A-Z]{3}[0-9]{7}$")
_MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")


def is_valid_ifsc(text: str) -> bool:
    """True for an IFSC code: four-letter bank code, reserved 0, six-char branch."""
    return bool(_IFSC_RE.match(text.strip().upper().replace(" ", "")))


def is_valid_indian_passport(text: str) -> bool:
    """True for an Indian passport number (letter + 7 digits; Q, X, Z unused)."""
    return bool(_PASSPORT_RE.match(text.strip().upper().replace(" ", "")))


def is_valid_voter_id(text: str) -> bool:
    """True for an EPIC voter ID: three-letter functional code + 7 digits."""
    return bool(_VOTER_ID_RE.match(text.strip().upper().replace(" ", "")))


def is_valid_indian_mobile(text: str) -> bool:
    """True for a 10-digit Indian mobile number, ignoring a +91/0 prefix."""
    number = _digits(text)
    if number.startswith("91") and len(number) == 12:
        number = number[2:]
    elif number.startswith("0") and len(number) == 11:
        number = number[1:]
    return bool(_MOBILE_RE.match(number))


#: Validator lookup by entity type, consumed by the recognizers in
#: :mod:`pii_redaction.detection.indian_ids`. Adding a new identifier type means
#: adding one function above and one entry here.
VALIDATORS: Dict[str, Callable[[str], bool]] = {
    "IN_AADHAAR": is_valid_aadhaar,
    "IN_PAN": is_valid_pan,
    "IN_GSTIN": is_valid_gstin,
    "CREDIT_CARD": is_valid_luhn,
    "IN_IFSC": is_valid_ifsc,
    "IN_PASSPORT": is_valid_indian_passport,
    "IN_VOTER_ID": is_valid_voter_id,
    "IN_MOBILE": is_valid_indian_mobile,
}
