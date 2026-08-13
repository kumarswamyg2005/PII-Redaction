"""
Zero-shot NER, the component that replaces the hardcoded name lists.

An earlier version of this tool detected people by listing their names. That
scores well on one document and is worthless on the next, which is the failure
this module exists to correct. GLiNER takes entity types described in plain
language — "person", "residential address" — and finds them without training
and without ever being told the answers, so the same code generalises to a
document it has never seen.

Two practical concerns are handled here:

**The model has a bounded context window.** A 446,000-character document must
be fed in pieces. Splitting blindly would cut entities in half, so chunks break
on paragraph and sentence boundaries and overlap slightly; offsets are then
mapped back to the full document and duplicate spans from the overlap removed.

**Loading is expensive.** The model is loaded once and reused, and the class
degrades gracefully — if the model is unavailable the recognizer reports
nothing rather than crashing the pipeline, so the identifier and policy layers
still run.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from presidio_analyzer import EntityRecognizer, RecognizerResult

logger = logging.getLogger(__name__)

__all__ = ["GlinerRecognizer", "GLINER_LABEL_MAP", "DEFAULT_MODEL"]

#: Small, CPU-friendly PII model. Larger variants trade latency for a few
#: points of F1; the choice is a config change, not a code change.
DEFAULT_MODEL = "knowledgator/gliner-pii-base-v1.0"

#: Natural-language label -> our entity type. The keys are what the model is
#: asked to find; extending coverage means adding a phrase here.
#:
#: The exact wording is not cosmetic — it is a tuned parameter. On this
#: document "name" scores people at 0.80-0.83 where "person name" scores the
#: same spans at 0.49, and "address" captures a whole postal block where
#: "location" captures only the city. The phrasings below were selected by
#: measurement, not intuition (see README, "Tuning the zero-shot labels").
GLINER_LABEL_MAP: Dict[str, str] = {
    "name": "PERSON",
    "company": "ORGANIZATION",
    "address": "LOCATION",
    "location": "LOCATION",
    "email address": "EMAIL_ADDRESS",
    "telephone number": "PHONE_NUMBER",
    "date of birth": "DATE_OF_BIRTH",
    "passport number": "IN_PASSPORT",
    "bank account number": "BANK_ACCOUNT",
    "website": "WEB_ADDRESS",
}


class GlinerRecognizer(EntityRecognizer):
    """Presidio recognizer backed by a GLiNER zero-shot model."""

    #: Characters per chunk. Comfortably inside the model's token window once
    #: English text is tokenised, with room for the label prompt.
    CHUNK_CHARS = 1200
    #: Overlap so an entity straddling a chunk edge is still seen whole.
    CHUNK_OVERLAP = 200

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        label_map: Optional[Dict[str, str]] = None,
        threshold: float = 0.30,
    ) -> None:
        # 0.30, not the library default of 0.50: at 0.50 this model misses
        # a three-part promoter name (0.49) and every postal address. Recall is
        # bought cheaply here because the policy layer downstream is what
        # actually decides whether a detected span gets redacted.
        self._label_map = dict(label_map or GLINER_LABEL_MAP)
        self._threshold = threshold
        self._model_name = model_name
        self._model = None
        super().__init__(
            supported_entities=sorted(set(self._label_map.values())),
            supported_language="en",
            name="GlinerRecognizer",
        )

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        """Load the model once. Failure is logged, not raised."""
        if self._model is not None:
            return
        try:
            from gliner import GLiNER

            self._model = GLiNER.from_pretrained(self._model_name)
            self._model.eval()
            logger.info("GLiNER model loaded: %s", self._model_name)
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning(
                "GLiNER unavailable (%s); continuing with validators and policy only. "
                "Name and organisation recall will be reduced.", exc,
            )
            self._model = None

    @property
    def is_available(self) -> bool:
        return self._model is not None

    # -- chunking ----------------------------------------------------------

    def _chunks(self, text: str) -> Iterator[Tuple[int, str]]:
        """Yield (offset, chunk) split on natural boundaries, with overlap."""
        position = 0
        length = len(text)
        while position < length:
            end = min(length, position + self.CHUNK_CHARS)
            if end < length:
                window_start = position + self.CHUNK_CHARS // 2
                boundary = max(
                    text.rfind("\n", window_start, end),
                    text.rfind(". ", window_start, end),
                )
                if boundary > position:
                    end = boundary + 1
            yield position, text[position:end]
            if end >= length:
                break
            position = max(position + 1, end - self.CHUNK_OVERLAP)

    # -- inference ---------------------------------------------------------

    def analyze(
        self,
        text: str,
        entities: Sequence[str],
        nlp_artifacts=None,
    ) -> List[RecognizerResult]:
        if self._model is None or not text.strip():
            return []

        labels = list(self._label_map)
        seen: set[Tuple[int, int, str]] = set()
        results: List[RecognizerResult] = []

        for offset, chunk in self._chunks(text):
            try:
                predictions = self._model.predict_entities(
                    chunk, labels, threshold=self._threshold
                )
            except Exception as exc:  # pragma: no cover - runtime robustness
                logger.debug("GLiNER chunk at %d failed: %s", offset, exc)
                continue

            for prediction in predictions:
                entity_type = self._label_map.get(prediction["label"].lower())
                if entity_type is None:
                    continue
                start = offset + prediction["start"]
                end = offset + prediction["end"]
                key = (start, end, entity_type)
                if key in seen:  # produced twice by the overlap region
                    continue
                seen.add(key)
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        score=float(prediction.get("score", self._threshold)),
                    )
                )
        return results
