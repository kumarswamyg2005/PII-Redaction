"""
Surrogate value generation: what a detected entity is replaced *with*.

Two properties matter, and they pull in the same direction as the project's
goal of an output that looks untouched:

**Format preservation.** A surrogate keeps the shape of what it replaces — a
PAN is replaced by something PAN-shaped, a phone number by a phone number of
the same digit count. The document stays internally consistent, and a reader
scanning the page sees ordinary content rather than obvious holes.

**Determinism.** Generation is seeded from the real value, so the same input
always produces the same surrogate. That makes runs reproducible and diffable
without storing any state between them.

Surrogates are drawn from ranges reserved for documentation and testing
wherever such a range exists (``example.com`` for e-mail, ``203.0.113.0/24``
for IP), so a surrogate can never collide with a real-world identifier.

Adding a PII type means adding one function here and registering it in
:data:`GENERATORS` — see the worked example in the README.
"""

from __future__ import annotations

import hashlib
import random
from typing import Callable, Dict

from ..detection.validators import verhoeff_check_digit

__all__ = ["generate", "GENERATORS"]


# Surname/forename pools are deliberately mundane and obviously synthetic.
# Wide enough that a document naming a hundred people does not produce four
# "Mary"s in one sentence, as an earlier 24-name pool did on the cover page.
_FORENAMES = (
    "Aarav", "Isha", "Rohan", "Meera", "Kabir", "Ananya", "Vivaan", "Diya",
    "Arjun", "Saanvi", "Reyansh", "Aditi", "Kiaan", "Naina", "Dhruv", "Tara",
    "Ishaan", "Anaya", "Advait", "Myra", "Vihaan", "Kiara", "Aryan", "Nitya",
    "Rudra", "Sanya", "Yash", "Riya", "Devansh", "Avni", "Karan", "Pooja",
    "Nikhil", "Sneha", "Rahul", "Divya", "Manish", "Kavya", "Sameer", "Ira",
    "John", "Jane", "Peter", "Mary", "Robert", "Emily", "James", "Sarah",
    "Daniel", "Laura", "Thomas", "Anna", "Michael", "Clara", "Simon", "Ruth",
)
_SURNAMES = (
    "Rao", "Iyer", "Menon", "Kapoor", "Bose", "Nair", "Sinha", "Chawla",
    "Verma", "Reddy", "Joshi", "Pillai", "Desai", "Bhatt", "Kulkarni", "Shetty",
    "Malhotra", "Chopra", "Saxena", "Trivedi", "Bansal", "Mehta", "Ghosh",
    "Rana", "Sethi", "Vora", "Dutta", "Rastogi", "Kaul", "Pandit",
    "Doe", "Smith", "Parker", "Johnson", "Wilson", "Davis", "Brown", "Miller",
    "Anderson", "Thomas", "Taylor", "Clarke", "Hughes", "Ward", "Price",
)
# Two independent word lists combined into a name. With 40 heads and 14 mid
# words the space is ~560 distinct companies before the tail is even chosen,
# so one prefix cannot open twenty different names the way "Apex" did.
_COMPANY_HEADS = (
    "Acme", "Pinnacle", "Summit", "Nexus", "Horizon", "Apex", "Sterling",
    "Vanguard", "Atlas", "Beacon", "Crestview", "Zenith", "Prism", "Trinity",
    "Paramount", "Meridian", "Quantum", "Starlight", "Pioneer", "Evergreen",
    "Northwind", "Ironbridge", "Baywood", "Calder", "Draycott", "Elmhurst",
    "Fairstead", "Glenmore", "Halcyon", "Inverness", "Jasper", "Kestrel",
    "Lyndon", "Mayfield", "Norbrook", "Oakhurst", "Penrose", "Quarry Hill",
    "Ravenswood", "Thornton",
)
_COMPANY_MIDS = (
    "", "", "Metals", "Polymers", "Logistics", "Engineering", "Textiles",
    "Chemicals", "Infra", "Systems", "Trading", "Components", "Materials",
    "Precision",
)
_COMPANY_TAILS = (
    "Limited", "Private Limited", "Industries Limited", "Holdings Limited",
    "Enterprises", "Solutions Private Limited", "Capital", "Group", "LLP",
)
_STREETS = (
    "Main Street", "Oak Avenue", "Pine Road", "Elm Drive", "Cedar Lane",
    "Maple Court", "Birch Way", "Walnut Street", "Willow Road", "Aspen Rise",
)
# Combined as "<qualifier> <base>" so the space is large enough that a document
# with hundreds of distinct place names still gets a distinct surrogate for
# each. A five-name pool cannot, and silently collapsing two real places into
# one fake would misrepresent the document.
_CITY_QUALIFIERS = (
    "", "North ", "South ", "East ", "West ", "New ", "Old ", "Upper ", "Lower ",
    "Port ", "Mount ", "Fort ", "Little ", "Great ",
)
_CITY_BASES = (
    "Springfield", "Riverdale", "Lakewood", "Fairview", "Greenville",
    "Burlington", "Concord", "Dover", "Ashford", "Bramford", "Clearwater",
    "Elmsworth", "Hartley", "Kingsford", "Marlow", "Norbury", "Oakhaven",
    "Redfield", "Stonebrook", "Thornbury", "Westbrook", "Yarrow",
)
_STATES = ("Example State", "Sample State", "Test State", "Model State")


def _rng(entity_type: str, original: str) -> random.Random:
    """Deterministic RNG seeded by the entity so output is reproducible."""
    digest = hashlib.sha256(f"{entity_type}:{original.lower()}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _person(rng: random.Random, original: str) -> str:
    """A forename/surname pair, matching the original's word count."""
    words = max(2, min(3, len(original.split())))
    # Sample without replacement so a three-part name cannot come back as
    # "Mary Pillai Pillai".
    parts = [rng.choice(_FORENAMES), *rng.sample(_SURNAMES, words - 1)]
    return " ".join(parts)


def _organization(rng: random.Random, original: str) -> str:
    parts = [rng.choice(_COMPANY_HEADS), rng.choice(_COMPANY_MIDS), rng.choice(_COMPANY_TAILS)]
    return " ".join(part for part in parts if part)


def _location(rng: random.Random, original: str) -> str:
    """
    An address whose line count tracks the original.

    Addresses in this document run from a bare city name to a six-line
    registered-office block; emitting a full postal address in place of one
    word would be conspicuous, so the surrogate matches the original's shape.
    """
    if len(original) < 20 and "\n" not in original:
        return f"{rng.choice(_CITY_QUALIFIERS)}{rng.choice(_CITY_BASES)}"
    return (
        f"{rng.randint(1, 999)} {rng.choice(_STREETS)}, "
        f"{rng.choice(_CITY_QUALIFIERS)}{rng.choice(_CITY_BASES)}, "
        f"{rng.choice(_STATES)} {rng.randint(10000, 99999)}"
    )


def _email(rng: random.Random, original: str) -> str:
    """Reserved documentation domain, so a surrogate can never reach a real inbox."""
    return f"{rng.choice(_FORENAMES).lower()}.{rng.choice(_SURNAMES).lower()}@example.com"


def _phone(rng: random.Random, original: str) -> str:
    """
    Preserve the +91 / leading-zero shape and the digit count.

    Always emits a plausible telephone number. An earlier version returned the
    literal string "00000000" when handed a span with no digits in it, which is
    how the *word* "Telephone" ended up rendered as "00000000:" in the output.
    A surrogate generator should never be the thing that makes a document look
    broken; spans that are not really phone numbers are now rejected upstream
    by the policy, and this stays well-formed regardless.
    """
    digits = "".join(character for character in original if character.isdigit())
    if original.strip().startswith("+") or digits.startswith("91"):
        return f"+91 {rng.randint(70000, 99999)} {rng.randint(10000, 99999)}"
    if 0 < len(digits) <= 8:
        return f"0{rng.randint(20, 99)} {rng.randint(100000, 999999)}"
    return f"0{rng.randint(20, 99)} {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}"


def _aadhaar(rng: random.Random, original: str) -> str:
    """A well-formed Aadhaar: 11 random digits plus a real Verhoeff check digit."""
    body = str(rng.randint(2, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(10))
    number = body + verhoeff_check_digit(body)
    return f"{number[:4]} {number[4:8]} {number[8:]}" if " " in original else number


def _pan(rng: random.Random, original: str) -> str:
    letters = lambda n: "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))
    return f"{letters(3)}P{letters(1)}{rng.randint(1000, 9999)}{letters(1)}"


def _gstin(rng: random.Random, original: str) -> str:
    # Structural surrogate; the checksum character is intentionally not solved
    # for, because a surrogate GSTIN must not be mistaken for a live one.
    letters = lambda n: "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))
    return f"{rng.randint(11, 37):02d}{letters(3)}P{letters(1)}{rng.randint(1000, 9999)}{letters(1)}1Z0"


def _corporate_id(rng: random.Random, original: str) -> str:
    """CIN shape: L/U + 5-digit industry + 2-letter state + year + type + serial."""
    return (
        f"{rng.choice('LU')}{rng.randint(10000, 99999)}"
        f"{rng.choice(('MH', 'DL', 'KA', 'TN'))}{rng.randint(1990, 2020)}"
        f"PLC{rng.randint(100000, 999999)}"
    )


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _date_of_birth(rng: random.Random, original: str) -> str:
    """Keep the original's separator and ordering so the field still parses."""
    day, month, year = rng.randint(1, 28), rng.randint(1, 12), rng.randint(1960, 2000)
    if "/" in original:
        return f"{day:02d}/{month:02d}/{year}"
    if "-" in original:
        return f"{day:02d}-{month:02d}-{year}"
    return f"{_MONTH_NAMES[month - 1]} {day}, {year}"


def _web_address(rng: random.Random, original: str) -> str:
    host = rng.choice(_COMPANY_HEADS).lower()
    prefix = "https://" if original.lower().startswith("http") else ""
    www = "www." if "www." in original.lower() else ""
    return f"{prefix}{www}{host}.example.com"


def _credit_card(rng: random.Random, original: str) -> str:
    return "4111 1111 1111 1111"  # the canonical test card number


def _ip_address(rng: random.Random, original: str) -> str:
    return f"203.0.113.{rng.randint(1, 254)}"  # TEST-NET-3, reserved for docs


def _generic(rng: random.Random, original: str) -> str:
    """
    A same-shaped stand-in for a type with no dedicated generator.

    Letters become letters, digits become digits, punctuation is preserved, so
    a registration number stays registration-number-shaped and the surrounding
    layout is undisturbed. The previous fallback emitted "[REDACTED-1234]",
    which reached the output document six times and looked like a bug rather
    than a redaction.
    """
    shaped = []
    for character in original:
        if character.isdigit():
            shaped.append(str(rng.randint(0, 9)))
        elif character.isupper():
            shaped.append(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"))
        elif character.islower():
            shaped.append(rng.choice("abcdefghijkmnpqrstuvwxyz"))
        else:
            shaped.append(character)
    rendered = "".join(shaped)
    # Never hand back something that could be mistaken for the original.
    return rendered if rendered != original else f"{rendered}X"


#: Generator lookup by entity type. Unknown types fall back to :func:`_generic`.
GENERATORS: Dict[str, Callable[[random.Random, str], str]] = {
    "PERSON": _person,
    "ORGANIZATION": _organization,
    "LOCATION": _location,
    "ADDRESS": _location,
    "EMAIL_ADDRESS": _email,
    "PHONE_NUMBER": _phone,
    "IN_MOBILE": _phone,
    "IN_AADHAAR": _aadhaar,
    "IN_PAN": _pan,
    "IN_GSTIN": _gstin,
    "IN_IFSC": lambda r, o: f"{''.join(r.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ') for _ in range(4))}0{r.randint(100000, 999999)}",
    "IN_PASSPORT": lambda r, o: f"{r.choice('ABCDEFGHJKLMNPRSTUVWY')}{r.randint(1000000, 9999999)}",
    "IN_VOTER_ID": lambda r, o: f"{''.join(r.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ') for _ in range(3))}{r.randint(1000000, 9999999)}",
    "CORPORATE_ID": _corporate_id,
    # Registration numbers of the intermediaries and auditors. They identify a
    # party we redact, so they are redacted too — but they need a shaped
    # surrogate, not the generic placeholder. Six "[REDACTED-1234]" strings
    # reached the output document before these existed.
    "BANK_ACCOUNT": lambda r, o: f"{r.randint(10**11, 10**12 - 1)}",
    "REGISTRATION_ID": lambda r, o: (
        f"IN{r.choice('ABMPR')}{r.randint(100000, 999999):06d}"
    ),
    "DATE_OF_BIRTH": _date_of_birth,
    "WEB_ADDRESS": _web_address,
    "URL": _web_address,
    "CREDIT_CARD": _credit_card,
    "IP_ADDRESS": _ip_address,
}


def generate(entity_type: str, original: str) -> str:
    """Produce a deterministic, format-preserving surrogate for one entity."""
    generator = GENERATORS.get(entity_type, _generic)
    return generator(_rng(entity_type, original), original)
