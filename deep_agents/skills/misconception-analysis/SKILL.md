---
name: misconception-analysis
description: How to write a misconception a teacher can actually put in front of a class, and how to ask it so children answer honestly rather than performing. Load when deriving misconceptions, writing a Level Set section, or designing a probe that reveals what children still believe.
---

# Misconception analysis

A misconception is not "a thing children find hard". It is **a specific wrong
belief that is reasonable from what the child has seen**, and that produces
correct answers on the exercise and wrong answers everywhere else.

## The form rule

Write it in the child's own voice, first person, naming a real thing.

    no   "a 3D object's view changes its identity"
    yes  "the cup changed when I walked round it"

    no   "the learner over-generalises the commutative property"
    yes  "if 3 + 5 is 8 then 3 - 5 must be 2 the other way too"

    no   "students conflate the symbol with the quantity"
    yes  "the 2 in 25 is just a two"

The first version in each pair is a *description of* a misconception. The second
**is** one - and it is the only form a teacher can read out to the class and ask
"who thinks this?" That question is the entire point. A misconception phrased in
the abstract cannot be turned into anything a seven-year-old will answer
honestly.

## Where they come from

Not from a list of "common errors". From **what the page actually shows**.

Use `get_page_range` and `list_figures` to see what the chapter puts in front of
the child, then ask: *what would a reasonable child conclude from exactly this,
that is wrong?*

Textbooks reliably manufacture misconceptions in three ways:

- **The unvaried example.** Every triangle printed points upward, so a triangle
  resting on its point stops being a triangle. The same failure shows up in
  composite figures: if every shape-counting exercise so far has shown
  non-overlapping shapes, an overlapping figure (two triangles sharing a side)
  reads as one ambiguous outline rather than several shapes sharing a line.
- **The convenient case.** Every division example divides evenly, so a remainder
  reads as a mistake the child made.
- **The silent assumption.** The page compares two objects of the same material,
  so "bigger" and "heavier" become the same word.

Look for these three before inventing anything.

## A flagged difficulty is a lead, not a misconception

A teacher's review, a lesson-plan critique, or your own first read of a page
will often name something as "confusing" or "abstract" without saying what the
child ends up wrongly believing. That is a difficulty, not a misconception, and
it is not usable in this form - it is a description, exactly the shape the form
rule above rejects.

Worked example. A review says: *"the chapter jumps into abstract 2D
representations of 3D objects - a top-view drawing of a chair looks confusing to
a struggling child."* That names where the trouble is, not what the child
concludes. Construct the belief:

    difficulty as flagged   the top-view drawing is confusing and abstract
    the actual belief       "that picture doesn't look like a chair, so it
                             must be showing something else"

That's decidable - show the same real chair and its printed top view side by
side and ask whether they're the same thing - and it's the form a teacher can
put in front of the class.

The same review's note that overlapping-shape figures "cause test anxiety"
similarly needs constructing rather than copying:

    difficulty as flagged   overlapping triangles are hard to count
    the actual belief       "I already used this line for one triangle, so it
                             can't be part of another triangle too"

Before either reaches a Level Set section, check the page reference the source
gave you against `get_page_range` yourself - see `grounding`'s note on
secondary sources. A review's page number is a lead to verify, not a citation
to inherit, and the misconception still has to be checkable against what that
page actually shows once you've confirmed it.

## Tagging

Tag each misconception with the topic indices where it is most likely to
surface. An untagged misconception is kept - it is still true of the chapter -
but it steers no single period's Level Set, so tag where you can.

A misconception that bites at every topic is usually too vague. Narrow it.

## What makes one usable downstream

The Level Set section of a prep material is built from these. It works by
putting the wrong belief in front of the class in a form where **believing it
produces a visibly wrong answer**. So the misconception must:

1. Name something concrete - an object from the anchor pool, or a thing printed
   on the page.
2. Be decidable. There must be a case where the belief gives one answer and the
   truth gives another.
3. Be one a child in *this grade* actually holds. A Grade 5 misconception in a
   Grade 2 chapter wastes the section.

If you cannot construct the contrasting case, you have written a difficulty
rather than a misconception. Three good ones beat six vague ones.

## Ask it so the answer is honest

A misconception is only worth surfacing if the children who hold it will admit
it, and the child two years behind the syllabus will not put a hand up in front
of forty peers to say they believe the wrong thing. So the Level Set probe is
asked with a **private-response device**: thumbs held against the chest, eyes
down with hands up, or fingers held up for option one or option two - the
teacher gets a count of the room, and no child is exposed
(`engagement`).

Write the probe as the sentence the teacher reads out, and say which device
collects the answer. "Check for understanding" is not a probe.

## It is also where the period closes

Level Set is the last section with the whole class's attention on the idea, so
the single revisable notebook line is written at the end of it - the child's own
sentence plus the term the exam will use (`assessment-alignment`). A well-built
Level Set makes that line nearly automatic: the class has just decided a case,
and the line is what they decided.

## Related

- `curriculum-reasoning` - where misconceptions sit in the chapter's derivation
- `core-pedagogy` - the child's-terms rule, stated generally
- `engagement` - private-response devices, without which this section misreads
  the room
- `assessment-alignment` - the closure line this section produces
- `grounding` - checking a secondary source's page claim before you rely on it