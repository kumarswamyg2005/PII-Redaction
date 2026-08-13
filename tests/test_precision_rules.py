"""
Precision rules: what the tool refuses to redact, and why.

Every case here is a defect that reached the output document at some point.
The comment on each test is the evidence, so a future change that reintroduces
one fails with an explanation rather than a bare assertion error.
"""

import pytest

from pii_redaction.detection.policy import RedactionPolicy


@pytest.fixture
def policy():
    """A policy that has read a small glossary, as a real document supplies."""
    rows = [
        ["Term", "Description"],
        ["ASBA", "Application Supported by Blocked Amount, the mechanism by which"],
        ["Equity Shares", "The equity shares of our Company of face value five rupees each"],
        ["Working Day", "All days on which commercial banks in Mumbai are open"],
        ["BookRunningLeadManagers", "The book running lead managers to the Offer, namely"],
    ]
    return RedactionPolicy().learn_from([], rows)


class TestFieldLabels:
    def test_telephone_is_a_caption_not_a_number(self, policy):
        """Rendered as "00000000:" in the output before this rule existed."""
        assert not policy.should_redact("PHONE_NUMBER", "Telephone")
        assert not policy.should_redact("ORGANIZATION", "Telephone")

    def test_other_captions(self, policy):
        for label in ("Website", "Email", "Name", "Contact Person", "Registered Office"):
            assert not policy.should_redact("ORGANIZATION", label), label

    def test_identifier_scheme_names_are_captions(self, policy):
        """"Aadhaar" labels the number; the number is the secret."""
        assert not policy.should_redact("ORGANIZATION", "Aadhaar")
        assert not policy.should_redact("ORGANIZATION", "PAN")

    def test_a_digit_bearing_type_needs_digits(self, policy):
        assert not policy.should_redact("IN_AADHAAR", "Aadhaar")
        assert policy.should_redact("IN_AADHAAR", "3141 5926 5351")


class TestGeography:
    def test_bare_places_are_not_addresses(self, policy):
        """"...factories across Maharashtra" identifies nobody."""
        for place in ("Mumbai", "Maharashtra", "Pune", "Bombay"):
            assert policy.is_bare_place("LOCATION", place), place

    def test_a_real_address_carries_a_number(self, policy):
        assert not policy.is_bare_place(
            "LOCATION", "11/3, Village Kharoli, Chakan Taluka - Khed, Pune - 410 501"
        )


class TestVocabularyAndProse:
    def test_phrases_built_from_glossary_words(self, policy):
        assert policy.is_vocabulary_phrase("Equity Shares")

    def test_prose_is_not_a_name(self, policy):
        """Became "Fairstead Components Limited is Rs 5 each" in the output."""
        assert policy.is_prose_phrase("ORGANIZATION", "The face value of the Equity Shares")
        assert not policy.should_redact("ORGANIZATION", "the Securities and Exchange Board")

    def test_real_names_survive_every_rule(self, policy):
        for name in (
            "Exemplar Wealth Management Limited",
            "Lakeside Motors and Electricals Private Limited",
            "Example & Partners LLP",
            "Riverside Industrial Park VI Private Limited",
        ):
            assert policy.should_redact("ORGANIZATION", name), name

    def test_all_caps_names_are_not_treated_as_prose(self, policy):
        """The cover page lists promoters in capitals; they are still names."""
        assert policy.should_redact("PERSON", "ARUN GANESH PRABHU")

    def test_run_together_glossary_terms_are_split(self, policy):
        """The source writes "BookRunningLeadManagers" as one token."""
        assert policy.is_defined_term("Book Running Lead Managers")


class TestTransactionReferences:
    """The brief's own example: Order and Ticket numbers."""

    def test_transaction_references_are_kept(self, policy):
        assert not policy.should_redact("CORPORATE_ID", "481920", "Order No. ")
        assert not policy.should_redact("CORPORATE_ID", "77321", "Ticket Number: ")

    def test_a_number_without_that_context_is_still_considered(self, policy):
        assert policy.should_redact("CORPORATE_ID", "481920", "and then ")

    def test_person_identifiers_are_never_exempted(self, policy):
        """Even labelled as an order, an Aadhaar number is still an Aadhaar."""
        assert policy.should_redact("IN_AADHAAR", "3141 5926 5351", "Order No. ")


class TestStructuredTypesAreNotJudgedAsProse:
    """
    An e-mail address is entirely lower case, so a casing-based prose test
    reads it as a sentence. Applying that test to structured types silently
    dropped `rm6.branch@bankexample.co.in` from a run and leaked the real domain.
    """

    def test_email_is_not_prose(self, policy):
        from pii_redaction.pipeline import _STRUCTURED_TYPES

        assert "EMAIL_ADDRESS" in _STRUCTURED_TYPES
        assert policy.should_redact("EMAIL_ADDRESS", "rm6.branch@bankexample.co.in")

    def test_a_structured_type_must_look_like_its_type(self, policy):
        assert not policy.should_redact("EMAIL_ADDRESS", "Contact the department")
        assert not policy.should_redact("WEB_ADDRESS", "The face value of the Equity Shares")


class TestCommonNounsWrittenInLowerCaseElsewhere:
    """
    The document teaches us its own vocabulary.

    A real run replaced the word "Delay" — opening the risk-factor bullet
    "Delay in or inability of the vendors to provide the equipment" — with the
    fake person "Sameer Bose", 19 times. The existing all-lower-case guard
    could not see it, because a sentence-initial word is capitalised. The same
    document writes "any delay in placing the orders" mid-sentence, and no
    proper noun is ever written that way.
    """

    @pytest.fixture
    def policy(self):
        prose = [
            "In the event of any delay in placing the orders, margins fall.",
            "Our manufacturing facilities are located across three states.",
            "Arun Ganesh Prabhu is our Managing Director.",
        ]
        return RedactionPolicy().learn_from(prose, [])

    def test_sentence_initial_common_noun_is_not_a_person(self, policy):
        assert policy.occurs_uncapitalised("Delay")
        assert not policy.should_redact("PERSON", "Delay")

    def test_multi_word_common_phrase_is_not_an_organisation(self, policy):
        assert not policy.should_redact("ORGANIZATION", "Manufacturing Facilities")

    def test_a_real_name_is_untouched(self, policy):
        """The rule must never fire on a name the document only capitalises."""
        assert not policy.occurs_uncapitalised("Arun Ganesh Prabhu")
        assert policy.should_redact("PERSON", "Arun Ganesh Prabhu")

    def test_structured_types_are_exempt(self, policy):
        """An e-mail address is legitimately all lower case."""
        assert policy.should_redact("EMAIL_ADDRESS", "kushal@example.com")

    def test_a_phrase_longer_than_the_index_is_not_matched(self, policy):
        assert not policy.occurs_uncapitalised("one two three four five six seven")


class TestGlossaryAbbreviations:
    """
    An offer document ends its glossary with an abbreviations table whose
    descriptions are short. Requiring 25 characters discarded 66 of this
    document's own terms, and "Underwriters" was then redacted as a company.
    """

    @pytest.fixture
    def policy(self):
        rows = [
            ["Term", "Description"],
            ["Underwriters", "[●]"],
            ["IPO", "Initial public offer"],
            ["RBI", "Reserve Bank of India"],
            # A data row, not a definition: long label, short value. Learning
            # this would protect a company from redaction.
            ["Example Registrar Services Private Limited (Formerly Link Intime India "
             "Private Limited)", "Radha Venkatesh"],
        ]
        return RedactionPolicy().learn_from([], rows)

    def test_a_term_with_a_placeholder_description_is_still_a_term(self, policy):
        assert policy.is_defined_term("Underwriters")
        assert not policy.should_redact("ORGANIZATION", "Underwriters")

    def test_short_abbreviations_are_learned(self, policy):
        assert policy.is_defined_term("IPO")
        assert policy.is_defined_term("RBI")

    def test_a_long_label_is_not_treated_as_an_abbreviation(self, policy):
        """Otherwise a company name acquires protection from redaction."""
        assert not policy.is_defined_term(
            "Example Registrar Services Private Limited (Formerly Link Intime India "
            "Private Limited)"
        )


class TestPublicHosts:
    """Every offer document points readers at the regulator's own website."""

    def test_regulator_and_exchange_sites_survive(self):
        policy = RedactionPolicy()
        for host in ("www.sebi.gov.in", "www.bseindia.com", "www.nseindia.com",
                     "https://www.sebi.gov.in/filings", "nsdl.co.in"):
            assert policy.is_public_host(host), host
            assert not policy.should_redact("WEB_ADDRESS", host), host

    def test_a_private_party_site_is_still_redacted(self):
        policy = RedactionPolicy()
        for host in ("www.issuerexample.com", "www.merchantbank.example.com", "bajajfinance.com"):
            assert not policy.is_public_host(host), host
            assert policy.should_redact("WEB_ADDRESS", host), host


class TestStructuralHeadingsAreNotTruecased:
    """
    Truecasing a heading invents an entity: "TABLE OF CONTENTS" became
    "Table Of Contents", was read as a company, and was replaced with
    "Jasper Textiles Capital" — destroying the contents page and emptying
    three paragraphs of the output.
    """

    def test_a_repeated_caps_heading_is_left_alone(self):
        from pii_redaction.pipeline import RedactionPipeline

        text = "TABLE OF CONTENTS\nsome body prose here\nTABLE OF CONTENTS\n"
        assert "Table Of Contents" not in RedactionPipeline._truecase(text)

    def test_a_contents_entry_with_a_page_number_is_left_alone(self):
        from pii_redaction.pipeline import RedactionPipeline

        text = "CERTAIN CONVENTIONS AND MARKET DATA15\nbody\n"
        assert "Market Data" not in RedactionPipeline._truecase(text)

    def test_a_cover_page_name_is_still_truecased(self):
        """The pass exists to recover these; the heading rule must not block it."""
        from pii_redaction.pipeline import RedactionPipeline

        text = "ARUN GANESH PRABHU\nManaging Director\n"
        assert "Arun Ganesh Prabhu" in RedactionPipeline._truecase(text)

    def test_a_corporate_id_ending_in_digits_is_not_mistaken_for_a_heading(self):
        """A looser page-number test skipped this line and cost two annotations."""
        from pii_redaction.pipeline import RedactionPipeline

        text = "KSH INTERNATIONAL LIMITED CORPORATE IDENTITY NUMBER: U00000XX0000XXX000000\n"
        assert "Ksh International Limited" in RedactionPipeline._truecase(text)


class TestExchangeLegalNames:
    """
    The exchanges' abbreviations were allow-listed but their legal names were
    not fully protected: the model returned the sub-span "India Limited" out of
    "National Stock Exchange of India Limited" and replaced it, leaving the
    name mangled while "BSE Limited" beside it survived.
    """

    def test_full_legal_names_survive(self):
        policy = RedactionPolicy()
        for name in ("BSE Limited", "National Stock Exchange of India Limited",
                     "National Stock Exchange", "NSE Limited"):
            assert not policy.should_redact("ORGANIZATION", name), name

    def test_a_tail_fragment_of_a_legal_name_survives(self):
        policy = RedactionPolicy()
        assert not policy.should_redact(
            "ORGANIZATION", "India Limited", "and National Stock Exchange of ")

    def test_the_exemption_depends_on_the_name_it_completes(self):
        """
        A distinctive tail is exempt only where it completes a public body.
        ("India Limited" is covered by the stricter boilerplate rule in
        TestGenericCorporateShell, so a distinctive fragment is used here.)
        """
        policy = RedactionPolicy()
        assert not policy.should_redact(
            "ORGANIZATION", "Exchange of India Limited", "the National Stock ")
        assert policy.should_redact(
            "ORGANIZATION", "Exchange of India Limited", "a subsidiary of Acme ")


class TestCompoundGlossaryTerms:
    """
    "Bid/Offer Closing Date" is one defined term, but the slash-splitting meant
    for genuine alternatives ("Rs. / Rupees / INR") tore it into "Bid" and
    "Offer Closing Date". The compound the document actually writes was then
    unprotected, and the sentence "The UPI mandate end time and date shall be
    at 5:00 p.m. on Bid/Offer Closing Day" acquired a company name.
    """

    @pytest.fixture
    def policy(self):
        rows = [
            ["Term", "Description"],
            ["Bid/Offer Closing Date", "Except in relation to any Bids received "
             "from the Anchor Investors, the date after which the Bidding closes"],
            ["Working Day", "All days on which commercial banks in Mumbai are open"],
            ["Bid", "An indication to make an offer during the Bid/Offer Period"],
            ["Offer", "The initial public offering of Equity Shares by the Company"],
        ]
        return RedactionPolicy().learn_from([], rows)

    def test_the_compound_term_is_protected(self, policy):
        assert policy.is_defined_term("Bid/Offer Closing Date")
        assert not policy.should_redact("ORGANIZATION", "Bid/Offer Closing Date")

    def test_alternatives_are_still_split(self, policy):
        """The split must keep working for genuine alternatives."""
        assert policy.is_defined_term("Bid")
        assert policy.is_defined_term("Offer")

    def test_a_near_variant_is_vocabulary_not_a_name(self, policy):
        """The document writes "Closing Day" twice where it means the Date."""
        assert not policy.should_redact("ORGANIZATION", "Bid/Offer Closing Day")


class TestGenericCorporateShell:
    """
    The model returns the tail of a name as well as the whole one. "India
    Limited", pulled out of "Solar Energy Corporation of India Limited",
    became a surrogate key — and because keys are rewritten everywhere they
    occur, the allow-listed "National Stock Exchange of India Limited" was
    rewritten too.
    """

    def test_a_span_of_only_boilerplate_is_not_a_name(self):
        policy = RedactionPolicy()
        for shell in ("India Limited", "Private Limited", "Bank Limited"):
            assert policy.is_generic_corporate_shell(shell), shell
            assert not policy.should_redact("ORGANIZATION", shell), shell

    def test_one_distinctive_word_is_enough_to_be_a_name(self):
        """
        "Solar Energy Corporation of India Limited" is deliberately not used
        here: it is a government corporation and is allow-listed by name, so it
        would fail for an unrelated reason.
        """
        policy = RedactionPolicy()
        for name in ("Elantas Beck India Limited",
                     "Lakeside Motors and Electricals Private Limited",
                     "Exemplar Wealth Management Limited"):
            assert not policy.is_generic_corporate_shell(name), name
            assert policy.should_redact("ORGANIZATION", name), name


class TestAbbreviationExpansions:
    """
    Protecting only the left column of a glossary row left the right column
    exposed, and the definitions themselves were rewritten across every
    abbreviation table in the document: "IST | Indian Standard Time" became
    "IST | Calder Textiles LLP", "MVA | Mega Volt-Amperes" became a person's
    name, "UL | Underwriters Laboratories" became a private company.
    """

    @pytest.fixture
    def policy(self):
        rows = [
            ["Term", "Description"],
            ["IST", "Indian Standard Time"],
            ["MVA", "Mega Volt-Amperes"],
            ["UL", "Underwriters Laboratories"],
            # A party to the offer: the description names an entity, so it must
            # keep being redacted - consistently, as one identity.
            ["Exemplar", "Exemplar Wealth Management Limited"],
            # An explanatory sentence, not an expansion: too long to admit into
            # the vocabulary index without dragging ordinary prose in with it.
            ["Board", "The board of directors of our Company, as disclosed in "
                      "the section Our Management on page 214 of this document"],
        ]
        return RedactionPolicy().learn_from([], rows)

    def test_expansions_are_vocabulary(self, policy):
        for expansion in ("Indian Standard Time", "Mega Volt-Amperes",
                          "Underwriters Laboratories"):
            assert not policy.should_redact("ORGANIZATION", expansion), expansion

    def test_a_party_named_in_the_description_is_still_redacted(self, policy):
        assert policy.should_redact("ORGANIZATION", "Exemplar Wealth Management Limited")

    def test_long_explanations_do_not_enter_the_vocabulary(self, policy):
        assert not policy._is_expansion(
            "The board of directors of our Company, as disclosed in "
            "the section Our Management on page 214 of this document")

    def test_it_applies_to_every_table_not_just_the_first(self, policy):
        """The rule keys off row shape, so a second table is covered too."""
        assert policy.is_defined_term("Mega Volt-Amperes")
        assert policy.is_defined_term("Indian Standard Time")


class TestGovernmentCorporations:
    """Their legal names end in "Limited", so nothing about the string says
    public body - SECI and CDSL have to be named explicitly."""

    def test_government_corporations_are_public_bodies(self):
        policy = RedactionPolicy()
        for name in ("Solar Energy Corporation of India Limited",
                     "Central Depository Services (India) Limited",
                     "Central Electricity Authority"):
            assert not policy.should_redact("ORGANIZATION", name), name


class TestReferenceDefinitions:
    """
    Abbreviation expansions are protected whole *and in part*, because the
    model takes phrases out of the middle of one: "Kisan Urja Suraksha" was
    pulled from PM-KUSUM's expansion and replaced with a person's name.

    The rule is keyed on row shape, so it covers every table in the document
    rather than the one that happened to be inspected.
    """

    @pytest.fixture
    def policy(self):
        rows = [
            ["Term", "Description"],
            ["PFCE", "Private Final Consumption Expenditure"],
            ["PM-KUSUM", "Pradhan Mantri Kisan Urja Suraksha evam Utthaan "
                         "Mahabhiyan Yojana"],
            ["UL", "Underwriters Laboratories"],
            # An acronym row that names a bank: must stay redacted.
            ["SBI", "State Bank of India"],
            # Party rows are not acronyms, so their definitions stay redactable.
            ["Promoters", "Arun Ganesh Prabhu, Latha Arun Prabhu and "
                          "Meena Girish Kamath"],
            ["Exemplar", "Exemplar Wealth Management Limited"],
        ]
        return RedactionPolicy().learn_from([], rows)

    def test_expansion_is_protected(self, policy):
        assert not policy.should_redact(
            "ORGANIZATION", "Private Final Consumption Expenditure")

    def test_a_fragment_of_an_expansion_is_protected(self, policy):
        """Exact matching alone cannot reach this; PM-KUSUM needed it."""
        assert policy.is_reference_vocabulary("Kisan Urja Suraksha")
        assert not policy.should_redact("PERSON", "Kisan Urja Suraksha")

    def test_bare_private_does_not_disqualify_an_expansion(self, policy):
        """"Private Final Consumption Expenditure" is economics, not a company."""
        assert policy._is_reference_definition(
            "PFCE", "Private Final Consumption Expenditure")

    def test_an_acronym_naming_a_bank_is_still_redacted(self, policy):
        assert not policy._is_reference_definition("SBI", "State Bank of India")
        assert policy.should_redact("ORGANIZATION", "State Bank of India")

    def test_party_definitions_are_never_protected(self, policy):
        """
        The glossary is also where the parties are defined. Exempting
        definition cells generally would leave every promoter in the output.
        """
        assert not policy._is_reference_definition(
            "Promoters", "Arun Ganesh Prabhu, Latha Arun Prabhu")
        assert policy.should_redact("PERSON", "Arun Ganesh Prabhu")
        assert policy.should_redact("ORGANIZATION", "Exemplar Wealth Management Limited")

    def test_expansions_stay_out_of_the_word_cache(self, policy):
        """
        Otherwise their words make unrelated prose look like vocabulary — the
        mechanism that let "State Bank of India" be read as glossary words.
        """
        assert "pradhan" not in policy._defined_term_words


class TestStandardsAndLanguages:
    def test_accounting_standards_are_cited_like_statutes(self):
        policy = RedactionPolicy()
        assert not policy.should_redact("ORGANIZATION", "Indian Accounting Standard")

    def test_a_language_names_nobody(self):
        """"Marathi being the regional language" became a company name."""
        policy = RedactionPolicy()
        for language in ("Marathi", "Hindi", "English", "Gujarati"):
            assert not policy.should_redact("ORGANIZATION", language), language
