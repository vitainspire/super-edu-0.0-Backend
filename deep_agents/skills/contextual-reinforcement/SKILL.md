---
name: contextual-reinforcement
description: How to adapt a lesson to the room it runs in without lowering what it teaches - the three purposes, the provenance rule, why the room itself needs no provenance, and every check the equity gate will run on your proposal. Load before proposing any contextual adaptation.
---

# Contextual reinforcement

You produce a **patch**, not a second lesson. The academic contract is already
settled: the mastery target, the required concepts, the competencies and the
textbook grounding were decided upstream and **you may not change any of them**.
What you decide is how the same lesson is *delivered* in this particular room.

## The three purposes

Every adaptation declares one:

- **learning** - deepens engagement with the same target. A local example that
  makes the idea recognisable.
- **access** - removes a barrier to the same target. A language bridge, a
  restaging for a room with no wall space.
- **delivery** - changes staging only. Rows instead of groups. Sequencing a
  real object and physical enactment before the printed page is also delivery
  - the target (reading a top/side view) is unchanged; only the order of
  concrete-to-abstract exposure moved.

If you cannot name which, you have not decided what the adaptation is for.

## The one thing you may never do

**Lower the standard.** Adapting delivery is required; reducing what a child ends
up able to do is forbidden, whatever the justification.

    delivery      teach place value with bottle caps instead of a chart
    delivery      rotate five groups of five through stations instead of
                  issuing every child their own material
    delivery      swap a hands-up check for a thumbs-on-chest check so the
                  child who is behind answers honestly
    access        bridge into the concept through the home language
    access        break a multi-shape counting task into count-small-then-
                  combine steps, so a struggling child produces scored,
                  step-by-step work toward the same total
    FORBIDDEN     teach two-digit numbers because three-digit "is too hard
                  here"
    FORBIDDEN     reduce the number of shapes a child is asked to count because
                  the room is below grade level

The counting example is the one to watch: sequencing *how* a child reaches the
full count is access. Shrinking *what counts as done* is a lowered standard
wearing a scaffold's name.

You are asked to answer `lowers_standard` honestly. It **defaults to true**
precisely so that an omission stops the plan rather than shipping it.

## Provenance: the rule that gets most proposals rejected

Every adaptation cites the profile fields it rests on, by their exact names as
the context tools printed them. This is checked, and the check is not a
formality:

- A **LOCAL** factor (community, culture, funds of knowledge) may only be
  proposed from **verified** evidence. "The model believed the village grows
  rice" and "an administrator recorded that the village grows rice" produce the
  same sentence and are not the same claim. One is context; the other is a
  fabrication about a real place.
- The same test applies to language. "Anchor the English term to its meaning
  in the recorded home language" is access, cited to a verified language
  field. "Anchor it in the local vernacular" because the school is government,
  rural, or in a given state is not access - it is the demographic inference
  check 8 exists to catch, wearing a pedagogy technique's clothes.
- Citing a field you did not read is worse than citing nothing. The gate looks
  the field up.
- `data_source` must be the **weakest** provenance among your citations. The
  gate re-derives it; a mismatch is a rejection.

**Claiming nothing is a legitimate answer.** An empty adaptation list for a room
with nothing recorded about it is correct, not a failure.

## The room itself is not a local claim

The provenance rule governs claims about a **community**. It does not govern the
four walls the teacher is standing in. A door, the floor, the blackboard, a
desk, a slate, a school bag, a water bottle, chalk, the playground, the child's
own hands and feet and shadow, the row they are sitting in - these are in every
classroom in the country, the teacher can verify each one by looking up, and
using them asserts nothing about anybody's village, caste, income or trade.

This matters because the provenance rule has a failure mode of its own: a room
with an empty context profile gets a lesson with no real-world hook at all,
which fails the child in a different way. It is never necessary. Anchor to the
room and the body by default, add recorded local context when it exists, and
never infer.

    always legal   the room, the building, the body, the way children sit
    legal if cited what `get_active_context` / `get_resource_profile` names
    never          anything concluded from where the school is or who it serves

See `real-world-connection` for how to build a Real Life section out of the
first row.

## What the gate will reject

Deterministic checks run on every proposal after you submit it. Knowing them is
cheaper than being refused:

1. **Inactive factor.** You proposed from a factor that activation ruled out.
   Only `get_active_context` entries are legal.
2. **Provenance violation.** A local claim on assumed or absent evidence.
3. **Contradicts the evidence.** The profile says one thing, your change assumes
   another.
4. **Unechoed evidence.** You cited a field whose value does not appear anywhere
   in your change or reason - which means you did not actually use it.
5. **Wrong target.** The adaptation changes what the topic teaches rather than
   how it is delivered.
6. **Resource contradiction.** You named a material the room does not have.
   Check `get_resource_profile` first - large class size recorded there
   justifies a rotation-staging adaptation, it does not justify guessing what
   materials the room has beyond that profile.
7. **Unsafe disclosure.** The change asks children to reveal personal or family
   circumstances, or singles a child out. Never do this: not "children whose
   parents farm", not "students who have been to a city". This also rules out
   take-home tasks that require a home to contain something in particular
   (`self-study`).
8. **Demographic inference.** Concluding a fact about this room from its
   location, language or economic band. Geography is not evidence. A class's
   home language, resource level, or class size are only usable when they come
   from `get_active_context` or `get_resource_profile`, never assumed because
   of what kind of school this is.

Note what check 8 does **not** cover: the room in front of the teacher, per the
section above. "Have the class look at the classroom door from the front and
then from the side" infers nothing about anybody.

## Bounds

- **Maximum three adaptations per topic.** A patch with eight entries is a
  rewrite wearing a patch's clothes.
- **Exactly one section per adaptation.** If it changes two sections, it is two
  adaptations or it is unclear.
- Write `change` concretely enough that a teacher could follow it, and `reason`
  so a reviewer can see which recorded fact drove it.

## Where each factor tends to land

Not a rule, an observation worth having:

    language / access          -> Refresher, Concept
    verified local relevance   -> Real Life
    resource reality           -> Challenge
    class size / large group   -> Challenge (usually a delivery adaptation:
                                   rotation staging, not a materials change)
    prior knowledge            -> Refresher, Level Set
    transfer, community links  -> Explore

## Related

- `language-support` - the commonest access adaptation, done properly
- `real-world-connection` - what to use when nothing local is recorded
- `activity-design` - where rotation staging and concrete-before-page get
  turned into an actual procedure once this file has approved the adaptation
- `differentiation` - why a floor and a step-scored task are access, not a
  smaller target
- `core-pedagogy` - why the standard does not move
