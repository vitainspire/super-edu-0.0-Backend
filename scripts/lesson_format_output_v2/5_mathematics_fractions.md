# Lesson plan: JSON vs Markdown

- Topic: **Fractions** · Grade 5 Mathematics
- Profile: `storyteller`
- Previous topic: Whole numbers and place value
- Repeats per format: 2
- Model: `google/gemini-2.5-flash`

Both arms share the same system prompt, profile, activity bank and rules — only the output-shape section differs — and both are scored by the same `validate_v2_lesson`.

## Reliability and cost

| Metric | JSON | Markdown |
|---|---|---|
| usable lesson | 2/2 | 2/2 |
| parsed first try | 1/2 | 2/2 |
| repair LLM calls | 1 | 0 |
| validator issues (initial) | 0.5 (min 0, max 1) | 0 |
| validator issues (after repair) | 0.5 (min 0, max 1) | 0 |
| missing required fields | 0 | 0 |
| parser warnings | 0.5 (min 0, max 1) | 0 |
| bullets filled | 13.5 (min 13, max 14) | 14 |
| prose chars (content) | 2773.5 (min 2679, max 2868) | 2438.0 (min 2377, max 2499) |
| output tokens | 1847.5 (min 1803, max 1892) | 617.0 (min 600, max 634) |
| prose chars per token | 1.5 (min 1.42, max 1.59) | 4.0 (min 3.94, max 3.96) |
| latency s | 26.6 (min 20, max 33.1) | 11.9 (min 9.4, max 14.5) |
| cost USD | 0.00533 (min 0.00493, max 0.00573) | 0.00226 (min 0.00222, max 0.0023) |

### Is the cheaper format saying less, or just spending fewer tokens on syntax?

- Content delivered: Markdown carries **88%** of JSON's prose (2438 vs 2774 chars).
- Tokens spent: Markdown uses **33%** of JSON's output tokens (617 vs 1848).
- The gap between those two is the answer: JSON spends roughly **2.6x more tokens per character of actual content**. Quoted keys, braces and escaped strings tokenize far worse than prose does.

Markdown is **58% cheaper** per lesson ($0.00226 vs $0.00533).

## Field fidelity

Whether each field the UI renders actually arrived. A format that parses but drops `explore.scenario` renders a blank card.

| Field | JSON | Markdown |
|---|---|---|
| `planningNote` | 2/2 | 2/2 |
| `concept` | 2/2 | 2/2 |
| `explore.scenario` | 2/2 | 2/2 |
| `explore.points` | 2/2 | 2/2 |
| `challenge.activity` | 2/2 | 2/2 |
| `challenge.points` | 2/2 | 2/2 |
| `materialsUsed` | 2/2 | 2/2 |
| `levelSet.points` | 2/2 | 2/2 |
| `levelSet.extendPrompt` | 2/2 | 2/2 |
| `previousTopicRefresher` *(optional)* | 2/2 | 2/2 |
| `explore.imageFocus` *(optional)* | 2/2 | 2/2 |

### json — problems

- validator: contains banned word "recall"
- parser: JSON parse failed: JSONDecodeError: Expecting ',' delimiter: line 29 column 3 (char 1397)

## The lessons — read these, the harness can't judge them

### From json

```
  [planning] Given the teacher's preference for storytelling and real-life connections, the Explore activity should be a relatable scenario where dividing things naturally leads to fractions. The 'Fraction Pizza Shop' from the bank fits this perfectly as a Challenge, allowing students to apply their understanding interactively.
  REFRESHER — Whole numbers and place value
    - Remember when we talked about big numbers, like how many people live in our village, or how much money is in a big savings account?
        ↳ This connects to the idea of 'whole' amounts.
    - We learned that every digit in a number has a special place, making it worth tens or hundreds or thousands.
        ↳ Reinforces understanding of quantity and value.
    - Today, we're going to explore what happens when we need to talk about parts of these whole things, not just the full amounts.
  CONCEPT
    - Sometimes we need to share things equally, but there isn't a whole number for everyone.
    - Imagine sharing one big roti among three friends; each person gets a part, not a whole roti.
    - Fractions help us describe these equal parts of a whole.
  EXPLORE
    scenario: Imagine our classroom is preparing for a special festival! We have one long, delicious banana cake, and we need to share it perfectly equally among the whole class so everyone gets a fair piece. But we can't just give one cake to each person, can we?
    sketch:   A long rectangle divided into many equal parts
    - Teacher draws a long rectangle on the board to represent the cake.
        ↳ Emphasize it's one whole cake.
    - Ask students: 'If there are 30 of us, how many equal pieces do we need to cut the cake into so everyone gets their share?'
        ↳ Guide them to understand the denominator.
    - Teacher then draws lines to divide the cake on the board into these equal parts, counting them out loud with the class.
        ↳ Visually represent the division and the concept of equal parts.
  CHALLENGE — Fraction Pizza Shop
    - Teacher draws large circles on the board, labeling them 'Pizza 1', 'Pizza 2', etc.
        ↳ Each circle represents a whole pizza.
    - Teacher announces an 'order': 'I need one pizza cut into 4 equal slices, and I'd like 2 of those slices, please!'
        ↳ This introduces the numerator and denominator in context.
    - Students raise their hands to explain how they would 'cut' and 'serve' the order, describing the fraction out loud.
        ↳ Encourage students to use fraction language like 'one-fourth' or 'two-fourths'.
  MATERIALS: Chalkboard
  LEVEL SET
    - Just like we shared that one big banana cake fairly among all of us, fractions help us share or describe parts of anything.
        ↳ Connect back to the Explore scenario to solidify the concept.
    - Whether it's a piece of land, a jug of water, or even time, fractions let us talk about those smaller, equal portions.
    extend: When else in your daily life do you see things being divided into equal parts, like our cake?
```

### From markdown

```
  [planning] Given the teacher's profile and the previous topic, the Explore scenario should involve sharing something tangible and familiar, connecting whole numbers to parts. The "Fraction Pizza Shop" activity from the bank fits the profile's enjoyment of storytelling and real-life connections while being simple to implement in a resource-constrained environment.
  REFRESHER — Whole numbers and place value
    - Remember how we used numbers like 1, 5, or 10 to count whole things, like a full basket of mangoes or all the students in our class?
    - We also learned about place value, where the position of a digit, like in the number 25, tells us its value – two tens and five ones.
    - Today, we're going to explore what happens when we need to talk about parts of those whole things, not just the whole things themselves.
  CONCEPT
    - Fractions represent parts of a whole object or a collection of objects.
    - The top number of a fraction, the numerator, tells us how many parts we have.
    - The bottom number, the denominator, tells us how many equal parts make up the whole.
  EXPLORE
    scenario: Imagine our class is having a special lunch, and we have one big, delicious roti for everyone to share. But there are many of us, so we need to cut it fairly.
    sketch:   A simple circle representing a roti, with lines to show cuts.
    - The teacher asks, "If we have one roti and want to share it equally among two friends, how would we cut it?"
    - Students discuss with a partner how they would cut the roti, and then a few volunteers describe their ideas to the class.
        ↳ Listen for ideas about cutting into halves.
    - The teacher then draws a roti on the board and shows how to cut it into equal parts, introducing the idea of "half" and writing 1/2.
  CHALLENGE — Fraction Pizza Shop
    - The teacher announces, "Welcome to our Fraction Roti Shop! I am the customer today."
    - The teacher orders, "I'd like half a roti, please!" and points to a student to 'cut' it on the chalkboard.
        ↳ The student uses chalk to draw a roti and divide it into two equal parts, shading one half.
    - The teacher then orders, "Now, I'd like one-fourth of a roti!" and another student draws and divides a roti into four equal parts, shading one.
  MATERIALS: Chalkboard
  LEVEL SET
    - So, just like we cut our big roti into smaller, equal pieces, fractions help us describe those pieces.
    - When we said "half a roti," we were talking about one part out of two equal parts, written as 1/2.
    extend: How could understanding fractions help us share mangoes fairly among all our friends?
```

### Raw responses

<details><summary>json — raw (3743 chars)</summary>

```
```json
{
  "planningNote": "Given the teacher's preference for storytelling and real-life connections, the Explore activity should be a relatable scenario where dividing things naturally leads to fractions. The 'Fraction Pizza Shop' from the bank fits this perfectly as a Challenge, allowing students to apply their understanding interactively.",
  "previousTopicRefresher": {
    "previousTopic": "Whole numbers and place value",
    "recap": [
      {
        "text": "Remember when we talked about big numbers, like how many people live in our village, or how much money is in a big savings account?",
        "detail": "This connects to the idea of 'whole' amounts."
      },
      {
        "text": "We learned that every digit in a number has a special place, making it worth tens or hundreds or thousands.",
        "detail": "Reinforces understanding of quantity and value."
      },
      {
        "text": "Today, we're going to explore what happens when we need to talk about parts of these whole things, not just the full amounts."
      }
    ]
  },
  "concept": [
    {
      "text": "Sometimes we need to share things equally, but there isn't a whole number for everyone."
    },
    {
      "text": "Imagine sharing one big roti among three friends; each person gets a part, not a whole roti."
    },
    {
      "text": "Fractions help us describe these equal parts of a whole."
    }
  },
  "explore": {
    "scenario": "Imagine our classroom is preparing for a special festival! We have one long, delicious banana cake, and we need to share it perfectly equally among the whole class so everyone gets a fair piece. But we can't just give one cake to each person, can we?",
    "points": [
      {
        "text": "Teacher draws a long rectangle on the board to represent the cake.",
        "detail": "Emphasize it's one whole cake."
      },
      {
        "text": "Ask students: 'If there are 30 of us, how many equal pieces do we need to cut the cake into so everyone gets their share?'",
        "detail": "Guide them to understand the denominator."
      },
      {
        "text": "Teacher then draws lines to divide the cake on the board into these equal parts, counting them out loud with the class.",
        "detail": "Visually represent the division and the concept of equal parts."
      }
    ],
    "imageFocus": "A long rectangle divided into many equal parts"
  },
  "challenge": {
    "activity": "Fraction Pizza Shop",
    "points": [
      {
        "text": "Teacher draws large circles on the board, labeling them 'Pizza 1', 'Pizza 2', etc.",
        "detail": "Each circle represents a whole pizza."
      },
      {
        "text": "Teacher announces an 'order': 'I need one pizza cut into 4 equal slices, and I'd like 2 of those slices, please!'",
        "detail": "This introduces the numerator and denominator in context."
      },
      {
        "text": "Students raise their hands to explain how they would 'cut' and 'serve' the order, describing the
```

</details>

<details><summary>markdown — raw (2575 chars)</summary>

```
planning-note: Given the teacher's profile and the previous topic, the Explore scenario should involve sharing something tangible and familiar, connecting whole numbers to parts. The "Fraction Pizza Shop" activity from the bank fits the profile's enjoyment of storytelling and real-life connections while being simple to implement in a resource-constrained environment.

## Refresher: Whole numbers and place value
- Remember how we used numbers like 1, 5, or 10 to count whole things, like a full basket of mangoes or all the students in our class?
- We also learned about place value, where the position of a digit, like in the number 25, tells us its value – two tens and five ones.
- Today, we're going to explore what happens when we need to talk about parts of those whole things, not just the whole things themselves.

## Concept
- Fractions represent parts of a whole object or a collection of objects.
- The top number of a fraction, the numerator, tells us how many parts we have.
- The bottom number, the denominator, tells us how many equal parts make up the whole.

## Explore
scenario: Imagine our class is having a special lunch, and we have one big, delicious roti for everyone to share. But there are many of us, so we need to cut it fairly.
sketch: A simple circle representing a roti, with lines to show cuts.
- The teacher asks, "If we have one roti and want to share it equally among two friends, how would we cut it?"
- Students discuss with a partner how they would cut the roti, and then a few volunteers describe their ideas to the class. :: Listen for ideas about cutting into halves.
- The teacher then draws a roti on the board and shows how to cut it into equal parts, introducing the idea of "half" and writing 1/2.

## Challenge
activity: Fraction Pizza Shop
- The teacher announces, "Welcome to our Fraction Roti Shop! I am the customer today."
- The teacher orders, "I'd like half a roti, please!" and points to a student to 'cut' it on the chalkboard. :: The student uses chalk to draw a roti and divide it into two equal parts, shading one half.
- The teacher then orders, "Now, I'd like one-fourth of a roti!" and another student draws and divides a roti into four equal parts, shading one.

## Materials
- Chalkboard
- Chalk

## Level set
- So, just like we cut our big roti into smaller, equal pieces, fractions help us describe those pieces.
- When we said "half a roti," we were talking about one part out of two equal parts, written as 1/2.
extend: How could understanding fractions help us share mangoes fairly among all our friends?
```

</details>
