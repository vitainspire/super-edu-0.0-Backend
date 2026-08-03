# Extraction cost: Markdown vs JSON

- Model: `google/gemini-2.5-flash`
- Pricing: **$0.3000/M in**, **$2.5000/M out** — fetched live from OpenRouter
- Token counting: tiktoken cl100k_base
- Page images: 1240x1753px at 150 DPI → 1,548 tokens/page (768px tiles @ 258 tok)
- Batching: 7 pages/call → 2 call(s) per chapter
- JSON parse-failure rate: 12% · Markdown: 0%

Chapter shape: 12 pages, 5 topics, 3 subtopics/topic, 4 exercises/topic, 1 sidebar(s)/topic.
Scale: 14 chapters/book × 40 book(s), 2 re-extraction(s).

## Per-chapter token breakdown

| Format | Prompt | Images | Output | Total in | Total out | vs JSON out |
|---|---:|---:|---:|---:|---:|---:|
| `json_ontology` | 800 | 18,576 | 3,217 | 22,597 | 3,603 | 1.00× |
| `markdown` | 378 | 18,576 | 1,545 | 19,332 | 1,545 | 0.48× |
| `markdown_meta` | 443 | 18,576 | 1,577 | 19,462 | 1,577 | 0.49× |

## Cost

| Format | Per chapter | Per book | Total | Saving vs JSON |
|---|---:|---:|---:|---:|
| `json_ontology` | $0.0158 | $0.2210 | $26.52 | — (baseline) |
| `markdown` | $0.0097 | $0.1353 | $16.23 | $10.29 (38.8%) |
| `markdown_meta` | $0.0098 | $0.1369 | $16.43 | $10.09 (38.0%) |

## Where the money actually goes

- Page images are 82% of input tokens, and input is 43% of the per-chapter cost. That part is **identical for every format** — the same pixels get sent either way.
- Output is 57% of the cost, and it is the only part the format changes.
- So the ceiling on any format change is roughly 57% of extraction spend. A 2× smaller output does not halve the bill.

Output-token overhead of the JSON ontology, by cause:

- `graphs` section: 634 tokens — fully derivable from the `chapter_id`/`topic_id` fields already present in `entities`.
- id and foreign-key fields: 1,200 tokens — replaced by heading depth in Markdown.
- Quoted keys repeated per item, plus braces, brackets and commas.

## Fidelity check — do both formats carry the same information?

JSON ontology → Markdown → parser round trip: **LOSSLESS**

| Entity | In JSON | After Markdown round trip |
|---|---:|---:|
| chapters | 1 | 1 |
| topics | 5 | 5 |
| subtopics | 15 | 15 |
| exercises | 20 | 20 |
| sidebars | 5 | 5 |
| prerequisite edges | 4 | 4 |

Parser reported no warnings. The cost figures below compare equal payloads.

## Adding a field later

| Approach | Tokens in | Tokens out | Per chapter | Per book |
|---|---:|---:|---:|---:|
| Full re-extract from the PDF | 22,597 | 3,603 | $0.0158 | $0.2210 |
| Re-enrich stored Markdown | 1,612 | 1,577 | $0.0044 | $0.0620 |

Re-enriching from Markdown is **72% cheaper** and avoids 18,576 image tokens per chapter.

The stronger argument isn't the money. A full re-extract re-reads the pixels, so transcription can come back different — syllabus text an admin already reviewed and approved can change underneath them. Re-enriching stored Markdown cannot do that, because nothing re-reads the page.

## Reading this

- `markdown` is the cheapest at $16.23, but drops `skill_type`/`exercise_type`/`prerequisites` into prose, so anything downstream that needs them must re-derive them — a second AI pass whose cost is not counted here.
- `markdown_meta` costs $16.43 (38% under JSON) and keeps every machine-readable field, which is why it's the sensible canonical format.
- The JSON figure above is generous to JSON: it assumes compact separators. Models asked for JSON routinely pretty-print, which inflates it further.

Not modelled: the `_chapter_prompt` → `_simplified_prompt` → `_minimal_prompt` degradation ladder. Each fallback is another full call with the same images, and it only triggers for JSON. Raising `--json-fail-rate` is the crude way to account for it; `--mode live` measures the real rate.