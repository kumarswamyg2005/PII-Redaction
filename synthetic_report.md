# Evaluation Report

- **Document**: `synthetic_test.docx`
- **Output**: `s.docx`
- **Runtime**: 1.3s

## What the run did

- Entities detected and redacted: **24**
- Spans suppressed by policy: **31**
- Distinct entities mapped to surrogates: **24**
- Replacements written into the document: **26**
- Defined terms learned from the document's own glossary: **3**

### Replacements by container

| Container | Replacements |
|---|---:|
| body | 26 |

### Detections by type

| Type | Count |
|---|---:|
| ORGANIZATION | 6 |
| PERSON | 3 |
| IN_AADHAAR | 3 |
| IN_PAN | 3 |
| EMAIL_ADDRESS | 3 |
| PHONE_NUMBER | 3 |
| LOCATION | 3 |

## Accuracy against ground truth

### Method

- Annotations: **22** total; **22** inside the scored region.
- Scored region: characters **0–1794**, annotated exhaustively so that an unmatched detection is a genuine false positive.
- A detection matches an annotation when the types agree and the spans overlap by at least 50% of each.

### Results

| Type | TP | FP | FN | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| EMAIL_ADDRESS | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| IN_AADHAAR | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| IN_PAN | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| LOCATION | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| ORGANIZATION | 4 | 2 | 0 | 0.667 | 0.667 | 1.000 | 0.800 |
| PERSON | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| PHONE_NUMBER | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.000 |
| **Micro (all)** | **22** | **2** | **0** | **0.917** | **0.917** | **1.000** | **0.957** |
| **Macro (per type)** | | | | | **0.952** | **1.000** | **0.971** |

*Accuracy* here is TP / (TP + FP + FN). Span extraction has no true-negative count — the number of non-PII spans a document could contain is unbounded — so the textbook (TP+TN)/total is undefined. This is the standard substitute for extraction tasks, stated openly rather than quietly redefined.

### What the false positives actually are

Precision alone does not say whether a tool is unsafe or merely untidy. Every false positive below is text that was replaced but did not need to be — none of them is a leak.

| Kind | Count | Example |
|---|---:|---|
| bare place name | 2 | `Aaaaaa Aaaaaa` |

### Over-redaction (false positives)

| Type | Text |
|---|---|
| ORGANIZATION | `Aaaaaa Aaaaaa` |
| ORGANIZATION | `Aaaaaaa` |

### Matching criteria compared

Three views of the same run. They differ only in how strictly a detection must agree with an annotation:

| Criterion | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| Strict — same type, 50% mutual overlap | 0.917 | 0.917 | 1.000 | 0.957 |
| Relaxed — same type, containment counts | 0.917 | 0.917 | 1.000 | 0.957 |
| **Coverage — any type, containment counts** | **0.917** | **0.917** | **1.000** | **0.957** |

**Coverage is the number that matters for redaction.** The per-type rows above punish the model for calling `Village Kharoli` an organisation rather than a location — but the output document is identical either way, because both types are replaced with a surrogate. Type confusion between PERSON, ORGANIZATION and LOCATION changes the label in the report, not the redaction. What would genuinely leak is a span nobody detected at all, and that is what the coverage recall measures.


## Why spans were suppressed

Detections the models proposed and policy rejected. This is where precision is actually won.

| Reason | Count | Example |
|---|---:|---|
| not a proper noun | 15 | `Aaaaaaa` |
| common word | 6 | `Aaaaa` |
| written in lower case elsewhere in the document | 4 | `aaaaa.aaaaaaa@aaaaaaa-aaaa.aaa` |
| document-defined term | 2 | `Aaaaa` |
| public body | 2 | `AAA` |
| bare place name | 2 | `Aaaa` |
