"""Command-line entry point: ``python -m pii_redaction``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .evaluation.report import write_report
from .pipeline import RedactionPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pii_redaction",
        description="Redact PII from a .docx, preserving the original formatting.",
    )
    parser.add_argument("source", type=Path, help="input .docx")
    parser.add_argument("destination", type=Path, nargs="?", help="output .docx")
    parser.add_argument(
        "--no-gliner", action="store_true",
        help="disable zero-shot NER (patterns and validators only); used for the ablation",
    )
    parser.add_argument("--no-images", action="store_true", help="skip image redaction")
    parser.add_argument("--no-logos", action="store_true", help="keep company logos")
    parser.add_argument(
        "--mapping", type=Path,
        help="write the real->surrogate mapping as JSON (a re-identification key)",
    )
    parser.add_argument(
        "--ground-truth", type=Path, help="annotations to score this run against"
    )
    parser.add_argument("--report", type=Path, help="write an evaluation report here")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.source.exists():
        print(f"error: no such file: {args.source}", file=sys.stderr)
        return 2
    if args.source.suffix.lower() != ".docx":
        print("error: input must be a .docx", file=sys.stderr)
        return 2

    destination = args.destination or args.source.with_name(
        f"{args.source.stem}-redacted.docx"
    )

    pipeline = RedactionPipeline(
        use_gliner=not args.no_gliner,
        redact_images=not args.no_images,
        redact_logos=not args.no_logos,
    )
    outcome = pipeline.run(args.source, destination)

    print(f"wrote {destination}")
    print(f"  entities detected : {len(outcome.detections)}")
    print(f"  suppressed by policy: {len(outcome.suppressed)}")
    print(f"  distinct surrogates : {len(outcome.mapping)}")
    print(f"  replacements written: {outcome.total_replacements} {outcome.rewrite_counts}")
    if outcome.image_findings:
        print(f"  images redacted     : {len(outcome.image_findings)}")
    print(f"  elapsed             : {outcome.seconds:.1f}s")

    if args.mapping:
        args.mapping.write_text(json.dumps(outcome.mapping, indent=2, sort_keys=True))
        print(f"  mapping written to  : {args.mapping} (keep private)")

    if args.report:
        write_report(outcome, args.report, ground_truth_path=args.ground_truth,
                     policy=outcome.policy)
        print(f"  report written to   : {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
