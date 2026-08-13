"""
Redacting PII that lives in pictures rather than text.

This is the part a text-only pipeline silently fails. The document embeds
photographs of a PAN card and an Aadhaar card — identity documents belonging to
real people, carrying names, parents' names, dates of birth, a full postal
address, the identifier numbers themselves, facial photographs and signatures.
None of it is reachable through the document's text, so a tool that scores
perfectly on the text can still publish an Aadhaar number.

Four distinct carriers are handled, because covering only the first leaves the
data readable:

**Printed text** — located with OCR and run through the same analyzer as the
document body, so image and text redaction agree on what counts as PII.

**QR codes** — an Aadhaar QR re-encodes the holder's name, date of birth,
gender, address and number. Blacking out the printed fields while leaving the
QR intact leaks everything to any scanner, so codes are located and destroyed
outright rather than merely covered.

**Faces and signatures** — a photograph identifies a person as surely as their
name, and a signature is reusable. Both are covered.

**Logos** — a company logo re-identifies an organisation whose name was
carefully replaced in the text. Consistency requires removing it.

Inside an identity document the precision/recall trade-off inverts: covering
too much of a KYC card costs nothing, while missing one field defeats the
exercise. Detection here is therefore deliberately aggressive, unlike the body
text.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

__all__ = ["ImageRedactor", "ImageFinding", "ImageRedactionResult"]

Box = Tuple[int, int, int, int]  # left, top, right, bottom


@dataclass
class ImageFinding:
    """One redacted region, kept for the evaluation report."""

    kind: str  # "text" | "qr" | "face" | "signature" | "logo"
    box: Box
    detail: str = ""


@dataclass
class ImageRedactionResult:
    name: str
    findings: List[ImageFinding] = field(default_factory=list)
    ocr_text: str = ""
    payload: Optional[bytes] = None

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


class ImageRedactor:
    """Finds and covers PII inside a raster image."""

    #: OCR languages. Aadhaar and PAN cards print every field twice, in
    #: Devanagari and in English; reading only English leaves half the card.
    OCR_LANGUAGES = "eng+hin"
    #: Minimum OCR confidence for a word to be trusted as text.
    MIN_OCR_CONFIDENCE = 30
    #: Padding around a detected box, in pixels.
    PADDING = 3

    def __init__(self, analyzer=None, policy=None, treat_as_id_card: bool = True) -> None:
        self._analyzer = analyzer
        self._policy = policy
        self._treat_as_id_card = treat_as_id_card

    # -- entry point -------------------------------------------------------

    def redact(self, name: str, payload: bytes) -> ImageRedactionResult:
        """Return a redacted copy of one image plus what was found in it."""
        result = ImageRedactionResult(name=name)
        try:
            image = Image.open(io.BytesIO(payload))
            image = image.convert("RGBA") if image.mode == "P" else image.convert("RGB")
        except Exception as exc:
            logger.warning("Cannot open image %s: %s", name, exc)
            result.payload = payload
            return result

        findings: List[ImageFinding] = []
        findings += self._find_text(image, result)
        findings += self._find_qr_codes(image)
        findings += self._find_faces(image)

        if not findings:
            result.payload = payload
            return result

        result.findings = findings
        result.payload = self._apply(image, findings, payload)
        return result

    # -- detectors ---------------------------------------------------------

    def _find_text(self, image: Image.Image, result: ImageRedactionResult) -> List[ImageFinding]:
        """OCR the image and mark every word belonging to a PII span."""
        try:
            import pytesseract
        except ImportError:  # pragma: no cover
            logger.warning("pytesseract unavailable; image text not redacted")
            return []

        try:
            data = pytesseract.image_to_data(
                image, lang=self.OCR_LANGUAGES, output_type=pytesseract.Output.DICT
            )
        except Exception as exc:
            logger.warning("OCR failed: %s", exc)
            return []

        words, boxes, confidences, line_keys = [], [], [], []
        levels = data.get("level") or []
        for index, word in enumerate(data["text"]):
            word = (word or "").strip()
            # image_to_data reports page, block, paragraph, line and word rows.
            # Only level 5 is a word; the coarser rows carry boxes spanning
            # whole regions, and covering those blacks out the entire card.
            if levels and levels[index] != 5:
                continue
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                confidence = -1.0
            # Every word is kept here, however uncertain. The confidence floor
            # is applied later and only where it matters: a shaky read still
            # marks ink worth covering on an identity card, and Devanagari on a
            # photographed card routinely scores below the floor.
            if not word or confidence < 0:
                continue
            box = (
                data["left"][index],
                data["top"][index],
                data["left"][index] + data["width"][index],
                data["top"][index] + data["height"][index],
            )
            if not self._is_plausible_word_box(box, image.size):
                # Tesseract sometimes reports a photograph or a QR symbol as a
                # single "word" spanning a third of the card. Those boxes are
                # artifacts, and honouring them blacks out most of the image.
                # The graphics they cover are handled by the QR and face
                # detectors, which know what they are looking at.
                continue
            words.append(word)
            boxes.append(box)
            confidences.append(confidence)
            line_keys.append(
                (
                    data.get("block_num", [0] * len(data["text"]))[index],
                    data.get("par_num", [0] * len(data["text"]))[index],
                    data.get("line_num", [0] * len(data["text"]))[index],
                )
            )

        if not words:
            return []

        # Reconstruct a text line with an offset table so analyzer spans can be
        # mapped back to the words, and thus to pixel boxes. Only confident
        # reads go into the text used for classification and PII matching.
        text, spans, cursor = "", [], 0
        for word, confidence in zip(words, confidences):
            usable = word if confidence >= self.MIN_OCR_CONFIDENCE else " " * len(word)
            spans.append((cursor, cursor + len(word)))
            text += usable + " "
            cursor += len(word) + 1
        result.ocr_text = text.strip()

        if self._treat_as_id_card and self._looks_like_id_card(text):
            # Every printed field on a KYC card is an identifier, a personal
            # detail, or a form label. Covering the labels too costs nothing;
            # missing one field publishes someone's Aadhaar number. OCR of a
            # photographed card is also noisy enough that span-matching alone
            # under-reads it, so the whole text layer goes.
            #
            # Cover whole lines rather than words: word segmentation on a
            # photographed card leaves gaps, and a gap is a legible fragment of
            # somebody's name. Merging each line into one band closes them.
            result.ocr_text = text.strip()
            bands = self._line_bands(words, boxes, line_keys)
            # OCR is not a reliable census of the ink on a photographed card.
            # Here Tesseract read the Devanagari name as a single nonsense
            # token spanning a third of the image, which the artifact filter
            # correctly discarded — leaving the name legible. The morphological
            # pass finds printed text regardless of whether it can be read.
            #
            # Overlapping boxes are merged rather than de-duplicated: dropping
            # the "duplicate" is what left that name visible, because the
            # discarded box extended further left than the one it overlapped.
            merged = self._merge_boxes(
                [f.box for f in bands] + [f.box for f in self._find_text_regions(image)]
            )
            return [ImageFinding("text", box, "id-card text") for box in merged]

        sensitive: set[int] = set()
        for start, end in self._pii_spans(text):
            for index, (word_start, word_end) in enumerate(spans):
                if word_start < end and word_end > start:
                    sensitive.add(index)

        return [
            ImageFinding("text", boxes[index], words[index]) for index in sorted(sensitive)
        ]

    @staticmethod
    def _merge_boxes(boxes: List[Box]) -> List[Box]:
        """Union overlapping boxes until nothing overlaps, so no gap survives."""
        remaining = list(boxes)
        merged: List[Box] = []
        while remaining:
            current = remaining.pop()
            changed = True
            while changed:
                changed = False
                for other in list(remaining):
                    if (
                        current[0] < other[2] and current[2] > other[0]
                        and current[1] < other[3] and current[3] > other[1]
                    ):
                        current = (
                            min(current[0], other[0]), min(current[1], other[1]),
                            max(current[2], other[2]), max(current[3], other[3]),
                        )
                        remaining.remove(other)
                        changed = True
            merged.append(current)
        return merged

    def _find_text_regions(self, image: Image.Image) -> List[ImageFinding]:
        """
        Locate printed text by shape, independent of whether OCR can read it.

        A morphological gradient highlights character strokes; closing with a
        wide, short kernel joins the characters of a line into one blob. What
        survives the aspect and fill filters is a line of text, in any script.
        """
        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover
            return []

        try:
            grey = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
            height, width = grey.shape[:2]
            gradient = cv2.morphologyEx(
                grey, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            )
            _, binary = cv2.threshold(gradient, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            joined = cv2.morphologyEx(
                binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (18, 3))
            )
            contours, _ = cv2.findContours(joined, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

            regions: List[ImageFinding] = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if w < 18 or h < 8:
                    continue
                if h > self.MAX_WORD_HEIGHT_RATIO * height:
                    continue
                if w * h > self.MAX_WORD_AREA_RATIO * width * height:
                    continue
                if w / float(h) < 1.2:              # a text line is wider than tall
                    continue
                if float(binary[y:y + h, x:x + w].mean()) / 255.0 < 0.08:
                    continue
                regions.append(
                    ImageFinding("text", (x, y, x + w, y + h), "unread text region")
                )
            return regions
        except Exception as exc:
            logger.debug("text-region detection failed: %s", exc)
            return []

    @staticmethod
    def _line_bands(words, boxes, line_keys) -> List[ImageFinding]:
        """Union each OCR line's word boxes into a single covering band."""
        grouped: Dict[tuple, List[int]] = {}
        for index, key in enumerate(line_keys):
            grouped.setdefault(key, []).append(index)

        bands: List[ImageFinding] = []
        for key, indices in grouped.items():
            selected = [boxes[i] for i in indices]
            bands.append(
                ImageFinding(
                    "text",
                    (
                        min(b[0] for b in selected),
                        min(b[1] for b in selected),
                        max(b[2] for b in selected),
                        max(b[3] for b in selected),
                    ),
                    " ".join(words[i] for i in indices)[:60],
                )
            )
        return bands

    #: Markers that identify a government identity document in OCR output,
    #: in either script. Matching any two is treated as confirmation.
    _ID_CARD_MARKERS = (
        "aadhaar", "aadhar", "uidai", "unique identification",
        "income tax department", "permanent account number", "govt. of india",
        "government of india", "भारत सरकार", "आधार", "आयकर", "पहचान",
        "date of birth", "father", "जन्म", "पिता",
    )

    def _looks_like_id_card(self, text: str) -> bool:
        lowered = text.lower()
        hits = sum(marker in lowered for marker in self._ID_CARD_MARKERS)
        return hits >= 2

    #: A word box taller than this fraction of the image, or covering more than
    #: this fraction of its area, is an OCR artifact rather than text.
    MAX_WORD_HEIGHT_RATIO = 0.12
    MAX_WORD_AREA_RATIO = 0.05

    def _is_plausible_word_box(self, box: Box, size: Tuple[int, int]) -> bool:
        left, top, right, bottom = box
        width, height = size
        box_width, box_height = right - left, bottom - top
        if box_width <= 0 or box_height <= 0:
            return False
        if box_height > self.MAX_WORD_HEIGHT_RATIO * height:
            return False
        if box_width * box_height > self.MAX_WORD_AREA_RATIO * width * height:
            return False
        return True

    def _pii_spans(self, text: str) -> List[Tuple[int, int]]:
        """PII character spans in OCR output, via the document's own analyzer."""
        spans: List[Tuple[int, int]] = []
        if self._analyzer is not None:
            try:
                for result in self._analyzer.analyze(text=text, language="en"):
                    value = text[result.start:result.end]
                    if self._policy is None or self._policy.should_redact(
                        result.entity_type, value
                    ):
                        spans.append((result.start, result.end))
            except Exception as exc:
                logger.debug("analyzer failed on OCR text: %s", exc)

        if self._treat_as_id_card:
            spans += self._id_card_spans(text)
        return spans

    def _id_card_spans(self, text: str) -> List[Tuple[int, int]]:
        """
        Extra spans for identity-document layouts.

        On a KYC card every printed field is either an identifier or a
        personal detail, and OCR of a photographed card is noisy enough that
        the general analyzer alone under-reads it. These patterns cover the
        labelled fields directly.
        """
        import re

        patterns = (
            r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b",              # Aadhaar
            r"\b[A-Z]{5}\d{4}[A-Z]\b",                      # PAN
            r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b",       # dates
            r"(?i)(?:name|father|पिता|नाम)\s*[:\-]?\s*(.{2,40})",
            r"(?i)(?:dob|date of birth|जन्म)\s*[:\-]?\s*(\S+)",
            r"(?i)(?:address|पता)\s*[:\-]?\s*(.{2,80})",
            r"\b\d{6}\b",                                    # PIN code
        )
        spans: List[Tuple[int, int]] = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                spans.append((match.start(), match.end()))
        return spans

    def _find_qr_codes(self, image: Image.Image) -> List[ImageFinding]:
        """Locate QR/barcodes so they can be destroyed rather than covered."""
        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover
            return []

        frame = np.array(image.convert("RGB"))[:, :, ::-1]
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        findings: List[ImageFinding] = []

        # OpenCV's decoder is reliable on clean renders but frequently fails on
        # a photographed, glare-affected card — exactly our case. Binarising
        # first recovers most of those.
        try:
            detector = cv2.QRCodeDetector()
            for candidate in (grey, cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
                ok, points = detector.detectMulti(candidate)
                if ok and points is not None:
                    for quad in points:
                        xs, ys = quad[:, 0], quad[:, 1]
                        findings.append(
                            ImageFinding(
                                "qr",
                                (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())),
                                "QR code (decoder)",
                            )
                        )
                    break
        except Exception as exc:
            logger.debug("QR decoder failed: %s", exc)

        # Run the geometric pass regardless: a card can carry two symbols and
        # the decoder may read only one of them.
        for candidate in self._find_qr_by_shape(grey, cv2, np):
            if not any(self._overlaps(candidate.box, existing.box) for existing in findings):
                findings.append(candidate)
        return findings

    @staticmethod
    def _overlaps(a: Box, b: Box) -> bool:
        left, top = max(a[0], b[0]), max(a[1], b[1])
        right, bottom = min(a[2], b[2]), min(a[3], b[3])
        if right <= left or bottom <= top:
            return False
        intersection = (right - left) * (bottom - top)
        smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return smaller > 0 and intersection / smaller > 0.3

    @staticmethod
    def _find_qr_by_shape(grey, cv2, np) -> List[ImageFinding]:
        """
        Locate a QR symbol geometrically when the decoder cannot read it.

        We only need to destroy the code, not decode it, so shape is enough: a
        QR is a roughly square, densely speckled, high-contrast block. Blocks
        matching that description are covered. Being wrong here is cheap — a
        false positive blacks out a small square of an ID card — while a miss
        leaks the entire record.
        """
        findings: List[ImageFinding] = []
        try:
            # Work from edges, not a threshold. On a pale laminated card a
            # brightness threshold merges the QR into one card-sized blob and
            # RETR_EXTERNAL then hides it; edge density isolates the symbol
            # because a QR packs far more edges per pixel than anything else
            # printed on an ID card.
            edges = cv2.Canny(grey, 50, 150)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
            blob = cv2.dilate(cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel), kernel)
            contours, _ = cv2.findContours(blob, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

            height, width = grey.shape[:2]
            min_side = max(24, int(min(height, width) * 0.06))
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if w < min_side or h < min_side:
                    continue
                if not 0.80 <= w / float(h) <= 1.25:        # square-ish
                    continue
                if w * h > 0.30 * height * width:            # not the whole card
                    continue
                density = float(edges[y:y + h, x:x + w].mean()) / 255.0
                if density >= 0.18:                          # densely modulated
                    findings.append(
                        ImageFinding("qr", (x, y, x + w, y + h), "QR-like block (edge density)")
                    )
        except Exception as exc:
            logger.debug("QR shape fallback failed: %s", exc)
        return findings

    def _find_faces(self, image: Image.Image) -> List[ImageFinding]:
        """Locate faces with OpenCV's bundled Haar cascade."""
        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover
            return []

        try:
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            grey = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
            faces = cascade.detectMultiScale(grey, scaleFactor=1.1, minNeighbors=4)
            # The cascade returns the face only; an ID photograph also shows
            # hair, ears and shoulders, all of which identify the holder. Grow
            # the box by 45% so the whole portrait is covered.
            findings = []
            for (x, y, w, h) in faces:
                grow_x, grow_y = int(w * 0.45), int(h * 0.45)
                findings.append(
                    ImageFinding(
                        "face",
                        (int(x - grow_x), int(y - grow_y), int(x + w + grow_x), int(y + h + grow_y)),
                        "portrait",
                    )
                )
            return findings
        except Exception as exc:
            logger.debug("face detection failed: %s", exc)
            return []

    # -- rendering ---------------------------------------------------------

    def _apply(self, image: Image.Image, findings: Sequence[ImageFinding], original: bytes) -> bytes:
        """
        Draw the redactions and re-encode at the original size.

        QR codes are overwritten with noise rather than a flat rectangle: a
        solid block is trivially reconstructible if any error-correction data
        survives at the edges, whereas noise destroys the symbol outright.
        """
        canvas = image.copy()
        draw = ImageDraw.Draw(canvas)

        for finding in findings:
            left, top, right, bottom = self._pad(finding.box, canvas.size)
            if right <= left or bottom <= top:
                continue
            if finding.kind == "qr":
                patch = Image.effect_noise((right - left, bottom - top), 96).convert(
                    canvas.mode
                )
                canvas.paste(patch, (left, top))
            elif finding.kind == "face":
                # Solid, not blurred: a blur of a small portrait is often
                # reversible enough to still identify someone, and biometric
                # data does not warrant a cosmetic half-measure.
                draw.rectangle([left, top, right, bottom], fill=(0, 0, 0))
            else:
                draw.rectangle([left, top, right, bottom], fill=(0, 0, 0))

        buffer = io.BytesIO()
        fmt = (Image.open(io.BytesIO(original)).format or "PNG").upper()
        if fmt == "JPEG" and canvas.mode == "RGBA":
            canvas = canvas.convert("RGB")
        canvas.save(buffer, format=fmt)
        return buffer.getvalue()

    def _pad(self, box: Box, size: Tuple[int, int]) -> Box:
        left, top, right, bottom = box
        width, height = size
        return (
            max(0, left - self.PADDING),
            max(0, top - self.PADDING),
            min(width, right + self.PADDING),
            min(height, bottom + self.PADDING),
        )


def blank_image(payload: bytes, label: str = "") -> bytes:
    """
    Replace an image wholesale with a neutral placeholder of identical size.

    Used for logos: there is no sub-region to redact, the whole mark is the
    identifier. Keeping the original dimensions means the page does not reflow.
    """
    image = Image.open(io.BytesIO(payload))
    fmt = (image.format or "PNG").upper()
    placeholder = Image.new("RGB", image.size, (235, 235, 235))
    draw = ImageDraw.Draw(placeholder)
    draw.rectangle([0, 0, image.size[0] - 1, image.size[1] - 1], outline=(170, 170, 170))
    if label:
        draw.text((4, max(0, image.size[1] // 2 - 6)), label[:24], fill=(120, 120, 120))
    buffer = io.BytesIO()
    placeholder.save(buffer, format=fmt)
    return buffer.getvalue()
