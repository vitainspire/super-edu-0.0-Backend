-- One-off, run manually in Supabase's SQL editor. NOT part of the numbered
-- migration sequence -- this deletes all ROWS (schema/tables/columns are
-- untouched), so the app keeps working exactly as before against an empty
-- database. Irreversible: no backup is taken before truncating.
--
-- Deliberately KEPT (global/shared reference data, not per-school test data,
-- expensive to regenerate -- see chat): activity_templates,
-- activity_template_competencies, activity_template_contexts, competencies,
-- concepts, contexts, vocabulary, topic_concepts, topic_competencies,
-- topic_vocabulary, topic_contexts, topic_learning_outcomes,
-- prep_chapter_cache, textbook_catalog_books, textbook_catalog_chapters.
--
-- PENDING: textbook_books/textbook_chapters/textbook_topics/textbook_images
-- are per-school (school_id on textbook_books) -- whether they're safe to
-- keep or get cascade-wiped by truncating `schools` depends on the FK check
-- (migrations/check_fks.sql). Not finalized in this list yet.
do $$
declare
  t text;
  tables text[] := array[
    'academic_events','admin_notifications','admins','announcements','attendance','argus_findings',
    'automation_events','catchup_materials','classes','exam_plan_items',
    'grade_subject_feedback_profiles','grade_subjects','interventions','lesson_feedback','marks',
    'peer_pairings','personality_stories','prep_batches','prep_flow_adaptive_state',
    'prep_flow_feedback','prep_flow_material_attempts','prep_flow_materials','prep_flow_patterns',
    'prep_flow_reviews','prep_flow_runs','prep_flow_topics','prep_materials','prep_session_topics',
    'recovery_attempts','scanner_profiles','school_schedule','school_timetable_periods','schools',
    'sessions','shared_prep_materials','student_doubts','student_topic_mastery','students',
    'syllabus_chapters','syllabus_dependencies','syllabus_exercises','syllabus_ontology_extractions',
    'syllabus_sidebars','syllabus_sub_topics','syllabus_topics','taught_topics','teacher_availability',
    'teacher_class_assignments','teacher_notifications','teachers','tests',
    'timetable','timetable_substitutions','topic_polls','worksheet_marks',
    'worksheets'
  ];
begin
  foreach t in array tables loop
    if exists (select 1 from information_schema.tables where table_schema = 'public' and table_name = t) then
      execute format('truncate table public.%I restart identity cascade', t);
      raise notice 'truncated %', t;
    end if;
  end loop;
end $$;
