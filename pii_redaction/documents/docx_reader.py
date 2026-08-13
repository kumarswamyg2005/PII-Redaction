"""
Reading a .docx for analysis.

Detection has to see everything the rewriter can change, or the two disagree
and PII survives in whichever container the reader skipped. Both sides
therefore walk the same package parts and the same ``w:p`` elements — body,
tables, text boxes, headers and footers — so coverage is identical by
construction rather than by careful maintenance of two lists.

Paragraphs are returned individually rather than as one blob because paragraph
boundaries are real boundaries: they stop an entity being detected across the
gap between a table cell and the next heading.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterator, List, Tuple

from lxml import etree

__all__ = ["iter_paragraphs", "read_paragraphs", "read_media", "joined_text"]

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
)


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


def iter_paragraphs(path: str | Path) -> Iterator[Tuple[str, str]]:
    """Yield ``(part_name, paragraph_text)`` for every paragraph in the package."""
    with zipfile.ZipFile(Path(path)) as archive:
        for name in archive.namelist():
            if not _TEXT_PARTS.match(name):
                continue
            root = etree.fromstring(archive.read(name))
            for paragraph in root.iter(_w("p")):
                text = "".join(
                    node.text or "" for node in paragraph.iter(_w("t"), _w("delText"))
                )
                if text.strip():
                    yield name, text


def read_paragraphs(path: str | Path) -> List[str]:
    """Every non-empty paragraph in the document, in package order."""
    return [text for _, text in iter_paragraphs(path)]


def joined_text(paragraphs: List[str]) -> str:
    """One string for the analyzer, with paragraph boundaries preserved."""
    return "\n".join(paragraphs)


def read_field_codes(path: str | Path) -> List[str]:
    """
    Every ``w:instrText`` field instruction in the package.

    Hyperlink targets live here rather than in the visible text, and some of
    them appear nowhere else: this document links ``ksh@portal.registrar.example.com``
    without ever printing it. Analysing only what is visible leaves that
    address undetected, so the rewriter has nothing to replace it with and the
    link keeps resolving to a real mailbox.
    """
    codes: List[str] = []
    with zipfile.ZipFile(Path(path)) as archive:
        for name in archive.namelist():
            if not _TEXT_PARTS.match(name):
                continue
            root = etree.fromstring(archive.read(name))
            for node in root.iter(_w("instrText")):
                if node.text and node.text.strip():
                    codes.append(node.text.strip())
    return codes


def iter_table_rows(path: str | Path) -> Iterator[List[str]]:
    """
    Yield each table row as a list of cell strings.

    Offer documents put their glossary in a two-column "Term | Description"
    table, so the terms are only recoverable from the table structure — read as
    a flat paragraph stream they arrive as alternating, unrelated lines.
    """
    with zipfile.ZipFile(Path(path)) as archive:
        for name in archive.namelist():
            if not _TEXT_PARTS.match(name):
                continue
            root = etree.fromstring(archive.read(name))
            for row in root.iter(_w("tr")):
                cells: List[str] = []
                for cell in row.iterfind(_w("tc")):
                    text = "".join(
                        node.text or "" for node in cell.iter(_w("t"), _w("delText"))
                    )
                    cells.append(" ".join(text.split()))
                if any(cells):
                    yield cells


def read_media(path: str | Path) -> List[Tuple[str, bytes]]:
    """Every embedded image, as ``(part_name, bytes)``."""
    with zipfile.ZipFile(Path(path)) as archive:
        return [
            (name, archive.read(name))
            for name in archive.namelist()
            if name.startswith("word/media/")
        ]
