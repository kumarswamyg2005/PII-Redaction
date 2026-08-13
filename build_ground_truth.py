#!/usr/bin/env python3
"""
Build the evaluation ground truth.

Annotations are produced by *locating* known PII strings in the document text,
not by asking the pipeline what it found — otherwise the system would be marking
its own homework and recall would be 1.0 by construction.

The region is annotated **exhaustively**: it covers the cover page and the
banker/registrar contact blocks, where every name, address, e-mail, phone number
and identifier was listed by hand from the source. That exhaustiveness is what
makes precision meaningful — inside this range, any detection that is not an
annotation is genuinely an over-redaction.

**Where the literal values live, and why they are not here.** The list of real
names, addresses and identifiers is read from ``ground_truth_values.json``,
which is deliberately *not* committed. A ground-truth file for a redaction task
is itself a PII disclosure: publishing it beside the redacted document would
hand back exactly what the tool removed. What ships instead is
``ground_truth.json`` carrying entity types and character offsets only — enough
to reproduce every number in the evaluation, and nothing that identifies anyone.

Run:  python build_ground_truth.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from pii_redaction.documents.docx_reader import joined_text, read_paragraphs

SOURCE = Path("Red Herring Prospectus.docx")
OUTPUT = Path("ground_truth.json")
VALUES = Path("ground_truth_values.json")


def load_values() -> dict[str, list[str]]:
    """
    Read the literal PII values, merging the primary list and its variants.

    Variants are alternative surface forms of values already listed — the same
    company written without a space, a phone number with different spacing.
    They are recorded separately only to keep the primary list readable.
    """
    if not VALUES.exists():
        print(
            f"error: {VALUES} not found.\n"
            "It holds the literal PII values and is intentionally excluded from\n"
            "version control. Recreate it from the source document to rebuild\n"
            "the ground truth; the committed ground_truth.json already carries\n"
            "the offsets needed to reproduce the evaluation.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    payload = json.loads(VALUES.read_text())
    combined: dict[str, list[str]] = {
        entity_type: list(values)
        for entity_type, values in payload["known_pii"].items()
    }
    for entity_type, values in payload.get("variants", {}).items():
        combined.setdefault(entity_type, []).extend(values)
    return combined


def main() -> int:
    text = joined_text(read_paragraphs(SOURCE))

    # The region runs from the start of the document to the end of the
    # banker/registrar contact blocks — the PII-dense front matter.
    anchor = text.find("Bankers to the Offer")
    if anchor == -1:
        anchor = min(len(text), 60_000)
    region_start, region_end = 0, min(len(text), anchor + 6_000)

    annotations, seen = [], set()
    for entity_type, values in load_values().items():
        for value in values:
            pattern = re.compile(
                r"(?<![A-Za-z0-9])" + r"\s+".join(re.escape(w) for w in value.split()),
                re.IGNORECASE,
            )
            for match in pattern.finditer(text, region_start, region_end):
                key = (match.start(), match.end())
                if key in seen:
                    continue
                seen.add(key)
                # Offsets and type only. The matched text is deliberately not
                # recorded — see the module docstring.
                annotations.append({
                    "type": entity_type,
                    "start": match.start(),
                    "end": match.end(),
                    "length": match.end() - match.start(),
                })

    annotations.sort(key=lambda a: a["start"])
    OUTPUT.write_text(json.dumps(
        {
            "document": SOURCE.name,
            "region": {"start": region_start, "end": region_end},
            "note": (
                "Annotated exhaustively over the region: every PII instance in "
                "this character range is listed, so an unmatched detection "
                "inside it is a true false positive. Entity types and offsets "
                "only — the literal values are held out of version control "
                "because a ground truth for a redaction task is itself a PII "
                "disclosure."
            ),
            "annotations": annotations,
        },
        indent=2,
    ))

    by_type: dict[str, int] = {}
    for annotation in annotations:
        by_type[annotation["type"]] = by_type.get(annotation["type"], 0) + 1
    print(f"region: chars {region_start}-{region_end}")
    print(f"annotations: {len(annotations)} (offsets only, no literal values)")
    for entity_type, count in sorted(by_type.items()):
        print(f"  {entity_type:16} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
