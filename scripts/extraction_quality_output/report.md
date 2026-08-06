# Extraction quality: standard vs simple tier

- PDF: `(dry run)`
- Repeats per tier: 2
- `standard` → google/gemini-2.5-flash
- `simple` → google/gemini-2.5-flash-lite

## Verdict

**Do not switch.** The simple tier is 77% cheaper per chapter and:

- SCRIPT MISMATCH — standard transcribes in Telugu, simple in Latin. The cheap model is transliterating, not reading. Automatic disqualification.
- COVERAGE LOSS — simple found 1.0 topics on average vs 2.0. It is missing content, not just describing it differently.

## Measurements

| Metric | standard | simple |
|---|---|---|
| runs succeeded | 2/2 | 2/2 |
| topics | 2.0 (min 2, max 2) | 1.0 (min 1, max 1) |
| subtopics | 2.0 (min 2, max 2) | 1.0 (min 1, max 1) |
| exercises | 3.0 (min 3, max 3) | 1.0 (min 1, max 1) |
| sidebars | 0.0 (min 0, max 0) | 0.0 (min 0, max 0) |
| dominant script | Telugu | Latin |
| parser warnings | 0.0 (min 0, max 0) | 0.0 (min 0, max 0) |
| generic skill_types | 0.0 (min 0, max 0) | 0.0 (min 0, max 0) |
| generic exercise_types | 0.0 (min 0, max 0) | 0.0 (min 0, max 0) |
| tokens out | 1577.0 (min 1577, max 1577) | 780.0 (min 780, max 780) |
| latency s | 12.0 (min 12, max 12) | 5.0 (min 5, max 5) |
| cost USD / chapter | $0.0095 | $0.0022 |

### Run-to-run stability

A difference between tiers only means something if it exceeds each tier's own noise.

- `standard`: topic counts across runs = [2, 2] (spread 0)
- `simple`: topic counts across runs = [1, 1] (spread 0)

## Review sheet — needs a reader of the script

**No topic names could be aligned between the two tiers at all.** They share too few characters to be treated as the same topic — which is itself the finding, usually transliteration or wholesale different reading.

**Only the standard tier found these — check whether they are really on the page:**
- సంఖ్యలు లెక్కించడం
- ఆకారాలు

**Only the simple tier found these — likely hallucinated or mis-split:**
- Sankhyalu Lekkinchadam

## Raw output

### standard — first 1500 chars of Markdown

```markdown
---
chapter: 2
title: సంఖ్యలు మన చుట్టూ
pages: 12-18
---

## సంఖ్యలు లెక్కించడం
---
pages: 12-14
---

Students count objects up to twenty and match each count to its numeral.

### ఒకటి రాయడం `writing_skill` p12
Trace the numeral while saying its name aloud.

#### Exercises
- `counting_activity` p13 — Count the mangoes in the basket and write the numeral.
- `writing_practice` p14 — Trace each numeral three times.

## ఆకారాలు
---
pages: 15-18
prerequisites: [సంఖ్యలు లెక్కించడం]
---

Students identify circles and squares in everyday objects.

### వృత్తం `recognition_skill` p15
Find round objects in the classroom.

#### Exercises
- `matching_exercise` p16 — Match each object to its shape.

```

### simple — first 1500 chars of Markdown

```markdown
---
chapter: 2
title: Sankhyalu Mana Chuttu
pages: 12-18
---

## Sankhyalu Lekkinchadam
---
pages: 12-15
---

Students count objects and write numbers.

### Okati Rayadam
Trace the numeral.

#### Exercises
- p13 — Count the mangoes and write the number.

```
