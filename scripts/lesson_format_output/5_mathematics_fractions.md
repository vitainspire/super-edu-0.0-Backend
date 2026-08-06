# Lesson plan: JSON vs Markdown

- Topic: **Fractions** · Grade 5 Mathematics
- Profile: `storyteller`
- Repeats per format: 1
- Model: `google/gemini-2.5-flash`

Both arms share the same system prompt, profile, activity bank and rules — only the output-shape section differs — and both are scored by the same `validate_v2_lesson`.

## Reliability and cost

| Metric | JSON | Markdown |
|---|---|---|
| usable lesson | 1/1 | 1/1 |
| parsed first try | 1/1 | 1/1 |
| repair LLM calls | 0 | 0 |
| validator issues (initial) | 1 | 1 |
| validator issues (after repair) | 1 | 1 |
| missing required fields | 0 | 0 |
| parser warnings | 0 | 0 |
| bullets filled | 6 | 6 |
| prose chars (content) | 601 | 601 |
| output tokens | 520 | 330 |
| prose chars per token | 1.16 | 1.82 |
| latency s | 7 | 5 |
| cost USD | 0.0014 | 0.0009 |

### Is the cheaper format saying less, or just spending fewer tokens on syntax?

- Content delivered: Markdown carries **100%** of JSON's prose (601 vs 601 chars).
- Tokens spent: Markdown uses **63%** of JSON's output tokens (330 vs 520).
- The gap between those two is the answer: JSON spends roughly **1.6x more tokens per character of actual content**. Quoted keys, braces and escaped strings tokenize far worse than prose does.

Markdown is **36% cheaper** per lesson ($0.00090 vs $0.00140).

## Field fidelity

Whether each field the UI renders actually arrived. A format that parses but drops `explore.scenario` renders a blank card.

| Field | JSON | Markdown |
|---|---|---|
| `planningNote` | 1/1 | 1/1 |
| `concept` | 1/1 | 1/1 |
| `explore.scenario` | 1/1 | 1/1 |
| `explore.points` | 1/1 | 1/1 |
| `challenge.activity` | 1/1 | 1/1 |
| `challenge.points` | 1/1 | 1/1 |
| `materialsUsed` | 1/1 | 1/1 |
| `levelSet.points` | 1/1 | 1/1 |
| `levelSet.extendPrompt` | 1/1 | 1/1 |
| `previousTopicRefresher` *(optional)* | 1/1 | 1/1 |
| `explore.imageFocus` *(optional)* | 1/1 | 1/1 |

### json — problems

- validator: materialsUsed includes items not in the resource list: chalk, paper

### markdown — problems

- validator: materialsUsed includes items not in the resource list: chalk, paper

## The lessons — read these, the harness can't judge them

### From json

```
  [planning] This teacher leans on stories, so Explore is a market scene.
  REFRESHER — Whole numbers
    - We counted whole baskets last time.
  CONCEPT
    - A fraction names equal parts of one whole.
    - The bottom number says how many equal parts.
  EXPLORE
    scenario: Amma buys one big watermelon at the Sunday market and four children want a fair share.
    sketch:   a circle folded into four equal parts
    - Fold a paper watermelon into four equal parts.
        ↳ Ask: is every part the same size? How do you know?
  CHALLENGE — Market stall role-play
    - Two children sell half-slices, two buy them.
  MATERIALS: chalk, paper
  LEVEL SET
    - Back to the watermelon — show one half with your hands.
    extend: Where else at home do you cut something into equal parts?
```

### From markdown

```
  [planning] This teacher leans on stories, so Explore is a market scene.
  REFRESHER — Whole numbers
    - We counted whole baskets last time.
  CONCEPT
    - A fraction names equal parts of one whole.
    - The bottom number says how many equal parts.
  EXPLORE
    scenario: Amma buys one big watermelon at the Sunday market and four children want a fair share.
    sketch:   a circle folded into four equal parts
    - Fold a paper watermelon into four equal parts.
        ↳ Ask: is every part the same size? How do you know?
  CHALLENGE — Market stall role-play
    - Two children sell half-slices, two buy them.
  MATERIALS: chalk, paper
  LEVEL SET
    - Back to the watermelon — show one half with your hands.
    extend: Where else at home do you cut something into equal parts?
```

### Raw responses

<details><summary>json — raw (967 chars)</summary>

```
{"planningNote": "This teacher leans on stories, so Explore is a market scene.", "previousTopicRefresher": {"previousTopic": "Whole numbers", "recap": [{"text": "We counted whole baskets last time."}]}, "concept": [{"text": "A fraction names equal parts of one whole."}, {"text": "The bottom number says how many equal parts."}], "explore": {"scenario": "Amma buys one big watermelon at the Sunday market and four children want a fair share.", "points": [{"text": "Fold a paper watermelon into four equal parts.", "detail": "Ask: is every part the same size? How do you know?"}], "imageFocus": "a circle folded into four equal parts"}, "challenge": {"activity": "Market stall role-play", "points": [{"text": "Two children sell half-slices, two buy them."}]}, "materialsUsed": ["chalk", "paper"], "levelSet": {"points": [{"text": "Back to the watermelon — show one half with your hands."}], "extendPrompt": "Where else at home do you cut something into equal parts?"}}
```

</details>

<details><summary>markdown — raw (767 chars)</summary>

```
planning-note: This teacher leans on stories, so Explore is a market scene.

## Refresher: Whole numbers
- We counted whole baskets last time.

## Concept
- A fraction names equal parts of one whole.
- The bottom number says how many equal parts.

## Explore
scenario: Amma buys one big watermelon at the Sunday market and four children want a fair share.
sketch: a circle folded into four equal parts
- Fold a paper watermelon into four equal parts. :: Ask: is every part the same size? How do you know?

## Challenge
activity: Market stall role-play
- Two children sell half-slices, two buy them.

## Materials
- chalk
- paper

## Level set
- Back to the watermelon — show one half with your hands.
extend: Where else at home do you cut something into equal parts?

```

</details>
