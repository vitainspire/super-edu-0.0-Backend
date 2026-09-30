---
name: curriculum-reasoning
description: How to derive prerequisites, their in-class repair, anchor objects, misconceptions and the forward/backward knowledge chain for one chapter. Load this when asked what a chapter requires, what it leaves behind, or how its topics connect. Contains the chain-slip failure and how to detect it.
---

# Curriculum reasoning

You are answering the questions **the textbook itself does not answer**. You are
not planning lessons and not writing material for children.

    what must the class already be able to do before page 1 means anything
    which real objects in this room make it concrete
    what will the child wrongly conclude
    what must each topic LEAVE so the next one has a floor

## Three things are settled once per chapter

Asked per topic, a model invents forty slightly different answers to the same
question. One coherent answer beats forty locally reasonable ones.

**Prerequisites.** What the class must already be able to do before topic 1.
Everyday capabilities in a child's terms - not textbook headings, and *not this
chapter's own content*. Before asserting one, call `search_book`: if the chapter
teaches it on a later page, it is content, not a prerequisite, and listing it
sends the class backwards. Aim for 4-8.

**And for each one, how it gets repaired in about 60 seconds.** Several
children in the room will not have the prerequisite - that is the normal
condition, not an edge case (`core-pedagogy`), and a prerequisite listed with
no repair is a list of reasons the lesson will fail rather than something a
teacher can act on. The repair is whole-class, level-0 and short:

    prerequisite  "I can count to twenty without stopping"
    repair        count to twenty together, clapping, twice - 40 seconds

    prerequisite  "I can tell which of two things is bigger"
    repair        hold up two objects, everyone points at the bigger one,
                  three times

Repairs belong in the first Refresher of the strand (`lesson-design`). Where a
prerequisite genuinely cannot be repaired in a minute, say so plainly - that is
a real finding about this chapter's fit to this class, and it is worth more than
a repair you invented to fill the field.

**Anchor objects.** The real things this chapter is taught with, **grouped by
strand**. Every one must exist in a government primary classroom at this
resource level: on a desk, in a bag, on the wall, in the playground, brought
from home.

Group them because different threads need different things. A thread about
shapes wants objects with faces you can look at. A thread about counting wants
things there are *hundreds* of - bottle caps, seeds, sticks, beads. A thread
about viewpoints or nets wants objects with a distinct front, side and top -
a chair, a tiffin box, a matchbox someone unfolds by hand. A thread about
tessellation or pattern wants many small identical units to arrange, not one
object to look at - chalk pieces, pebbles, leaves. One pool for all of these
serves none of them. Call `list_figures` first: an object already printed in
the chapter is one the teacher can point at.

Anchors are **things, not activities**. "water bottle" yes; "bottle sorting
game" no.

Include, in each strand's pool, at least one object **a home also reliably
has** - an empty matchbox, a plate, a bucket, the child's own hand. It costs
nothing to prefer and it is what the take-home question is built from
(`self-study`).

**Misconceptions.** 3-6 for the whole chapter, each tagged with the topic
numbers where it bites. See the `misconception-analysis` skill for how to write
one that is usable. A misconception nobody in this grade actually has is worse
than one fewer.

## One thing is per topic: the chain

Four short fields. One clause each, never a sentence, under 15 words:

    strand     the thread this topic belongs to, 1-3 words
    gained     what the child can do after this topic that they could not before
    bridgesTo  how that becomes the NEXT topic's starting knowledge, or null
    assumes    what must already be understood for THIS topic to make sense

**`bridgesTo` for topic N and `assumes` for topic N+1 are two sides of one
handover. Write them naming the same thing in the same words.** If topic 3
bridges to "an outline can be named as a shape", topic 4 assumes "an outline can
be named as a shape" - not "basic geometry", not "shape knowledge". These are
checked against each other for word overlap downstream, and they were authored
to match, so a mismatch is a real defect and not a phrasing difference.

`gained` is written for a child at grade level. What the child *furthest behind*
leaves with is a separate, smaller, demonstrable thing, decided per period
downstream - see `experience-design` for where it is marked and
`differentiation` for how it is written. Do not resolve that here by writing a
weaker `gained`; the chain would then hand a weaker floor to every topic after
it.

## The chain-slip failure

This is the rule most often broken, and it is broken by accident because the
result agrees with itself at every seam.

A topic whose `gained` says the same thing as its own `assumes` has taught
nothing: the class walked in able to do it and walked out able to do it.

    WRONG - the chain has slipped by one, and nothing is ever learned
      T4  assumes: can point to family members they resemble
          gained:  can point to family members they resemble    <- identical
          bridges: can say their family's last name             <- the next TITLE

    RIGHT - each line is a step past the one above it
      T4  assumes: family members are connected to each other
          gained:  can point to a feature they share with a relative
          bridges: people who share features often share a name

The wrong version is produced by copying `gained` from the previous topic's
`bridgesTo`, and `bridgesTo` from the next topic's title. It reads as a
progression and promises nothing.

**Read every entry as three lines and check the middle one moves.**

If a topic genuinely consolidates rather than teaches - some review and exercise
topics do - write what the consolidation makes newly possible ("can state,
unprompted, which family members live together"), never a copy of `assumes`.
Consolidation topics are also the natural place to put the chapter's printed
questions that no other period covered (`assessment-alignment`); say so when you
identify one.

## A chapter is often more than one thread

Textbooks bind several strands into one chapter for page-count reasons, not
teaching ones. Four topics on shapes followed by two on two-digit numbers is one
chapter and two threads.

Where that happens, **say so**: give the topics different `strand` names, set the
last topic of the first thread to `"bridgesTo": null`, and the first topic of the
next thread to `"assumes": null`.

Do **not** manufacture a link across the seam. "Counting shapes leads to
comparing numbers" is a sentence, not a progression. A fabricated bridge is worse
than a declared break: the break is something a teacher can see and plan around;
the fabrication is something they discover mid-period.

Most chapters are one strand. Use a second only when the topics genuinely stop
being about the same thing - not because the difficulty rose.

## Bounds

- One chain entry per topic, same index, same order. All of them.
- `bridgesTo` must follow from that same topic's `gained`. If what the topic
  leaves cannot reach the bridge you wrote, the bridge is invented - use null.
- Topic 1's `assumes` is null. What topic 1 needs is the chapter prerequisites.
- `gained` is a promise about the child, so it is bound by the grade band. Call
  `lookup_grade_constraints` and use a verb that band can actually perform.
- Name concrete things. "Develops spatial reasoning" helps nobody; "which side
  of the bottle you can see" does.

## Related

- `core-pedagogy` - the room, and demand vs content
- `learning-goals` - what the chapter's derivation is ultimately serving
- `misconception-analysis` - how to write one a teacher can use
- `differentiation` - the floor `gained` does not describe
- `assessment-alignment` - the chapter's printed questions and who covers them
- `grounding` - checking a claim against the printed page
