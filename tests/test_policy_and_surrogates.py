"""
Policy decisions and surrogate consistency.

Policy is where precision is won: the detector proposes, and these rules
dispose. Each test below encodes a mistake the tool made at some point.
"""

from pii_redaction.detection.policy import RedactionPolicy
from pii_redaction.surrogates.cache import SurrogateCache
from pii_redaction.surrogates.generators import generate


def glossary_policy():
    """A policy that has read a small two-column glossary, as a real one does."""
    rows = [
        ["Term", "Description"],
        ["ASBA", "Application Supported by Blocked Amount, a process of applying"],
        ["Red Herring Prospectus", "This red herring prospectus dated December 2025 issued"],
        ["Working Day", "All days on which commercial banks in Mumbai are open for business"],
        ["Exemplar", "Exemplar Wealth Management Limited"],
        ["Corporate Promoter", "Riverside Industrial Park VI Private Limited"],
    ]
    return RedactionPolicy().learn_from([], rows)


class TestGlossaryLearning:
    def test_terms_are_learned_from_the_table(self):
        policy = glossary_policy()
        assert policy.is_defined_term("ASBA")
        assert policy.is_defined_term("Working Day")

    def test_an_abbreviation_for_a_company_is_not_allow_listed(self):
        """"Exemplar" names a company; protecting it would defeat the redaction."""
        policy = glossary_policy()
        assert not policy.is_defined_term("Exemplar")
        assert policy.entity_aliases.get("Exemplar") == "Exemplar Wealth Management Limited"

    def test_a_role_definition_is_not_treated_as_an_alias(self):
        """"Corporate Promoter" is a role, not another name for the company."""
        policy = glossary_policy()
        assert "Corporate Promoter" not in policy.entity_aliases

    def test_a_fragment_of_a_defined_term_is_protected(self):
        """Zero-shot NER proposed "Red Herring"; replacing it broke the title."""
        policy = glossary_policy()
        assert policy.should_redact("ORGANIZATION", "Red Herring") is False


class TestPolicyDecisions:
    def test_public_bodies_are_not_redacted(self):
        policy = RedactionPolicy()
        assert not policy.should_redact("ORGANIZATION", "SEBI")
        assert not policy.should_redact("ORGANIZATION", "BSE Limited")

    def test_private_companies_are_redacted(self):
        policy = RedactionPolicy()
        assert policy.should_redact("ORGANIZATION", "ICICI Bank Limited")

    def test_identifiers_are_always_redacted(self):
        policy = glossary_policy()
        # Even if a glossary somehow defined it, an Aadhaar number is PII.
        assert policy.should_redact("IN_AADHAAR", "3141 5926 5351")
        assert policy.should_redact("EMAIL_ADDRESS", "someone@example.org")

    def test_common_nouns_are_not_entities(self):
        policy = RedactionPolicy()
        assert not policy.should_redact("ORGANIZATION", "bank")
        assert not policy.should_redact("ORGANIZATION", "private limited")

    def test_spans_that_swallowed_structure_are_rejected(self):
        """A model reading a contact block can return one span for three fields."""
        policy = RedactionPolicy()
        assert not policy.should_redact("ORGANIZATION", "ipo@merchantbank.example.com Telephone: +91")
        assert not policy.should_redact("PERSON", "Contact Person\nName")


class TestSurrogateConsistency:
    def test_same_entity_always_maps_to_the_same_surrogate(self):
        cache = SurrogateCache()
        first = cache.surrogate_for("ORGANIZATION", "ICICI Bank Limited")
        assert cache.surrogate_for("ORGANIZATION", "  icici bank limited  ") == first

    def test_different_entities_never_share_a_surrogate(self):
        cache = SurrogateCache()
        seen = {
            cache.surrogate_for("PERSON", f"Person Number {index}")
            for index in range(500)
        }
        assert len(seen) == 500
        assert cache.collisions() == []

    def test_pool_exhaustion_does_not_abort_the_run(self):
        """A document naming hundreds of places must still complete."""
        cache = SurrogateCache()
        values = {cache.surrogate_for("LOCATION", f"Place {i}") for i in range(300)}
        assert len(values) == 300

    def test_alias_shares_the_canonical_surrogate(self):
        cache = SurrogateCache()
        full = cache.surrogate_for("ORGANIZATION", "Exemplar Wealth Management Limited")
        cache.alias("Exemplar", "Exemplar Wealth Management Limited")
        assert cache.mapping["exemplar"] == full
        assert cache.collisions() == []  # an alias is not a collision


class TestSurrogateFormat:
    def test_generation_is_deterministic(self):
        assert generate("PERSON", "Priya Deshmukh") == generate("PERSON", "Priya Deshmukh")

    def test_surrogate_aadhaar_is_well_formed(self):
        from pii_redaction.detection.validators import is_valid_aadhaar

        assert is_valid_aadhaar(generate("IN_AADHAAR", "3141 5926 5351"))

    def test_surrogate_email_uses_the_reserved_domain(self):
        assert generate("EMAIL_ADDRESS", "someone@real.com").endswith("@example.com")

    def test_phone_surrogate_keeps_the_country_prefix(self):
        assert generate("PHONE_NUMBER", "+91 22 30752929").startswith("+91")
