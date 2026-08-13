"""
End-to-end verification of a redacted .docx.

Checks the things a metric cannot: that the document is structurally identical
to its source, that no known PII survives in text, hyperlinks or images, and
that no two entities share a surrogate. Exits non-zero if anything fails.

    python verify_redaction.py [original.docx] [redacted.docx]
"""
import io, json, pathlib, re, sys, zipfile
from lxml import etree
from PIL import Image
import pytesseract

# Paths are relative to the repository root, and overridable, so this runs on
# any machine:  python verify_redaction.py [original.docx] [redacted.docx]
SRC = sys.argv[1] if len(sys.argv) > 1 else "Red Herring Prospectus.docx"
OUT = sys.argv[2] if len(sys.argv) > 2 else "Red Herring Prospectus - REDACTED.docx"

VALUES = pathlib.Path("ground_truth_values.json")


def load_probes(section: str = "known_pii") -> list:
    """
    Read the real values to search for from the un-committed values file.

    Returns an empty list when the file is absent, so the script still runs for
    anyone who has the code but not the source document; those checks simply
    report as skipped rather than failing misleadingly.
    """
    if not VALUES.exists():
        return []
    payload = json.loads(VALUES.read_text())
    if section == "known_pii":
        values = [v for group in payload["known_pii"].values() for v in group]
        return sorted({v for v in values if len(v) > 6})
    return payload.get(section, [])
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
def w(t): return f"{{{W}}}{t}"

ok = True
def check(label, condition, detail=""):
    global ok
    ok &= bool(condition)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label} {detail}")

def parts_text(path):
    out = {}
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if re.match(r"^word/(document|header\d*|footer\d*)\.xml$", n):
                root = etree.fromstring(z.read(n))
                out[n] = "\n".join(
                    "".join(t.text or "" for t in p.iter(w("t"), w("delText")))
                    for p in root.iter(w("p"))
                )
    return out

def counts(path):
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
        return {
            "paragraphs": len(list(root.iter(w("p")))),
            "tables": len(list(root.iter(w("tbl")))),
            "rows": len(list(root.iter(w("tr")))),
            "cells": len(list(root.iter(w("tc")))),
            "runs": len(list(root.iter(w("r")))),
            "drawings": len(list(root.iter(f"{{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}}inline"))),
            "sections": len(list(root.iter(w("sectPr")))),
            "breaks": len(list(root.iter(w("br")))),
            "parts": len(zipfile.ZipFile(path).namelist()),
        }

print("=" * 68)
print("1. STRUCTURAL IDENTITY  (document must be unchanged apart from PII)")
print("=" * 68)
a, b = counts(SRC), counts(OUT)
for k in a:
    check(f"{k}: {a[k]} -> {b[k]}", a[k] == b[k])

# The XML element counts above stayed equal even while redaction was emptying
# three table-of-contents paragraphs, because an emptied <w:p> is still a
# <w:p>. Counting paragraphs that still carry text is what catches that.
def non_empty_paragraphs(path):
    return sum(1 for text in parts_text(path).values() for line in text.split("\n") if line.strip())

src_text_paras, out_text_paras = non_empty_paragraphs(SRC), non_empty_paragraphs(OUT)
check(
    f"paragraphs still carrying text: {src_text_paras} -> {out_text_paras}",
    src_text_paras == out_text_paras,
    "" if src_text_paras == out_text_paras else "(redaction emptied paragraphs)",
)

print("\n" + "=" * 68)
print("2. TEXT PII REMOVED")
print("=" * 68)
out_text = "\n".join(parts_text(OUT).values())
src_text = "\n".join(parts_text(SRC).values())
# The values to search for are the real ones, so they live in the git-ignored
# ground_truth_values.json rather than in this file. A verification script that
# carries a list of somebody's name, address, e-mail and CIN is a compact
# dossier, and committing it would undo the redaction it exists to confirm.
must_zero = load_probes()
residual = load_probes(section="known_residual") or {}
if not must_zero:
    print("  SKIPPED — ground_truth_values.json not present; see build_ground_truth.py")

# Known residuals are declared, not hidden. Each is a value the tool does not
# remove, with the reason and the count seen when it was last reviewed. The
# check fails if one gets worse, or if anything not on the list leaks — so the
# list cannot quietly absorb a new regression.
for t in must_zero:
    n = out_text.count(t)
    if t in residual:
        allowed = residual[t]["count"]
        check(f"{t!r} residual within baseline ({residual[t]['reason']})",
              n <= allowed, f"(baseline {allowed}, now {n})")
    else:
        check(f"{t!r} absent", n == 0, f"(was {src_text.count(t)}, now {n})")

print("\n" + "=" * 68)
print("3. LEGITIMATE CONTENT PRESERVED")
print("=" * 68)
must_keep = ["ASBA", "QIB", "Anchor Investor", "Working Day", "Board", "SEBI",
             "BSE", "NSE", "Equity Shares", "Red Herring Prospectus", "Allotment"]
for t in must_keep:
    n = out_text.count(t)
    check(f"{t!r} preserved", n > 0, f"({src_text.count(t)} -> {n})")

print("\n" + "=" * 68)
print("4. HYPERLINK FIELD CODES")
print("=" * 68)
with zipfile.ZipFile(OUT) as z:
    doc = z.read("word/document.xml").decode("utf8", "ignore")
for t in load_probes(section="link_targets"):
    check(f"field target {t!r} gone", doc.count(t) == 0)
check("surrogate links present", "example.com" in doc)

print("\n" + "=" * 68)
print("5. IMAGE PII (OCR of the redacted output)")
print("=" * 68)
# Same reasoning: the ID-card values are read from the ignored file. Publishing
# an Aadhaar number is an offence under the Aadhaar Act, and these belong to
# private individuals rather than to the issuer.
SECRETS = load_probes(section="image_secrets")
with zipfile.ZipFile(OUT) as z:
    media = [n for n in z.namelist() if n.startswith("word/media/")]
    for n in media:
        txt = pytesseract.image_to_string(Image.open(io.BytesIO(z.read(n))), lang="eng+hin")
        leaked = [s for s in SECRETS if s.lower() in txt.lower()]
        check(f"{n} clean", not leaked, f"leaked={leaked}" if leaked else "")

print("\n" + "=" * 68)
print("6. SURROGATE MAPPING INTEGRITY")
print("=" * 68)
m = json.load(open("entity_mapping.json"))
rev = {}
for real, fake in m.items():
    rev.setdefault(fake, []).append(real)
shared = {f: r for f, r in rev.items() if len(r) > 1}
# Variants of one entity share a surrogate on purpose ("Exemplar" and "Exemplar
# Wealth Management Limited"). A real collision is two *unrelated* entities
# sharing one, i.e. neither name contains the other.
def related(names):
    """
    True when every name is a variant of the same entity.

    Two tests, because entities are written two ways. Multi-word names are
    compared as whole phrases, so "KSH International" relates to "KSH
    International Limited" but not to an unrelated company that merely shares a
    word. Addresses and URLs have no spaces at all, so they are compared by
    plain containment after stripping scheme, "www." and punctuation — which is
    what makes "bajajfinance.com", "www.bajajfinance.com" and
    "http://www.bajajfinance.com/" one entity rather than three collisions.
    """
    def host(value):
        cleaned = re.sub(r"\s+", "", value.strip().lower())
        return re.sub(r'^[\"\']*(?:https?://)?(?:www\.)?|[./\"\']*$', "", cleaned)

    def linked(a, b):
        if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
            return True
        ha, hb = host(a), host(b)
        return bool(ha) and bool(hb) and (ha in hb or hb in ha)

    return all(any(linked(a, b) for b in names if b != a) for a in names)
real_collisions = {f: r for f, r in shared.items() if not related(r)}
check(f"{len(m)} names -> {len(rev)} surrogates; {len(shared)} intentional aliases",
      not real_collisions,
      f"UNRELATED entities sharing a surrogate: {list(real_collisions.items())[:3]}"
      if real_collisions else "")

print("\n" + "=" * 68)
print("7. NO REAL DOMAINS SURVIVING ANYWHERE")
print("=" * 68)
hosts = re.findall(r"[a-z0-9.-]+\.(?:com|in|co\.in|org|net)\b", out_text, re.I)
from collections import Counter
allowed = re.compile(r"(example\.com|sebi\.gov\.in|bseindia\.com|nseindia\.com|fbil\.org\.in|uidai\.gov\.in)$", re.I)
bad = {h: c for h, c in Counter(h.lower() for h in hosts).items() if not allowed.search(h)}
check("only public/regulator/surrogate hosts remain", not bad, str(sorted(bad.items())[:8]))

print("\n" + "=" * 68)
print("RESULT:", "ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
print("=" * 68)

sys.exit(0 if ok else 1)
