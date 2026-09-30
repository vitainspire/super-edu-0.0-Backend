-- Read-only. Reports row counts for every table involved in the wipe, split
-- into three groups, so we can see in one shot: (1) did the intended test
-- data actually get wiped, (2) did the shared/global reference data survive
-- untouched, (3) did textbook_books/chapters/topics/images survive -- this
-- settles the pending FK question empirically.
create temp table if not exists _wipe_check (category text, table_name text, row_count bigint);
truncate _wipe_check;

do $$
declare
  r record;
  groups jsonb := '{
    "should be 0 (per-school test data)": [
      "academic_events","admin_notifications","admins","announcements","attendance",
      "argus_findings","automation_events","catchup_materials","classes","exam_plan_items",
      "grade_subject_feedback_profiles","grade_subjects","interventions","lesson_feedback","marks",
      "peer_pairings","personality_stories","prep_batches","prep_flow_adaptive_state",
      "prep_flow_feedback","prep_flow_material_attempts","prep_flow_materials","prep_flow_patterns",
      "prep_flow_reviews","prep_flow_runs","prep_flow_topics","prep_materials","prep_session_topics",
      "recovery_attempts","scanner_profiles","school_schedule","school_timetable_periods","schools",
      "sessions","shared_prep_materials","student_doubts","student_topic_mastery","students",
      "syllabus_chapters","syllabus_dependencies","syllabus_exercises","syllabus_ontology_extractions",
      "syllabus_sidebars","syllabus_sub_topics","syllabus_topics","taught_topics","teacher_availability",
      "teacher_class_assignments","teacher_notifications","teachers","tests",
      "timetable","timetable_substitutions","topic_polls","worksheet_marks","worksheets"
    ],
    "should still have rows (kept, global reference data)": [
      "activity_templates","activity_template_competencies","activity_template_contexts",
      "competencies","concepts","contexts","vocabulary","topic_concepts","topic_competencies",
      "topic_vocabulary","topic_contexts","topic_learning_outcomes","prep_chapter_cache",
      "textbook_catalog_books","textbook_catalog_chapters"
    ],
    "pending -- did this survive?": [
      "textbook_books","textbook_chapters","textbook_topics","textbook_images"
    ]
  }'::jsonb;
  cat text;
  t text;
  cnt bigint;
begin
  for cat in select jsonb_object_keys(groups) loop
    for t in select jsonb_array_elements_text(groups -> cat) loop
      if exists (select 1 from information_schema.tables where table_schema = 'public' and table_name = t) then
        execute format('select count(*) from public.%I', t) into cnt;
        insert into _wipe_check values (cat, t, cnt);
      else
        insert into _wipe_check values (cat, t, -1);
      end if;
    end loop;
  end loop;
end $$;

select category, table_name, row_count
from _wipe_check
order by category, row_count desc, table_name;
