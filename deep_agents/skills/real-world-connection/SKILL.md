---
name: real-world-connection
description: How to attach a concept to something the child already knows without inventing facts about their village - the three tiers of real-world evidence, why the room and the body always qualify, and the difference between a mention and an application. Load when writing a Real Life section, choosing an example, or whenever a plan risks being generic because no local profile was recorded.
---

# Real world connection

A child engages with an idea when it lands on something they already know is
real. That is the whole mechanism, and it fails in two opposite ways: examples
invented about a community nobody verified, and examples so generic they attach
to nothing.

`contextual-reinforcement` stops the first. This file stops the second.

## The three tiers

**Tier 1 - the room, the body, the building, the way here.** Always available,
always legitimate, **no provenance required.** A door, the blackboard, the
floor, a desk, a slate, a school bag, a water bottle, a tiffin box, chalk,
the child's own hands and feet and shadow, the row they sit in, the queue they
stand in, the playground, the wall, a matchbox from a pocket, the number of
steps from the door to the window.

Using these is not a claim about anybody's community. It is a claim about the
room the teacher is standing in, which the teacher can check by looking. This
tier is why **"no local data recorded" is never a reason for a generic Real
Life section.** Tier 1 exists in every classroom in the country.

**Tier 2 - recorded local context.** Anything `get_active_context` or
`get_resource_profile` actually names, cited by field name. A crop, a trade, a
festival, a language, a landmark. Powerful when present, and governed entirely
by the provenance rules in `contextual-reinforcement`.

**Tier 3 - assumed local context.** Forbidden. "The village grows rice", "these
children have seen a tractor", "the family runs a shop" - inferred from the
district, the language or the fact that the school is government-run. This is
the demographic-inference failure, and it is the same failure whether it lands
in an adaptation patch or in a Real Life paragraph.

    default        Tier 1, always, for every topic
    when recorded  Tier 2, cited
    never          Tier 3

## Recognisable, not aspirational

Choose the example the child has *already seen*, not the one that sounds
modern. A lift, an escalator, a stadium, an aeroplane window, a supermarket
aisle may be entirely unknown to this room, and an unknown example does not
just fail - it costs the child a second unfamiliar thing to hold while they are
trying to learn the first.

    weak    "when you look down at a city from a plane, that is a top view"
    strong  "stand up and look straight down at your own bag. That is a top
            view. Now sit and look at it from the side."

## A mention is not an application

Goal G1 asks for the concept **and what it is for**. A sentence that says where
the idea occurs is a mention. An application is a **question the concept
decides** - one the child could plausibly meet with no teacher present:

    mention      "shapes are everywhere around us"
    application  "will this book fit flat in your bag, or does it have to go
                 in sideways? How can you tell before you try?"

    mention      "we use numbers when we shop"
    application  "two of you have 8 caps and 15 caps. Who has more tens?"

    mention      "folding is used to make boxes"
    application  "the shopkeeper hands you a flat piece of card. Which way does
                 it fold up into a box - show me with the matchbox"

Write one application per period, and make it one the class answers out loud or
with their hands. One that lands beats three that are named.

Where the application can be phrased so that the child furthest behind can also
do it, use that phrasing for the floor as well - "I can tell if my book will
lie flat in my bag" is both the use and the guarantee, and it costs one
sentence instead of two (`differentiation`).

## Where it sits in the period

Real Life comes **before** Concept for a reason (`lesson-design`): the class
handles or enacts the thing first, then meets the printed page. So the
real-world instance is not an illustration added after the explanation - it is
the thing being explained, met first.

One useful shape for the section, at zero cost:

    1. everybody look at / hold / stand over the tier-1 thing        (10 s)
    2. one question they can all answer about it                     (20 s)
    3. one question they cannot yet answer - which is what the
       period is for                                                 (20 s)

Step 3 is what makes it engagement rather than warm-up: the class now has a
question of their own before the textbook opens.

## No child is asked to disclose anything

Never build the example on family circumstances, possessions, occupation or
travel - not "children whose parents farm", not "who has been to a city", not
"what did you eat last night". This singles children out along exactly the
lines that hurt, and it is check 7 of the equity gate. Tier 1 avoids it
naturally: everybody in the room has a hand, a bag, a floor and a door.

## Related

- `learning-goals` - G2, and the application test
- `contextual-reinforcement` - the provenance rules and the equity gate
- `engagement` - making the example something the class does, not hears
- `activity-design` - the anchor object and the level-0 material list
- `core-pedagogy` - concrete before symbolic, which this section implements
