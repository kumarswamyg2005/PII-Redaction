"""
Policy: deciding which detected entities are actually PII.

A detector answers "is this a person or an organisation". It cannot answer "is
this private", and that second question is where a redaction tool earns or
loses its precision. Three rules apply here, and each is written as a
*category* rather than a list of this document's answers — the difference
between configuration and memorising the test set.

**Public bodies are not PII.** A market regulator or exchange is public
knowledge regardless of which document names it. Redacting "SEBI" protects
nobody and makes the document unreadable.

**A document's own defined terms are not PII.** Offer documents open with a
"Definitions and Abbreviations" glossary; the terms it defines are the
document's vocabulary. Rather than hardcode them, the glossary is *parsed at
run time* and its terms become the allow-list. That generalises to any document
carrying a glossary, and is the single most effective precision measure here.

**Common nouns are not PII.** Zero-shot NER occasionally proposes bare words
like "bank" or "private limited". A candidate that is entirely lowercase in the
source, or is a single very common word, is rejected.

Nothing in this module names a person, a company, or an address.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

__all__ = ["RedactionPolicy", "PUBLIC_BODY_TERMS"]


#: Regulators, exchanges, depositories and statutes: public by nature, in any
#: document. These are institutions, not counterparties — a category, not a
#: lookup table of the entities that happen to appear in this file.
PUBLIC_BODY_TERMS: Set[str] = {
    # Regulators and statutory bodies
    "sebi", "securities and exchange board of india", "rbi", "reserve bank of india",
    "irdai", "irda", "pfrda", "mca", "ministry of corporate affairs",
    "roc", "registrar of companies", "cci", "competition commission of india",
    "dgft", "director general of foreign trade",
    "directorate general of foreign trade",
    "npci", "national payments corporation of india", "income tax department",
    "uidai", "unique identification authority of india", "government of india",
    # Exchanges and depositories
    "bse", "bse limited", "nse", "nse limited",
    "national stock exchange", "national stock exchange of india",
    "national stock exchange of india limited",
    "nsdl", "cdsl", "national securities depository limited",
    "central depository services", "central depository services (india) limited",
    # Government corporations. Their legal names end in "Limited", so nothing
    # about the string itself says public body — they have to be named.
    "seci", "solar energy corporation of india",
    "solar energy corporation of india limited",
    "cea", "central electricity authority",
    "cerc", "central electricity regulatory commission",
    # Frameworks and statutes referenced by name
    "icdr", "sebi icdr regulations", "companies act", "sebi listing regulations",
    "fema", "sebi act", "depositories act", "income tax act",
    # Accounting standards are cited the way statutes are, and read as company
    # names because of the word "Indian": "Ind AS 24 | Indian Accounting
    # Standard 24" became "Acme Precision Limited 24".
    "ind as", "indian accounting standard", "indian accounting standards",
    "companies (indian accounting standards) rules",
}

#: Languages. A language names nobody, but "Marathi" and "Hindi" sit beside
#: newspaper titles in the publication clause and were read as organisations —
#: "Marathi being the regional language of Maharashtra" became a company. This
#: is a closed, universal set rather than anything learned from one document.
LANGUAGE_NAMES: Set[str] = {
    "english", "hindi", "marathi", "gujarati", "bengali", "tamil", "telugu",
    "kannada", "malayalam", "punjabi", "odia", "oriya", "assamese", "urdu",
    "sanskrit", "konkani", "manipuri", "nepali", "sindhi", "kashmiri",
    "maithili", "santhali", "bodo", "dogri",
}

#: Words introducing a *transaction* reference — a number identifying an event
#: or a piece of paperwork rather than a party to it.
#:
#: The brief singles this out ("Order" or "Ticket" numbers) and asks for an
#: explicit choice. Ours draws the line at **what the number resolves to**:
#:
#: * Resolves to a *person* (Aadhaar, PAN, passport, voter ID, DIN) -> redacted.
#: * Resolves to a *party we already redact* (CIN, SEBI intermediary
#:   registration, auditor firm registration) -> redacted, because leaving it
#:   would re-identify the company whose name we just replaced. A CIN is
#:   publicly searchable; leaving it beside a fake company name is not
#:   redaction, it is a lookup key.
#: * Resolves to a *transaction or filing event* (order, ticket, invoice, folio,
#:   receipt, challan, acknowledgement, application) -> kept. Removing these
#:   protects nobody and destroys the document's auditability.
#:
#: Recognition needs the surrounding words, because a bare six-digit number is
#: shapeless — it could be a folio number or a postal code. Context is what
#: makes the call, not the digits.
_TRANSACTION_REFERENCE_CONTEXT = re.compile(
    r"\b(?:order|ticket|invoice|folio|receipt|challan|acknowledgement|"
    r"application|transaction|reference|docket|case|batch|serial)\s*"
    r"(?:no\.?|number|id|#)?\s*[:\-]?\s*$",
    re.IGNORECASE,
)

#: A bare number or short alphanumeric code, i.e. something that carries no
#: identity of its own and is only meaningful next to its label.
_BARE_CODE = re.compile(r"^[A-Z]{0,3}[\d][\d/\-]{2,}[A-Z]?$", re.IGNORECASE)

#: Vocabulary of field captions in a contact block or data table. A span made
#: only of these words is a label, not a value.
_FIELD_LABEL_WORDS: Set[str] = {
    "telephone", "tel", "phone", "mobile", "fax", "email", "e", "mail",
    "website", "web", "url", "address", "name", "contact", "person",
    "designation", "title", "office", "registered", "corporate", "branch",
    "number", "no", "id", "code", "date", "details", "particulars",
    "investor", "grievance", "compliance", "officer", "secretary",
    "weighted", "average", "cost", "total", "amount", "value", "price",
    "sr", "s", "description", "term", "type", "category", "status",
    # Heading vocabulary. The truecasing pass rewrites ALL-CAPS runs so the
    # models can read cover-page names, which also makes section headings
    # ("OFFER SIZE", "RISKS IN RELATION", "BID/OFFER PERIOD") look like proper
    # nouns. These are structural words in any document, not this one's answers.
    "offer", "size", "risks", "relation", "period", "closes", "opens",
    "bid", "issue", "general", "information", "table", "contents", "section",
    "part", "annexure", "schedule", "note", "notes", "summary", "overview",
    "our", "the", "and", "for", "of", "to", "in", "on", "at", "by",
    # The *names* of identifier schemes. "Aadhaar" and "PAN" caption the
    # number beside them; the number is the secret, the caption is not. Left
    # alone they were detected as organisations on every KYC line.
    "aadhaar", "aadhar", "pan", "gstin", "gst", "ifsc", "passport", "voter",
    "din", "cin", "isin", "uid", "epic", "tan",
}

#: Types whose values are inherently numeric. A span of one of these types with
#: no digit in it was mislabelled.
_DIGIT_BEARING_TYPES: Set[str] = {
    "PHONE_NUMBER", "IN_MOBILE", "IN_AADHAAR", "IN_PAN", "IN_GSTIN",
    "IN_PASSPORT", "IN_VOTER_ID", "CREDIT_CARD", "IP_ADDRESS", "IBAN_CODE",
    "DATE_OF_BIRTH", "BANK_ACCOUNT", "CORPORATE_ID",
}

#: Grammatical filler, ignored when judging what a phrase is made of.
_STRUCTURAL_WORDS: Set[str] = {
    "the", "and", "for", "with", "from", "that", "this", "our", "its", "their",
    "all", "any", "such", "been", "have", "has", "are", "was", "were", "will",
    "shall", "may", "not", "under", "over", "into", "onto", "upon", "per",
}

#: Very common words that a zero-shot model sometimes proposes as entities.
_COMMON_WORDS: Set[str] = {
    "bank", "company", "board", "trust", "limited", "private limited", "group",
    "corporation", "authority", "government", "department", "office", "branch",
    "india", "indian", "state", "city", "town", "village", "district",
    "the company", "our company", "the board", "the bank", "issuer",
    "promoter", "promoters", "director", "directors", "auditor", "auditors",
    "shareholder", "shareholders", "investor", "investors", "member", "members",
}

_GLOSSARY_HEADING = re.compile(
    r"^\s*(?:section\s+[ivx]+\s*[:\-]?\s*)?"
    r"(?:definitions?(?:\s+and\s+abbreviations?)?|abbreviations?|glossary)\s*$",
    re.IGNORECASE,
)
_DEFINITION_LINE = re.compile(
    r"^\s*(?P<term>[^\n|]{2,80}?)\s*(?:\||\s{2,}|\s+means\s+|\s+shall\s+mean\s+|:)\s*\S",
    re.IGNORECASE,
)


#: Sentence grammar: a determiner opening the span, or a prepositional phrase
#: inside it. Names do not read this way — "State Bank of India" has "of" but
#: never "of the", and no company is called "The ... of the ...".
_PROSE_GRAMMAR = re.compile(
    r"^(?:the|this|these|those|our|its|their|all|any|such|details|summary)\b"
    r"|\b(?:of|for|in|to|on|with|from|under|and)\s+the\b",
    re.IGNORECASE,
)


def _split_camel_case(text: str) -> str:
    """``BookRunningLeadManagers`` -> ``Book Running Lead Managers``."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


@dataclass
class RedactionPolicy:
    """Decides whether a detected span should actually be redacted."""

    #: Terms the document defines for itself; populated by :meth:`learn_from`.
    defined_terms: Set[str] = field(default_factory=set)
    #: Short name -> full name, as the document's own glossary declares them
    #: ("Exemplar" -> "Exemplar Wealth Management Limited").
    entity_aliases: Dict[str, str] = field(default_factory=dict)
    #: Word sequences the document itself writes with a lower-case first
    #: letter; populated by :meth:`learn_from`. See :meth:`occurs_uncapitalised`.
    uncapitalised_phrases: Set[str] = field(default_factory=set, repr=False)
    #: Upper-cased section headings and contents entries, supplied by the
    #: pipeline, which is where the document's line structure is known.
    structural_headings: Set[str] = field(default_factory=set, repr=False)
    #: Expansions of the document's technical acronyms, matched whole or in
    #: part. Held apart from :attr:`defined_terms` so they never reach the word
    #: cache — see :meth:`_is_reference_definition`.
    reference_definitions: Set[str] = field(default_factory=set, repr=False)
    #: Lazily-built index of the individual words those terms are made of.
    _word_cache: Optional[Set[str]] = field(default=None, repr=False)
    #: Defined terms ordered longest-first, for phrase stripping.
    _terms_by_length: Optional[List[str]] = field(default=None, repr=False)
    #: Entity types that are always PII, whatever the surrounding words say.
    always_redact: Set[str] = field(
        default_factory=lambda: {
            "IN_AADHAAR", "IN_PAN", "IN_GSTIN", "IN_PASSPORT", "IN_VOTER_ID",
            "CREDIT_CARD", "EMAIL_ADDRESS", "PHONE_NUMBER", "IN_MOBILE",
            "DATE_OF_BIRTH", "IP_ADDRESS", "IBAN_CODE",
        }
    )

    # -- learning ----------------------------------------------------------

    def learn_from(
        self,
        paragraphs: Sequence[str],
        table_rows: Optional[Iterable[Sequence[str]]] = None,
    ) -> "RedactionPolicy":
        """
        Harvest the document's own defined terms.

        Offer documents put their glossary in a two-column ``Term |
        Description`` table, so the terms are read from the table structure.
        Read as a flat paragraph stream they arrive as alternating unrelated
        lines and nothing matches — the reason an earlier version of this
        learned exactly one term.

        A prose fallback handles documents that write definitions inline
        ("X means Y"). Documents with no glossary contribute nothing and the
        rest of the policy still applies.
        """
        rows = list(table_rows) if table_rows is not None else []
        if rows:
            self._learn_from_tables(rows)
        self._learn_from_prose(paragraphs)
        self._learn_uncapitalised(
            list(paragraphs) + [" ".join(cells) for cells in rows]
        )
        return self

    #: Longest phrase recorded by :meth:`_learn_uncapitalised`. Entity spans
    #: longer than this are names or addresses, not common vocabulary.
    _MAX_UNCAPITALISED_WORDS = 6

    #: A word for the purposes of the vocabulary index. Keeps intra-word
    #: punctuation ("post-offer", "e-mail") so hyphenated nouns are recorded
    #: as one token rather than two.
    _WORD = re.compile(r"[A-Za-z][A-Za-z'&.\-]*")

    def _learn_uncapitalised(self, texts: Sequence[str]) -> None:
        """
        Record every word sequence the document writes in lower case.

        This is the document teaching us its own common vocabulary. A proper
        noun is capitalised wherever it appears, so a span that the document
        elsewhere writes with a lower-case first letter is a common noun, not a
        name — "Delay in or inability of the vendors" opens a risk-factor
        bullet, and the same document writes "any delay in placing the orders"
        mid-sentence.

        Only sequences *starting* with a lower-case word are recorded, which is
        what excludes sentence-initial capitals from teaching us anything. The
        result replaces a hand-written stop-list: it is derived per document, so
        it transfers to a prospectus whose vocabulary we have never seen.
        """
        limit = self._MAX_UNCAPITALISED_WORDS
        for line in texts:
            words = self._WORD.findall(line)
            for index, word in enumerate(words):
                if not word[0].islower():
                    continue
                phrase = word.lower()
                self.uncapitalised_phrases.add(phrase)
                for following in words[index + 1:index + limit]:
                    phrase = f"{phrase} {following.lower()}"
                    self.uncapitalised_phrases.add(phrase)

    def occurs_uncapitalised(self, text: str) -> bool:
        """True when the document elsewhere writes this span in lower case."""
        words = self._WORD.findall(text)
        if not words or len(words) > self._MAX_UNCAPITALISED_WORDS:
            return False
        return " ".join(word.lower() for word in words) in self.uncapitalised_phrases

    def _learn_from_tables(self, table_rows: Iterable[Sequence[str]]) -> None:
        in_glossary = False
        for cells in table_rows:
            if len(cells) < 2:
                continue
            first, second = cells[0].strip(), cells[1].strip()

            # A "Term | Description" header marks the glossary and everything
            # under it, until another table announces different columns.
            if first.lower() == "term" and second.lower().startswith("description"):
                in_glossary = True
                continue
            if first.lower() in ("particulars", "sr. no.", "s. no.", "name"):
                in_glossary = False
            if not in_glossary:
                continue

            term = first.strip(" \t\"'“”‘’.,;:")
            if self._defines_an_entity(second):
                # The glossary also introduces short names for the parties:
                # "Exemplar | Exemplar Wealth Management Limited". Allow-listing
                # those would protect a company's own abbreviation from
                # redaction, which is precisely backwards. Instead the document
                # is telling us the two names denote one entity, so record it
                # as an alias — the short form then inherits the full form's
                # surrogate and both read consistently.
                if 1 < len(term) <= 80 and self._is_abbreviation_of(term, second):
                    self.entity_aliases[term] = " ".join(second.split())
                continue
            # A definition has a short label and a substantive description; a
            # long left cell means this is a data table, not the glossary.
            #
            # The description may also be *short*, because an offer document
            # ends its glossary with an abbreviations table — "IPO | Initial
            # public offer", "RBI | Reserve Bank of India", and "Underwriters |
            # [●]", whose expansion is still to be filled in at pricing.
            # Requiring 25 characters there silently discarded 66 of this
            # document's own terms, and "Underwriters" was then redacted as a
            # company. A short expansion is accepted only for a correspondingly
            # short label, which is what an abbreviation is; that keeps data
            # rows such as "<company name> | <contact person>" out, because
            # their left cell is long.
            if 1 < len(term) <= 80 and (len(second) >= 25 or len(term) <= 40):
                # Keep the whole label as well as its parts. The split below
                # exists for genuine alternatives ("Rs. / Rupees / INR"), but a
                # slash is also used to join one compound term — the glossary
                # defines "Bid/Offer Closing Date", and splitting it left only
                # "Bid" and "Offer Closing Date", so the compound the document
                # actually writes was unprotected and got a company surrogate.
                self.defined_terms.add(term.lower())
                for alternative in re.split(r"\s*/\s*|\s+or\s+", term):
                    alternative = alternative.strip(" \t\"'“”‘’.,;:")
                    if 1 < len(alternative) <= 80:
                        self.defined_terms.add(alternative.lower())
                        # Some glossary cells run the words together —
                        # "BookRunningLeadManagers" arrives as one token, and
                        # lowercasing it destroys the word boundaries the
                        # vocabulary rules depend on. Store the split form too.
                        spaced = _split_camel_case(alternative)
                        if spaced != alternative:
                            self.defined_terms.add(spaced.lower())
                # An abbreviation's expansion is vocabulary in its own right.
                # Protecting only the left cell left the right one exposed, and
                # the definitions themselves were rewritten: "IST | Indian
                # Standard Time" became "IST | Calder Textiles LLP", "MVA |
                # Mega Volt-Amperes" became a person's name. A row whose
                # description *is* a party's name is excluded by
                # _defines_an_entity above, so "Exemplar | Exemplar Wealth
                # Management Limited" is still redacted, consistently, as one
                # entity.
                if self._is_expansion(second):
                    self.defined_terms.add(second.lower())
                # A technical abbreviation's expansion is protected whole *and*
                # in part, because the model pulls phrases out of the middle of
                # one: "Kisan Urja Suraksha" was taken from PM-KUSUM's expansion
                # and replaced with a person's name. Exact matching alone cannot
                # reach that, so these are indexed separately for fragment
                # lookup — and kept out of the word cache, which decides whether
                # unrelated prose looks like vocabulary.
                if self._is_reference_definition(term, second):
                    self.reference_definitions.add(" ".join(second.lower().split()))

    #: An acronym or short code in the term column: PFCE, PM-KUSUM, IST, UL,
    #: A.Y., W&C. Party roles — "Promoters", "Group Entities", "Exemplar" — are
    #: not written this way, which is what keeps their definitions redactable.
    _ACRONYM_TERM = re.compile(r"^[A-Z][A-Z0-9&./\-]{1,11}$")

    def _is_reference_definition(self, term: str, description: str) -> bool:
        """
        True when a row is technical reference vocabulary rather than a party.

        Both halves are load-bearing:

        *The term must be an acronym.* In an offer document the glossary is
        also where the parties are defined — "Promoters | Kushal Subbayya
        Hegde, Latha Arun Prabhu, …", "Group Entities | …". Exempting
        definition cells generally would leave every promoter's name in the
        output, so only acronym rows qualify.

        *The definition must name no institution.* "SBI | State Bank of India"
        is an acronym row, and the bank must still be redacted;
        ``verify_redaction.py`` carries SBI as a probe and caught exactly this
        over-protection once already.
        """
        cleaned = " ".join(description.split())
        if not cleaned or not self._ACRONYM_TERM.match(term.strip()):
            return False
        return not self._INSTITUTIONAL_WORD.search(cleaned)

    def is_reference_vocabulary(self, text: str) -> bool:
        """True when a span is, or sits inside, an acronym's expansion."""
        candidate = " ".join(text.lower().split()).strip(" .,;:\"'“”‘’()")
        if len(candidate) < 3 or not self.reference_definitions:
            return False
        return any(
            candidate == definition or candidate in definition
            for definition in self.reference_definitions
        )

    def _is_expansion(self, description: str) -> bool:
        """
        True when a glossary description is an abbreviation spelled out.

        "Indian Standard Time", "Mega Volt-Amperes", "International Monetary
        Fund" — a short noun phrase, not a sentence of explanation and not a
        party's name. Long explanatory descriptions are excluded on purpose:
        their words feed the vocabulary index, and admitting whole sentences
        there would let ordinary prose start looking like defined vocabulary.
        """
        cleaned = " ".join(description.split())
        if not cleaned or not cleaned[:1].isupper():
            return False
        if self._defines_an_entity(cleaned):
            return False
        # _defines_an_entity only recognises a corporate word as a *suffix*, so
        # "SBI | State Bank of India" slipped through and the bank's name was
        # protected. Any institutional word anywhere in the expansion disqualifies
        # it; genuinely public institutions are named in PUBLIC_BODY_TERMS and
        # are exempted there instead, which keeps the two judgements separate.
        if self._INSTITUTIONAL_WORD.search(cleaned):
            return False
        return len(cleaned.split()) <= 8 and len(cleaned) <= 70

    #: Words that make an expansion a named organisation rather than vocabulary.
    #: "Fund" and "Agency" are deliberately absent — the International Monetary
    #: Fund and the International Energy Agency are vocabulary in this document,
    #: not parties to the offer.
    #: "Securities" and "capital" are absent here too: "Securities transaction
    #: tax" and "Return on Capital Employed" are glossary vocabulary, and a
    #: company whose name contains either still carries "Limited" or is caught
    #: as an entity definition.
    #: "Private" alone is not one of them: "PFCE | Private Final Consumption
    #: Expenditure" is an economics term, and disqualifying it on that word
    #: turned the expansion into a company name. Only "Private Limited" counts.
    _INSTITUTIONAL_WORD = re.compile(
        r"\b(?:bank|limited|ltd|llp|corporation|incorporated|inc"
        r"|company|holdings|trust|& co)\b|\bp(?:riva)?t[ve]?\.?\s+l(?:imi)?t[ed]*\b",
        re.IGNORECASE)

    #: The narrower set for :meth:`is_vocabulary_phrase`. "Securities" and
    #: "capital" are left out here on purpose — "Securities transaction tax"
    #: and "Return on Capital Employed" are this document's own vocabulary.
    _ORG_NAME_WORD = re.compile(
        r"\b(?:bank|limited|ltd|llp|corporation|incorporated|inc|& co)\b",
        re.IGNORECASE)

    #: A definition that is just a company or person name, not an explanation.
    _ENTITY_DEFINITION = re.compile(
        r"^[A-Z][\w&.,'’\- ]{2,70}\b"
        r"(?:Limited|Ltd\.?|LLP|Private Limited|Pvt\.? Ltd\.?|Inc\.?|Corporation|"
        r"Bank|Securities|Trust|Associates|& Co\.?)\s*$"
    )

    #: Words too generic to establish that two names denote the same entity.
    _GENERIC_NAME_WORDS = frozenset({
        "limited", "ltd", "private", "pvt", "llp", "inc", "company", "corporation",
        "corp", "group", "holdings", "india", "indian", "bank", "trust", "the",
        "and", "of", "services", "solutions", "enterprises", "industries",
        "international", "national", "capital", "securities", "finance",
        "financial", "promoter", "promoters", "corporate", "our", "wealth",
        "management",
    })

    def _is_abbreviation_of(self, term: str, full_name: str) -> bool:
        """
        True when ``term`` is plausibly a short name for ``full_name``.

        A glossary row like "Corporate Promoter | Waterloo Industrial Park VI
        Private Limited" defines a *role*, not an abbreviation. Treating it as
        an alias would rewrite every occurrence of "Corporate Promoter" as a
        company name. A genuine short form shares a distinctive word with the
        full name — "Exemplar" with "Exemplar Wealth Management Limited" — so a
        shared generic word like "Limited" or "India" proves nothing.
        """
        def distinctive(value: str) -> Set[str]:
            return {
                word
                for word in re.findall(r"[a-z]+", value.lower())
                if len(word) >= 4 and word not in self._GENERIC_NAME_WORDS
            }

        term_words = distinctive(term)
        if not term_words:
            # An all-caps initialism such as "SECI" carries no shared word;
            # accept it only if its letters open the words of the full name.
            letters = re.sub(r"[^A-Za-z]", "", term)
            if 2 <= len(letters) <= 6 and term.upper() == term:
                initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", full_name))
                return letters.upper() in initials.upper()
            return False
        return bool(term_words & distinctive(full_name))

    def _defines_an_entity(self, description: str) -> bool:
        """True when a glossary entry's definition is simply a named party."""
        cleaned = " ".join(description.split())
        if len(cleaned.split()) > 10:
            return False  # a real explanation, not a name
        return bool(self._ENTITY_DEFINITION.match(cleaned))

    def _learn_from_prose(self, paragraphs: Sequence[str]) -> None:
        inside = False
        for raw in paragraphs:
            line = raw.strip()
            if not line:
                continue
            if _GLOSSARY_HEADING.match(line):
                inside = True
                continue
            if inside:
                if line.isupper() and len(line) < 80 and not _DEFINITION_LINE.match(line):
                    inside = False
                    continue
                match = _DEFINITION_LINE.match(line)
                if match:
                    term = match.group("term").strip(" \t\"'“”‘’.,;")
                    if 1 < len(term) <= 80:
                        self.defined_terms.add(term.lower())

    # -- decisions ---------------------------------------------------------

    #: Determiners a detector routinely includes in a span. "the Equity Shares"
    #: is the same defined term as "Equity Shares", and failing to see that was
    #: responsible for a large share of this tool's over-redaction.
    _LEADING_ARTICLES = re.compile(r"^(?:the|our|its|their|a|an)\s+", re.IGNORECASE)

    @classmethod
    def _normalise(cls, text: str) -> str:
        collapsed = " ".join(text.split()).lower().strip(".,;:'\"()“”‘’")
        return cls._LEADING_ARTICLES.sub("", collapsed).strip()

    def is_generic_corporate_shell(self, text: str) -> bool:
        """
        True for a span built only from the filler words of a company name.

        The model sometimes returns the tail of a name rather than the whole
        thing — "India Limited" out of "Solar Energy Corporation of India
        Limited". Redacting that is not merely untidy: the surrogate table is
        keyed on the span, so a key that generic is then rewritten *everywhere*
        it occurs, which is how "National Stock Exchange of India Limited"
        became "National Stock Exchange of Quantum Materials Industries
        Limited" despite the exchange being allow-listed.

        A real name contributes at least one word that is not shared boilerplate;
        reusing :attr:`_GENERIC_NAME_WORDS` keeps that judgement in one place.
        """
        words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3]
        return bool(words) and all(word in self._GENERIC_NAME_WORDS for word in words)

    def completes_public_body(self, text: str, preceding: str) -> bool:
        """
        True when a span is the tail end of a public body's full legal name.

        The model does not always take the whole name. In "…and National Stock
        Exchange of India Limited" it returned just "India Limited", which on
        its own resolves to nothing and was replaced — leaving the exchange's
        legal name mangled while "BSE Limited" beside it survived intact.

        Only a match that *ends* at the span counts, so a public body merely
        mentioned earlier in the sentence exempts nothing.
        """
        candidate = self._normalise(text)
        if not candidate:
            return False
        tail = self._normalise(f"{preceding[-80:]}{text}")
        return any(
            tail.endswith(term) and term.endswith(candidate) and term != candidate
            for term in PUBLIC_BODY_TERMS
        )

    def is_public_body(self, text: str) -> bool:
        normalised = self._normalise(text)
        if normalised in PUBLIC_BODY_TERMS:
            return True
        # "the SEBI ICDR Regulations" or "BSE Limited and NSE" resolve to a
        # public body even though the exact string is longer.
        return any(
            re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalised)
            for term in PUBLIC_BODY_TERMS
            if len(term) > 4
        )

    def is_defined_term(self, text: str) -> bool:
        """
        True when the document defines this term itself.

        Glossaries label the plural or parenthetical form — ``QIB(s)``,
        ``BRLMs``, ``SCSB(s)`` — while the body uses the singular. Comparing
        exact strings therefore misses the majority of real uses, so a few
        morphological variants are tried.
        """
        normalised = self._normalise(text)
        stem = normalised[:-1] if normalised.endswith("s") else normalised
        candidates = {
            normalised,
            normalised.rstrip("()s"),
            f"{normalised}s",
            f"{normalised}(s)",
            normalised.replace("(s)", ""),
            normalised.replace("(s)", "s"),
            # The combination the glossary actually uses: it defines "SCSB(s)"
            # and "QualifiedInstitutionalBuyer(s)" while the body writes
            # "SCSBs" and "Qualified Institutional Buyers". Without the
            # singular stem plus "(s)", none of those matched and every use
            # was redacted as an organisation.
            stem,
            f"{stem}(s)",
        }
        if any(candidate in self.defined_terms for candidate in candidates if candidate):
            return True
        return self.is_fragment_of_defined_term(normalised)

    def is_fragment_of_defined_term(self, normalised: str) -> bool:
        """
        True when a span is part of one of the document's defined terms.

        Zero-shot NER sometimes grabs a fragment rather than the whole phrase —
        here it proposed "Red Herring" as an organisation. Replacing that
        fragment rewrites "Red Herring Prospectus" throughout the document,
        destroying its title. A fragment of the document's own vocabulary is
        vocabulary, not PII.
        """
        if len(normalised) < 4 or " " not in normalised:
            # Single words are too collision-prone to resolve this way; a
            # one-word fragment is handled by the common-word rule instead.
            return normalised in self._defined_term_words
        return any(
            f" {normalised} " in f" {term} "
            for term in self.defined_terms
            if len(term) > len(normalised)
        )

    @property
    def _defined_term_words(self) -> Set[str]:
        if self._word_cache is None:
            self._word_cache = {
                word
                for term in self.defined_terms
                for word in re.split(r"[/\s]+", term)
                # Three letters, not four: the glossary defines "Working Day"
                # and "Bid", and excluding those left "Bid/Offer Closing Day"
                # looking like a name.
                if len(word) > 2
            }
        return self._word_cache

    def _matches_type_shape(self, entity_type: str, text: str) -> bool:
        """
        True when a span is at least shaped like the type it was given.

        Cheap, and it catches a whole family of mislabels that are otherwise
        invisible: a caption typed as the value beside it, or a sentence typed
        as an account number. Only structured types are checked — a person or
        an organisation has no fixed shape to test against.
        """
        if entity_type in _DIGIT_BEARING_TYPES:
            return any(character.isdigit() for character in text)
        if entity_type == "EMAIL_ADDRESS":
            return "@" in text
        if entity_type in ("URL", "WEB_ADDRESS"):
            # A dot and only a word or two. Rejecting anything containing a
            # space was too strict and caused a leak: the source splits
            # "www.issuerexample. com" across a line break, and refusing it
            # left the real domain in the output. Word count is what separates
            # a broken URL from a sentence.
            return "." in text and len(text.split()) <= 4
        if entity_type == "IN_IFSC":
            return len(text.split()) == 1 and any(c.isdigit() for c in text)
        return True

    def is_field_label(self, text: str) -> bool:
        """
        True for the caption of a field rather than its value.

        Contact blocks are label/value pairs, and a model reading them as prose
        sometimes returns the caption. That is how "Telephone" acquired a phone
        surrogate and appeared in the output as "00000000:" — the single most
        visible defect in the document.

        Labels are recognised structurally: a short, lower-cased-when-normalised
        phrase drawn entirely from field-caption vocabulary.
        """
        normalised = self._normalise(text)
        if len(normalised.split()) > 4:
            return False
        words = [w for w in re.findall(r"[a-z]+", normalised) if w]
        if not words:
            return False
        return all(word in _FIELD_LABEL_WORDS for word in words)

    def is_vocabulary_phrase(self, text: str) -> bool:
        """
        True for a phrase assembled from the document's own defined vocabulary.

        The glossary allow-list only matches terms exactly, so the document's
        vocabulary reappears in slightly different arrangements and gets
        redacted: "allotted equity shares", "asba account number",
        "permanent account number". Every content word in those comes from the
        glossary; none of them names anybody.

        A real entity contributes at least one word the glossary never defines —
        "Exemplar" in "Exemplar Wealth Management Limited" — which is exactly the
        signal used here.
        """
        if not self.defined_terms:
            return False
        normalised = self._normalise(text)
        if len(normalised.split()) < 2:
            return False  # single words are handled by the label/common rules
        # "State Bank of India" is built entirely from words this glossary
        # defines — state, bank, india — and was being read as vocabulary. A
        # word that forms an organisation's name settles it the other way; the
        # genuinely public institutions are exempted by name in
        # PUBLIC_BODY_TERMS, which is checked before this.
        if self._ORG_NAME_WORD.search(normalised):
            return False

        # Remove every defined term the phrase contains, longest first, then
        # see what is left. "The face value of the Equity Shares" is two
        # glossary terms joined by filler and leaves nothing behind; "Exemplar
        # Wealth Management Limited" leaves "nuvama", which is what names it.
        # A slash joins two words rather than belonging to either, so it has to
        # separate them here or "bid/offer" matches no glossary term at all.
        remainder = f" {normalised.replace('/', ' ')} "
        for term in self._defined_terms_by_length:
            if f" {term} " in remainder:
                remainder = remainder.replace(f" {term} ", " ")
        leftover = [
            word for word in re.findall(r"[a-z]+", remainder)
            if len(word) > 2
            and word not in _STRUCTURAL_WORDS
            and word not in self._defined_term_words
        ]
        return not leftover

    @property
    def _defined_terms_by_length(self) -> List[str]:
        if self._terms_by_length is None:
            self._terms_by_length = sorted(self.defined_terms, key=len, reverse=True)
        return self._terms_by_length

    #: Share of lower-case words above which a span reads as prose. Proper
    #: names carry the odd lower-case particle ("Vantara Motors and Electricals"
    #: is one in six), but not much more than that.
    _PROSE_LOWERCASE_SHARE = 0.34

    def is_prose_phrase(self, entity_type: str, text: str) -> bool:
        """
        True for a run of ordinary sentence text mislabelled as a name.

        Catches what no allow-list can: "The face value of the Equity Shares"
        is not in the glossary, is not a common word, and contains a capitalised
        defined term — yet it is plainly a sentence fragment, and replacing it
        with a company name mangles the sentence.

        Casing is the signal. A name is capitalised throughout; prose is not.
        This deliberately ignores ALL-CAPS spans, which are headings or
        cover-page names rather than prose, and are handled by the truecasing
        pass instead.
        """
        if entity_type not in ("PERSON", "ORGANIZATION", "LOCATION"):
            return False
        words = [word for word in re.findall(r"[A-Za-z][A-Za-z'&.\-]*", text) if word]
        if len(words) < 3:
            return False

        # ALL-CAPS spans cannot be judged on casing — the cover page prints
        # real promoter names in capitals. They are judged on grammar instead:
        # a heading reads like a sentence fragment ("THE FACE VALUE OF THE
        # EQUITY SHARES"), and a name does not. This matters more than it
        # looks, because mapping keys are case-insensitive: a heading that
        # acquires a surrogate rewrites the ordinary sentence elsewhere in the
        # document that happens to use the same words.
        if text.isupper():
            return bool(_PROSE_GRAMMAR.search(text))

        lowercase = sum(1 for word in words if word[0].islower())
        return lowercase / len(words) > self._PROSE_LOWERCASE_SHARE

    def is_bare_place(self, entity_type: str, text: str) -> bool:
        """
        True for a place name that is not a mailing address.

        The brief asks for "physical/mailing addresses". A city or state on its
        own is neither: "our factories in Maharashtra" identifies no one, and
        replacing it with a fake town makes the document read as nonsense while
        protecting nobody. An address is recognised by carrying a building or
        postal number — that is what narrows a place to a doorstep.
        """
        if entity_type not in ("LOCATION", "ADDRESS"):
            return False
        candidate = text.strip()
        if any(character.isdigit() for character in candidate):
            return False  # has a house number or PIN code: a real address
        return len(candidate.split()) <= 3

    def is_transaction_reference(self, text: str, preceding: str = "") -> bool:
        """
        True for a number identifying a transaction rather than a party.

        ``preceding`` is the text immediately before the span, which is what
        actually decides it: "Order No. 481920" is paperwork, while the same
        digits under "Folio" or standing alone are not necessarily. Person and
        company identifiers never reach here — they are handled by
        :attr:`always_redact` and by the entity rules above.
        """
        if not _BARE_CODE.match(text.strip()):
            return False
        return bool(_TRANSACTION_REFERENCE_CONTEXT.search(preceding))

    def is_common_word(self, text: str) -> bool:
        return self._normalise(text) in _COMMON_WORDS

    def is_structural_heading(self, text: str) -> bool:
        """
        True when a span is a section heading, or a run of words inside one.

        A whole heading is matched outright. A *part* of one is matched only
        from three words up, because a contents entry is long and the model
        tends to pull a phrase out of the middle of it: the entry "CERTAIN
        CONVENTIONS, USE OF FINANCIAL INFORMATION AND MARKET DATA AND CURRENCY
        OF PRESENTATION" yielded "Market Data And Currency Of Presentation" as
        a company. Requiring three words keeps a short name that happens to
        appear inside a heading from being protected by accident.
        """
        candidate = text.strip().upper()
        if not candidate or not self.structural_headings:
            return False
        if candidate in self.structural_headings:
            return True
        if len(candidate.split()) < 3:
            return False
        return any(candidate in heading for heading in self.structural_headings)

    #: Host of a web address, ignoring scheme, "www." and any path.
    _HOSTNAME = re.compile(r"(?:https?://)?(?:www\.)?([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
    #: Domains only a government or statutory body can hold.
    _PUBLIC_SUFFIXES = (".gov.in", ".nic.in", ".gov", ".gov.uk")
    #: Suffixes an Indian institution appends to its own acronym.
    _HOST_QUALIFIERS = ("", "india", "indialimited", "indialtd", "ltd", "limited")

    def is_public_host(self, text: str) -> bool:
        """
        True for a regulator's or exchange's own website.

        Every offer document points readers at sebi.gov.in, bseindia.com and
        nseindia.com. Those are public infrastructure, not a party's contact
        details, and replacing them with a surrogate misdirects the reader.
        ``PUBLIC_BODY_TERMS`` already names the institutions; this resolves a
        hostname onto that list rather than repeating it as a second list of
        domains that would then drift out of step.
        """
        match = self._HOSTNAME.search(re.sub(r"\s+", "", text))
        if not match:
            return False
        host = match.group(1).rstrip("./").lower()
        if host.endswith(self._PUBLIC_SUFFIXES):
            return True
        label = host.split(".")[0]
        return any(
            f"{term}{qualifier}" == label
            for term in PUBLIC_BODY_TERMS
            for qualifier in self._HOST_QUALIFIERS
        )

    def should_redact(self, entity_type: str, text: str, preceding: str = "") -> bool:
        """
        True when a detected span is genuinely private information.

        ``preceding`` is the text just before the span. It is optional so the
        rules stay unit-testable, but the pipeline always supplies it: some
        decisions cannot be made from the span alone.
        """
        candidate = text.strip()
        if not candidate:
            return False

        # A structured type must actually look like its type, whatever the
        # recognizer claimed. Recognizers occasionally label a field caption
        # with the type of the value beside it — the word "Telephone" once
        # acquired a phone surrogate and rendered as "081 8686 6388:" — and a
        # whole sentence was once labelled a bank account number and replaced
        # with a company name. This check has to run before anything else,
        # because the always-redact shortcut below would otherwise skip it.
        if not self._matches_type_shape(entity_type, candidate):
            return False

        # A regulator's website is public infrastructure. Checked before the
        # always-redact shortcut for the same reason the shape test is.
        if entity_type in ("WEB_ADDRESS", "URL") and self.is_public_host(candidate):
            return False

        if entity_type in self.always_redact:
            return True

        if len(candidate) < 3:
            return False
        if self.is_transaction_reference(candidate, preceding):
            return False
        if self._is_malformed(entity_type, candidate):
            return False
        if self.is_field_label(candidate):
            return False
        # Note: bare-place suppression is deliberately NOT applied here. The
        # pieces of an address ("Village Kharoli", "Harbour Reclamation") look
        # like bare places individually, and dropping them at this point would
        # destroy the address before it can be assembled. The pipeline applies
        # is_bare_place() after coalescing, when a real address carries its PIN
        # code and a lone city name does not.
        if self.is_vocabulary_phrase(candidate):
            return False
        if self.is_prose_phrase(entity_type, candidate):
            return False
        if self.is_public_body(candidate):
            return False
        if self.completes_public_body(candidate, preceding):
            return False
        if self.is_defined_term(candidate):
            return False
        if self.is_reference_vocabulary(candidate):
            return False
        if self._normalise(candidate) in LANGUAGE_NAMES:
            return False
        if self.is_common_word(candidate):
            return False
        # A span with no uppercase letter at all is prose, not a proper noun.
        if entity_type in ("PERSON", "ORGANIZATION", "LOCATION"):
            # A whole section heading is document structure. Not truecasing
            # headings stops one route to this, but the model also reads them
            # straight from the capitals, which is how "TABLE OF CONTENTS" in a
            # header part became a company and emptied three paragraphs.
            if self.is_structural_heading(candidate):
                return False
            if self.is_generic_corporate_shell(candidate):
                return False
            if candidate.islower():
                return False
            # ...and neither is one the document itself writes in lower case
            # somewhere else. This catches the sentence-initial capital the
            # test above cannot see: "Delay in or inability of the vendors"
            # is not a person, because the same document says "any delay in".
            if self.occurs_uncapitalised(candidate):
                return False
        return True

    #: Spans that ran across a structural boundary rather than naming one thing.
    _MALFORMED = re.compile(r"[\n\r|]|@|https?://|\b(?:telephone|email|website|e-mail)\s*:", re.I)

    def _is_malformed(self, entity_type: str, text: str) -> bool:
        """
        Reject name-like spans that swallowed neighbouring structure.

        A contact block puts a company, an e-mail and a phone number on
        consecutive lines, and a model reading it as flowing text will
        occasionally return one span covering all three — "ipo@merchantbank.example.com
        Telephone: +91". Assigning a surrogate to that string maps a fragment
        of layout rather than an entity, so it is dropped; the individual
        e-mail and phone detections inside it stand on their own.
        """
        if entity_type not in ("PERSON", "ORGANIZATION", "LOCATION"):
            return False
        if self._MALFORMED.search(text):
            return True
        # A name is not mostly digits.
        digits = sum(character.isdigit() for character in text)
        return digits > len(text) * 0.4

    def reasons(self, entity_type: str, text: str) -> List[str]:
        """Why a span was suppressed — surfaced in the evaluation report."""
        why: List[str] = []
        if self.is_public_body(text):
            why.append("public body")
        if self.is_defined_term(text):
            why.append("document-defined term")
        if self.is_common_word(text):
            why.append("common word")
        if self.occurs_uncapitalised(text):
            why.append("written in lower case elsewhere in the document")
        return why
