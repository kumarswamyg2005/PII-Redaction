"""
Checksum validators.

The Aadhaar numbers here are generated and carry genuine Verhoeff check digits,
so they exercise the arithmetic exactly as a real one would. The real values
from the prospectus's embedded ID cards are deliberately absent: those identify
private individuals, and publishing an Aadhaar number is an offence under the
Aadhaar Act. A test fixture is not a good enough reason to disclose one.
"""

import random

from pii_redaction.detection.validators import (
    is_valid_aadhaar,
    is_valid_gstin,
    is_valid_ifsc,
    is_valid_indian_mobile,
    is_valid_indian_passport,
    is_valid_luhn,
    is_valid_pan,
    verhoeff_check_digit,
)


class TestAadhaar:
    def test_accepts_a_well_formed_number(self):
        assert is_valid_aadhaar("3141 5926 5351")
        assert is_valid_aadhaar("314159265351")

    def test_rejects_wrong_check_digit(self):
        assert not is_valid_aadhaar("3141 5926 5352")

    def test_rejects_reserved_leading_digits(self):
        # UIDAI never issues a number beginning 0 or 1.
        assert not is_valid_aadhaar("094365933461")
        assert not is_valid_aadhaar("194365933461")

    def test_rejects_wrong_length(self):
        assert not is_valid_aadhaar("31415926535")
        assert not is_valid_aadhaar("3141592653510")

    def test_checksum_removes_most_random_candidates(self):
        """The precision claim: the regex alone is not enough."""
        rng = random.Random(7)
        candidates = [str(rng.randint(2 * 10**11, 10**12 - 1)) for _ in range(20_000)]
        survivors = sum(is_valid_aadhaar(c) for c in candidates)
        assert survivors / len(candidates) < 0.12  # ~10%, i.e. ~90% rejected

    def test_generated_check_digit_round_trips(self):
        assert is_valid_aadhaar("31415926535" + verhoeff_check_digit("31415926535"))


class TestPan:
    def test_accepts_a_well_formed_pan(self):
        assert is_valid_pan("ABCPD1234E")

    def test_rejects_invalid_holder_type(self):
        # The fourth character encodes holder type; Z is not one.
        assert not is_valid_pan("ABCZD1234E")

    def test_rejects_wrong_shape(self):
        assert not is_valid_pan("ABCDE1234")
        assert not is_valid_pan("ABCD12345F")
        assert not is_valid_pan("12345ABCDE")


class TestOtherIdentifiers:
    def test_luhn(self):
        assert is_valid_luhn("4539578763621486")
        assert not is_valid_luhn("4539578763621487")

    def test_gstin_checksum(self):
        assert is_valid_gstin("27AAPFU0939F1ZV")
        assert not is_valid_gstin("27AAPFU0939F1ZX")

    def test_ifsc_requires_reserved_fifth_character(self):
        assert is_valid_ifsc("HDFC0001234")
        assert not is_valid_ifsc("HDFC1001234")

    def test_indian_mobile_series(self):
        assert is_valid_indian_mobile("+91 9876543210")
        assert is_valid_indian_mobile("09876543210")
        assert not is_valid_indian_mobile("1234567890")  # must start 6-9

    def test_passport_excludes_unused_letters(self):
        assert is_valid_indian_passport("A1234567")
        assert not is_valid_indian_passport("Q1234567")
        assert not is_valid_indian_passport("A123456")
