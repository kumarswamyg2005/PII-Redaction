"""
PII redaction for Word documents.

Reads a ``.docx``, finds personally identifiable information, and writes a copy
in which every real value is replaced by a consistent fake one — leaving the
document otherwise byte-identical: same fonts, tables, images, headers and page
breaks.

Typical use::

    from pii_redaction import redact_document

    outcome = redact_document("prospectus.docx", "prospectus-redacted.docx")
    print(outcome.entity_counts, outcome.total_replacements)

The moving parts, if you need them individually:

* :mod:`pii_redaction.detection` — recognizers, checksum validators, policy
* :mod:`pii_redaction.surrogates` — consistent real -> fake mapping
* :mod:`pii_redaction.documents` — reading and rewriting the package, images
* :mod:`pii_redaction.evaluation` — scoring against annotated ground truth
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .pipeline import RedactionOutcome, RedactionPipeline

__all__ = ["redact_document", "RedactionPipeline", "RedactionOutcome", "__version__"]

__version__ = "3.0.0"


def redact_document(
    source: str | Path,
    destination: str | Path,
    pipeline: Optional[RedactionPipeline] = None,
    **options,
) -> RedactionOutcome:
    """
    Redact one document.

    Pass an existing ``pipeline`` to reuse a loaded model across many
    documents; building one per call reloads the transformer each time.
    """
    return (pipeline or RedactionPipeline(**options)).run(source, destination)
