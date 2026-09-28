# Topic 1 — Shalini and Rajani like drawing pictures.
*(Grade 3 Maths, ts_scert_class3_maths_en, Chapter 1)*

> **REAL FULL PIPELINE OUTPUT.** Node 1 (reasoning, `deep_agents: True`) +
> Generation + Node 3 validation — the actual `generate_lessons_from_published_book()`
> entry point, real Gemini 2.5 Flash, real cost. Repair loop disabled (removed
> per request). **Result: needsReview — did not ship.**

**Concept**
1. Observe a match-box from the front. Hold up a match-box. Ask students to describe what shape they see when looking directly at its largest face. Confirm it looks like a rectangle.
2. Observe the match-box from the side. Turn the match-box to show a side view. Ask students to describe the new shape. Point out it's still a rectangle, but a different one.
3. Observe the match-box from the top. Turn the match-box to show its top view. Explain that the object is the same, but the view changes.

**Real Life**
1. See a chair differently at home. Ask students to imagine a chair at home — looking at it from the front, side, or top makes it appear different, just like the match-box.
2. Understand why views change. A chair has a seat, legs, and back, but you only see some parts from each view.
3. Identify house views. Direct students to the house pictures on Page None. Ask them to identify which picture shows the house from the top, front, and side.

**Challenge — "If Shalini drew a chair and what the reader" (activity name itself malformed)**
- *Play:* In pairs, students look at Shalini's chair drawing on Page None. They discuss if it looks like a chair and why. Two minutes.
- *Reflect:* Pairs share what they noticed about Shalini's drawing — missing parts or unusual angles.
- *Act:* Students turn to Page None and identify objects from their side and top views; side view of (i) is a bottle, (ii) is a cup.

**Level Set**
1. Recap object views change; partners discuss table views (always a rectangle, or it depends).
2. Share easiest or most interesting part.
3. Notice a bicycle or water pump from different angles on the way home.

**Explore**
1. Tell students to observe a match-box from directly above.
2. Tell students to observe a bangle's traced outline.
3. Tell students to observe a book's traced outline.

Materials used: match-box, textbook.
Timings: Refresher 3, Concept 5, Real Life 4, Challenge 6, Level Set 4, Explore 8.

Mastery target: how something looks depends on where you look at it from.

---
### Why this did NOT ship (real Node 3 validation findings)
- Explore opens with the teacher talking, not a student action — a rule violation.
- References "Page None" repeatedly — the real chapter's textbook excerpt was never actually ingested into this run's contract (`textbookExcerptChars: 0` in every topic's provenance), so Node 1 had no real page numbers to cite.
- Challenge stages pair discussion for 30-60 children on fixed benches — flagged as unmanageable as written.
- The named gap/misconception was never given an explicit if-then response.
