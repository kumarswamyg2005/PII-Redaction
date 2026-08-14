# Evaluation Report

- **Document**: `Red Herring Prospectus.docx`
- **Output**: `Red Herring Prospectus - REDACTED.docx`
- **Runtime**: 88.8s

## What the run did

- Entities detected and redacted: **812**
- Spans suppressed by policy: **2785**
- Distinct entities mapped to surrogates: **386**
- Replacements written into the document: **994**
- Defined terms learned from the document's own glossary: **599**

### Replacements by container

| Container | Replacements |
|---|---:|
| body | 797 |
| field_codes | 196 |
| header68.xml | 1 |

### Detections by type

| Type | Count |
|---|---:|
| ORGANIZATION | 319 |
| PERSON | 271 |
| EMAIL_ADDRESS | 104 |
| PHONE_NUMBER | 39 |
| URL | 29 |
| WEB_ADDRESS | 28 |
| CORPORATE_ID | 9 |
| LOCATION | 8 |
| BANK_ACCOUNT | 5 |

### Images

| Image | Regions redacted |
|---|---:|
| `word/media/image1.jpeg` | 1 |
| `word/media/image1.png` | 1 |
| `word/media/image2.jpeg` | 1 |
| `word/media/image2.png` | 1 |
| `word/media/image3.jpeg` | 1 |
| `word/media/image3.png` | 1 |
| `word/media/image4.png` | 24 |
| `word/media/image5.png` | 25 |

## Accuracy against ground truth

### Method

- Annotations: **148** total; **148** inside the scored region.
- Scored region: characters **0–46595**, annotated exhaustively so that an unmatched detection is a genuine false positive.
- A detection matches an annotation when the types agree and the spans overlap by at least 50% of each.

### Results

| Type | TP | FP | FN | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| BANK_ACCOUNT | 0 | 1 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| CORPORATE_ID | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| EMAIL_ADDRESS | 11 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| LOCATION | 2 | 1 | 11 | 0.143 | 0.667 | 0.154 | 0.250 |
| ORGANIZATION | 52 | 37 | 9 | 0.531 | 0.584 | 0.852 | 0.693 |
| PERSON | 43 | 30 | 6 | 0.544 | 0.589 | 0.878 | 0.705 |
| PHONE_NUMBER | 7 | 1 | 0 | 0.875 | 0.875 | 1.000 | 0.933 |
| WEB_ADDRESS | 5 | 1 | 0 | 0.833 | 0.833 | 1.000 | 0.909 |
| **Micro (all)** | **122** | **71** | **26** | **0.557** | **0.632** | **0.824** | **0.716** |
| **Macro (per type)** | | | | | **0.694** | **0.735** | **0.686** |

*Accuracy* here is TP / (TP + FP + FN). Span extraction has no true-negative count — the number of non-PII spans a document could contain is unbounded — so the textbook (TP+TN)/total is undefined. This is the standard substitute for extraction tasks, stated openly rather than quietly redefined.

### Missed PII (false negatives)

The shipped ground truth carries entity types and character offsets but not the values themselves, because a ground-truth file for a redaction task is itself a disclosure. A miss is therefore located rather than quoted; run against your own copy of the source to see the text at each offset.

| Type | Where |
|---|---|
| LOCATION | characters 356–373 |
| LOCATION | characters 443–467 |
| PERSON | characters 779–798 |
| PERSON | characters 6757–6777 |
| PERSON | characters 9833–9852 |
| ORGANIZATION | characters 9879–9899 |
| ORGANIZATION | characters 9996–10039 |
| LOCATION | characters 20676–20687 |
| LOCATION | characters 21003–21022 |
| LOCATION | characters 21410–21421 |
| LOCATION | characters 23704–23721 |
| LOCATION | characters 23822–23846 |
| LOCATION | characters 24935–24952 |
| LOCATION | characters 27535–27559 |
| ORGANIZATION | characters 27640–27683 |
| ORGANIZATION | characters 29261–29304 |
| ORGANIZATION | characters 29871–29888 |
| ORGANIZATION | characters 29923–29966 |
| PERSON | characters 32148–32167 |
| ORGANIZATION | characters 32237–32255 |
| ORGANIZATION | characters 32311–32354 |
| PERSON | characters 32748–32767 |
| ORGANIZATION | characters 32827–32849 |
| LOCATION | characters 32937–32954 |
| LOCATION | characters 35811–35828 |

### What the false positives actually are

Precision alone does not say whether a tool is unsafe or merely untidy. Every false positive below is text that was replaced but did not need to be — none of them is a leak.

| Kind | Count | Example |
|---|---:|---|
| bare place name | 26 | `Aaaaa Aaaa` |
| address fragment | 19 | `Aaaaaa - Aaaa Aaaa` |
| type mismatch (text was redacted) | 18 | `Aaaaaaa Aaaaaaaaa` |
| other | 8 | `+99 99 99999999` |

### Over-redaction (false positives)

| Type | Text |
|---|---|
| ORGANIZATION | `Aaaaaaa Aaaaaaaaa` |
| PERSON | `Aaaaaa - Aaaa Aaaa` |
| ORGANIZATION | `Aaaaa 9` |
| ORGANIZATION | `Aaaaaaaa Aaaaaaaa Aaaaaa` |
| PERSON | `Aaaaa Aaaa` |
| ORGANIZATION | `AAAAA AAAAAA AAAAAA` |
| ORGANIZATION | `AAAAA AAAAAA AAAAAA` |
| PERSON | `AAAAAAAA AAAAAAAAAA AAAA AA AAAAAAA AAAAAAA` |
| ORGANIZATION | `A Aaaaa` |
| PERSON | `Aaaaaa Aaaaa Aaaaaaa` |
| PERSON | `Aaaaaa Aaaa` |
| PHONE_NUMBER | `+99 99 99999999` |
| LOCATION | `Aaaaaaa Aaaaa, Aaaaaaaaa Aaaaaaa Aaaa, Aaaaaaaaa` |
| PERSON | `Aaaaaaaa` |
| BANK_ACCOUNT | `AAA999999999` |
| PERSON | `AAAA` |
| ORGANIZATION | `Aaaaaaa Aaaaaaaaa` |
| PERSON | `Aaaaaa Aaaaaa - Aaaa` |
| ORGANIZATION | `Aaaaa 9` |
| ORGANIZATION | `Aaaaaaaa Aaaaaaaa Aaaaaa` |
| PERSON | `Aaaaa` |
| ORGANIZATION | `Aaaaaaa Aaaaaaaaa` |
| PERSON | `Aaaaaa Aaaaaa - Aaaa` |
| WEB_ADDRESS | `aaaaa://aaaaaaaaaaaaaaaa.aaa/aaaaaaaa-aaaaaaaaa/` |
| PERSON | `Aaaaaaa Aaaaaaa` |

### Matching criteria compared

Three views of the same run. They differ only in how strictly a detection must agree with an annotation:

| Criterion | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| Strict — same type, 50% mutual overlap | 0.532 | 0.615 | 0.797 | 0.694 |
| Relaxed — same type, containment counts | 0.557 | 0.632 | 0.824 | 0.716 |
| **Coverage — any type, containment counts** | **0.688** | **0.720** | **0.939** | **0.815** |

**Coverage is the number that matters for redaction.** The per-type rows above punish the model for calling `Village Kharoli` an organisation rather than a location — but the output document is identical either way, because both types are replaced with a surrogate. Type confusion between PERSON, ORGANIZATION and LOCATION changes the label in the report, not the redaction. What would genuinely leak is a span nobody detected at all, and that is what the coverage recall measures.


## Why spans were suppressed

Detections the models proposed and policy rejected. This is where precision is actually won.

| Reason | Count | Example |
|---|---:|---|
| document-defined term, written in lower case elsewhere in the document | 865 | `AAA AAAAAAA AAAAAAAAAA` |
| document-defined term | 616 | `AaaaAaaa Aaaaaaaa` |
| not a proper noun | 374 | `aaaa://aaa.aaaa.aaa.aa/aaaaaaa/aaaaa/AaaaaAaaaaa` |
| written in lower case elsewhere in the document | 283 | `aaaaa://aaa.aaaaaaaa.aaa/Aaaaaa/AaaaaaAaaaaa/Aaa` |
| document-defined term, common word, written in lower case elsewhere in the document | 235 | `Aaaaaaaaa` |
| public body, document-defined term | 173 | `aaa Aaaaaaaaaa aaa Aaaaaaaa Aaaaa aa Aaaaa` |
| document-defined term, common word | 129 | `Aaaaa` |
| bare place name | 56 | `Aaaaaa` |
| public body | 38 | `Aaaaaaaa Aaaaa Aaaaaaaa aa` |
| common word | 6 | `AAAAAA` |
| public body, written in lower case elsewhere in the document | 5 | `aaa AAA Aaaaaaa` |
| public body, document-defined term, written in lower case elsewhere in the document | 5 | `aaa Aaaaaaaaaa aa Aaaaa` |
