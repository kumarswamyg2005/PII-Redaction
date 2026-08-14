# PII Redaction for Word Documents

Reads a `.docx`, replaces every piece of personally identifiable information
with a consistent fake value, and writes back **the same document** — same
fonts, tables, images, headers and page breaks, byte-identical structure.

**Live demo:** https://unsaved-deodorize-copious.ngrok-free.dev

> Hosted from a laptop through an ngrok tunnel, so it answers only while that
> machine is awake and online — treat it as a demo window, not an uptime
> claim. If it does not load, everything it shows is reproducible locally in
> two commands (see [Running it](#running-it)); nothing about the results
> depends on the tunnel. Upload `synthetic_test.docx` for a result in seconds;
> a 126-page prospectus takes around two minutes, and the page counts the
> seconds up rather than leaving you guessing.

```bash
# Runs on a clean clone — the synthetic document ships with the repo.
python -m pii_redaction synthetic_test.docx out.docx \
    --report report.md --ground-truth synthetic_ground_truth.json
```

The supplied prospectus is **not** in this repository: it contains real
personal data, so it is git-ignored along with the surrogate mapping and the
literal ground-truth values. Point the same command at your own copy to
reproduce the headline numbers. Everything else — code, tests, both reports,
and the redacted output — is here.

---

## Summary

**Approach:** three layers — regex recognizers proven by checksum, a zero-shot
transformer ([GLiNER-PII](https://huggingface.co/knowledgator/gliner-pii-base-v1.0))
for names and organisations, and a policy layer that decides what is actually
private. Orchestrated with [Microsoft Presidio](https://microsoft.github.io/presidio/).
**There are no hardcoded names anywhere in the code**; the allow-list is learned
from the document's own glossary at run time.

**Results** (full method and tables in [`evaluation_report.md`](evaluation_report.md)):

| Document | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| Supplied prospectus | 0.688 | 0.720 | **0.939** | 0.815 |
| Unseen synthetic prospectus | 0.917 | 0.917 | **1.000** | 0.957 |

**Trade-off, stated plainly:** tuned toward recall. A missed entity is a
disclosed identity; a false positive is a readability cost. Precision on the
supplied document is 0.72 and is the weakest number here — the error analysis
below says exactly what those errors are, and none of them is a leak.

**Three things worth a look:**

1. It redacts the **PAN and Aadhaar cards embedded as images**. The only dates
   of birth in the document — a PII type the brief explicitly requires — exist
   only inside those pictures.
2. It redacts **117 hyperlink field codes**. One address appears *only* inside a
   link and nowhere in the visible text.
3. It is measured on a **document it has never seen**, with ground truth exact
   by construction.

---

## Approach in detail

### 1. Identifiers: pattern, then proof

A regex cannot tell an Aadhaar number from an invoice number — `\b[2-9][0-9]{11}\b`
matches ten billion values, and a financial document is full of twelve-digit
runs. So every identifier pairs a loose regex with a **validator**, and only a
candidate the validator accepts is reported.

| Type | Proof |
|---|---|
| Aadhaar | Verhoeff check digit (the 12th digit, as UIDAI specifies) |
| PAN | Structural — `AAAAA9999A` with a legal holder-type character |
| GSTIN | Base-36 checksum + valid state code |
| Credit card | Luhn |
| IFSC, passport, voter ID | Structural |

Measured: **Verhoeff rejects ~90% of the random 12-digit candidates the regex
accepts** (asserted in `tests/test_validators.py`). That is arithmetic, not a
hand-written exception list.

### 2. Names and organisations: zero-shot NER

No pattern captures a person's name. GLiNER finds entity types described in
plain language with no training and no name list, which is what lets the same
code work on a document it has never seen. Two parameters were tuned by
measurement, not intuition:

- **Threshold 0.30**, not the library default 0.50 — at 0.50 the model misses
  a three-part promoter name on the cover page (it scores 0.49) and every
  postal address.
- **Label wording is a parameter.** Asking for `"name"` scores people at
  0.80–0.83; asking for `"person name"` scores the *same spans* at 0.49.

ALL-CAPS text is passed through a **truecasing** step first. Capitalisation is
one of the strongest cues an NER model has and performance collapses without it
([>40 F1 drop](https://arxiv.org/abs/1912.07095)); this document lists its
promoters on the cover page in full capitals.

### 3. Policy: what is actually private

- **Public bodies stay** — SEBI, BSE, NSE, RoC, RBI. Written as a category, not
  as this file's answers.
- **The document's own vocabulary stays.** The "Definitions and Abbreviations"
  glossary is parsed at run time — **599 terms learned here** — so `ASBA`,
  `QIB` and `Working Day` are recognised as vocabulary. This is the single
  most effective precision measure in the tool.
- **Prose is not a name.** `The face value of the Equity Shares` is not in any
  glossary and is not a common word, but it is plainly a sentence fragment.
  Casing is the signal: names are capitalised throughout, prose is not.
- **Field captions are not values.** `Telephone` labels the number beside it.
- **The document's own casing decides what is a common noun.** Every word
  sequence the file writes with a lower-case first letter is indexed as
  vocabulary, because a proper noun is capitalised wherever it appears. A
  sentence-initial capital defeats the casing test above — `Delay in or
  inability of the vendors…` opens a risk-factor bullet and was being replaced
  with a fake person's name — but the same document writes `any delay in
  placing the orders` mid-sentence, and that settles it. This is a per-document
  index, not a stop-word list, so it transfers to a prospectus whose vocabulary
  we have never seen.
- **Section headings are structure, not entities.** A heading is identified by
  repetition — a contents entry names a section that carries the same words as
  its own title — or by a trailing page number. Truecasing `TABLE OF CONTENTS`
  made it read as a company; it was replaced throughout, which destroyed the
  contents page and emptied three paragraphs of the output.
- **Regulators' websites stay.** `sebi.gov.in`, `bseindia.com` and
  `nseindia.com` resolve onto the public-body list rather than a second list of
  domains that would drift out of step with it.
- **An abbreviation's expansion is vocabulary, whole and in part.** A row whose
  term is an acronym and whose definition names no institution — `PFCE |
  Private Final Consumption Expenditure`, `PM-KUSUM | Pradhan Mantri Kisan Urja
  Suraksha evam Utthaan Mahabhiyan Yojana` — is reference material, and phrases
  taken from the middle of it are protected too. Both halves of that test carry
  weight. `SBI | State Bank of India` is an acronym row, and the bank is still
  redacted. And the glossary is also where the *parties* are defined —
  `Promoters | Arun Ganesh Prabhu, …` — so requiring an acronym is what
  keeps every promoter's name redactable. Exempting definition columns wholesale
  would disclose the people the tool exists to protect.

Each of these was measured against the ground truth before it shipped. One
candidate rule — *suppress a phrase whose every word appears in lower case* —
would have removed one more false positive and cost **eight** true ones, so it
was dropped rather than traded.

---

## Scoping decisions

The brief asks to be explicit about borderline choices. These are ours.

**Order and ticket numbers — the brief's own example.** The line is drawn at
what the number *resolves to*:

| Resolves to | Decision | Examples |
|---|---|---|
| A person | redact | Aadhaar, PAN, passport, voter ID, DIN |
| A party we already redact | redact | CIN, SEBI intermediary registration, auditor firm registration |
| A transaction or filing | **keep** | order, ticket, invoice, folio, receipt, challan |

The middle row matters: a CIN is publicly searchable, so leaving it beside a
fake company name is not redaction, it is a lookup key. Deciding this needs the
surrounding words, so the policy reads the text before the span, not just the
span.

**Bare place names are kept.** The brief asks for physical/mailing addresses. A
city is not one — "our factories in Maharashtra" identifies nobody, and
replacing it with a fake town makes the document unreadable. An address is
recognised by carrying a building or postal number, and a **whole address block
is merged into one span** replaced by one coherent fake address, so it still
reads like an address.

**Company names are treated as PII**, as the brief lists them. Consequently
anything that re-identifies a redacted company — its domain, its logo, its CIN
— is redacted too.

---

## What is in this document that text-only tools miss

**A PAN card and an Aadhaar card, as photographs.** They carry a PAN number, an
Aadhaar number printed twice, two names, two fathers' names, two dates of birth,
a full postal address in **both Devanagari and English**, two photographs, a
signature and two QR codes. They sit immediately after text about *Promoter
Selling Shareholders* — real people's KYC documents.

OCR of the originals leaks **16 distinct secrets**; OCR of the redacted output
returns **zero characters**.

QR codes are destroyed rather than covered — a QR re-encodes every field on the
card, so blacking out the printed text alone leaks the whole record to a
scanner. Faces and signatures are covered; company logos are replaced, because
a logo re-identifies the company whose name was just replaced.

**Word fragments entities across runs.** Paragraphs here average 14 runs and one
has 424; `Redwood Family Trust` is spread across five. Matching is done on a
paragraph's concatenated text and written back only into the runs the match
covers, so formatting elsewhere is untouched.

---

## Evaluation

Ground truth is built by locating hand-transcribed PII in the document
(`build_ground_truth.py`), **not** by asking the pipeline what it found — which
would make recall 1.0 by construction. 148 annotations over an exhaustively
annotated region, so a detection that is not an annotation is genuinely an
over-redaction.

**The ground truth ships without its literal values, on purpose.** A
ground-truth file for a redaction task is itself a PII disclosure: publishing
the list of real names and addresses beside the redacted document hands back
exactly what the tool removed. `ground_truth.json` therefore carries entity
types and character offsets only — enough to reproduce every number here — and
the literal values stay in an un-committed `ground_truth_values.json`. For the
same reason the error tables in the reports print character *shapes*
(`Aaaaa Aaaaaa`) rather than the values themselves: a list of false negatives is,
by definition, a list of PII that was missed.

Three matching criteria are reported. **Coverage** — any type, containment
counts — is the one that matters for redaction: the models sometimes label
`Village Kharoli` an organisation rather than a location, but the output
document is identical either way, because both get a surrogate.

### Where the errors are

| Kind | Count |
|---|---:|
| bare place name kept deliberately, scored as an error | 34 |
| unclassified | 26 |
| address fragment | 19 |
| type mismatch — text *was* redacted, under another label | 19 |
| truncated span | 1 |
| heading fragment | 1 |

None of these is a leak. Every one is text replaced that did not need to be.

### Ablation — what the transformer is worth

| Configuration | Coverage precision | Coverage recall | F1 |
|---|---:|---:|---:|
| Validators + patterns only (`--no-gliner`) | 0.652 | 0.872 | 0.746 |
| **+ GLiNER zero-shot NER** | **0.720** | **0.939** | **0.815** |

**+6.7 points of recall** from the transformer. That is smaller than it was
before the precision work, and the reason is worth stating: the checksum
validators, the glossary allow-list and the field-caption rules do most of the
heavy lifting on a document like this one. GLiNER earns its place on the names
and organisations no pattern can reach — and on a document whose glossary is
thin, it matters considerably more.

### Generalisation

`make_test_document.py` generates a prospectus-shaped document with different
people, companies, addresses and valid-checksum identifiers, recording ground
truth exactly as it writes. On that unseen file: **recall 1.000**, precision
0.917, F1 0.957, with perfect per-type scores on email, phone, Aadhaar, PAN,
person and address.

**False negatives**: concentrated in ALL-CAPS text even after truecasing.
**False positives**: dominated by document vocabulary the glossary does not
define — precision tracks how rich a document's glossary is, which is the main
limitation of learning the allow-list from it.

---

## Adding a new PII type

Three edits, nothing else changes. For Indian passport numbers:

```python
# 1. detection/validators.py — how to prove it
def is_valid_indian_passport(text: str) -> bool:
    return bool(re.match(r"^[A-PR-WY][0-9]{7}$", text.strip().upper()))

# 2. detection/identifiers.py — how to find candidates
IdentifierSpec(
    entity="IN_PASSPORT",
    patterns=[("passport_8_char", r"\b[A-PR-WY]\d{7}\b", 0.20)],
    context=["passport", "travel document", "issued at"],
    validator=validators.is_valid_indian_passport,
)

# 3. surrogates/generators.py — what to replace it with
"IN_PASSPORT": lambda r, o: f"{r.choice('ABCDEFGHJ')}{r.randint(1000000, 9999999)}",
```

Then add the type to `SUPPORTED_ENTITIES`, and to `always_redact` if it should
be redacted regardless of context.

---

## Layout

```
pii_redaction/
  detection/    validators  identifiers  gliner_backend  policy  registry
  surrogates/   cache  generators  matcher
  documents/    docx_reader  docx_writer  images
  evaluation/   report
  pipeline.py   cli.py
tests/          100 tests, ~20 s, no network
build_ground_truth.py   make_test_document.py   verify_redaction.py
app.py                  web front end
```

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
brew install tesseract tesseract-lang        # Linux: see packages.txt

python -m pii_redaction input.docx output.docx --report report.md
pytest tests/ -q
python verify_redaction.py                   # end-to-end leak check
python app.py                                # web UI on :7860
```

`--no-gliner` runs patterns and validators only (the ablation). `--mapping
out.json` writes the real→surrogate table — **a re-identification key; it is
git-ignored and must never travel with the redacted document.**

### Docker, and a public demo

```bash
./serve.sh                    # build if needed, run locally on :7860
./serve.sh --public my-demo   # ...and expose it via ngrok
```

`serve.sh` builds the image, waits for the model to load, and — with
`--public` — opens an ngrok tunnel and holds the machine awake so a sleeping
laptop does not take the demo down. Pass a reserved ngrok domain (free tier
allows one) as the second argument; without it ngrok issues a fresh random URL
on every restart, which is useless in anything already sent to somebody.

One-time: sign up at ngrok, then `ngrok config add-authtoken <token>`.

The container serves through gunicorn with **one worker and several threads**.
The worker count is deliberate — each worker loads its own copy of the ~500 MB
zero-shot model — and so is the threading: a single *sync* worker serves one
request at a time, so a two-minute document blocked the page, the health check
and every other upload, which a tunnel reports to the browser as a 503.

Uploads are handled off the request. `POST /redact` starts a job and returns
`202` with an id; the client polls `GET /status/<id>`, which reports elapsed
seconds while the job runs. No connection is held open for the length of the
work, so nothing in the path — tunnel, proxy or browser — can time out and lose
a run. The job table lives in memory, which is why the worker count must stay
at one.

Equivalent by hand:

```bash
docker build -t pii-redactor .
docker run --rm -p 7860:7860 pii-redactor
ngrok http --url=my-demo.ngrok-free.app 7860
```

`.dockerignore` keeps source documents, ground truth and the mapping out of the
image. Hugging Face Spaces was the intended host, but Docker Spaces now require
a paid plan, and Render's free tier caps below this stack's memory; the
container runs anywhere with ~2 GB.

## What is knowingly left in the document

`verify_redaction.py` checks every real value against the output. Eight survive,
each declared with a reason rather than quietly dropped from the probe list —
the script fails if one gets worse or if anything not on the list leaks.

| Residual | Count | Why |
|---|---:|---|
| A research provider's name | 7 | the glossary defines it, so the allow-list protects it — the same rule that saves `ASBA` also saves this |
| A single-word law-firm name | 1 | zero-shot NER does not fire on a one-word company |
| An address fragment | 2 | not detected |
| Five locality names | 1–2 each | bare place names, kept **by policy** — see the scoping decision above |

The first is the honest cost of learning the allow-list from the document: a
glossary that defines a company protects that company. Detecting the conflict
needs a signal the glossary cannot supply.

## Known limitations

- **Precision 0.72** on the supplied document. Undefined domain vocabulary is
  over-redacted. Next step: a document-frequency model of vocabulary that does
  not depend on the glossary.
- **Newspaper names are redacted** — `Financial Express`, `Jansatta`,
  `Loksatta` in the statutory publication clause. They are public media rather
  than personal PII, so this costs precision. It is left alone deliberately:
  the only fix is a hardcoded list of newspaper names, and every allow-list
  here is either learned from the document or written as a category. The
  surrounding text is untouched — *"Marathi being the regional language of
  Maharashtra"* still reads correctly — so the clause loses its mastheads, not
  its meaning.
- **ALL-CAPS recall** is still weaker than title case after truecasing.
- **PAN's final character** is an NSDL-internal check digit with no public
  algorithm, so PAN validation is structural only.
- **ID-card images are redacted line-by-line**, not field-by-field — safe, but
  visibly heavy-handed.
- `.docx` only. A PDF cannot be edited in place and returned as Word.

## Research consulted

- [GLiNER-PII](https://huggingface.co/knowledgator/gliner-pii-base-v1.0) · [GLiNER2](https://arxiv.org/html/2507.18546v1)
- [Microsoft Presidio](https://microsoft.github.io/presidio/) — analyzer, context enhancement, image redactor
- [Verhoeff checksum for Aadhaar](https://crewcheck.in/glossary/verhoeff-checksum) and [false-positive reduction](https://crewcheck.in/learn/data-types/aadhaar-detection)
- [Robust NER with Truecasing Pretraining](https://arxiv.org/abs/1912.07095)
- [Hybrid methods for multilingual PII detection](https://arxiv.org/html/2510.07551v1)

---

## Appendix — before and after

Redaction examples use the **synthetic** document, whose PII is generated
and therefore safe to publish. Printing the real document's values here
would undo the exercise — the same reason the ground truth ships without
them.

### PII replaced

```
before  Mrinalini Fernandes holds an Aadhaar number 5283 7698 9084 and PAN LISPE2120L. Contact: mrinalini.fernandes@example-corp.com, Telephone: +91 84084 21826.
after   Ruth Doe holds an Aadhaar number 2855 1836 4405 and PAN KYSPH1973O. Contact: manish.kaul@example.com, Telephone: +91 91906 26465.
```

Name, Aadhaar, PAN, e-mail and telephone all replaced; the Aadhaar
surrogate carries a valid Verhoeff check digit and the PAN is
structurally valid, so the document still passes format validation.

### Left alone, in the real document

None of these carries PII. Earlier versions mangled all three — the
first became a fake company name, the second lost the word
"WEIGHTED", the third lost its defined term.

```
unchanged: This being the first public offering of equity shares of our Company, there has been no formal market for the Equity Shares. The face value 
```

```
unchanged: DETAILS OF THE PROMOTER SELLING SHAREHOLDERS, OFFER FOR SALE AND WEIGHTED AVERAGE COST OF ACQUISITION PER EQUITY SHARE
```

```
unchanged: (1) Our Company, in consultation with the Book Running Lead Managers, may consider participation by Anchor Investors in accordance with the 
```

