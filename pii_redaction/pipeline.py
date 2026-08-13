"""
The pipeline: read, detect, decide, replace, write.

Ordering matters more than it looks. Detection runs over the whole document
*before* anything is rewritten, so the surrogate table is complete when
rewriting starts; that is what lets a single entity resolve to one surrogate
even in the places the detector happened to miss. Policy is consulted between
detection and surrogate assignment, so a suppressed span never consumes a
surrogate and never appears in the mapping.

Images are processed alongside text and merged into the same output file, so
there is exactly one redacted artefact rather than a document plus a pile of
separately-redacted pictures.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .detection.policy import RedactionPolicy
from .detection.registry import SUPPORTED_ENTITIES, build_analyzer
from .documents.docx_reader import (
    iter_table_rows,
    joined_text,
    read_field_codes,
    read_media,
    read_paragraphs,
)
from .documents.docx_writer import DocxRedactor
from .documents.images import ImageRedactor, blank_image
from .surrogates.cache import SurrogateCache

logger = logging.getLogger(__name__)

__all__ = ["RedactionPipeline", "RedactionOutcome"]

#: Types whose spans are decided by a pattern or a checksum rather than by a
#: model's judgement. They take precedence when spans overlap.
_STRUCTURED_TYPES = frozenset({
    "EMAIL_ADDRESS", "PHONE_NUMBER", "IN_MOBILE", "IN_AADHAAR", "IN_PAN",
    "IN_GSTIN", "IN_IFSC", "IN_PASSPORT", "IN_VOTER_ID", "CREDIT_CARD",
    "IBAN_CODE", "IP_ADDRESS", "CORPORATE_ID", "URL", "WEB_ADDRESS",
    "DATE_OF_BIRTH",
})


@dataclass
class Detection:
    """A detected span, kept with its text so reporting needs no re-slicing."""

    entity_type: str
    start: int
    end: int
    score: float
    text: str


@dataclass
class RedactionOutcome:
    """Everything a caller or report needs to know about one run."""

    input_path: str
    output_path: str
    detections: List[Detection] = field(default_factory=list)
    suppressed: List[Tuple[Detection, List[str]]] = field(default_factory=list)
    mapping: Dict[str, str] = field(default_factory=dict)
    entity_counts: Dict[str, int] = field(default_factory=dict)
    image_findings: Dict[str, int] = field(default_factory=dict)
    rewrite_counts: Dict[str, int] = field(default_factory=dict)
    defined_terms_learned: int = 0
    seconds: float = 0.0
    #: The analysed text, kept so the evaluation can recover an annotation's
    #: value from its offsets. The shipped ground truth stores offsets only,
    #: because a ground-truth file for a redaction task is itself a PII
    #: disclosure. Held in memory, never written out.
    document_text: str = field(default="", repr=False)
    #: The policy used for this run, so the evaluation can classify errors with
    #: the same rules that produced them rather than a second, divergent set.
    policy: Optional[object] = field(default=None, repr=False)

    @property
    def total_replacements(self) -> int:
        return sum(self.rewrite_counts.values())


class RedactionPipeline:
    """End-to-end redaction of a .docx."""

    #: Analyzer confidence floor. Deliberately permissive — policy, not the
    #: score, is what suppresses non-PII here.
    MIN_SCORE = 0.35

    def __init__(
        self,
        analyzer=None,
        use_gliner: bool = True,
        redact_images: bool = True,
        redact_logos: bool = True,
    ) -> None:
        self._analyzer = analyzer or build_analyzer(use_gliner=use_gliner)
        self._redact_images = redact_images
        self._redact_logos = redact_logos

    # -- main ---------------------------------------------------------------

    def run(self, source: str | Path, destination: str | Path) -> RedactionOutcome:
        started = time.time()
        source, destination = Path(source), Path(destination)
        outcome = RedactionOutcome(str(source), str(destination))

        paragraphs = read_paragraphs(source)
        # Field instructions are appended after the body so body offsets stay
        # aligned with the visible document, which is what ground-truth
        # annotations are expressed against.
        field_codes = read_field_codes(source)
        text = joined_text(paragraphs + field_codes)
        outcome.document_text = text
        logger.info(
            "read %d paragraphs and %d field codes (%d chars)",
            len(paragraphs), len(field_codes), len(text),
        )

        policy = RedactionPolicy().learn_from(paragraphs, iter_table_rows(source))
        policy.structural_headings = {
            text[start:end].strip().upper()
            for start, end in self._structural_caps_spans(text)
        }
        outcome.defined_terms_learned = len(policy.defined_terms)
        logger.info("learned %d defined terms from the document's glossary",
                    outcome.defined_terms_learned)

        raw = self._detect(text) + self._sweep_unambiguous(text)
        logger.info("analyzer produced %d spans", len(raw))

        # Policy first, overlap resolution second. The other order lets a long
        # span win the overlap contest and then get suppressed, taking a
        # perfectly good shorter span down with it — which is how
        # "ICICI Bank Limited" survived five times in an earlier run.
        permitted, suppressed = [], []
        for detection in raw:
            # Some decisions need the words before the span — "Order No." in
            # front of a number changes what that number is.
            preceding = text[max(0, detection.start - 40):detection.start]
            if policy.should_redact(detection.entity_type, detection.text, preceding):
                permitted.append(detection)
            else:
                suppressed.append(detection)
        outcome.suppressed = [
            (d, policy.reasons(d.entity_type, d.text)) for d in suppressed
        ]

        detections = self._coalesce_addresses(self._resolve_overlaps(permitted), text)

        # Coalescing invents new spans, so they have to face the policy too —
        # a merge of two permitted neighbours can produce a stretch of ordinary
        # prose that neither of them was. And now that addresses are whole, a
        # LOCATION still lacking any number is a bare place name ("Mumbai"),
        # which identifies nobody.
        kept: List[Detection] = []
        for detection in detections:
            preceding = text[max(0, detection.start - 40):detection.start]
            if policy.is_bare_place(detection.entity_type, detection.text):
                outcome.suppressed.append((detection, ["bare place name"]))
            elif (
                detection.entity_type not in _STRUCTURED_TYPES
                and policy.is_prose_phrase("ORGANIZATION", detection.text)
            ):
                # Only the prose test is re-applied, and only to name-like
                # spans. Running the whole policy over merged spans rejected
                # genuine addresses and cost 18 points of recall; running the
                # prose test over *structured* spans was worse still, because
                # an e-mail address is entirely lower case and so reads as
                # prose by that measure — which silently dropped one.
                outcome.suppressed.append((detection, ["prose, after merge"]))
            else:
                kept.append(detection)
        detections = kept

        canonical = self._canonicalise(detections)

        cache = SurrogateCache()
        for detection in detections:
            outcome.detections.append(detection)
            # Assign the surrogate against the canonical form, so that
            # "ICICI Securities" and "ICICI Securities Limited" resolve to the
            # same fake company rather than two unrelated ones, then alias the
            # variant so the rewriter replaces it too.
            preferred = canonical.get(
                (detection.entity_type, self._key(detection.text)), detection.text
            )
            cache.surrogate_for(detection.entity_type, preferred)
            cache.alias(detection.text, preferred)
            outcome.entity_counts[detection.entity_type] = (
                outcome.entity_counts.get(detection.entity_type, 0) + 1
            )

        # A website is written several ways — "www.example.com",
        # "https://example.com/path", or the bare host. Mapping only the form
        # that happened to be detected leaves the others in the document, which
        # is how three "https://issuerexample.com/..." links survived a run
        # in which the "www." form was correctly replaced.
        for detection in list(detections):
            if detection.entity_type not in ("WEB_ADDRESS", "URL"):
                continue
            host = self._registrable_host(detection.text)
            if host:
                cache.alias(host, detection.text)
                cache.alias(f"www.{host}", detection.text)

        # The glossary declares the document's own abbreviations. Applying them
        # catches short forms the detector never saw in isolation — "Exemplar"
        # appears bare six times here and is not detectable as a company on its
        # own, but the document states it means the full entity.
        for short_name, full_name in policy.entity_aliases.items():
            cache.alias(short_name, full_name)

        outcome.mapping = cache.mapping
        logger.info("%d entities kept, %d suppressed by policy, %d surrogates",
                    len(outcome.detections), len(outcome.suppressed), len(cache))

        media = self._redact_media(source, policy) if self._redact_images else {}
        outcome.image_findings = {
            name: count for name, count in getattr(self, "_last_image_counts", {}).items()
        }

        outcome.policy = policy
        stats = DocxRedactor(outcome.mapping).redact(source, destination, media)
        outcome.rewrite_counts = stats.summary()
        outcome.seconds = time.time() - started
        logger.info("rewrote %d spans in %.1fs", outcome.total_replacements, outcome.seconds)
        return outcome

    # -- steps --------------------------------------------------------------

    def _detect(self, text: str) -> List[Detection]:
        # Ask only for the types we actually treat as PII. Presidio's built-in
        # DATE_TIME otherwise fires on every date in the document — 703 of them
        # here, from "1981" to "10 years" — none of which is personal
        # information. Birth dates are covered separately by a context-aware
        # recognizer, which is what the brief asks for.
        detections = self._analyze(text, text)
        # The second pass exists only to recover entities the models miss in
        # ALL-CAPS text, so it is run over the ALL-CAPS neighbourhoods rather
        # than the whole document. On the supplied prospectus that is 7% of the
        # text; re-scanning the other 93% costs ~90 seconds and can, by
        # construction, find nothing the first pass did not already see.
        for start, end in self._shouted_windows(text):
            window = text[start:end]
            for d in self._analyze(self._truecase(window), window):
                d.start += start
                d.end += start
                detections.append(d)
        return detections

    #: Context kept either side of an ALL-CAPS run. Wide enough that the run
    #: sits whole inside its window and Presidio's context-word enhancement
    #: still sees the surrounding sentence.
    _SHOUT_MARGIN = 2000

    @classmethod
    def _shouted_windows(cls, text: str) -> List[Tuple[int, int]]:
        """Merged (start, end) slices covering every ALL-CAPS run with context."""
        windows: List[Tuple[int, int]] = []
        for match in cls._SHOUTED.finditer(text):
            start = max(0, match.start() - cls._SHOUT_MARGIN)
            end = min(len(text), match.end() + cls._SHOUT_MARGIN)
            if windows and start <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        return windows

    def _analyze(self, analysed: str, original: str) -> List[Detection]:
        results = self._analyzer.analyze(
            text=analysed,
            language="en",
            entities=SUPPORTED_ENTITIES,
            score_threshold=self.MIN_SCORE,
        )
        # Spans are reported against `analysed` but must be recorded against the
        # real document text; title-casing preserves length, so offsets carry.
        return [
            Detection(r.entity_type, r.start, r.end, r.score, original[r.start:r.end])
            for r in results
        ]

    #: A run of three or more capitalised words — a heading or a cover-page name.
    _SHOUTED = re.compile(r"\b[A-Z][A-Z&.'\-]*(?:\s+[A-Z][A-Z&.'\-]*){2,}\b")

    @classmethod
    def _truecase(cls, text: str) -> str:
        """
        Title-case ALL-CAPS runs so the models can see them.

        Capitalisation is one of the strongest cues an NER model has, and
        performance collapses on uppercase text — published work measures a drop
        of over 40 F1 when casing is absent. This document puts its promoters'
        names on the cover page in full capitals, where the zero-shot model
        largely missed them.

        Title-casing preserves string length exactly, so every span found in the
        transformed copy maps back to the original by offset with no
        realignment. The transformed text is used only for detection; the
        document itself is never touched.

        Structural headings are left in capitals deliberately — see
        :meth:`_structural_caps_spans`.
        """
        skip = cls._structural_caps_spans(text)

        def recase(match: "re.Match[str]") -> str:
            # Overlap, not containment: a capitalised run is allowed to span a
            # line break, so a match that begins on a heading line and runs on
            # into the next one would otherwise slip past a containment test.
            if any(match.start() < end and start < match.end() for start, end in skip):
                return match.group(0)
            return match.group(0).title()

        return cls._SHOUTED.sub(recase, text)

    #: Trailing page number on a table-of-contents line ("...OF THE OFFER15").
    #: Deliberately narrow: at most three digits, and the character before them
    #: must be a letter. A looser "\d+$" also matched the cover page's corporate
    #: identity number, U00000XX0000XXX000000, and skipping that line cost two
    #: real annotations.
    _TOC_PAGE_NUMBER = re.compile(r"(?<=[A-Za-z])\d{1,3}\s*$")

    @classmethod
    def _structural_caps_spans(cls, text: str) -> List[Tuple[int, int]]:
        """
        Character ranges of ALL-CAPS lines that are document structure.

        Truecasing a heading invents an entity: "TABLE OF CONTENTS" becomes
        "Table Of Contents", which a zero-shot model reads as a company and the
        pipeline then replaces with a surrogate — destroying the contents page
        and, because the entry is a field, dropping paragraphs from the output.

        A heading is recognised by repetition, not by a list of known headings:
        a contents entry names a section that also carries the same words as its
        own title, so the line occurs more than once. A person's name on the
        cover page occurs once and is still truecased — which is what this pass
        exists for.

        A trailing page number is the second signal, for the contents entry that
        appears only once because Word stores it as a field. See
        :attr:`_TOC_PAGE_NUMBER` for why that test is as narrow as it is.
        """
        lines: List[Tuple[int, int, str, bool]] = []
        offset = 0
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped and stripped == stripped.upper() and any(c.isalpha() for c in stripped):
                key = cls._TOC_PAGE_NUMBER.sub("", stripped).strip()
                if key:
                    lines.append((offset, offset + len(line), key, key != stripped))
            offset += len(line) + 1  # the split consumed one "\n"

        repeated = Counter(key for _, _, key, _ in lines)
        return [
            (start, end)
            for start, end, key, paginated in lines
            if paginated or repeated[key] > 1
        ]

    #: A postal code, which is what marks the end of an Indian address block.
    _PIN_CODE = re.compile(r"\b\d{3}\s?\d{3}\b")
    #: Captions that introduce an address.
    #: A caption introducing an address. Not end-anchored: a building number
    #: usually sits between the caption and the first detected part
    #: ("Corporate Office: 201, Tower 2, ..."), and anchoring left that whole
    #: block unmerged.
    _ADDRESS_LEAD = re.compile(
        r"(?:registered|corporate|branch|head)\s+office\s*[:\-]|address\s*[:\-]",
        re.IGNORECASE,
    )
    #: Largest gap, in characters, still considered "the same address block".
    _ADDRESS_GAP = 25
    #: Longest span that can result from merging. Beyond this it has stopped
    #: being an address and started swallowing the paragraph.
    _ADDRESS_MAX = 180
    #: What may sit between two pieces of one address: a short run of words
    #: with no sentence punctuation. Listing permitted connectives was too
    #: strict — real addresses contain unlisted fragments like "Off Pallod
    #: Farms" — so the test is shape, not vocabulary.
    _ADDRESS_JOINER = re.compile(r"^[^.;!?]{0,25}$")
    #: Field captions that mark the end of an address block.
    _ADDRESS_STOP = re.compile(
        r"\b(?:telephone|tel|phone|fax|e-?mail|website|contact|cin|sebi|"
        r"registration|investor|grievance)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _coalesce_addresses(cls, detections: List[Detection], text: str) -> List[Detection]:
        """
        Merge the pieces of one postal address into a single LOCATION.

        An address is a run of proper nouns, and the models label its parts
        inconsistently — a street as an organisation, a locality as a person.
        Replaced individually the result is unreadable:

            11/3, Village Kharoli, Chakan Taluka - Khed, Pune - 410 501
            -> 11/3, Apex Group, Peter Bose Kapoor, 371 Maple Court, ...

        Detections that sit close together inside an address context therefore
        become one span, replaced by one coherent fake address. Being wrong
        here is cheap: the merged span is still redacted, just as one unit.
        """
        if not detections:
            return detections

        merged: List[Detection] = []
        group: List[Detection] = []

        def flush() -> None:
            if not group:
                return
            if len(group) == 1:
                merged.append(group[0])
            else:
                start, end = group[0].start, group[-1].end
                merged.append(Detection(
                    entity_type="LOCATION",
                    start=start, end=end,
                    score=max(d.score for d in group),
                    text=text[start:end],
                ))
            group.clear()

        for detection in detections:
            # An e-mail, phone or URL is never part of a postal address.
            # Letting one join a group put it inside a merged LOCATION span,
            # and when that span was later rejected the address went with it —
            # which is how "rm6.branch@bankexample.co.in" survived into the output.
            if detection.entity_type in _STRUCTURED_TYPES:
                flush()
                merged.append(detection)
                continue
            if group:
                previous = group[-1]
                between = text[previous.end:detection.start]
                joinable = (
                    detection.start - previous.end <= cls._ADDRESS_GAP
                    and detection.end - group[0].start <= cls._ADDRESS_MAX
                    # Never merge across a paragraph break. Paragraphs are the
                    # unit the writer can rewrite within, so a span crossing one
                    # produces a mapping key that cannot match anything — and
                    # both entities it swallowed then survive into the output.
                    # This merged the tail of one address onto the next
                    # paragraph's company name and leaked four values.
                    and "\n" not in between
                    and cls._ADDRESS_JOINER.match(between)
                    and not cls._ADDRESS_STOP.search(between)
                    and cls._in_address_context(text, group[0].start, detection.end)
                )
                if joinable:
                    group.append(detection)
                    continue
                flush()
            group.append(detection)
        flush()
        return merged

    @classmethod
    def _in_address_context(cls, text: str, start: int, end: int) -> bool:
        """True when a span sits in something that reads like a postal address."""
        window = text[start:end]
        if cls._PIN_CODE.search(window):
            return True
        return bool(cls._ADDRESS_LEAD.search(text[max(0, start - 60):start]))

    _HOST = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})", re.I)

    @classmethod
    def _registrable_host(cls, url: str) -> str:
        """The bare host from a URL, ignoring scheme, www and any path."""
        match = cls._HOST.search(re.sub(r"\s+", "", url))
        return match.group(1).rstrip("./").lower() if match else ""

    #: E-mail addresses and web addresses, including the split forms this
    #: document produces at line breaks ("www.issuerexample. com").
    _EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\s?\.\s?[A-Za-z]{2,}\b")
    _URL = re.compile(
        r"\b(?:https?://|www\s?\.\s?)[A-Za-z0-9.\-]+\s?\.\s?[A-Za-z]{2,}(?:/[^\s,;)\]]*)?"
    )

    @classmethod
    def _sweep_unambiguous(cls, text: str) -> List[Detection]:
        """
        Find every e-mail address and URL directly, without asking a model.

        These two types have an exact syntax, so there is no judgement to
        delegate — and delegating it cost real leaks: one address survived
        because a longer, vaguer span outranked it during overlap resolution,
        and another because it only ever appeared inside a hyperlink.

        Running the sweep alongside the analyzer means coverage of these types
        no longer depends on a model's confidence. Duplicates are harmless;
        overlap resolution collapses them.
        """
        found: List[Detection] = []
        for pattern, entity_type in ((cls._EMAIL, "EMAIL_ADDRESS"), (cls._URL, "WEB_ADDRESS")):
            for match in pattern.finditer(text):
                found.append(Detection(
                    entity_type=entity_type,
                    start=match.start(), end=match.end(),
                    score=1.0, text=match.group(0),
                ))
        return found

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).lower()

    @classmethod
    def _canonicalise(cls, detections: List[Detection]) -> Dict[Tuple[str, str], str]:
        """
        Collapse variants of one entity onto a single canonical form.

        A company is written several ways in the same document — "Exemplar",
        "Exemplar Wealth Management Limited" — and left alone each variant would
        receive its own unrelated surrogate, so the reader could no longer tell
        that two mentions referred to the same firm. Where one variant is a
        contiguous phrase inside a longer one of the same type, the longer form
        wins and both share its surrogate.

        Containment must be phrase-aligned: matching on raw substrings would
        merge "A Sharma" into "A B Sharma", which are not
        necessarily the same person.
        """
        by_type: Dict[str, set] = {}
        for detection in detections:
            by_type.setdefault(detection.entity_type, set()).add(cls._key(detection.text))

        canonical: Dict[Tuple[str, str], str] = {}
        for entity_type, variants in by_type.items():
            ordered = sorted(variants, key=len, reverse=True)
            for short in ordered:
                for long in ordered:
                    if len(long) <= len(short):
                        continue
                    if f" {short} " in f" {long} ":
                        canonical[(entity_type, short)] = long
                        break
        return canonical

    @staticmethod
    def _resolve_overlaps(detections: List[Detection]) -> List[Detection]:
        """
        Keep the most informative span where several cover the same characters.

        Recognizers overlap by design, so the same address may arrive as one
        long LOCATION and three short fragments. Preferring the longest span,
        then the highest score, keeps the version that carries the most
        context — a partly-replaced address is worse than a fully-replaced one.
        """
        # Structured types win over name-like ones regardless of length. An
        # e-mail address is either an e-mail address or it is not; a LOCATION
        # or ORGANIZATION span from a zero-shot model is a guess. Ranking by
        # length alone let a long fuzzy span swallow "rm6.branch@bankexample.co.in"
        # and, when that span was later rejected, the address leaked.
        ordered = sorted(
            detections,
            key=lambda d: (
                0 if d.entity_type in _STRUCTURED_TYPES else 1,
                -(d.end - d.start),
                -d.score,
            ),
        )
        kept: List[Detection] = []
        for candidate in ordered:
            if any(candidate.start < k.end and candidate.end > k.start for k in kept):
                continue
            kept.append(candidate)
        return sorted(kept, key=lambda d: d.start)

    def _redact_media(self, source: Path, policy: RedactionPolicy) -> Dict[str, bytes]:
        """Redact every embedded image, returning replacement part bytes."""
        redactor = ImageRedactor(analyzer=self._analyzer, policy=policy)
        replacements: Dict[str, bytes] = {}
        counts: Dict[str, int] = {}

        for name, payload in read_media(source):
            result = redactor.redact(name, payload)
            if result.redacted:
                replacements[name] = result.payload
                counts[name] = len(result.findings)
                logger.info("redacted %d regions in %s", len(result.findings), name)
            elif self._redact_logos and self._looks_like_logo(payload):
                # A logo carries no readable PII, so nothing above fires — but
                # it still identifies the organisation whose name was replaced
                # in the text. Leaving it defeats the text redaction entirely.
                replacements[name] = blank_image(payload)
                counts[name] = 1
                logger.info("replaced probable logo %s", name)

        self._last_image_counts = counts
        return replacements

    @staticmethod
    def _looks_like_logo(payload: bytes, max_pixels: int = 400_000) -> bool:
        """Small graphics with no readable text are treated as brand marks."""
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
            return width * height <= max_pixels
        except Exception:
            return False
