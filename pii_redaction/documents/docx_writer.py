"""
Rewriting a .docx in place, so the output differs from the source only in its
PII.

The naive approach — read the text, write a new document — destroys the file.
This module edits the original's XML instead, which is what keeps fonts,
tables, images, numbering, headers and page breaks byte-identical.

Two things make that harder than a search-and-replace:

**Word fragments text arbitrarily.** A single sentence is split into runs
whenever formatting, language or a spellcheck marker changes. In this document
paragraphs average 14 runs and one has 424; ``Redwood Family Trust`` is spread
across five. Replacing run by run therefore misses most multi-word entities.
The fix is to match against a paragraph's concatenated text and then write the
result back across the specific runs each match actually covers, leaving every
other run untouched.

**Visible text is not the only text.** PII also lives in ``w:instrText`` field
codes (``HYPERLINK "mailto:..."`` — 117 of them here), in headers and footers,
inside text boxes, in tracked-change deletions, in image alt-text and in the
document properties. Editing only ``w:t`` leaves a document that looks redacted
but whose links still resolve to the real addresses.

Rather than use the object model — which does not expose comments, footnotes or
text boxes uniformly — this walks the package parts directly, so a container
the model does not know about is still covered.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

from lxml import etree

from ..surrogates.matcher import EntityMatcher

__all__ = ["DocxRedactor", "RewriteStats"]

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML = "http://www.w3.org/XML/1998/namespace"

#: Package parts that can carry visible text or field codes.
_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
)
#: Document properties that routinely carry the author's real name.
_METADATA_FIELDS = (
    "{http://purl.org/dc/elements/1.1/}creator",
    "{http://purl.org/dc/elements/1.1/}title",
    "{http://purl.org/dc/elements/1.1/}subject",
    "{http://purl.org/dc/elements/1.1/}description",
    "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy",
    "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}keywords",
    "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Company",
    "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Manager",
)


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


@dataclass
class RewriteStats:
    """What the rewrite actually changed, for reporting and verification."""

    replacements_by_context: Counter = field(default_factory=Counter)
    parts_modified: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.replacements_by_context.values())

    def summary(self) -> Dict[str, int]:
        return dict(self.replacements_by_context)


class DocxRedactor:
    """Applies a real -> surrogate mapping to every text carrier in a .docx."""

    def __init__(self, mapping: Dict[str, str]) -> None:
        self._matcher = EntityMatcher(mapping)

    # -- public API --------------------------------------------------------

    def redact(
        self,
        source: str | Path,
        destination: str | Path,
        redacted_media: Dict[str, bytes] | None = None,
    ) -> RewriteStats:
        """
        Write a redacted copy of ``source`` to ``destination``.

        ``redacted_media`` optionally replaces image parts by name (for example
        ``word/media/image4.png``) with already-redacted bytes, so image and
        text redaction land in a single output file.
        """
        source, destination = Path(source), Path(destination)
        stats = RewriteStats()
        media = redacted_media or {}

        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            payloads = {entry.filename: archive.read(entry.filename) for entry in entries}

        for name in list(payloads):
            if name in media:
                payloads[name] = media[name]
                stats.parts_modified.append(name)
            elif _TEXT_PARTS.match(name):
                rewritten, count = self._rewrite_part(payloads[name], name, stats)
                if count:
                    payloads[name] = rewritten
                    stats.parts_modified.append(name)
            elif name in ("docProps/core.xml", "docProps/app.xml"):
                rewritten, count = self._rewrite_metadata(payloads[name])
                if count:
                    payloads[name] = rewritten
                    stats.replacements_by_context["metadata"] += count
                    stats.parts_modified.append(name)

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the original entry order and compression so the package is
        # structurally identical apart from the parts we rewrote.
        with zipfile.ZipFile(source) as archive, zipfile.ZipFile(
            destination, "w", zipfile.ZIP_DEFLATED
        ) as out:
            for entry in archive.infolist():
                out.writestr(entry, payloads[entry.filename])
        return stats

    # -- part rewriting ----------------------------------------------------

    def _rewrite_part(self, payload: bytes, name: str, stats: RewriteStats):
        root = etree.fromstring(payload)
        count = 0

        # Visible text, one paragraph at a time. w:t and w:delText both render
        # as text the reader (or a change-tracking reviewer) can see.
        for paragraph in root.iter(_w("p")):
            nodes = [
                node
                for node in paragraph.iter(_w("t"), _w("delText"))
                if node is not None
            ]
            count += self._rewrite_nodes(nodes)

        context = "body" if name.endswith("document.xml") else name.split("/")[-1]
        if count:
            stats.replacements_by_context[context] += count

        # Field codes: HYPERLINK targets live here, not in w:t, so a document
        # can display a surrogate address while still linking to the real one.
        field_count = 0
        for node in root.iter(_w("instrText")):
            if not node.text:
                continue
            new_text, hits = self._matcher.apply(node.text)
            if hits:
                node.text = new_text
                field_count += hits
        if field_count:
            stats.replacements_by_context["field_codes"] += field_count

        # Image alt-text and shape names are author-supplied strings.
        alt_count = 0
        for node in root.iter():
            for attribute in ("descr", "name"):
                value = node.get(attribute)
                if not value:
                    continue
                new_value, hits = self._matcher.apply(value)
                if hits:
                    node.set(attribute, new_value)
                    alt_count += hits
        if alt_count:
            stats.replacements_by_context["alt_text"] += alt_count

        total = count + field_count + alt_count
        return (etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), total)

    def _rewrite_nodes(self, nodes: Sequence[etree._Element]) -> int:
        """
        Rewrite one paragraph's text nodes, preserving per-run formatting.

        The match is found on the concatenation of every node, then written
        back only into the nodes it actually covers: the first keeps its prefix
        plus the surrogate, the last keeps its suffix, and any node wholly
        inside the match is emptied. Runs outside the match are never touched,
        which is what preserves bold, italic and font changes elsewhere in the
        paragraph.
        """
        if not nodes:
            return 0

        segments = [node.text or "" for node in nodes]
        combined = "".join(segments)
        replacements = self._matcher.find(combined)
        if not replacements:
            return 0

        bounds = []
        cursor = 0
        for segment in segments:
            bounds.append((cursor, cursor + len(segment)))
            cursor += len(segment)

        # Right to left, so the offsets of not-yet-applied matches stay valid.
        for start, end, surrogate in reversed(replacements):
            covered = [
                index
                for index, (node_start, node_end) in enumerate(bounds)
                if node_start < end and node_end > start
            ]
            if not covered:
                continue

            first, last = covered[0], covered[-1]
            prefix = segments[first][: start - bounds[first][0]]
            suffix = segments[last][end - bounds[last][0]:]

            if first == last:
                segments[first] = prefix + surrogate + suffix
            else:
                segments[first] = prefix + surrogate
                for index in covered[1:-1]:
                    segments[index] = ""
                segments[last] = suffix

        for node, segment in zip(nodes, segments):
            node.text = segment
            # Word collapses leading/trailing spaces unless told not to; without
            # this, removing an entity can silently glue two words together.
            if segment != segment.strip():
                node.set(f"{{{_XML}}}space", "preserve")

        return len(replacements)

    def _rewrite_metadata(self, payload: bytes):
        root = etree.fromstring(payload)
        count = 0
        for node in root.iter():
            if node.tag in _METADATA_FIELDS and node.text:
                new_text, hits = self._matcher.apply(node.text)
                if hits:
                    node.text = new_text
                    count += hits
        return (
            etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True),
            count,
        )
