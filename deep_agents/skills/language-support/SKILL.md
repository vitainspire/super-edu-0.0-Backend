---
name: language-support
description: How to bridge a gap between home language and medium of instruction without shrinking the target - the three-step bridge, terms with no clean anchor, and the vocabulary ceiling for one period. Load when the language profile shows a mismatch, or when a topic's vocabulary load is the barrier.
---

# Language support

A gap between the language a child thinks in and the language the lesson is
delivered in is an **access** problem. It is never a reason to teach less.

It is also the commonest single reason a below-grade child cannot enter a task
at all: they are stuck on the word, not the idea. Where a period has one access
adaptation to spend, this is usually where it belongs.

## The bridge, not the smaller target

    WRONG   the class struggles with English, so teach fewer terms
    RIGHT   the class struggles with English, so the term is introduced in the
            home language, used in both for one period, and used in the medium
            of instruction from then on

The child ends up able to do the same thing. The route in is different.

## The three-step bridge

1. **Anchor in the known language.** Name the thing in the language the child
   already thinks in. One word, spoken, not written.
2. **Hold both together.** Use both terms in the same breath, repeatedly, while
   the child is handling the actual object. This is the step usually skipped, and
   it is the one that does the work.
3. **Move to the medium.** From here on, the medium-of-instruction term only.
   Do not keep re-translating - a term translated in four consecutive periods is
   a term the child never has to learn.

**Some terms have no clean one-word anchor.** "Top view", "net", "tessellate"
often do not have a single home-language word a child already owns - unlike
"triangle", which usually does. Where that's the case, step 1 anchors to a short
description of the action instead of a word ("the shape you'd see looking
straight down"), and step 2 holds that description alongside the term rather
than alongside a translation that doesn't exist. Forcing a one-word translation
that isn't real usage is worse than a longer anchor - the child learns a word
nobody says.

## The term that reaches the notebook is the exam's term

Step 3 is not only good language teaching, it is what makes the pass possible.
The child is examined in the medium of instruction, so the closure line in the
notebook carries the printed term next to the child's own sentence
(`assessment-alignment`). A child revising from a page that holds only the
home-language word has nothing to recognise in the question paper.

The same applies to the take-home question (`self-study`): it is **spoken** by
the teacher, so it may be spoken in whichever language the class thinks in, and
the one line that goes in the notebook uses the medium's term.

## Vocabulary load is separate from language gap

Even in a monolingual room, a period that introduces seven new terms will not
land. Count them. **Three is about the ceiling** for a single period at primary
level, and that includes terms the chapter treats as already known.

A spatial-reasoning period is a common place to blow past this without
noticing: "top view", "side view", and "triangle" in one period is already at
the ceiling, because the first two are unfamiliar in *any* language the child
speaks, not just the medium of instruction - vocabulary load and language gap
are stacking, not separate problems, for exactly the terms that don't have a
clean anchor above. Where the count is genuinely higher, that is information
about the sequencing rather than a reason to go faster. Say so.

## What not to do

- **Do not propose translation on speculation.** If no language data is
  recorded, do not assume a mismatch. Check `get_language_profile` and take
  silence as silence.
- **Do not infer language from geography or demographics.** A district name is
  not evidence about a classroom, and neither is the fact that a school is
  government-run or rural. "Anchor it in the local vernacular" is only a
  legitimate proposal once `get_language_profile` names which language - not
  because a room of this kind usually needs one.
- **Do not write the bridge in a script the teacher may not read.** If you are
  proposing a home-language term, propose it in a form the teacher can actually
  say aloud.
- **Do not simplify the mastery target's wording into a smaller claim.** "Can
  compare three-digit numbers" does not become "can recognise big numbers"
  because the room is multilingual.

## Related

- `contextual-reinforcement` - the provenance rules that govern this
- `differentiation` - the floor, which a child cannot reach through a word they
  do not have
- `assessment-alignment` - why step 3 is what makes the pass possible
- `core-pedagogy` - demand vs content
- `literacy` - when the language IS the subject
