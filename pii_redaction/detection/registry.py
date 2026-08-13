"""
Assembling the analyzer.

Presidio supplies the orchestration — score reconciliation, context boosting,
span resolution — while the recognizers registered here supply the judgement.
Three sources contribute, deliberately overlapping so that a miss by one is
usually caught by another:

* :mod:`.identifiers` — regex candidates proven by checksum. Near-perfect
  precision on the types it covers.
* :mod:`.gliner_backend` — zero-shot NER for names, organisations and
  addresses, which no pattern can capture.
* Presidio's own built-ins — e-mail, URL, IBAN and similar, which are already
  well solved and not worth reimplementing.

spaCy is present only to tokenise and lemmatise for the context enhancer;
``en_core_web_sm`` is sufficient for that and keeps the image ~550 MB smaller
than the large model, because GLiNER is doing the entity work.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

from .gliner_backend import DEFAULT_MODEL, GlinerRecognizer
from .identifiers import DateOfBirthRecognizer, build_identifier_recognizers

logger = logging.getLogger(__name__)

__all__ = ["build_analyzer", "SUPPORTED_ENTITIES"]

#: Everything the pipeline can detect. Presidio built-ins are listed alongside
#: our own so a caller can request them uniformly.
SUPPORTED_ENTITIES: List[str] = [
    "PERSON", "ORGANIZATION", "LOCATION",
    "EMAIL_ADDRESS", "PHONE_NUMBER", "IN_MOBILE",
    "IN_AADHAAR", "IN_PAN", "IN_GSTIN", "IN_IFSC", "IN_PASSPORT", "IN_VOTER_ID",
    "CREDIT_CARD", "IBAN_CODE", "IP_ADDRESS", "URL",
    "CORPORATE_ID", "DATE_OF_BIRTH", "BANK_ACCOUNT", "WEB_ADDRESS",
]

#: spaCy is a tokeniser here, not the entity model.
_SPACY_MODEL = "en_core_web_sm"


def build_analyzer(
    use_gliner: bool = True,
    gliner_model: Optional[str] = None,
    gliner_threshold: float = 0.30,
    languages: Sequence[str] = ("en",),
) -> AnalyzerEngine:
    """
    Build the analyzer.

    ``use_gliner=False`` disables the transformer and leaves only patterns,
    validators and Presidio's built-ins. That mode exists for the ablation
    reported in the evaluation — it is how the contribution of zero-shot NER is
    measured, rather than asserted.
    """
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
        }
    ).create_engine()

    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(nlp_engine=nlp_engine, languages=list(languages))

    for recognizer in build_identifier_recognizers():
        registry.add_recognizer(recognizer)
    registry.add_recognizer(DateOfBirthRecognizer())

    if use_gliner:
        recognizer = GlinerRecognizer(
            model_name=gliner_model or DEFAULT_MODEL,
            threshold=gliner_threshold,
        )
        recognizer.load()
        if recognizer.is_available:
            registry.add_recognizer(recognizer)
        else:
            logger.warning("Continuing without zero-shot NER; recall will be lower.")

    return AnalyzerEngine(
        nlp_engine=nlp_engine,
        registry=registry,
        supported_languages=list(languages),
    )
