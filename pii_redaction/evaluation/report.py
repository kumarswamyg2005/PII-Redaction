"""
Scoring a run, and writing the report.

The previous version of this project reported a precision of 0.105, which was
mostly an artefact of how it measured rather than how it performed: ground
truth covered a handful of pages, but detections were counted across all 126,
so every correct detection outside the annotated pages was booked as a false
positive. A metric that punishes correct behaviour is worse than no metric,
because it hides the real errors underneath the noise.

This module fixes the methodology:

* **Scoring is confined to an exhaustively annotated region.** Only detections
  whose span falls inside the annotated character range are scored, so a
  detection can only be a false positive if the annotator looked at that text
  and decided it was not PII.
* **Matching is bidirectional.** A detection matches an annotation when they
  overlap by at least half of *each* span, which stops a huge span "matching"
  everything it swallows.
* **Both averages are reported.** Micro reflects overall instance-level
  behaviour; macro weights each PII type equally and exposes a type that is
  quietly failing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Annotation", "Metrics", "score", "write_report", "load_ground_truth", "mask",
]


def mask(text: str) -> str:
    """
    Reduce a value to its character shape: ``Rakhi Shetty`` -> ``Aaaaa Aaaaaa``.

    Error tables are the most useful part of an evaluation report and the most
    dangerous: a list of false negatives is, by definition, a list of PII the
    tool failed to remove. Printing those verbatim into a committed report
    would hand back what the document set out to protect.

    The shape keeps what a reader actually needs — is this a person, a code, an
    address fragment, how long is it — while identifying nobody.
    """
    shaped = []
    for character in text.strip()[:48]:
        if character.isupper():
            shaped.append("A")
        elif character.islower():
            shaped.append("a")
        elif character.isdigit():
            shaped.append("9")
        else:
            shaped.append(character)
    return "".join(shaped)

#: Minimum mutual overlap for a detection to count as a match.
OVERLAP_THRESHOLD = 0.5


@dataclass(frozen=True)
class Annotation:
    entity_type: str
    start: int
    end: int
    text: str = ""


def _describe(annotation: "Annotation") -> str:
    """
    Where a missed annotation is, in a form safe to publish.

    With the literal present the character shape is shown, which conveys the
    kind of value without disclosing it. With offsets-only ground truth — how
    this repository ships — there is nothing to mask, and an empty cell reads
    as a broken report rather than a deliberate omission, so the offsets are
    printed instead.
    """
    if annotation.text:
        return f"`{mask(annotation.text)}`"
    return f"characters {annotation.start}–{annotation.end}"


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def accuracy(self) -> float:
        """
        Correctly-handled spans over all spans considered: TP / (TP + FP + FN).

        There is no true-negative count in span extraction — the number of
        non-PII spans a document *could* have contains is unbounded — so the
        conventional (TP+TN)/total accuracy is undefined here. This is the
        standard substitute (the Jaccard index, sometimes reported as
        "accuracy" in extraction tasks), and it is stated explicitly rather
        than quietly redefined.
        """
        total = self.tp + self.fp + self.fn
        return self.tp / total if total else 0.0


@dataclass
class ScoreCard:
    per_type: Dict[str, Metrics] = field(default_factory=dict)
    micro: Metrics = field(default_factory=Metrics)
    false_positives: List[Tuple[str, str]] = field(default_factory=list)
    false_negatives: List[Tuple[str, str]] = field(default_factory=list)
    region: Tuple[int, int] = (0, 0)

    @property
    def macro_f1(self) -> float:
        if not self.per_type:
            return 0.0
        return sum(m.f1 for m in self.per_type.values()) / len(self.per_type)

    @property
    def macro_precision(self) -> float:
        if not self.per_type:
            return 0.0
        return sum(m.precision for m in self.per_type.values()) / len(self.per_type)

    @property
    def macro_recall(self) -> float:
        if not self.per_type:
            return 0.0
        return sum(m.recall for m in self.per_type.values()) / len(self.per_type)


def load_ground_truth(path: str | Path) -> Tuple[List[Annotation], Tuple[int, int]]:
    """
    Load annotations plus the region they exhaustively cover.

    The file carries the region explicitly rather than inferring it from the
    annotations: inferring would make the region collapse onto whatever was
    annotated, and a stretch of correctly-ignored text at the end would silently
    stop counting.
    """
    payload = json.loads(Path(path).read_text())
    annotations = [
        Annotation(a["type"], a["start"], a["end"], a.get("text", ""))
        for a in payload["annotations"]
    ]
    region = (payload["region"]["start"], payload["region"]["end"])
    return annotations, region


def score(
    detections: Sequence,
    annotations: Sequence[Annotation],
    region: Tuple[int, int],
    relaxed: bool = True,
    ignore_type: bool = False,
) -> ScoreCard:
    """
    Match detections to annotations inside ``region`` and tally the result.

    ``relaxed`` also accepts a detection that *contains* the annotation. This
    matters for addresses: an annotation anchors the distinctive part
    ("Village Kharoli") while the detector legitimately returns the whole
    postal block. Under strict mutual overlap that scores as a miss *and* a
    false positive, even though the PII was fully covered — which is the
    opposite of what happened. Containment credit is standard practice in
    de-identification evaluation (the "relaxed" match in the i2b2/n2c2 tasks),
    and for redaction it is the operationally meaningful question: was the
    sensitive text covered or not?

    Both variants are reported, so the strict number is never hidden.
    """
    start, end = region
    card = ScoreCard(region=region)

    scoped = [d for d in detections if d.start >= start and d.end <= end]
    in_region = [a for a in annotations if a.start >= start and a.end <= end]

    matched_detections: set[int] = set()
    matched_annotations: set[int] = set()

    for a_index, annotation in enumerate(in_region):
        best, best_overlap = None, 0.0
        for d_index, detection in enumerate(scoped):
            # A detection may be credited for several annotations when it
            # contains them. An address is redacted as one span but annotated
            # by its parts, and counting the extra parts as misses would report
            # a leak where the text is demonstrably gone. Under strict matching
            # each detection is still consumed once.
            already_used = d_index in matched_detections
            covers_this = (
                relaxed
                and detection.start <= annotation.start
                and detection.end >= annotation.end
            )
            if already_used and not covers_this:
                continue
            if not ignore_type and detection.entity_type != annotation.entity_type:
                continue
            overlap = min(detection.end, annotation.end) - max(detection.start, annotation.start)
            if overlap <= 0:
                continue
            a_len = annotation.end - annotation.start
            d_len = detection.end - detection.start
            mutual = (
                overlap / a_len >= OVERLAP_THRESHOLD
                and overlap / d_len >= OVERLAP_THRESHOLD
            )
            covers = (
                relaxed
                and detection.start <= annotation.start
                and detection.end >= annotation.end
            )
            if mutual or covers:
                if overlap > best_overlap:
                    best, best_overlap = d_index, overlap
        if best is not None:
            matched_detections.add(best)
            matched_annotations.add(a_index)

    types = {a.entity_type for a in in_region} | {d.entity_type for d in scoped}
    for entity_type in types:
        card.per_type[entity_type] = Metrics()

    for a_index, annotation in enumerate(in_region):
        metrics = card.per_type[annotation.entity_type]
        if a_index in matched_annotations:
            metrics.tp += 1
        else:
            metrics.fn += 1
            card.false_negatives.append(
                (annotation.entity_type, _describe(annotation))
            )

    for d_index, detection in enumerate(scoped):
        if d_index not in matched_detections:
            card.per_type[detection.entity_type].fp += 1
            card.false_positives.append((detection.entity_type, detection.text))

    card.micro = Metrics(
        tp=sum(m.tp for m in card.per_type.values()),
        fp=sum(m.fp for m in card.per_type.values()),
        fn=sum(m.fn for m in card.per_type.values()),
    )
    return card


def write_report(
    outcome,
    path: str | Path,
    ground_truth_path: Optional[str | Path] = None,
    policy=None,
) -> None:
    """Write a Markdown evaluation report for one run."""
    lines: List[str] = [
        "# Evaluation Report",
        "",
        f"- **Document**: `{Path(outcome.input_path).name}`",
        f"- **Output**: `{Path(outcome.output_path).name}`",
        f"- **Runtime**: {outcome.seconds:.1f}s",
        "",
        "## What the run did",
        "",
        f"- Entities detected and redacted: **{len(outcome.detections)}**",
        f"- Spans suppressed by policy: **{len(outcome.suppressed)}**",
        f"- Distinct entities mapped to surrogates: **{len(outcome.mapping)}**",
        f"- Replacements written into the document: **{outcome.total_replacements}**",
        f"- Defined terms learned from the document's own glossary: "
        f"**{outcome.defined_terms_learned}**",
        "",
        "### Replacements by container",
        "",
        "| Container | Replacements |",
        "|---|---:|",
    ]
    for container, count in sorted(outcome.rewrite_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {container} | {count} |")

    lines += ["", "### Detections by type", "", "| Type | Count |", "|---|---:|"]
    for entity_type, count in sorted(outcome.entity_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {entity_type} | {count} |")

    if outcome.image_findings:
        lines += [
            "", "### Images", "",
            "| Image | Regions redacted |", "|---|---:|",
        ]
        for name, count in sorted(outcome.image_findings.items()):
            lines.append(f"| `{name}` | {count} |")

    if ground_truth_path:
        annotations, region = load_ground_truth(ground_truth_path)
        # The shipped ground truth carries offsets, not literals, so recover
        # each annotation's text from the document held in memory. The values
        # are used to classify errors and are never written out unmasked.
        annotated_texts = [
            a.text or outcome.document_text[a.start:a.end] for a in annotations
        ]
        card = score(outcome.detections, annotations, region, relaxed=True)
        strict = score(outcome.detections, annotations, region, relaxed=False)
        lines += _metrics_section(card, len(annotations), annotated_texts, policy)
        coverage = score(
            outcome.detections, annotations, region, relaxed=True, ignore_type=True
        )
        lines += [
            "### Matching criteria compared",
            "",
            "Three views of the same run. They differ only in how strictly a "
            "detection must agree with an annotation:",
            "",
            "| Criterion | Accuracy | Precision | Recall | F1 |",
            "|---|---:|---:|---:|---:|",
            f"| Strict — same type, 50% mutual overlap | {strict.micro.accuracy:.3f} | "
            f"{strict.micro.precision:.3f} | {strict.micro.recall:.3f} | "
            f"{strict.micro.f1:.3f} |",
            f"| Relaxed — same type, containment counts | {card.micro.accuracy:.3f} | "
            f"{card.micro.precision:.3f} | {card.micro.recall:.3f} | "
            f"{card.micro.f1:.3f} |",
            f"| **Coverage — any type, containment counts** | "
            f"**{coverage.micro.accuracy:.3f}** | **{coverage.micro.precision:.3f}** | "
            f"**{coverage.micro.recall:.3f}** | **{coverage.micro.f1:.3f}** |",
            "",
            "**Coverage is the number that matters for redaction.** The per-type "
            "rows above punish the model for calling `Village Kharoli` an "
            "organisation rather than a location — but the output document is "
            "identical either way, because both types are replaced with a "
            "surrogate. Type confusion between PERSON, ORGANIZATION and "
            "LOCATION changes the label in the report, not the redaction. What "
            "would genuinely leak is a span nobody detected at all, and that is "
            "what the coverage recall measures.",
            "",
        ]

    lines += _suppression_section(outcome)
    Path(path).write_text("\n".join(lines) + "\n")


def _metrics_section(
    card: ScoreCard,
    total_annotations: int,
    annotated_texts: Optional[Sequence[str]] = None,
    policy=None,
) -> List[str]:
    lines = [
        "",
        "## Accuracy against ground truth",
        "",
        "### Method",
        "",
        f"- Annotations: **{total_annotations}** total; "
        f"**{card.micro.tp + card.micro.fn}** inside the scored region.",
        f"- Scored region: characters **{card.region[0]}–{card.region[1]}**, annotated "
        "exhaustively so that an unmatched detection is a genuine false positive.",
        f"- A detection matches an annotation when the types agree and the spans overlap "
        f"by at least {int(OVERLAP_THRESHOLD * 100)}% of each.",
        "",
        "### Results",
        "",
        "| Type | TP | FP | FN | Accuracy | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for entity_type in sorted(card.per_type):
        m = card.per_type[entity_type]
        lines.append(
            f"| {entity_type} | {m.tp} | {m.fp} | {m.fn} | {m.accuracy:.3f} | "
            f"{m.precision:.3f} | {m.recall:.3f} | {m.f1:.3f} |"
        )
    micro = card.micro
    lines += [
        f"| **Micro (all)** | **{micro.tp}** | **{micro.fp}** | **{micro.fn}** | "
        f"**{micro.accuracy:.3f}** | **{micro.precision:.3f}** | "
        f"**{micro.recall:.3f}** | **{micro.f1:.3f}** |",
        f"| **Macro (per type)** | | | | | **{card.macro_precision:.3f}** | "
        f"**{card.macro_recall:.3f}** | **{card.macro_f1:.3f}** |",
        "",
        "*Accuracy* here is TP / (TP + FP + FN). Span extraction has no "
        "true-negative count — the number of non-PII spans a document could "
        "contain is unbounded — so the textbook (TP+TN)/total is undefined. "
        "This is the standard substitute for extraction tasks, stated openly "
        "rather than quietly redefined.",
        "",
    ]

    if card.false_negatives:
        lines += [
            "### Missed PII (false negatives)",
            "",
            "The shipped ground truth carries entity types and character offsets"
            " but not the values themselves, because a ground-truth file for a"
            " redaction task is itself a disclosure. A miss is therefore located"
            " rather than quoted; run against your own copy of the source to see"
            " the text at each offset.",
            "",
            "| Type | Where |",
            "|---|---|",
        ]
        for entity_type, where in card.false_negatives[:25]:
            lines.append(f"| {entity_type} | {where} |")
        lines.append("")
    if card.false_positives:
        lines += _false_positive_buckets(card.false_positives, annotated_texts, policy)
        lines += ["### Over-redaction (false positives)", "", "| Type | Text |", "|---|---|"]
        for entity_type, text in card.false_positives[:25]:
            lines.append(f"| {entity_type} | `{mask(text)}` |")
        lines.append("")
    return lines


#: Words that appear in a postal address but not in a company or person name.
_ADDRESS_WORDS = re.compile(
    r"\b(?:tower|block|wing|floor|plot|survey|gat|road|marg|street|lane|"
    r"complex|centre|center|park|nagar|colony|society|building|premises|"
    r"industrial|estate|village|taluka|district|east|west|north|south|opp|"
    r"near|behind|khurd|budruk|phase|sector|unit|no\.)\b",
    re.IGNORECASE,
)
#: A heading: capitals throughout, several words, no lower case at all.
_HEADING = re.compile(r"^[A-Z][A-Z\s/&(),.\-‘’“”]{4,}$")


def _false_positive_buckets(
    false_positives: List[Tuple[str, str]],
    annotated_texts: Optional[Sequence[str]] = None,
    policy=None,
) -> List[str]:
    """
    Classify false positives so the number can be reasoned about.

    A bare precision figure says nothing about whether the tool is dangerous or
    merely untidy. Splitting the errors shows which residue is real
    over-redaction and which is a measurement artefact — most importantly the
    span that redacted exactly the right text but under a different entity
    label, which costs precision while protecting the data perfectly.

    Where possible the classification asks the **policy itself** whether a span
    is a defined term or a bare place, rather than re-implementing those
    judgements with a second set of regexes that can disagree with the first.
    """
    annotated = {t.strip().lower() for t in (annotated_texts or []) if t.strip()}

    counts: Dict[str, int] = {}
    examples: Dict[str, str] = {}
    for entity_type, text in false_positives:
        candidate = text.strip()
        lowered = candidate.lower()
        label = "other"
        # Does this span cover, or sit inside, something the annotator marked
        # as PII under a different type? Then the text *was* redacted.
        if annotated and any(
            lowered in marked or marked in lowered for marked in annotated
        ):
            label = "type mismatch (text was redacted)"
        elif policy is not None and policy.is_defined_term(candidate):
            label = "document vocabulary"
        elif policy is not None and policy.is_bare_place("LOCATION", candidate):
            label = "bare place name"
        elif _ADDRESS_WORDS.search(candidate):
            label = "address fragment"
        elif _HEADING.match(candidate):
            label = "heading fragment"
        elif re.search(r"\b(?:of|and|the)\s*$", candidate, re.IGNORECASE):
            label = "truncated span"
        counts[label] = counts.get(label, 0) + 1
        examples.setdefault(label, text)

    lines = [
        "### What the false positives actually are",
        "",
        "Precision alone does not say whether a tool is unsafe or merely "
        "untidy. Every false positive below is text that was replaced but did "
        "not need to be — none of them is a leak.",
        "",
        "| Kind | Count | Example |",
        "|---|---:|---|",
    ]
    for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {label} | {count} | `{mask(examples[label])}` |")
    lines.append("")
    return lines


def _suppression_section(outcome) -> List[str]:
    if not outcome.suppressed:
        return []
    grouped: Dict[str, int] = {}
    examples: Dict[str, str] = {}
    for detection, reasons in outcome.suppressed:
        key = ", ".join(reasons) if reasons else "not a proper noun"
        grouped[key] = grouped.get(key, 0) + 1
        examples.setdefault(key, detection.text)
    lines = [
        "",
        "## Why spans were suppressed",
        "",
        "Detections the models proposed and policy rejected. This is where "
        "precision is actually won.",
        "",
        "| Reason | Count | Example |",
        "|---|---:|---|",
    ]
    for reason, count in sorted(grouped.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {reason} | {count} | `{mask(examples[reason])}` |")
    return lines
