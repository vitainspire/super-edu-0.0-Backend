---
name: activity-design
description: How to choose or propose a classroom activity that will actually run - resource reality, staging cost for forty children, group rotation model, how many children are actually doing something, differentiation across ability levels, variety across a chapter, and when an invented activity is legitimate. Load when selecting or naming an activity.
---

# Activity design

## Prefer the library, always

Activity selection in this pipeline is **deterministic by design**: competencies
go into the Pedagogy Library and a matching activity comes out. Your job is not
to replace that. It is to check, before planning a period around an outcome, that
the outcome has resourcing behind it.

Call `lookup_activities_for_competency` with real competency ids from
`lookup_competencies`. An empty answer is the signal that matters: the outcome
you were planning has no activity behind it. Move the outcome toward one that
does, or say plainly that this topic needs an invented activity.

Call `lookup_activity_bank` for the named formats this subject allows. The final
material may only name a format from the bank - inventing one gives the teacher
an instruction with no procedure behind it.

## The staging test

Before accepting any activity, walk it through the real room:

1. **How long to set up?** Forty children re-forming into groups costs 5-8
   minutes. In a 22-minute teaching window, that is a third of the lesson spent
   on furniture.
2. **What does it need?** At resource level 0: a blackboard, chalk, the
   textbook, and what the children carry. Chart paper is a material nobody has.
   So are counters, dice, and printed cards. Chalk pieces, pebbles, and leaves
   are level-0 materials for pattern/tessellation work; a matchbox or medicine
   box from home is a level-0 material for nets and unfolding, not a printed kit.
3. **Can one teacher run it?** If it needs continuous attention at six tables
   simultaneously, it will not happen. The staging pattern that survives one
   teacher and thirty children is **rotation, not simultaneous individual work**
   - see below. Whole-class and row-based staging also run; six-way
   individualized staging does not.
4. **What happens to the child who finishes first? And to the one who cannot
   start?** An activity with no answer to both stops being an activity three
   minutes in. See Differentiation below for the default answers.
5. **How many children are doing something at any given minute?** Count it. An
   activity where one child demonstrates at the front and thirty watch has an
   engagement rate of one in thirty however good the demonstration is. Prefer
   the version every child performs from where they are sitting - it costs the
   same minutes (`engagement`).

An activity that fails any of these is not a good activity that needs better
resourcing. It is the wrong activity for this room.

## Group rotation, not individual materials

Do not design an activity around every child holding their own object or
worksheet at once - with 25-30 children that is chaos, not engagement, and it
eats the class period.

Default staging model: split the class into 5-6 groups of about 5. Set up
2-3 stations (e.g. one object/view station, one tracing or matchstick station,
one folding/unfolding station). Rotate groups through stations at roughly
10 minutes each. This holds resource needs to what one or two of each material
can supply, and lets the teacher walk between stations instead of supervising
thirty simultaneous individual set-ups.

When an activity's procedure is written, write it as what one group does at one
station, plus the rotation instruction - not as "give each child a matchbox."

Rotation is also the expensive option: it costs the group-forming minutes in
point 1 and it must fit inside Challenge's share of the period, not run to its
own schedule (`lesson-design`). Where a whole-class or row-based version of the
same activity exists, prefer it - the minutes saved are minutes of teaching.

## Concrete before the page

For a competency that asks a child to read or produce a 2D/abstract
representation of something physical (a top view, a side view, a net), the
activity should stage the physical version first and the printed or drawn
version second - not the reverse. A child struggling with grade-level
literacy or numeracy will not parse an abstract line drawing before they have
stood over, walked around, or physically folded the real object.

Concretely: real object (or a level-0 stand-in the child already owns - a
school bag, a water bottle, a slate) → physical enactment (stand above it,
squat beside it, unfold it by hand) → only then the textbook page or
worksheet. An activity that opens with the printed page has skipped a stage,
not saved time.

## Differentiation inside the same activity

The same activity has to hold the child below grade level, the child at grade
level, and the child who finishes in half the time, without becoming three
separate activities. `differentiation` and `stretch-design` own this; what
follows is what it means for a procedure:

- **Mixed-ability grouping.** When the pipeline or a teacher forms the
  5-groups-of-5 above, one capable child paired with two average and two
  struggling children is the default mix, not random or same-ability grouping.
- **A named floor.** The procedure says what the child who cannot start does
  first, in one demonstrable step - "trace one triangle with your finger",
  "put ten caps in a pile". Everybody reaches this before anybody is asked for
  the whole task.
- **Scaffolded, step-scored tasks.** For anything with a "count/find all the
  X" shape (shapes in a figure, matches in a pattern), the procedure should
  say to find and mark the smallest units first, then combine into larger
  ones - so a child who never finishes the whole figure still has completed,
  visible, scoreable steps.
- **Zero-cost stretch, not more of the same.** A fast finisher's extension
  should be open-ended and use the same level-0 materials, not a harder
  worksheet requiring new resources - e.g. "make a shape with exactly 10
  matchsticks that isn't a square or rectangle" rather than a second printed
  sheet. Write the sentence the teacher says, and write it at the point in the
  procedure where the first child will finish.
- **Extension work travels home only if it costs nothing.** A take-home
  instruction is only legitimate if every child can complete it with what a
  home already has (an empty matchbox, a vegetable being cut for cooking) -
  never something that assumes a purchased or unusual item. See `self-study`
  for the full conditions, including that it needs no adult and can be checked
  by the child.

## Variety across a chapter

A chapter is forty periods. The same format four periods running stops being an
activity and becomes a routine the class performs without thinking.

The pipeline enforces a no-repeat window in code. What you can do is make the
window achievable: if every topic in a strand can only be served by one format,
say so, because that is a real constraint on the chapter and somebody should see
it.

Vary the **format**, not the participation devices. Thumbs-on-chest, choral
answer and show-me-with-fingers should be constant across the chapter so they
cost no explanation; it is the activity around them that changes
(`engagement`).

## When inventing is legitimate

Rarely, and only when the library genuinely has nothing for the competency at
this resource level. When you do:

- Name it plainly, in words a teacher will understand without a diagram.
- Give the procedure in the order it happens, staged as rotation (see above)
  where the class size requires it, and concrete-before-page where the
  competency requires it.
- Name every material, and name only materials the room has.
- Say what the teacher watches for. An activity with no observable is a game.
- Say what the child who cannot start does, and what the child who finishes
  early is asked - both in one sentence each. An invented activity without
  these two lines is an activity for the middle of the class.
- Anchor any technical or English term (e.g. "top view", "net") to a phrase in
  the language the class actually thinks and talks in. Introducing the English
  term is fine; leaving it untranslated in the moment is not - a below-grade
  child gets stuck on the word, not the concept.
- Where the activity has a version a child could do alone at home with nothing,
  say it in one line. That line is often the best take-home question the period
  has (`self-study`).

## The anchor is not the activity

The anchor object is a **thing**. The activity is what the class **does** with
it. "Water bottle" is an anchor; "bottle sorting game" is an activity. Keep them
apart - they are decided by different stages for different reasons.

## Related

- `experience-design` - the trajectory the activity must serve
- `lesson-design` - where the activity sits in the period, and its minute budget
- `differentiation` - the floor, step-scoring and the three-question pattern
- `stretch-design` - the six moves that make a fast finisher's prompt
- `engagement` - participation rate and the free whole-class devices
- `core-pedagogy` - the resource reality
- `learning-goals` - what the activity is ultimately judged against
