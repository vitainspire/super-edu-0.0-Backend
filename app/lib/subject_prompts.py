"""Subject-routed prompt modules. Mirrors frontend/lib/prep/subject-prompts.ts.

The prep-material prompt has a SHARED base (low-resource principles, JSON
shape, bullet rules, Tier-2 rules — all in the smart-lesson route) and a
SWAPPABLE subject module: the activity bank + the pedagogy guidance the model
reasons over. Only the module changes per subject; the base stays identical so
validation, rotation, and rendering keep working unchanged.

Why this exists: a single generic bank was really a *math* bank ("place value,
number line, fractions…"), so Math produced good lessons and every other
subject was forced to improvise around a math toolkit. Routing each subject to
its own bank + pedagogy fixed that.

Kept as plain reference TEXT, not a queryable structure — the model picks and
adapts an activity by name; the route only checks the chosen name is in-bank.
Scope: TSBIE / SCERT Telangana primary (Grades 1-5): Mathematics, First
Language (Telugu/Urdu/Hindi/Kannada/Marathi/Tamil/Sanskrit), English, and EVS
(integrated Science + Social + Health). Unknown subjects fall back to GENERIC.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SubjectModule:
    key: str
    bank: str
    pedagogy: str


# ── MATHEMATICS ──────────────────────────────────────────────────────────────
_MATH_BANK = """Pick ONE activity for "challenge" from this bank — adapt the specifics (numbers, wording) to the actual topic, don't invent a new one unless truly nothing here fits.

MOVEMENT (best for: place value, number line, comparing numbers, fractions, geometry)
- Human Number Machine: each student holds a digit card, the group arranges itself into a called number; swap two students and ask what changed.
- Living Number Line: a line taped/drawn on the floor; students stand where their number card belongs.
- Skip Counting Jump Path: numbers marked on the floor; students hop by 2s/5s/10s, or avoid a rule ("don't land on multiples of 4").
- Human Bar Graph: students physically stand in columns by their answer to a question; the class becomes the graph.
- Human Calculator: some students are numbers, one is an operator (+/-); they physically act out the calculation.
- Freeze Frame: students form a math symbol or relationship with their bodies (e.g. two numbers and a student as ">" between them).
- Four Corners: label room corners Agree/Disagree/Not Sure; students move to the corner matching their view on a statement.

MYSTERY (best for: place value, number properties, estimation)
- Secret Number Interview: one student secretly picks a number; others ask only yes/no questions to guess it.
- Mystery Bag: students feel objects without looking and classify them (longer/shorter, more/less, shape).
- Missing Digit Mystery: a partly-hidden number with clues ("greater than 500, even, not divisible by 3") to deduce the missing digit.
- Error Detective: the teacher deliberately makes a mistake out loud; students catch and correct it.
- Guess My Rule: teacher gives a sequence (2, 4, 8, 16...); students guess the rule behind it.

ROLE PLAY (best for: fractions, money, reading numbers, decimals)
- Fraction Pizza Shop: paper "pizzas"; the teacher orders a fraction amount, students cut and serve it correctly.
- Math Restaurant: a menu with prices; students rotate through customer/waiter/cashier roles totalling bills and change.
- Decimal Money Market: a pretend market with decimal prices; students calculate change with play money.

BUILD & CREATE (best for: shapes, geometry, revision)
- Geometry Architects: using sticks/straws, groups build the strongest triangle or tallest structure, then discuss why it holds.
- Build the Tallest Tower: each correct answer earns a "block" (stone/bottle cap); groups race to build the tallest tower.
- Math Art Gallery: students create art from only basic shapes, then label every shape used.

THINKING (best for: reflection and conceptual understanding)
- Teach the Teddy: a student explains the idea to a puppet/toy; if it "doesn't understand," they explain differently.
- Hot Seat: one student faces away from the board; the class gives clues about a written number for them to guess."""

_MATH_PEDAGOGY = "Pedagogy for Mathematics: never explain a rule then test it — number sense emerges by handling concrete things (stones, fingers, the students' own bodies) BEFORE symbols. Explore turns the concept into a real-life quantity problem the class recognizes; Challenge is a hands-on or movement game."

# ── LANGUAGE (First Language: Telugu/Urdu/Hindi/… AND English) ───────────────
_LANGUAGE_BANK = """Pick ONE activity for "challenge" from this bank — adapt it to the actual language, letters, and words of the lesson; don't invent a new one unless truly nothing here fits.

STORY & SPEAKING (best for: comprehension, expression, vocabulary)
- Story Prediction: teacher reads/tells a story and stops; students predict what happens next, then hear the rest.
- Add-a-Sentence Story: the class builds one story together, each student adding a single spoken sentence in turn.
- Freeze-Frame Scene: small groups act out a moment from the story as a still picture; the class reads the scene aloud.
- Picture Talk: describe a scene drawn on the board; each student adds one describing word or sentence.

WORDS & SOUNDS (best for: phonics, spelling, vocabulary)
- Word Chain: last sound (or letter) of one word starts the next; the class keeps the chain going around the room.
- Sound Hunt: teacher says a sound/letter; students find and name things in the room that begin with it.
- Syllable Clap: clap once per syllable of a word; sort words by how many claps.
- Rhyme Pairs: teacher says a word; students call out words that rhyme with it.
- Word Building: letter/syllable cards; groups arrange them into words the teacher calls out.

READING & GRAMMAR-IN-USE (best for: reading fluency, sentence sense, grammar)
- Word Sort Corners: label corners (e.g. naming words / action words); students move to the corner their word belongs to.
- Sentence Scramble: word cards out of order; groups stand up and reorder themselves into a correct sentence.
- Echo/Choral Reading: teacher reads a line, class echoes it with the same expression; build up a whole passage.
- Hot Seat Word: one student faces away; the class gives clues (meaning, first sound, use it in a sentence) to guess the word.

DIALOGUE & ROLE PLAY (best for: speaking, greetings, real-life language)
- Shopkeeper & Customer: act a market conversation using the target words/phrases.
- Interview a Character: one student becomes a character from the story; classmates ask questions in the language.

IMPORTANT — match the language role:
- If this subject is the child's FIRST language (Telugu/Urdu/Hindi/Kannada/Marathi/Tamil/Sanskrit): the children already speak it. Aim higher — reading fluency, comprehension, richer vocabulary, and (upper grades) composing their own sentences. Use fuller text.
- If this subject is ENGLISH: assume the children may NOT understand English yet. Scaffold heavily — pair every new English word with its meaning in the local language, a gesture, and a picture; keep sentences very short; put listening and speaking BEFORE reading and writing. Never assume they understood."""

_LANGUAGE_PEDAGOGY = "Pedagogy for Language: a language is learned by USING it — speaking, listening, acting, and playing with words and sounds — not by copying rules off the board. Explore is a short story or real-life scene the class steps INTO and talks about; Challenge is a word/language game. For English especially, build a bridge from the local language and never leave a new word unexplained."

# ── EVS (integrated Science + Social + Health + Environment) ─────────────────
_EVS_BANK = """Pick ONE activity for "challenge" from this bank — adapt it to the actual topic; don't invent a new one unless truly nothing here fits.

OBSERVE & CLASSIFY (best for: living/non-living, plants, animals, materials)
- Look-Closely: pass around or point to a real thing (leaf, stone, seed); students say what they see/feel/smell, then sort by a property.
- Sorting Circles: two chalk circles on the floor; students place picture-cards or objects into the right group (living/non-living, hard/soft, wild/domestic).
- Odd-One-Out: show four things; students find which doesn't belong and say WHY.

MY SURROUNDINGS (best for: family, community, neighbourhood, directions)
- Surroundings Hunt: students find examples in the room or yard (things made of wood, sources of light, plants).
- Draw-My-Way: each student sketches the path from home to school, naming what they pass.
- Community Helper Role Play: students act out helpers (farmer, doctor, postman) and the class guesses who and why we need them.
- Class Survey: ask one question (how do you come to school?), tally answers on the board, discuss what it shows.

TRY IT OUT (best for: water, air, materials, weather, health)
- Sink or Float: predict then test everyday objects in a bowl of water; sort into sink/float.
- What Dissolves?: predict which things (salt, sand, sugar) vanish in water; test and discuss.
- Shadow Watch: mark a shadow now and later; talk about what changed and why.
- Sequence It: picture-cards of a day / plant growth / seasons; groups put them in the right order.

CARE & HEALTH (best for: hygiene, food, environment, safety)
- Do-It-Right Demo: students act the correct way to wash hands / brush / cross a road; the class spots any step missed.
- Healthy Sort: sort foods or habits into "good for us" / "not so good" and explain one choice.
- Good-or-Harmful: sort actions into "helps our surroundings" / "harms it"; plan one small class action.
- Four Corners Opinion: a statement (e.g. "we should never waste water"); students move to Agree/Disagree/Not Sure and give a reason."""

_EVS_PEDAGOGY = "Pedagogy for EVS: start from the child's OWN surroundings — what they already see, do, and touch at home and in the village. Understanding grows from observing real things, exploring the immediate environment, and asking \"why\", never from copying a definition. Bring in health, community, and care for nature wherever it fits naturally, since EVS blends science and social living."

# ── GENERIC fallback (unknown subject) ───────────────────────────────────────
_GENERIC_BANK = """Pick ONE activity for "challenge" from this bank — adapt it to the actual topic; don't invent a new one unless truly nothing here fits.

- Four Corners: label corners Agree/Disagree/Not Sure; students move to the one matching their view on a statement, and give a reason.
- Sorting Circles: two or three chalk circles on the floor; students place objects or picture-cards into the right group.
- Add-a-Sentence Story: the class builds one story about the topic together, each student adding one spoken sentence.
- Role Play: small groups act out a short real-life scene connected to the topic; the class watches and discusses.
- Hot Seat: one student faces away; the class gives clues about a written word/idea for them to guess.
- Odd-One-Out: show four things; students find which doesn't belong and explain why.
- Teach the Teddy: a student explains the idea to a puppet/toy; if it "doesn't understand," they explain differently."""

_GENERIC_PEDAGOGY = "Pedagogy: never explain the idea and then test it — understanding emerges through Explore (a real-life scene the class steps into) and Challenge (a hands-on game or discussion), with the teacher guiding and the students discovering."

MATH_MODULE = SubjectModule(key="math", bank=_MATH_BANK, pedagogy=_MATH_PEDAGOGY)
LANGUAGE_MODULE = SubjectModule(key="language", bank=_LANGUAGE_BANK, pedagogy=_LANGUAGE_PEDAGOGY)
EVS_MODULE = SubjectModule(key="evs", bank=_EVS_BANK, pedagogy=_EVS_PEDAGOGY)
GENERIC_MODULE = SubjectModule(key="generic", bank=_GENERIC_BANK, pedagogy=_GENERIC_PEDAGOGY)


def resolve_subject_module(subject: str) -> SubjectModule:
    """Checked math -> evs -> language so 'Social Studies'/'Environmental
    Studies' route to EVS before the language net, and unrecognized subjects
    fall back to GENERIC. Matches English and Telugu-script subject names alike."""
    s = (subject or "").lower()
    if re.search(r"math|maths|ganit|గణిత", s):
        return MATH_MODULE
    if re.search(r"evs|environ|science|social|పరిసర", s):
        return EVS_MODULE
    if re.search(r"english|telugu|hindi|urdu|kannada|marathi|tamil|sanskrit|language|భాష|తెలుగు|హిందీ|ఉర్దూ|ఇంగ్ల", s):
        return LANGUAGE_MODULE
    return GENERIC_MODULE
