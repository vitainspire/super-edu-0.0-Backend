"""Previous topic for the lesson refresher ("Last time: …"). Mirrors
frontend/lib/refresher-topic.ts.

The syllabus is the authoritative ordered curriculum: topics by order_index,
and within each topic its sub-topics by order_index. Flattening those two
levels gives the exact sequence the class works through, so "what came just
before this lesson" is a lookup, not a guess.

That matters because the previous implementation asked taught_topics instead —
"what did this class most recently log?" — and taught_topics is only written
when a teacher logs attendance with a topic or opens the prep modal. In a class
that stopped logging after Week 1, the newest row WAS Week 1, so a Week-8
"Measurement" lesson opened with a place-value recap of "Large Numbers".

Order of preference:
  1. Previous SUB-TOPIC inside the same topic — mid-unit, this is exactly the
     last thing the class did, and it's guaranteed related.
  2. At a topic boundary: the declared prerequisite (teacher's "Requires
     first"), then the extracted dependency graph.
  3. Still at a boundary: the last sub-topic of the preceding topic.
  4. Recency-bounded taught_topics — only when the topic isn't in the syllabus
     at all (ad-hoc lesson), since then there's no sequence to walk.
Nothing qualifying returns None, and the lesson gets no refresher — correct far
more often than inventing a bridge between unrelated topics.

Note there is deliberately no "distance" guard on the walk. Steps are adjacent
entries in the flattened sequence, so the previous step is never more than one
topic back — that's what walking the syllabus buys over querying taught_topics,
where a Week-1 row could be "most recent" in Week 8. Whether the previous unit
is pedagogically RELATED is a judgment the prompt makes (it may set the
refresher to null); this function's job is to report the sequence faithfully.
"""
from datetime import datetime, timedelta
from typing import Optional

# How stale a taught_topics row may be before it stops counting as "last time".
# Only used for lessons with no syllabus position to walk.
REFRESHER_MAX_AGE_DAYS = 28


def _label(step: dict) -> str:
    return (step["subtopicName"] or step["topicName"]).strip()


async def fetch_previous_topic(
    admin, class_id: str, topic: str,
    topic_definition_id: Optional[str] = None, current_subtopic: Optional[str] = None,
    subject: Optional[str] = None,
) -> Optional[str]:
    cur_topic = topic.strip().lower()
    cur_sub = (current_subtopic or "").strip().lower()
    cur_subject = (subject or "").strip().lower()

    # ── Load the syllabus for this class ──────────────────────────────────
    topic_rows: list[dict] = []
    try:
        topic_rows = (
            admin.table("syllabus_topics")
            .select("id, topic, definition_id, order_index, is_completed, subject")
            .eq("class_id", class_id)
            .order("order_index")
            .execute()
        ).data or []
    except Exception:
        pass  # table missing — every branch below degrades to None

    # Same-subject topics only. A class is grade+section with no subject of its
    # own, so an unfiltered syllabus mixes Maths and Science and the "previous
    # topic" can silently come from the wrong subject.
    same_subject = (
        [r for r in topic_rows if (r.get("subject") or "").strip().lower() == cur_subject]
        if cur_subject else topic_rows
    )
    # Fall back to the unfiltered list only when the subject column is
    # unpopulated (pre-migration-016 data), not when this subject genuinely has
    # no topics.
    subject_unpopulated = not any(r.get("subject") for r in topic_rows)
    scoped = same_subject if same_subject else (topic_rows if subject_unpopulated else [])

    sub_topic_rows: list[dict] = []
    try:
        sub_topic_rows = (
            admin.table("syllabus_sub_topics")
            .select("topic_id, name, order_index, is_completed")
            .eq("class_id", class_id)
            .order("order_index")
            .execute()
        ).data or []
    except Exception:
        pass  # no sub-topics — the walk below degrades to topic granularity

    subs_by_topic_id: dict[str, list[dict]] = {}
    for s in sub_topic_rows:
        subs_by_topic_id.setdefault(s["topic_id"], []).append(s)
    for lst in subs_by_topic_id.values():
        lst.sort(key=lambda s: s.get("order_index") or 0)

    # ── Flatten to the ordered sequence of lessons ─────────────────────────
    steps: list[dict] = []
    for topic_index, t in enumerate(scoped):
        subs = subs_by_topic_id.get(t["id"], [])
        if not subs:
            steps.append({
                "topicIndex": topic_index, "topicName": t["topic"], "topicDefId": t.get("definition_id"),
                "subtopicName": None, "isCompleted": t.get("is_completed") is True,
            })
            continue
        for s in subs:
            steps.append({
                "topicIndex": topic_index, "topicName": t["topic"], "topicDefId": t.get("definition_id"),
                "subtopicName": s["name"], "isCompleted": s.get("is_completed") is True,
            })

    def name_for_definition_id(def_id: str) -> Optional[str]:
        for r in scoped:
            if r.get("definition_id") == def_id:
                return r["topic"].strip()
        for r in topic_rows:
            if r.get("definition_id") == def_id:
                return r["topic"].strip()
        return None

    # ── Locate today's lesson in that sequence ─────────────────────────────
    def matches_topic(s: dict) -> bool:
        if topic_definition_id:
            return s["topicDefId"] == topic_definition_id
        return s["topicName"].strip().lower() == cur_topic

    current_index = -1
    if cur_sub:
        for i, s in enumerate(steps):
            if matches_topic(s) and (s["subtopicName"] or "").strip().lower() == cur_sub:
                current_index = i
                break
    if current_index == -1:
        # No sub-topic given, or the name didn't match a syllabus row (renamed /
        # ad-hoc). Treat the lesson as sitting at the start of its topic — the
        # conservative choice, since assuming a later position would invent
        # same-unit history the class may not have covered yet.
        for i, s in enumerate(steps):
            if matches_topic(s):
                current_index = i
                break

    current = steps[current_index] if current_index >= 0 else None
    current_def_id = (
        topic_definition_id
        or (current or {}).get("topicDefId")
        or next((r.get("definition_id") for r in scoped if r["topic"].strip().lower() == cur_topic), None)
    )

    # 1. Mid-unit: the previous sub-topic of the same topic. This is literally
    # the previous lesson, so it needs no relevance test — "Weight: Conversion"
    # really does lead into "Weight: Addition and subtraction".
    #
    # Prefer the nearest COMPLETED sub-topic, because is_completed records what
    # was actually taught while order_index only records the plan. A teacher
    # who skips ahead shouldn't get "Last time: <something the class never
    # did>". The search stays inside the current unit, so it can't run away to
    # a Week-1 topic the way an unbounded is_completed scan would.
    if current and current_index > 0:
        unit_start = next(i for i, s in enumerate(steps) if s["topicIndex"] == current["topicIndex"])
        fallback: Optional[dict] = None
        for i in range(current_index - 1, unit_start - 1, -1):
            if fallback is None:
                fallback = steps[i]  # immediately-preceding step
            if steps[i]["isCompleted"]:
                name = _label(steps[i])
                if name:
                    return name
        # Nothing in this unit is marked done — the class may simply not be
        # tracking completion, so fall back to the planned previous lesson
        # rather than nothing.
        if fallback:
            name = _label(fallback)
            if name:
                return name

    # 2. At a topic boundary, "what should they already know?" is the real
    # question — so the declared prerequisite wins. It's the one signal that is
    # unambiguously about whether the old topic feeds this one.
    if current_def_id:
        try:
            prereq_rows = (
                admin.table("syllabus_topics")
                .select("prerequisite_definition_id")
                .eq("class_id", class_id)
                .eq("definition_id", current_def_id)
                .not_.is_("prerequisite_definition_id", "null")
                .limit(1)
                .execute()
            ).data or []
            prereq_def_id = prereq_rows[0].get("prerequisite_definition_id") if prereq_rows else None
            if prereq_def_id:
                name = name_for_definition_id(prereq_def_id)
                if name:
                    return name
        except Exception:
            pass  # column not migrated — try the dependency graph

    # 3. The extracted dependency graph (migration 019), when a textbook
    # analysis recorded what this topic builds on. 'required' before
    # 'recommended'.
    if current_def_id:
        try:
            deps = (
                admin.table("syllabus_dependencies")
                .select("to_definition_id, dependency_type, strength")
                .eq("from_definition_id", current_def_id)
                .in_("dependency_type", ["depends_on", "prerequisite", "recommended"])
                .execute()
            ).data or []
            ranked = sorted(deps, key=lambda d: 0 if d.get("strength") == "required" else 1)
            for d in ranked:
                name = name_for_definition_id(d["to_definition_id"])
                if name and name.lower() != cur_topic:
                    return name
        except Exception:
            pass  # table not migrated — fall through

    # 4. Nothing declared, so fall back to the preceding step in the syllabus —
    # the LAST sub-topic of the previous topic, which is more specific than the
    # bare topic name.
    if current and current_index > 0:
        name = _label(steps[current_index - 1])
        if name:
            return name

    # 5. The lesson isn't in the syllabus at all (ad-hoc topic), so there's no
    # sequence to walk. Recent taught history is the only remaining signal —
    # bounded by age, and required to belong to this subject.
    if current:
        return None
    try:
        cutoff = (datetime.now() - timedelta(days=REFRESHER_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
        taught = (
            admin.table("taught_topics")
            .select("topic, subtopic, date, created_at")
            .eq("class_id", class_id)
            .gte("date", cutoff)
            .order("date", desc=True)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        ).data or []

        # taught_topics has no subject column, so the only way to keep a
        # Science row out of a Maths lesson is to require the topic to exist in
        # this subject's syllabus. Skipped when the syllabus isn't
        # subject-scoped.
        def in_this_subject(t: str) -> bool:
            if not cur_subject or not scoped:
                return True
            return any(r["topic"].strip().lower() == t for r in scoped)

        prev = None
        for r in taught:
            t = (r.get("topic") or "").strip().lower()
            s = (r.get("subtopic") or "").strip().lower()
            if t == cur_topic and s == cur_sub:
                continue  # don't recap ourselves
            if in_this_subject(t):
                prev = r
                break

        if prev:
            # Prefer the sub-topic label — that's the specific thing the class did.
            s = (prev.get("subtopic") or "").strip()
            t = (prev.get("topic") or "").strip()
            if s:
                return s
            if t:
                return t
    except Exception:
        pass  # nothing left to try

    return None
