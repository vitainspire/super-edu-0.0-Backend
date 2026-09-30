"""The six-section contract, in one place.

Every prep material in a chapter batch has the same six sections, always, in
this order. What differs between them is not their presence but WHERE THEIR
CONTENT IS ALLOWED TO COME FROM, and that is the thing this file makes
explicit:

    Refresher   <- the PREVIOUS topic's Explore. Not the previous topic; the
                   previous topic's Explore specifically. It is the student-
                   facing callback to what the class actually did last time.
    Concept     <- the textbook, and nothing else. Same definitions, same
                   numbers, same worked examples. Invention here is a defect.
                   Since moves.py, the book also fixes the ORDER the bullets
                   explain in: a book that names before it shows gets a sheet
                   that names before it shows.
    Real Life   <- the textbook's own named contexts first, then the students'
                   own world. Half-grounded, half-local.
    Challenge   <- the Pedagogy Library's matched activity, staged freely.
    Level Set   <- free. Self-check, reflection, take-home invitation.
    Explore     <- the most free, and the only one with an obligation to the
                   FUTURE: it must end on a hook the next topic's Refresher
                   can pick up. That hook is what makes 40 topics a sequence
                   rather than 40 unrelated sheets.

The prompt builder, the validator and the renderer all read this table. A rule
written here is enforced everywhere; a rule written in a prompt string is
enforced nowhere.
"""
from typing import Optional

# The CANONICAL order — the one a topic teaches in when its book does not say
# otherwise. moves.teaching_order() may reorder the middle four for a topic whose
# pages explain themselves in a different sequence; Refresher stays first and
# Explore stays last always, because those two answer to the lessons either side
# rather than to the page. Everything that iterates sections for STRUCTURE
# (containers, validation, the six-section contract) still uses this tuple —
# only the teaching sequence varies.
#
# Same tuple, same order, same spelling as the single-topic pilot's
# SECTION_ORDER — the two pipelines produce interchangeable material and a
# divergence here would be invisible until something downstream read the wrong
# key.
SECTION_ORDER = ("refresher", "concept", "realLife", "challenge", "levelSet", "explore")

SECTION_LABELS = {
    "refresher": "Refresher",
    "concept": "Concept",
    "realLife": "Real Life",
    "challenge": "Challenge",
    "levelSet": "Level Set",
    "explore": "Explore",
}

# How much of the section's substance the model is inventing, 0–1. Not a
# temperature (one call writes all six sections, so there is only one
# temperature) — it decides how hard the validator leans on grounding checks
# and how the prompt frames the section's licence.
GROUNDED, MIXED, CREATIVE = "grounded", "mixed", "creative"

# Where the JSON puts each section's bullets. The pilot's shape, preserved:
# `concept` is a bare list, the rest are wrapped in {"points": [...]}, and the
# refresher lives under `previousTopicRefresher` with its bullets under
# "recap".
_CONTAINERS = {
    "refresher": ("previousTopicRefresher", "recap"),
    "concept": ("concept", None),
    "realLife": ("realLife", "points"),
    "challenge": ("challenge", "points"),
    "levelSet": ("levelSet", "points"),
    "explore": ("explore", "points"),
}

BULLETS_PER_SECTION = 3

SECTION_POLICY: dict[str, dict] = {
    "refresher": {
        "label": "Refresher",
        "mode": GROUNDED,
        "source": "previous_explore",
        "audience": "student",
        "minutes": 3,
        "rule": (
            "Built ENTIRELY from what the PREVIOUS topic's Explore told the class to go "
            "notice in real life — quoted for you above as PREVIOUS EXPLORE. Bullet 1 "
            "brings back that real-life thing by name so students recognise it "
            "instantly; bullet 2 has pairs share whether they actually noticed it — a "
            "genuine check-in on the teacher's own instruction, not a question invented "
            "fresh here; bullet 3 turns whatever they noticed (or didn't) into the "
            "doorway to today's topic. Never introduce a new example here — if it was "
            "not one of the previous Explore's own points, it does not belong in the "
            "Refresher."
        ),
    },
    "concept": {
        "label": "Concept",
        "mode": GROUNDED,
        "source": "textbook",
        "audience": "class",
        "minutes": 6,
        "rule": (
            "THE TEXTBOOK IS THE SOURCE OF THE IDEA, NOT THE SCRIPT FOR TEACHING IT. "
            "Two different things, and only the first is fixed. FIXED: every definition, "
            "number, rule and technical term is the book's — same values, same wording "
            "for terms. You may not add a fact those pages do not contain, contradict "
            "one, or quietly improve one you think is imprecise. YOURS: the example that "
            "carries the idea into the room. Use the book's own illustration when it is "
            "genuinely the best one for these children, and reach for a better one when "
            "it is not — the object this topic's experience plan already chose is "
            "usually it. A class that meets the idea through a cup they are holding has "
            "learned the same fact as a class that was pointed at a picture of a cup, "
            "and has learned it better. THE OBJECT IS IN YOUR HANDS, NOT THEIRS. "
            "Demonstrating with the anchor is why this section works — hold the cup "
            "up, walk round it, push ten caps into a group where everyone can see. "
            "What does NOT belong here is a STUDENT ACTIVITY: no \"give each pair\", "
            "no timed pair work, no \"come up and\", no swapping roles, no minutes "
            "counted off for a task. That is the Challenge, and a Concept that stages "
            "one leaves the period running two activities with the minutes for "
            "neither. Here the class watches, answers and thinks; they get their "
            "hands on it in the Challenge. "
            "IF CHALLENGE (BELOW) RETURNS TO THE SAME OBJECT OR PICTURE, DO NOT "
            "DEMONSTRATE IT HERE TOO. You can see what Challenge is about to do "
            "with this topic before you write Concept -- if it already brings the "
            "class back to the book's own picture (Shalini's chair, the house "
            "views, whatever this topic's page centres on), Concept gets ONE brief "
            "mention of it at most, not a full multi-view demonstration that "
            "Challenge is about to repeat minutes later. Do the actual "
            "demonstration work here with a DIFFERENT, smaller object instead — a "
            "pencil, a matchbox, a cup — and let Challenge be the only place the "
            "book's own picture gets real attention. Two sections teaching the "
            "same object the same way is wasted minutes, not reinforcement. "
            "CITE THE CONCEPT, NOT THE PICTURE: (Page N) marks "
            "where the idea comes from, using the <!-- page N --> markers, whether or not "
            "the example is the book's. Never imply the book shows something it does not "
            "— \"the book shows us that a view changes (Page 12) — so look at YOUR cup\" "
            "is right; \"look at the cup on page 12\" when there is no cup on page 12 is "
            "a defect the teacher meets mid-lesson. HOW THE BULLETS ARE STAGED is "
            "settled per topic by the book's own move order — see the STAGING line "
            "in this topic's block, which is authoritative over any habit you have. "
            "THE FIRST BULLET IS THE EASY ENTRY, NOT THE MAIN IDEA. If THE FLOOR is "
            "given above, that sentence IS this bullet — stage it, do not invent a "
            "different easy case beside it. A child behind grade level needs a "
            "smaller, one-variable version of the idea BEFORE the main example, not "
            "just a concrete object — an actually easier case (fewer items, one "
            "property instead of two, a direct comparison instead of a definition), "
            "which the main bullet then builds on. \"Two matchboxes pushed together "
            "leave no gap; two bangles pushed together do\" before asking which shapes "
            "tile a floor is an easy entry; \"observe the matchbox\" on its own is not "
            "— it previews the task instead of making a smaller one. "
            "IF THE POINT DEPENDS ON THE CLASS SEEING A CHANGE IN ONE OBJECT AT THE "
            "FRONT, PUT THE RESULT ON THE BOARD, NOT JUST IN THE AIR. Holding an object "
            "up and asking 30-60 children on fixed benches to watch it change is not "
            "something most of the room can actually do -- a child six rows back cannot "
            "see a chair's angle shift. Where a bullet needs the class to register a "
            "change (an object looking different from two angles, a shape appearing or "
            "vanishing), the teacher draws what is seen at each step on the blackboard "
            "so the comparison sits in chalk where everyone can read it, and the object "
            "itself is only what the drawing is taken from -- never the only record of it. "
            "THE DEMONSTRATION OBJECT ITSELF MUST BE SMALL AND ALREADY IN THE "
            "TEACHER'S HAND -- a cup, a matchbox, a book, a pencil -- never something "
            "that has to be fetched, lifted or carried around the room to be shown "
            "from a different side. A full-size chair or desk is not a Concept "
            "object even when the book's own story features one: it is heavy and "
            "awkward to turn in place, and if Challenge is about to return to that "
            "same picture, its story belongs there (see the SAME-OBJECT rule "
            "above), where the class handles the actual printed pictures, not the "
            "actual furniture. Do not assume the room even has spare chairs to "
            "demonstrate with, either -- many government-school classrooms seat "
            "children on shared benches or the floor, not individual chairs. "
            "NAME THE REASON, NOT JUST THE FACT.'Some shapes join without gaps' is a "
            "label -- a child who hears it has been told WHAT happens, not WHY. At "
            "least one bullet must give the mechanism in one clause: 'the matchbox's "
            "straight edges touch flush, so nothing is left over' teaches something; "
            "'this one fits and this one doesn't' only names an outcome the child "
            "already has to take on faith. A Concept built entirely from bare labels "
            "has not taught the idea, it has announced it. "
            "IF A GAP/GAPCLOSER IS GIVEN ABOVE, ONE BULLET MUST STATE THE IF-THEN "
            "RESPONSE, NOT JUST TOUCH THE TOPIC. gapCloser exists because a specific "
            "wrong idea was named -- a bullet that mentions the right idea without "
            "ever saying what the teacher does when a child holds the WRONG one has "
            "not closed the gap, it has repeated the main point next to it. The "
            "pattern is: 'if a child says/thinks X, the teacher Y' -- concrete, in "
            "the room, not 'address misconceptions' or 'clarify understanding'. "
            "WRONG (states the fact, never the response to the wrong idea): "
            "'Different tools are needed for each farming stage.' -- this is the "
            "right idea, stated once, with nothing for the teacher to do when a "
            "child doesn't believe it. RIGHT (the if-then, shown live): 'If a child "
            "says any tool works for any job, the teacher tries digging with the "
            "flat side of a sickle right there -- it barely scratches the soil -- "
            "then switches to a trowel to show it works easily: the wrong tool "
            "doesn't fail because it's bad, it fails because it's the wrong shape "
            "for that one job.' The gapCloser bullet is not optional decoration on "
            "top of the main point -- it IS one of the three bullets, written as a "
            "live, specific, physical demonstration of the wrong idea failing, not "
            "a sentence ABOUT the wrong idea."
        ),
    },
    "realLife": {
        "label": "Real Life",
        "mode": GROUNDED,
        "source": "textbook_contexts_then_local",
        "audience": "class",
        "minutes": 4,
        "rule": (
            "TEACHER-LED, LIKE CONCEPT. The teacher EXPLAINS where this idea already "
            "lives in the children's own world; the class listens, answers and thinks. "
            "This is not an activity slot: no pair work, no group work, no timed task, "
            "no \"give each pair\", no \"partners name their own\". Students get their "
            "hands on it in the Challenge. "
            "THE INSTANCES ARE THE BOOK'S FIRST. Take the concrete instances from the "
            "textbook's OWN named things (listed above as TEXTBOOK CONTEXTS) — the class "
            "has already seen them on the page, and the book is the source here, not a "
            "starting point you improve on. Fall back to the selected context only when "
            "the book names none, and invent one only if neither fits. "
            "(1) the teacher names ONE concrete instance the book itself uses and tells "
            "the class where they would meet it — the village, the market, the kitchen, "
            "the field; (2) the teacher connects that instance back to the idea just "
            "taught in Concept, saying plainly why it is the same thing; (3) the teacher "
            "works ONE instance through aloud with real numbers, stating the answer so "
            "the class hears it done. A question asked to the whole class is fine and "
            "welcome ONLY AS AN OPENING HOOK, BEFORE the explanation — hands up, a few "
            "seconds of thinking, one or two called-on guesses. THE BULLET MUST NOT END "
            "THERE. Ending a bullet on \"ask students to explain why\" or \"call on "
            "someone to say why\" is the one mistake this section keeps making on real "
            "chapters: a hook the teacher never resolves leaves every child with "
            "whichever guess they made, right or wrong, and no one is told the answer. "
            "The teacher's own explanation always comes AFTER the hook, in the "
            "teacher's voice, stating the real answer plainly — "
            "wrong: \"Ask students to explain why squares work better than circles for "
            "a floor.\" (the class never hears the actual reason, and \"better\" teaches "
            "a verdict on the shape instead of the property); "
            "right, as GUIDANCE not a script (describe the move, never quote the words): "
            "ask why squares cover a floor with no gaps, let one or two children guess, "
            "then explain plainly that every side of a square touches the next square's "
            "side exactly with nothing left over, which is what a circle's curved edge "
            "cannot do. Still the section's normal ONE bullet, ONE detail, 1-2 sentences "
            "— the hook and the answer share the same short detail, not two. "
            "A QUESTION IS ONE WAY TO OPEN, NOT THE ONLY ONE, AND NOT EVERY BULLET NEEDS "
            "ONE — the rule above is that a hook must not be left unresolved, not that "
            "every bullet must be phrased as a question. Three bullets that all open "
            "\"Ask whether...\" / \"Ask why...\" / \"Ask what...\" read as the same line "
            "repeated, which is its own defect. Vary the opening move across the three: "
            "one can ask a question, another can point at the instance and state the fact "
            "directly, another can describe two contrasting cases side by side before "
            "explaining which is which. "
            "What is not fine, at any point, is handing the room a task to go and do. "
            "Indian rural settings throughout, and things a government-school child in a "
            "village actually has seen. "
            "SAME PROPERTY, NOT SAME THEME. Every instance must demonstrate the actual "
            "thing Concept taught, not just live in the same general subject area. A "
            "winding river being hard to walk along and shapes leaving gaps when placed "
            "together sound related -- paths, nature, edges -- but they are not the "
            "same property: one is about difficulty of movement, the other is about "
            "whether two edges touch flush. That is a THEME match wearing a PROPERTY "
            "match's clothes, and it teaches the wrong lesson: that this idea is "
            "loosely 'like' a bunch of things, not that it precisely predicts one "
            "thing. Before using an instance, name the exact property Concept taught "
            "and check this instance actually has it -- round pots or bangles stacked "
            "together leaving gaps is the same property as the bangle in Concept; a "
            "river being hard to walk beside is not. "
            "A LITTLE OF THE ROOM'S OWN LANGUAGE HELPS RECOGNITION, USED AS A TOUCH "
            "NOT A TRANSLATION. Where this region's own everyday language has a "
            "common, well-known word for the real-life object being named -- a "
            "matka, a muggu, a thali -- say it alongside the English term the first "
            "time that object appears in this section, not instead of it: 'a matka "
            "(water pot)', never the local word alone and never the English word "
            "alone where a better-known local one exists. This is a recognition aid "
            "for a child whose home language names the object before English does, "
            "not a language lesson and not a full-sentence translation -- one word "
            "is enough. NEVER GUESS AT A SPELLING OR WORD YOU ARE NOT CONFIDENT IS "
            "ACTUALLY USED IN THAT REGION'S LANGUAGE. A wrong or invented regional "
            "word is worse than none at all: it reads as inauthentic to exactly the "
            "child it was meant to reach, and undermines every other local detail "
            "on the sheet. "
            "CHECK FOR THIS ON EVERY REAL LIFE SECTION, NOT ONLY WHEN IT COMES TO "
            "MIND. Before finishing this section, look back at every object you just "
            "named and ask whether a common local word for it exists. Skipping the "
            "check because the last topic already used one, or because English felt "
            "sufficient this time, is exactly how this rule ends up applied to one "
            "topic in a chapter and silently dropped on the next -- which reads as "
            "accidental, not as a real choice not to use one. "
            "STAY IN THE ROOM. THE TEACHER NARRATES THE REAL WORLD FROM WHERE THE "
            "CLASS ALREADY IS -- never sends the child home in their head, and never "
            "asks them to imagine, picture or recall a personal object of their own "
            "(their chair, their table, their house) to check something. That "
            "imagining-at-home move is Explore's job, done for real after the period "
            "ends, not this section's, done as a mental exercise in the middle of "
            "class. Real Life's instances are things the teacher can narrate as "
            "shared knowledge every child already has from the village, the market, "
            "the kitchen, the field -- said ABOUT the world, not asked of the child's "
            "OWN unseen object. WRONG (sends the child home in their head, to an "
            "object only they can picture): 'Mention that a chair at home can look "
            "like Shalini's drawing if you are standing directly above it. Ask "
            "students to imagine looking down at their own chair from the ceiling.' "
            "RIGHT (same real-world connection, narrated as shared knowledge, "
            "nothing personal to imagine): 'Explain that a water pot at the village "
            "well looks like a small circle from directly above but a curved-sided "
            "shape from the side -- same pot, same idea as Shalini's chair drawing.' "
            "If an instance can only work by having each child privately picture "
            "their own home's version of it, it is not this section's instance -- "
            "either find one the whole class can be told about as a shared, "
            "recognisable scene, or leave it for Explore, where going and checking "
            "a real object at home is exactly the point."
        ),
    },
    "challenge": {
        "label": "Challenge",
        "mode": CREATIVE,
        "source": "pedagogy_activity",
        "audience": "student",
        "minutes": 8,
        "rule": (
            "TWO CASES, AND THE TOPIC BLOCK TELLS YOU WHICH. (a) THE PAGES SET A "
            "TASK — the topic block quotes it. That task IS the Challenge, and "
            "\"challenge.activity\" is a short name for WHAT THE BOOK ASKS, in the "
            "book's own nouns: \"Match the three house views\", \"Build the six "
            "stick shapes\", \"Read the cricket score table\". The SELECTED ACTIVITY "
            "below is only the staging — do NOT copy its name here. A Challenge that "
            "runs page 18's match-sticks under the title \"Clap Patterns\" tells the "
            "teacher to do two different things, and the one they will do is whichever "
            "they read first. (b) THE PAGES SET NO TASK — then the NAME is fixed: copy "
            "\"challenge.activity\" character-for-character from the SELECTED ACTIVITY "
            "above. Everything about how it runs is "
            "yours. Stage it PLAY -> REFLECT "
            "-> ACT: (1) PLAY carries a real either/or choice the students make; (2) "
            "REFLECT is a 30-second 'what worked?' turn-and-tell, never about mistakes; "
            "(3) ACT applies the idea to one real problem, with the worked answer stated "
            "in 'detail' so the teacher never computes in front of the class. "
            "PLAY NEEDS AN EASY SIDE, NOT JUST A CHOICE. If THE FLOOR is given above, "
            "that is the easy side to stage, in this section's own terms — do not "
            "invent a separate one. A child behind grade level needs a genuinely "
            "smaller version of the task reachable inside PLAY itself "
            "-- fewer items, one property instead of two, a worked case before the open "
            "one -- not just permission to attempt the same task slower. 'Sort these 4 "
            "shapes first if 8 feels like too many, then try the rest' is an easy side; "
            "'make as many shapes as you can' alone is not, however encouraging the "
            "wording, because there is nothing smaller inside it to fall back to. "
            "A REAL EITHER/OR MEANS THE CLASS ACTUALLY PICKS, NOT THAT THEY DO BOTH "
            "IN ORDER. Running the easy case first and the open task second is good "
            "staging, but it is not itself a choice -- nobody decided anything, they "
            "were just walked through two steps. A genuine choice means at some point "
            "a pair picks ONE path and the other genuinely goes unused for them. "
            "WRONG (staged, not chosen): 'First push two matchboxes together and see "
            "there is no gap. Then push two bangles together and see the gap.' -- "
            "every pair does both, in the same order, nothing was picked. RIGHT (a "
            "genuine choice): 'Each pair picks matchboxes OR bangles to arrange "
            "without gaps -- whichever they pick is the one they work with; a pair "
            "that gets stuck stays on the easy two-piece case inside their OWN chosen "
            "object.' The floor still has to be reachable inside whichever branch a "
            "pair picks, not a separate ungraded warm-up everyone does first "
            "regardless of the choice. "
            "IF THE SAME GAP/GAPCLOSER WAS ALREADY CLOSED IN CONCEPT AND THE BOOK "
            "POSES THAT SAME MISCONCEPTION AS ITS OWN ACTIVITY QUESTION, CLOSE IT "
            "AGAIN HERE FROM A SECOND ANGLE -- ON PURPOSE, NOT AS A REPEAT. Concept "
            "closes the general version with a neutral object, teacher-led, no book "
            "needed. Where the book's own printed question is literally the "
            "misconception restated ('Has Shalini drawn the picture of a chair? "
            "What do you think?' IS the child's doubt from the gap, not a separate "
            "task), ACT resolves that exact book instance in the students' own "
            "hands. State plainly that it is the same idea as the Concept object, so "
            "the second pass reads as reinforcement of one idea from two angles, not "
            "as the lesson teaching the same thing twice by accident. A gap that only "
            "gets closed once, abstractly, and is never brought back to the book's "
            "own concrete question, leaves the exact doubt the book raised sitting "
            "unresolved for the child who was thinking of THAT picture, not the "
            "neutral one. "
            "BEFORE YOU FINISH PLAY, CHECK IT AGAINST ITS OWN RULE, EVERY TIME: read "
            "back the PLAY bullet and find the exact moment a pair picks ONE path and "
            "the other genuinely goes unused. If you cannot point to that moment -- if "
            "every pair still ends up doing the same thing, just in a different order "
            "-- it is not a choice yet, and it does not become one by adding the word "
            "'choose' to a sentence where nothing was actually decided. Fix it before "
            "moving on, the same way the regional-language check in Real Life gets "
            "checked every time rather than trusted to memory. "
            "TIE ACT BACK TO CONCEPT'S OBJECT BY NAME, WHEN THEY ARE THE SAME IDEA. "
            "Concept demonstrated the mechanism on some small object -- a cup, a "
            "matchbox, two bangles. If ACT is the class applying that SAME mechanism "
            "to a new case, say so in the room: 'same idea as the cup from earlier' or "
            "'just like the matchboxes', naming the actual object, not a vague 'like "
            "we discussed'. This is what makes six sections read as one lesson instead "
            "of six unrelated ones -- a class that watched a demonstration and then "
            "does a related task with no acknowledgement that the two are connected "
            "has to notice the connection itself, and most of them will not. Skip this "
            "only when ACT genuinely does not share Concept's mechanism -- forcing a "
            "callback to an unrelated object is worse than none."
        ),
    },
    "levelSet": {
        "label": "Level Set",
        "mode": CREATIVE,
        "source": "free",
        "audience": "student",
        "minutes": 5,
        "rule": (
            "STILL EXACTLY 3 BULLETS TOTAL IN THIS SECTION — not 4, not 5. Bullet 1 packs "
            "TWO moves into its ONE detail, same 1-2-sentence limit as every other bullet "
            "on this sheet, NOT 1-2 sentences per move: a one-clause recap, then a "
            "one-clause check, sharing a single detail field. If it does not fit in 1-2 "
            "sentences total, the recap is too long, not the exception to the limit. "
            "FIRST CLAUSE, THE RECAP: the teacher restates the actual idea this period "
            "taught, in the same wording and example Concept already used, not a new one "
            "— this exists for the child who was elsewhere in their head for some or all "
            "of the period and would otherwise leave without the idea at all, so it must "
            "stand on its own, understandable by someone who heard NOTHING else today. "
            "SECOND CLAUSE, THE CHECK, without the teacher: partners check each other. "
            "Where a likely misconception is named for this topic, put it here — a "
            "two-option check that gives the wrong idea equal footing with the right one, "
            "so a child who holds it can genuinely pick either side rather than a yes/no "
            "question that only ever collects yes. With no misconception named, describe "
            "a plain without-the-teacher self-check instead. The check should apply the "
            "idea to a new instance the lesson has not already used, not just repeat back "
            "the recap — worth the attentive child's time too, not just a repeat. "
            "(2) a reflection on what was easiest or most interesting today — never on "
            "errors; (3) A CHOICE THAT LITERALLY LEAVES THE ROOM, NOT A DISCUSSION THAT "
            "ENDS IN IT. \"Carries the idea home\" means the child notices something "
            "OUTSIDE class before this counts as done — on the way home, at home, around "
            "the village — never a question asked and answered with a partner in the "
            "same five minutes and then forgotten. No writing, nothing collected or "
            "checked next class, framed as an invitation, never homework. STAY ON THE "
            "PROPERTY, NOT A VERDICT ON A CATEGORY — the choice is between two concrete "
            "things to go look at, not an opinion about which shape is \"better\": "
            "wrong: \"Ask students to pick square blocks or round blocks for a strong "
            "wall and tell a partner why. Square blocks are the better choice.\" (never "
            "leaves the room, and \"better choice\" is a verdict on the shape, not the "
            "property); "
            "right: on the way home, or once there, find ONE thing with straight edges "
            "(a tile, a book, a brick) and ONE with curved edges (a bangle, a wheel, a "
            "bottle cap) and notice whether each sits flush against its neighbour or "
            "leaves a gap — nothing to bring back, just something to notice."
        ),
    },
    "explore": {
        "label": "Explore",
        "mode": CREATIVE,
        "source": "free",
        "audience": "student",
        "minutes": 5,
        "rule": (
            "ONE THING, THREE POINTS, NOTHING ELSE. This section is the TEACHER, "
            "still in the room, giving the class real-life noticing instructions to "
            "carry out on their own time — never framed as homework, never "
            "collected, never checked, never graded. "
            "THIS SPLIT IS BETWEEN 'text' AND 'detail', NOT A CHOICE OF ONE STYLE FOR "
            "BOTH. 'text' is the short student-action headline, same as every other "
            "section on this sheet — 'Observe a chair from above', not 'Tell students "
            "to observe a chair from above'. The 'Tell students that/to...' framing "
            "belongs ONLY in 'detail', which reports the teacher saying it. Putting "
            "the telling-framing into 'text' as well reads as the teacher talking "
            "before the class has done anything, which is wrong for every section's "
            "headline including this one. "
            "EVERY point's detail MUST report the teacher's own act of telling the "
            "class — start it with 'Tell students that...' or 'Tell students to...' "
            "(or 'Ask students to...'), reporting what the teacher says to the whole "
            "room before the period ends. Do NOT write the detail as a direct "
            "second-person instruction addressed at the child ('Look at a chair from "
            "above.', 'Take a cup and look at it...', 'Find one object in your "
            "house...') — that reads as a worksheet handed to the child, which is "
            "exactly the homework framing this section must never have, even when "
            "no literal word for homework appears. "
            "RIGHT (reports the telling, names a specific real object, ties to "
            "today's idea): 'Tell students that on their way home, they should look "
            "at a tiled floor or a brick wall and notice whether the tiles or bricks "
            "fit together with no empty space between them.' "
            "WRONG (direct command to the child, not reported teacher speech): "
            "'At home, look at a chair from directly above. Notice its shape and "
            "what parts you can see.' "
            "Each point ties to today's idea, built on something the textbook "
            "itself named where possible. No wait-time, no pair work, no discussion "
            "staged here — that happens tomorrow, in the Refresher, when the class "
            "compares what they noticed. "
            "NOTHING TO FILL BEYOND THE THREE POINTS. There is no separate handoff "
            "object — the third point itself is what tomorrow's Refresher recalls, "
            "so make it specific enough to bring back by name, not a vague "
            "'notice things around you' or 'find an object' with nothing named."
        ),
    },
}

assert tuple(SECTION_POLICY) == SECTION_ORDER, "SECTION_POLICY must match SECTION_ORDER"

# The one section a first topic legitimately lacks: T1 has no previous Explore
# to build a Refresher from.
OPTIONAL_WHEN_FIRST = ("refresher",)

CREATIVE_SECTIONS = tuple(s for s, p in SECTION_POLICY.items() if p["mode"] == CREATIVE)
GROUNDED_SECTIONS = tuple(s for s, p in SECTION_POLICY.items() if p["mode"] == GROUNDED)


def _loose(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


# Every spelling of a section name that should resolve to its canonical key.
# Built from both the keys and the labels, because a prompt that shows a model
# "Level Set" will get "Level Set" back, and a filter keyed only on "levelSet"
# then discards it.
_SECTION_ALIASES: dict[str, str] = {}
for _key in SECTION_ORDER:
    _SECTION_ALIASES[_loose(_key)] = _key
    _SECTION_ALIASES[_loose(SECTION_LABELS[_key])] = _key
del _key


def canonical_section(name: str) -> Optional[str]:
    """A section name in any casing or spacing, as its canonical key — or None.

    This exists because of a bug that hid for a whole feedback cycle. The
    optimizer's prompt names the sections by their LABELS ("Level Set", "Real
    Life") in both the section list and every pattern line, so the model answered
    with `{"Level Set": "..."}`. The filter accepted only `levelSet` and dropped
    the rest with a bare `continue` — so the optimizer's most valuable output, the
    per-section directives, was silently discarded while its changelog cheerfully
    reported having written them.

    Returning None (rather than guessing) is deliberate: callers log the miss,
    which is the part that was missing.
    """
    return _SECTION_ALIASES.get(_loose(name))


def container_for(section: str) -> tuple[str, Optional[str]]:
    """(top-level JSON key, key holding the bullet list inside it). The inner
    key is None for `concept`, which is a bare list."""
    return _CONTAINERS[section]


def bullets_of(material: dict, section: str) -> Optional[list]:
    """The bullet list for `section`, tolerating the two shape deviations the
    models actually produce: a `{"points": [...]}` wrapper dropped in favour of
    a bare list, and vice versa."""
    outer_key, inner_key = _CONTAINERS[section]
    value = material.get(outer_key)
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if inner_key and isinstance(value.get(inner_key), list):
            return value[inner_key]
        # Wrapper present but under the wrong name — take the only list in it
        # rather than reporting a missing section over a key spelling.
        lists = [v for v in value.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return None


def section_text(material: dict, section: str) -> str:
    """All prose in a section, flattened — what the continuity and grounding
    checks actually compare against."""
    parts: list[str] = []
    for bullet in bullets_of(material, section) or []:
        if isinstance(bullet, dict):
            parts.append(str(bullet.get("text") or ""))
            parts.append(str(bullet.get("detail") or ""))
        elif isinstance(bullet, str):
            parts.append(bullet)
    return " ".join(p for p in parts if p).strip()


def handoff_of(material: dict) -> dict:
    """The Explore section's forward-declaration to the next topic's Refresher.

    Returned as a dict even when the model omitted it, so callers can always
    ask for .get("question") — a missing handoff is a validation finding, not
    a crash three nodes later.
    """
    explore = material.get("explore")
    raw = explore.get("handoff") if isinstance(explore, dict) else None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {"question": raw.strip()}
    return {}


def handoff_summary(material: dict, topic: str = "") -> str:
    """The handoff rendered for the NEXT topic's prompt. This string is the
    entire mechanism by which Refresher n+1 knows about Explore n, so it is
    deliberately built from the handoff block AND the Explore prose: a model
    that skipped the structured handoff still leaves a usable Explore behind,
    and losing the chain over a missing key would be the worst possible
    failure mode for a 40-topic batch."""
    handoff = handoff_of(material)
    lines: list[str] = []
    if topic:
        lines.append(f'Previous topic: "{topic}"')
    for key, label in (
        ("scene", "Scene the class was in"),
        ("object", "Object/example they handled"),
        ("question", "Open question left hanging"),
        ("discovery", "What they worked out"),
    ):
        value = handoff.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
    prose = section_text(material, "explore")
    if prose:
        lines.append(f"Explore as written: {prose[:900]}")
    return "\n".join(lines) if lines else "(the previous Explore produced nothing usable)"
