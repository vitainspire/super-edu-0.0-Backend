---
name: core-pedagogy
description: The constants that hold across every subject, grade and stage of this pipeline - what a low-resource government primary classroom actually is, what every period owes the child in it, the difference between cognitive demand and content, and why concrete precedes symbolic. Load this before any reasoning about what a lesson should do.
---

# Core pedagogy

Everything this system produces is for **one teacher, one period, one government
primary classroom in India**. Not a lesson plan template, not a worksheet, and
not a resource pack. If a decision would not survive contact with that room,
it is the wrong decision however good it looks on the page.

This file describes the room. `learning-goals` describes what a period has to
achieve in it - load both.

## The room, stated once

- **25 to 40 children.** A single classroom most often runs 25-30; a combined
  or multi-grade room can reach 40. Any instruction beginning "in pairs, have
  each child..." costs minutes of movement before anything is learned that a
  22-minute window does not have. Whole-class, row-based, or rotation staging
  (see `activity-design`) is what actually runs - not simultaneous individual
  work.
- **Several children in the room are already below the grade the syllabus
  assumes.** This is not the same fact as the next bullet, and both are true
  at once: a single nominal grade is rarely a single level. This is the normal
  condition the pipeline designs for, not an edge case to special-case around.
  It is also true at the other end: some children in the same room finish in a
  third of the time and are, in practice, the least taught children present.
  See `differentiation`.
- **The teacher may also be teaching two grades at once.** A different
  problem from the one above - here the room itself contains two syllabi, not
  one syllabus at mixed mastery. Anything requiring continuous teacher
  attention for more than a few minutes will not survive either version of
  this room.
- **30 minutes.** Including settling. The real teaching window is about 22.
- **Resource level 0 is the default.** A blackboard, chalk, the textbook, and
  whatever the children carry. No printables, no photocopies, no manipulative
  kits, no screens. A "cheap" material is still a material nobody has.
- **The textbook is the syllabus.** It is also often the only printed thing in
  the room. What it prints is what the child will be examined on.
- **Home study cannot be assumed.** No second book, no printables, no
  guaranteed quiet space or light, no guaranteed adult who reads the medium of
  instruction. A plan that needs work done at home to succeed has not
  succeeded. See `assessment-alignment` for what follows from this, and
  `self-study` for what may still legitimately be invited.
- **The textbook's language and the child's language are often not the same
  one.** This is not solved by translating on the fly - see how vocabulary
  enters below, and see `contextual-reinforcement` for when a language bridge
  is a legitimate adaptation versus an assumption about who this room is.

## What every period owes the child

Stated in full in `learning-goals`, and compressed here because every stage
needs it in view:

1. The **child furthest behind** reaches a named floor, and knows what the
   idea is *for*.
2. The class is **acting, not watching**, on something real that is in the room
   or on their own body.
3. A **pass is earned in class time** - including contact with the book's own
   printed questions and one revisable line in the notebook.
4. The child leaves with **one question they can explore alone**, for free,
   with no adult.
5. The child who finishes early is **stretched on the same content**, not given
   more of it.

None of these buys extra minutes, and none of them may be met by moving the
target. The five together are about two minutes of new time in a 22-minute
period; if your plan needs more, it has added activities rather than
redesigning the ones it had.

## Demand is not content

The single most useful distinction in this system, and the one most often
collapsed:

- **Content** is what the chapter teaches. Three-digit numbers, the water cycle,
  the parts of a plant. This is fixed by the syllabus and is not yours to move.
- **Demand** is what the child is asked to *do* with it. Recognise, name, sort,
  compare, explain, justify, generalise.

A Grade 3 chapter on three-digit numbers teaches three-digit numbers. You do not
reduce that. What you bound is the demand: you do not ask a Grade 3 class to
*justify a generalisation* about place value, because that is a Grade 5 act
performed on Grade 3 content.

Call `lookup_grade_constraints` to get the actual verb list for this band. Use
its verbs. Over-pitching is the commonest way a technically correct lesson fails
in a real room, and it is invisible on the page - the words are short, the
sentences are simple, and the act is impossible.

The bound is on what the class is **required** to do and on what is assessed.
An open invitation offered to a child who has already finished may reach a band
higher, because failing an invitation costs that child nothing - see
`stretch-design`.

## Concrete before symbolic, always

A child arrives at a symbol by handling the thing the symbol stands for. The
order is not a preference:

1. **A real object**, held or counted or pointed at. A chair, a tiffin box, a
   matchbox - not a drawing of one.
2. **A picture or arrangement** of that object, or a physical enactment of it
   (standing over it, unfolding it by hand) if the target is a viewpoint or a
   net rather than a static picture.
3. **The spoken idea** about it, in whatever language the child actually
   thinks and talks in. This step does not wait for the child's English.
4. **The written symbol** - and this is also where a technical or English term
   is introduced, anchored back to step 3. Not before it.

A lesson that opens at step 4 and works backwards teaches the notation and not
the idea. The child can then complete the exercise and cannot answer a question
phrased any other way - which is exactly what "learned it for the test" means.
A child stuck on an untranslated English term at step 1 has been given the same
failure by a different door.

The exam's own question wording belongs **after** step 4, not before step 1.
Meeting the printed question in class is required (`assessment-alignment`);
opening the period with it inverts this order and produces the same hollow
pass.

This is why anchor objects matter so much in this pipeline, and why an anchor
must be a **thing** and not an activity. "Water bottle" is an anchor. "Bottle
sorting game" is somebody else's job - see `activity-design`.

## Write in the child's terms

Every artifact in this system that describes what a child thinks, believes,
gains or gets wrong is written **in the child's own words**, first person where
it is a belief.

    no   "the learner conflates surface area with volume"
    yes  "the big box must hold more because it is bigger"

The first is a description of a misconception. The second **is** one, and it is
the only form a teacher can put back in front of the class to find out who still
believes it. The same rule governs `gained`, `inference`, the floor, and every
misconception you write.

## What "never lower the standard" means

Adapting delivery is required. Reducing the target is forbidden. The distinction
is sharp and it is testable:

- Teaching place value through bottle caps instead of a place-value chart:
  **delivery**. Same target.
- Rotating small groups through stations because thirty children cannot each
  hold their own material: **delivery**. Same target.
- Bridging into the concept through the child's home language:
  **access**. Same target.
- Breaking a counting or classification task into smaller, scored steps so a
  below-grade child completes some of it rather than none: **access**. Same
  target, reached at the child's pace, not a smaller target.
- Teaching two-digit numbers because three-digit "is too hard for this class":
  **lowering the standard**. Forbidden, whatever the justification.

A child in a low-resource school is not owed a smaller education. They are owed
the same one, routed through what the room actually has.

**The floor is not a lowered standard.** Naming what the child furthest behind
leaves able to do - one demonstrable thing, on the same road as the target - is
how the standard is made real for that child rather than aspirational. The
target does not move; the floor is the guarantee underneath it. A period with a
target and no floor sorts the class silently into those who kept up and those
who did not. See `differentiation`.

## Related

- `learning-goals` - the five outcomes every period is judged against
- `curriculum-reasoning` - deriving what must be learned
- `experience-design` - the path a child takes to it
- `differentiation` - the floor under that path and the ceiling above it
- `activity-design` - staging that survives this room's size and the
  differentiation this room's mixed levels require
- `engagement` - keeping the whole room acting for 22 minutes
- `real-world-connection` - what "concrete" is allowed to mean here
- `assessment-alignment` - the pass, earned in class
- `contextual-reinforcement` - what the room may change about that path, and
  what evidence it needs to change it
